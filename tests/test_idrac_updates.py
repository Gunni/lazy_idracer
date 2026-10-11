"""Offline tests for repository updates; never contact an iDRAC."""
from io import StringIO
from pathlib import Path
import importlib.util
import sys
import signal
import json
import tempfile
from threading import Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


from offline_support import HTTPError, HTTPStatusError, Clock, fake_httpx

with patch.dict(sys.modules, {'httpx': fake_httpx}):
    from lazy_idracer import updates


class Host:
    def __init__(self, name='test'):
        self.name = name
        self.session_path = '/redfish/session'
        self.blocked = False
        self.errors = {}
        self.client = SimpleNamespace(base_url='https://' + name)
        self.power = 'Off'
        self.submissions = []

    def request(self, method, path, **kwargs):
        if method == 'GET' and path == updates.JOBS:
            return SimpleNamespace(json=lambda: {'Members': []})
        if method == 'GET' and path.endswith('UpdateService'):
            return SimpleNamespace(json=lambda: {'Actions': {'#UpdateService.SimpleUpdate': {'target': '/redfish/update'}}})
        if method == 'GET' and path.endswith('System.Embedded.1'):
            return SimpleNamespace(json=lambda: {'PowerState': self.power})
        if method == 'POST':
            self.submissions.append(kwargs['json'])
            return SimpleNamespace(headers={'Location': '/redfish/job/firmware'})
        raise AssertionError((method, path))


def package(name='BIOS', version='2', reboot='HOST'):
    return {'PackagePath': name + '.exe', 'DisplayName': name,
            'PackageVersion': version, 'ComponentInstalledVersion': ['1'], 'RebootType': reboot}


def api():
    clock = Clock()
    app = SimpleNamespace(httpx=SimpleNamespace(HTTPError=HTTPError, HTTPStatusError=HTTPStatusError),
                          CONSOLE=Console(file=StringIO(), width=140), Panel=Panel, Text=Text, Table=Table,
                          PROGRESS=SimpleNamespace(add_task=Mock(return_value=1), update=Mock(), advance=Mock()),
                          OVERALL_TASK=0, OPERATION_TASK=1, SYSTEM_PATH='/redfish/v1/Systems/System.Embedded.1',
                          Lock=Lock, Event=Event, time=clock, save=Mock(), append_log=Mock(), ask=Mock(return_value='UPDATE'),
                          error_detail=str, job_progress=lambda data: (50, 100, 'percent'))
    def parallel(hosts, description, function):
        results = {}
        for host in hosts:
            try:
                results[host] = function(host)
            except (HTTPError, ValueError, KeyError) as exc:
                host.errors[description] = str(exc)
        return results
    app.parallel = parallel
    app.power_action = Mock(side_effect=lambda host, reset: setattr(host, 'power', 'On'))
    app.restore_power = Mock(side_effect=lambda host, original: setattr(host, 'power', original))
    return app


def args(**kwargs):
    return SimpleNamespace(**dict(update_repository='https://firmware.example.test/', update_catalog='Catalog.xml',
                                  update_state=Path('unused'), unavailable_grace=300, update_timeout=100,
                                  read_only=False, no_power_monitor=True, power_throttle=False, failed=False, **kwargs))


class Tests(unittest.TestCase):
    def test_fleet_waits_for_lagging_dell_queue_before_restoration_and_rescan(self):
        app, host, options = api(), Host(), args()
        task_path = '/redfish/v1/TaskService/Tasks/JID_123'
        queue_path = updates.JOBS + '/JID_123'
        states = iter([(task_path, 'Scheduled'), (task_path, 'Running'),
                       (task_path, 'Completed'), (queue_path, 'Running'),
                       (queue_path, 'Completed')])
        queue_completed = False
        original_request = host.request

        def request(method, path, **kwargs):
            response = original_request(method, path, **kwargs)
            if method == 'POST':
                response.headers['Location'] = task_path
            return response

        def read_job(server, path):
            nonlocal queue_completed
            expected_path, state = next(states)
            self.assertEqual(path, expected_path)
            app.restore_power.assert_not_called()
            queue_completed = path == queue_path and state == 'Completed'
            return {'JobState': state, 'JobType': 'FirmwareUpdate'}

        def restore(server, original):
            self.assertTrue(queue_completed)
            server.power = original

        def discover(*unused):
            if host.submissions:
                self.assertTrue(queue_completed)
                app.restore_power.assert_called_once_with(host, 'Off')
                self.assertEqual(host.power, 'Off')
                return []
            return [package()]

        host.request = request
        app.restore_power.side_effect = restore
        with patch.object(updates, 'discover', side_effect=discover), \
             patch.object(updates, 'require_idle'), patch.object(updates, 'job_data', side_effect=read_job):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        self.assertEqual(len(host.submissions), 1)
        app.power_action.assert_called_once()
        self.assertEqual(host.power, 'Off')
        self.assertFalse(options.failed)
        self.assertEqual(app.save.call_args.args[1]['hosts'][host.name]['status'], 'up-to-date')
        self.assertEqual(list(states), [])

    def test_task_completion_also_waits_for_dell_firmware_queue(self):
        app, host = api(), Host()
        start, observe = Mock(), Mock()
        path = '/redfish/v1/TaskService/Tasks/JID_917089112150'
        with patch.object(updates, 'wait_job', side_effect=[{'JobState': 'Completed'},
                         {'JobState': 'Completed'}]) as wait:
            updates.wait_firmware_job(host, path, app, 1, 100, start, observe)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(wait.call_args_list[1].args[1], updates.JOBS + '/JID_917089112150')
        self.assertEqual(wait.call_args_list[0].kwargs['on_scheduled'], start)
        self.assertNotIn('on_scheduled', wait.call_args_list[1].kwargs)
        self.assertEqual(wait.call_args_list[1].kwargs['observe'], observe)

    def test_unresolved_dell_queue_propagates_after_task_completion(self):
        with patch.object(updates, 'wait_job', side_effect=[{'JobState': 'Completed'},
                         ValueError('queue still running')]):
            with self.assertRaisesRegex(ValueError, 'queue still running'):
                updates.wait_firmware_job(Host(), '/redfish/v1/TaskService/Tasks/JID_123', api(), 1, 100)

    def test_unresolved_previous_state_prompts_and_preserves_backup_on_continue(self):
        for answer in ('', 'CONTINUE'):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                app, host, options = api(), Host(), args()
                options.update_state = Path(directory) / 'state.json'
                previous = {'hosts': {host.name: {'status': 'stopped-uncertain',
                            'original_power': 'Off', 'restore_power_needed': True}}}
                options.update_state.write_text(json.dumps(previous))
                app.ask.return_value = answer
                with patch.object(updates, 'discover', return_value=[]) as discover:
                    updates.discover_updates([host], options, app, {host: ('user', 'secret')})
                app.ask.assert_called_once()
                self.assertFalse(options.failed)
                app.power_action.assert_not_called()
                self.assertEqual(json.loads(options.update_state.read_text()), previous)
                if answer:
                    discover.assert_called_once()
                    backup_call = app.save.call_args_list[0]
                    self.assertNotEqual(backup_call.args[0], options.update_state)
                    self.assertIn('.previous-', backup_call.args[0].name)
                    self.assertEqual(backup_call.args[1], previous)
                else:
                    discover.assert_not_called()
                    app.save.assert_not_called()

    def test_existing_jobs_offer_and_completed_reboot_records(self):
        app, host, options = api(), Host(), args()
        historical = {'JobState': 'unknown', 'PercentComplete': 100, 'Message': 'Reboot Completed'}
        self.assertEqual(updates.job_state(historical), 'Completed')
        reboot = {'JobState': 'RebootCompleted', 'JobType': 'RebootForce',
                  'PercentComplete': 100, 'Message': 'Reboot is complete.'}
        self.assertEqual(updates.job_state(reboot), 'Completed')
        self.assertTrue(updates.offer_existing_jobs(host, {'/redfish/job': reboot}, options, app, 1, Mock(), Mock()))
        app.ask.assert_not_called()
        with patch.object(updates, 'read_jobs', return_value={'/redfish/job': reboot}):
            updates.require_idle(host)
        self.assertIsNone(updates.job_state({'PercentComplete': 100}))
        scheduled = {'JobState': 'Scheduled', 'JobType': 'BIOSConfiguration'}
        record, start = Mock(), Mock()
        app.ask.return_value = ''
        self.assertFalse(updates.offer_existing_jobs(host, {'/redfish/job': scheduled}, options, app, 1, record, start))
        start.assert_not_called()
        app.ask.return_value = 'APPLY'
        with patch.object(updates, 'job_data', side_effect=[scheduled, {'JobState': 'Completed'}]):
            self.assertTrue(updates.offer_existing_jobs(host, {'/redfish/job': scheduled}, options, app, 1, record, start))
        start.assert_called_once_with(host)
        app.restore_power.assert_called_once_with(host, 'Off')

    def test_existing_running_jobs_are_watched_without_restart(self):
        app, host = api(), Host()
        app.ask.return_value = 'APPLY'
        start = Mock()
        running = {'JobState': 'Running', 'JobType': 'FirmwareUpdate'}
        with patch.object(updates, 'job_data', side_effect=[running, {'JobState': 'Completed'}]):
            self.assertTrue(updates.offer_existing_jobs(host, {'/redfish/job': running}, args(), app, 1, Mock(), start))
        start.assert_not_called()
        app.restore_power.assert_not_called()

    def test_existing_queue_decline_is_not_a_discovery_failure(self):
        app, host, options = api(), Host(), args()
        app.ask.return_value = ''
        with patch.object(updates, 'read_jobs', return_value={
                '/redfish/job': {'JobState': 'Scheduled', 'JobType': 'BIOSConfiguration'}}), \
             patch.object(updates, 'discover') as discover:
            updates.FirmwareUpdater([host], options, {host: ('user', 'secret')}, runtime=app).run()
        discover.assert_not_called()
        self.assertFalse(options.failed)
        self.assertEqual(app.save.call_args.args[1]['hosts'][host.name]['status'], 'existing-jobs-skipped')

    def test_downloaded_job_is_logged_and_skipped_without_waiting(self):
        app, host, options = api(), Host('downloaded-host'), args()
        downloaded = {'JobType': 'FirmwareUpdate', 'JobState': 'Downloaded',
                      'Message': 'Package successfully downloaded.', 'Id': 'JID_837036990797',
                      'Password': 'secret', 'Oem': {'Dell': {'Message': 'Contains secret'}}}
        with patch.object(updates, 'read_jobs', return_value={'/redfish/job': downloaded}), \
             patch.object(updates, 'discover') as discover:
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        discover.assert_not_called()
        app.ask.assert_not_called()
        app.power_action.assert_not_called()
        self.assertEqual(app.time.now, 0)
        logged = app.append_log.call_args.args[1]
        self.assertEqual(logged['job'], '/redfish/job')
        self.assertEqual(logged['response']['JobState'], 'Downloaded')
        self.assertNotIn('secret', str(logged))
        saved = app.save.call_args.args[1]['hosts'][host.name]
        self.assertEqual(saved['job_snapshots']['/redfish/job']['Password'], '[REDACTED]')
        self.assertEqual(saved['status'], 'existing-jobs-skipped')

    def test_clear_host_is_scanned_before_existing_job_review(self):
        app, clear, busy, options = api(), Host('clear'), Host('busy'), args()
        order = []
        def discover(host, *unused):
            order.append('scan:' + host.name)
            return []
        app.ask.side_effect = lambda *a, **k: order.append('review:busy') or ''
        with patch.object(updates, 'read_jobs', side_effect=lambda host: {} if host is clear else {
                '/redfish/job': {'JobState': 'Scheduled', 'JobType': 'BIOSConfiguration'}}), \
             patch.object(updates, 'discover', side_effect=discover):
            updates.discover_updates([clear, busy], options, app,
                                     {clear: ('user', 'secret'), busy: ('user', 'secret')})
        self.assertEqual(order, ['scan:clear', 'review:busy'])
        descriptions = [call.kwargs.get('description', '') for call in app.PROGRESS.update.call_args_list]
        self.assertIn('clear: 0 updates found; discovery complete', descriptions)

    def test_read_only_existing_jobs_never_prompt_or_change_power(self):
        app, host = api(), Host()
        options = args()
        options.read_only = True
        start = Mock()
        self.assertFalse(updates.offer_existing_jobs(host, {
            '/redfish/job': {'JobState': 'Scheduled', 'JobType': 'BIOSConfiguration'}}, options, app, 1, Mock(), start))
        app.ask.assert_not_called()
        start.assert_not_called()

    def test_queue_pagination_and_oem_job_state(self):
        host = Host()
        responses = [SimpleNamespace(json=lambda: {'Members': [{'@odata.id': '/redfish/job/1'}],
                                                   'Members@odata.nextLink': '/redfish/jobs/page2'}),
                     SimpleNamespace(json=lambda: {'Oem': {'Dell': {'JobState': 'RebootCompleted'}}}),
                     SimpleNamespace(json=lambda: {'Members': [{'@odata.id': '/redfish/job/2'}]}),
                     SimpleNamespace(json=lambda: {'JobState': 'Scheduled'})]
        with patch.object(host, 'request', side_effect=responses):
            jobs = updates.read_jobs(host)
        self.assertEqual(jobs['/redfish/job/1']['JobState'], 'Completed')
        self.assertEqual(jobs['/redfish/job/2']['JobState'], 'Scheduled')

    def test_report_arrays_dedup_and_empty(self):
        entry = '<INSTANCENAME CLASSNAME="DCIM_RepoUpdateSWID"><PROPERTY NAME="PackagePath"><VALUE>BIOS.exe</VALUE></PROPERTY><PROPERTY NAME="PackageVersion"><VALUE>2</VALUE></PROPERTY><PROPERTY.ARRAY NAME="ComponentInstalledVersion"><VALUE.ARRAY><VALUE>1</VALUE><VALUE>1</VALUE></VALUE.ARRAY></PROPERTY.ARRAY></INSTANCENAME>'
        report = '<CIM><MESSAGE>' + entry * 2 + '</MESSAGE></CIM>'
        parsed = updates.parse_packages(report)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['ComponentInstalledVersion'], ['1', '1'])
        self.assertEqual(updates.parse_packages('<CIM><MESSAGE/></CIM>'), [])
        for bad in ('', '<unknown/>', '<CIM>' + entry.replace('BIOS.exe', '') + '</CIM>', '<!DOCTYPE CIM><CIM/>'):
            with self.assertRaises(ValueError):
                updates.parse_packages(bad)

    def test_paths_and_error_are_not_zero_updates(self):
        self.assertEqual(updates.package_url('https://firmware.example.test/', package()), 'https://firmware.example.test/BIOS.exe')
        for path in ('http://evil/BIOS.exe', '../BIOS.exe', '%2e%2e/BIOS.exe', '//evil/BIOS.exe'):
            with self.assertRaises(ValueError):
                updates.package_url('https://firmware.example.test/', {'PackagePath': path})
        with self.assertRaises(ValueError):
            updates.local_path('https://other/redfish/job', Host())
        app = api()
        self.assertFalse(updates.no_updates(HTTPStatusError(404, {'error': {'message': 'Catalog not found'}}), app))
        self.assertTrue(updates.no_updates(HTTPStatusError(400, {'error': {'@Message.ExtendedInfo': [{'MessageId': 'IDRAC.2.8.SUP029'}]}}), app))

    def test_scheduled_power_once_and_timeout(self):
        app = api()
        start = Mock()
        states = [{'JobState': state} for state in ('Scheduled', 'Scheduled', 'Running', 'Completed')]
        with patch.object(updates, 'job_data', side_effect=states):
            updates.wait_job(Host(), '/redfish/job', app, 1, 100, start)
        start.assert_called_once()
        with patch.object(updates, 'job_data', return_value={'JobState': 'Running'}):
            with self.assertRaises(ValueError):
                updates.wait_job(Host(), '/redfish/job', app, 1, 10)

    def test_scan_only_payload_and_parse(self):
        app, host = api(), Host()
        report = '<CIM><MESSAGE/></CIM>'
        responses = [SimpleNamespace(headers={'Location': '/redfish/scan'}), SimpleNamespace(json=lambda: {'PackageList': report})]
        with patch.object(updates, 'require_idle'), patch.object(updates, 'action', side_effect=['/redfish/scan-action', '/redfish/list-action']), patch.object(updates, 'wait_job'), patch.object(host, 'request', side_effect=responses) as request:
            self.assertEqual(updates.discover(host, args(), app, 1, Mock()), [])
        payload = request.call_args_list[0].kwargs['json']
        self.assertEqual(payload['ApplyUpdate'], 'False')
        self.assertIs(payload['RebootNeeded'], False)
        self.assertNotIn('ApplyDowngradeVersions', payload)
        self.assertNotIn('ApplySameVersions', payload)
        self.assertEqual(payload['IPAddress'], 'firmware.example.test')

    def run_loop(self, reports, outcome=None, options=None):
        app, host = api(), Host()
        options = options or args()
        reports = iter(reports)
        def discover(*unused):
            return next(reports)
        def wait(host, path, app, task, timeout, on_scheduled=None, observe=None):
            if on_scheduled:
                on_scheduled()
            if outcome:
                return outcome(host)
            return {'JobState': 'Completed'}
        with patch.object(updates, 'discover', side_effect=discover), patch.object(updates, 'require_idle'), patch.object(updates, 'wait_job', side_effect=wait):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        return host, app, options

    def test_success_recheck_restore_and_controller_only(self):
        host, app, options = self.run_loop([[package()], []])
        self.assertEqual(len(host.submissions), 1)
        self.assertEqual(host.submissions[0]['@Redfish.OperationApplyTime'], 'OnReset')
        app.power_action.assert_called_once()
        app.restore_power.assert_called_once()
        self.assertEqual(host.power, 'Off')
        self.assertFalse(options.failed)
        self.assertIsNone(host.renew_session)
        host, app, _ = self.run_loop([[package('iDRAC', reboot='IDRAC')], []])
        self.assertEqual(host.submissions[0]['@Redfish.OperationApplyTime'], 'Immediate')
        app.power_action.assert_not_called()

    def test_ctrl_c_drains_submitted_job_before_restoring_and_skips_recheck(self):
        for interrupt_state in ('Pending', 'Running'):
            with self.subTest(interrupt_state=interrupt_state):
                app, host, options = api(), Host(), args()
                previous_handler = signal.getsignal(signal.SIGINT)
                states = iter(['Pending', 'Scheduled', 'Running', 'Completed'])
                def read_job(*unused):
                    state = next(states)
                    if state == interrupt_state:
                        signal.raise_signal(signal.SIGINT)
                        app.restore_power.assert_not_called()
                    return {'JobState': state}
                with patch.object(updates, 'discover', return_value=[package(), package('next')]) as discover, \
                     patch.object(updates, 'require_idle'), patch.object(updates, 'job_data', side_effect=read_job):
                    updates.discover_updates([host], options, app, {host: ('user', 'secret')})
                self.assertEqual(len(host.submissions), 1)
                self.assertEqual(discover.call_count, 1)
                app.power_action.assert_called_once()
                app.restore_power.assert_called_once_with(host, 'Off')
                self.assertEqual(host.power, 'Off')
                self.assertTrue(options.interrupted)
                self.assertFalse(options.failed)
                self.assertEqual(signal.getsignal(signal.SIGINT), previous_handler)
                entry = app.save.call_args.args[1]['hosts'][host.name]
                self.assertEqual(entry['status'], 'stopped-by-user')
                self.assertFalse(entry['restore_power_needed'])

    def test_ctrl_c_before_submission_does_not_submit_or_change_power(self):
        app, host, options = api(), Host(), args()
        original = host.request
        def request(method, path, **kwargs):
            if path.endswith('UpdateService'):
                signal.raise_signal(signal.SIGINT)
            return original(method, path, **kwargs)
        host.request = request
        with patch.object(updates, 'discover', return_value=[package()]), patch.object(updates, 'require_idle'):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        self.assertEqual(host.submissions, [])
        app.power_action.assert_not_called()
        app.restore_power.assert_not_called()
        self.assertTrue(options.interrupted)
        self.assertEqual(app.save.call_args.args[1]['hosts'][host.name]['status'], 'stopped-by-user')

    def test_second_ctrl_c_exits_without_powering_off_unfinished_update(self):
        app, host, options = api(), Host(), args()
        previous_handler = signal.getsignal(signal.SIGINT)
        states = iter(['Scheduled', 'Running'])

        def read_job(*unused):
            state = next(states)
            if state == 'Running':
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGINT)
            return {'JobState': state}

        with patch.object(updates, 'discover', return_value=[package(), package('next')]), \
             patch.object(updates, 'require_idle'), patch.object(updates, 'job_data', side_effect=read_job):
            with self.assertRaises(KeyboardInterrupt):
                updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        self.assertEqual(len(host.submissions), 1)
        app.power_action.assert_called_once()
        app.restore_power.assert_not_called()
        self.assertEqual(host.power, 'On')
        self.assertTrue(host.cancel_reads.is_set())
        self.assertTrue(app.save.call_args.args[1]['hosts'][host.name]['restore_power_needed'])
        self.assertEqual(signal.getsignal(signal.SIGINT), previous_handler)

    def test_ctrl_c_during_submission_does_not_interrupt_or_repeat_post(self):
        app, host, options = api(), Host(), args()
        original = host.request
        def request(method, path, **kwargs):
            if method == 'POST':
                signal.raise_signal(signal.SIGINT)
            return original(method, path, **kwargs)
        host.request = request
        with patch.object(updates, 'discover', return_value=[package('iDRAC', reboot='IDRAC')]), \
             patch.object(updates, 'require_idle'), patch.object(updates, 'job_data', return_value={'JobState': 'Completed'}):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        self.assertEqual(len(host.submissions), 1)
        self.assertTrue(options.interrupted)
        app.power_action.assert_not_called()
        app.restore_power.assert_not_called()

    def test_ctrl_c_at_update_prompt_exits_without_submitting(self):
        app, host, options = api(), Host(), args()
        app.ask.side_effect = lambda *a, **k: signal.raise_signal(signal.SIGINT)
        with patch.object(updates, 'discover', return_value=[package()]):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        self.assertTrue(options.interrupted)
        self.assertEqual(host.submissions, [])
        app.power_action.assert_not_called()

    def test_ctrl_c_skips_next_host_and_restores_the_started_host(self):
        app, first, second, options = api(), Host('first'), Host('second'), args()
        def wait(host, path, app, task, timeout, on_scheduled=None, observe=None):
            on_scheduled()
            signal.raise_signal(signal.SIGINT)
            return {'JobState': 'Completed'}
        with patch.object(updates, 'discover', return_value=[package()]), \
             patch.object(updates, 'require_idle'), patch.object(updates, 'wait_job', side_effect=wait):
            updates.discover_updates([first, second], options, app,
                                     {first: ('user', 'secret'), second: ('user', 'secret')})
        self.assertEqual(len(first.submissions), 1)
        self.assertEqual(second.submissions, [])
        app.restore_power.assert_called_once_with(first, 'Off')
        self.assertEqual([first.power, second.power], ['Off', 'Off'])

    def test_ctrl_c_timeout_keeps_unfinished_flash_on_and_retains_recovery_state(self):
        app, host, options = api(), Host(), args()
        calls = 0
        def read_job(*unused):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'JobState': 'Scheduled'}
            if calls == 2:
                signal.raise_signal(signal.SIGINT)
            return {'JobState': 'Running'}
        with patch.object(updates, 'discover', return_value=[package()]), \
             patch.object(updates, 'require_idle'), patch.object(updates, 'job_data', side_effect=read_job):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        app.restore_power.assert_not_called()
        self.assertEqual(host.power, 'On')
        entry = app.save.call_args.args[1]['hosts'][host.name]
        self.assertEqual(entry['status'], 'stopped-uncertain')
        self.assertTrue(entry['restore_power_needed'])
        self.assertTrue(options.failed)

    def test_terminal_failure_and_no_progress_stop(self):
        def fail(host):
            raise updates.UpdateFailure('known terminal failure')
        host, app, options = self.run_loop([[package()], [package()]], fail)
        self.assertEqual(len(host.submissions), 1)
        self.assertTrue(options.failed)
        host, app, options = self.run_loop([[package()], [package()]])
        self.assertEqual(len(host.submissions), 1)
        self.assertTrue(options.failed)
        self.assertIn('completed package is still offered', app.CONSOLE.file.getvalue())

    def test_failed_prerequisite_retried_only_after_success(self):
        attempts = []
        def outcome(host):
            name = host.submissions[-1]['ImageURI'].rsplit('/', 1)[-1]
            attempts.append(name)
            if attempts == ['dependent.exe']:
                raise updates.UpdateFailure('prerequisite missing')
        dependent, prerequisite = package('dependent'), package('prerequisite')
        reports = [[dependent, prerequisite], [dependent, prerequisite], [dependent], []]
        host, app, options = self.run_loop(reports, outcome)
        self.assertEqual(attempts, ['dependent.exe', 'prerequisite.exe', 'dependent.exe'])
        self.assertEqual(len(host.submissions), 3)

    def test_read_only_and_ambiguous_write_never_repeated(self):
        readonly = args()
        readonly.read_only = True
        host, app, _ = self.run_loop([[package()]], options=readonly)
        self.assertFalse(host.submissions)
        app.ask.assert_not_called()
        app, host, options = api(), Host(), args()
        original_request = host.request
        def lost_response(method, path, **kwargs):
            if method == 'POST':
                host.submissions.append(kwargs['json'])
                raise HTTPError('connection lost after sending firmware')
            return original_request(method, path, **kwargs)
        host.request = lost_response
        with patch.object(updates, 'discover', return_value=[package()]), patch.object(updates, 'require_idle'):
            updates.discover_updates([host], options, app, {host: ('user', 'secret')})
        self.assertEqual(len(host.submissions), 1)
        self.assertTrue(options.failed)
        app.restore_power.assert_not_called()

    def test_expired_session_get_renews_but_post_does_not(self):
        fake_httpx = SimpleNamespace(HTTPError=HTTPError, HTTPStatusError=HTTPStatusError, TransportError=type('TransportError', (HTTPError,), {}))
        spec = importlib.util.spec_from_file_location('offline_compare', Path(__file__).resolve().parents[1] / 'src' / 'lazy_idracer' / 'cli.py')
        app = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'httpx': fake_httpx}):
            spec.loader.exec_module(app)
        response = SimpleNamespace(raise_for_status=lambda: None)
        client = SimpleNamespace(request=Mock(side_effect=[HTTPStatusError(401, {}), response]))
        self.enterContext(patch.dict(app.DellServer.request.__globals__, {'httpx': fake_httpx}))
        host = app.DellServer('test', client)
        host.renew_session = Mock()
        host.request('GET', '/redfish/job')
        host.renew_session.assert_called_once()
        self.assertEqual(client.request.call_count, 2)
        client.request = Mock(side_effect=HTTPStatusError(401, {}))
        host.renew_session.reset_mock()
        with self.assertRaises(HTTPStatusError):
            host.request('POST', '/redfish/update')
        self.assertEqual(client.request.call_count, 1)
        host.renew_session.assert_not_called()

    def test_update_cli_defaults_and_conflicting_mode(self):
        spec = importlib.util.spec_from_file_location('offline_cli', Path(__file__).resolve().parents[1] / 'src' / 'lazy_idracer' / 'cli.py')
        app = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'httpx': SimpleNamespace()}):
            spec.loader.exec_module(app)
        parsed = []
        original = app.argparse.ArgumentParser.parse_args
        def capture(parser, *parameters, **kwargs):
            result = original(parser, *parameters, **kwargs)
            parsed.append(result)
            return result
        with patch.object(sys, 'argv', ['lazy-idracer', '--hosts', 'test', '--update-repository', 'https://firmware.example.test/', '--discover-updates']), patch.object(app.argparse.ArgumentParser, 'parse_args', capture), patch.object(app.getpass, 'getpass', side_effect=RuntimeError('stop before login')):
            with self.assertRaisesRegex(RuntimeError, 'stop before login'):
                app.main()
        self.assertEqual(parsed[0].update_repository, 'https://firmware.example.test/')
        self.assertEqual(parsed[0].update_catalog, 'Catalog.xml')
        self.assertTrue(parsed[0].power_throttle)
        self.assertEqual(parsed[0].power_budget, 2000)
        with patch.object(sys, 'argv', ['lazy-idracer', '--hosts', 'test', '--discover-updates', '--power-state']), patch.object(sys, 'stderr', StringIO()):
            with self.assertRaises(SystemExit) as exc:
                app.main()
        self.assertEqual(exc.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
