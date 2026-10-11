"""Offline regressions for interactive writes, recovery and fleet power checks."""
import importlib.util
import json
import os
from pathlib import Path
from io import StringIO
import sys
import symtable
import builtins
import tempfile
from threading import Lock, Event, Barrier, get_ident
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from offline_support import HTTPError, HTTPStatusError, Clock, fake_httpx, transport_error
from rich.console import Console


with patch.dict(sys.modules, {'httpx': fake_httpx}):
    from lazy_idracer.jobs import normalize_job
    from lazy_idracer import configuration as app, server, power, storage, ui, jobs
    spec = importlib.util.spec_from_file_location('compare_test_app', Path(__file__).resolve().parents[1] / 'src' / 'lazy_idracer' / 'cli.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
def parse_options(argv):
    return cli.parse_args(['--hosts', 'test', *argv])


PATH = '/redfish/v1/Systems/System.Embedded.1/Bios'


def response(attributes, etag='test-etag'):
    return SimpleNamespace(json=lambda: {'Attributes': attributes}, headers={'etag': etag}, status_code=200)


class Tests(unittest.TestCase):
    def test_activity_spinner_survives_completed_job_percent(self):
        progress = ui.FleetProgress(console=Console(file=StringIO()))
        task_id = progress.add_task('Downloading', total=100, busy=True)
        progress.update(task_id, completed=100)
        task = progress.tasks[0]
        self.assertTrue(task.finished)
        column = ui.ActivitySpinnerColumn()
        column.spinner = Mock()
        column.render(task)
        column.spinner.render.assert_called_once()
        progress.update(task_id, busy=False)
        self.assertEqual(column.render(task), column.finished_text)
        column.spinner.render.assert_called_once()

    def setUp(self):
        self.console = patch.object(app, 'CONSOLE', Console(file=StringIO(), width=100))
        self.console.start()
        self.addCleanup(self.console.stop)
        self.progress = patch.object(app, 'PROGRESS', Mock())
        self.progress.start()
        self.addCleanup(self.progress.stop)

    def host(self, active='Disabled', pending='Enabled', metadata=None):
        host = server.DellServer('test', Mock())
        host.session_path = '/redfish/session'
        host.resources[PATH] = {'values': {'Key': active}, 'pending': {'Key': pending},
                               'settings': PATH + '/Settings',
                               'metadata': {'Key': metadata or {'ReadOnly': False}}}
        return host

    def test_removed_cli_flags_are_rejected_before_credentials(self):
        for flag in ('--http1', '--plan', '--apply-pending', '--reboot'):
            with self.subTest(flag=flag), patch.object(sys, 'stderr', StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parse_options([flag, 'old-value'])
                self.assertEqual(error.exception.code, 2)

    def test_cli_conflicts_defaults_and_start_prompts(self):
        options = parse_options(['--start-from'])
        self.assertEqual(options.start_from, '')
        self.assertTrue(options.prompt_start_field)
        self.assertEqual(options.start_field, '')
        self.assertTrue(options.power_throttle)
        self.assertEqual(options.power_budget, 2000)
        self.assertEqual(parse_options(['--setting', PATH + '/Settings', 'Key']).setting, [PATH, 'Key'])
        self.assertTrue(parse_options(['--discover-pending-reboot']).discover_pending_reboot)
        for flags in (['--setting', '--power-state'], ['--discover-updates', '--start-from'],
                      ['--search', ''], ['--hosts', 'a', 'a'], ['--ca', 'ca.pem', '--insecure'],
                      ['--no-power-monitor'], ['--job-timeout', '0']):
            with self.subTest(flags=flags), patch.object(sys, 'stderr', StringIO()):
                with self.assertRaises(SystemExit):
                    parse_options(flags)

    def test_job_normalization_and_progress_share_completion_rules(self):
        for job in ({'JobState': 'RebootCompleted'},
                    {'Oem': {'Dell': {'JobState': 'RebootCompleted', 'JobType': 'RebootForce'}}},
                    {'JobState': 'unknown', 'Message': 'Reboot is complete.', 'PercentComplete': 100},
                    {'TaskState': 'Completed'}):
            with self.subTest(job=job):
                original = json.loads(json.dumps(job))
                self.assertEqual(normalize_job(job)['JobState'], 'Completed')
                self.assertEqual(jobs.job_progress([job]), (100, 100, 'percent'))
                self.assertEqual(job, original)
        self.assertEqual(jobs.job_progress([{'JobState': 'Running'}]), (0, 1, 'jobs'))
        self.assertEqual(jobs.job_progress([{'PercentComplete': 20}, {'PercentComplete': 40}]), (30, 100, 'percent'))

    def test_registry_labels_map_only_when_unambiguous_and_keep_json_types(self):
        registry = {'Value': [{'ValueName': '3', 'ValueDisplayName': 'eHTML5'}]}
        self.assertEqual(app.registry_value(registry, 'eHTML5'), '3')
        self.assertTrue(app.registry_equal(registry, 'eHTML5', '3'))
        self.assertFalse(app.registry_equal({}, False, 0))
        registry['Value'].append({'ValueName': '4', 'ValueDisplayName': 'eHTML5'})
        self.assertEqual(app.registry_value(registry, 'eHTML5'), 'eHTML5')

    def test_readonly_override_does_not_override_immutable_unique_or_password(self):
        host = self.host(metadata={'ReadOnly': True})
        self.assertFalse(host.writable(PATH, 'Key'))
        self.assertTrue(host.writable(PATH, 'Key', allow_readonly=True))
        for flags in ({'ReadOnly': False, 'Immutable': True},
                      {'ReadOnly': False, 'IsSystemUniqueProperty': True},
                      {'ReadOnly': False, 'Type': 'Password'}, {'ReadOnly': 'false'}, {}):
            host.resources[PATH]['metadata']['Key'] = flags
            with self.subTest(flags=flags):
                self.assertFalse(host.writable(PATH, 'Key', allow_readonly=True))
        host.resources[PATH]['metadata']['Key'] = {'Readonly': False}
        self.assertTrue(host.writable(PATH, 'Key'))

    def test_resource_read_ignores_pending_registry_aliases_that_match_active(self):
        host = self.host()
        metadata = {'Key': {'ReadOnly': False, 'Value': [
            {'ValueName': '0', 'ValueDisplayName': 'Disabled'}]}}
        host.attribute_metadata = Mock(return_value=metadata)
        active = SimpleNamespace(headers={}, json=lambda: {
            'Attributes': {'Key': 'Disabled'},
            '@Redfish.Settings': {'SettingsObject': {'@odata.id': PATH + '/Settings'}}})
        host.request = Mock(side_effect=[active, response({'Key': '0'})])
        host.read_resource(PATH)
        self.assertEqual(host.resources[PATH]['pending'], {})

    def test_active_match_overwrites_conflicting_pending_and_preserves_other_fields(self):
        host = self.host()
        host.request = Mock(side_effect=[response({'Key': 'Disabled'}),
                                        response({'Key': 'Enabled', 'Other': 9}), response({}),
                                        response({'Key': 'Disabled'}), response({'Key': 'Disabled', 'Other': 9})])
        self.assertIn('verified active', host.apply(PATH, 'Key', 'Disabled', 'Disabled'))
        write = host.request.call_args_list[2]
        self.assertEqual(write.args, ('PATCH', PATH + '/Settings'))
        self.assertEqual(write.kwargs['json'], {'Attributes': {'Key': 'Disabled'}})
        self.assertEqual(write.kwargs['headers'], {'If-Match': 'test-etag'})

    def test_label_writes_use_registry_encoding_and_readback_accepts_label(self):
        host = self.host('Java', 'Java', {'ReadOnly': False, 'Value': [
            {'ValueName': '1', 'ValueDisplayName': 'Java'}, {'ValueName': '3', 'ValueDisplayName': 'eHTML5'}]})
        host.request = Mock(side_effect=[response({'Key': 'Java'}), response({'Key': 'Java'}),
                                        response({}), response({'Key': 'Java'}), response({'Key': 'eHTML5'})])
        self.assertIn('verified pending', host.apply(PATH, 'Key', 'eHTML5', 'Java'))
        self.assertEqual(host.request.call_args_list[2].kwargs['json'], {'Attributes': {'Key': '3'}})

    def test_concurrent_change_and_bad_readback_are_failures(self):
        host = self.host()
        host.request = Mock(return_value=response({'Key': 'Changed'}))
        with self.assertRaisesRegex(ValueError, 'changed since comparison'):
            host.apply(PATH, 'Key', 'Other', 'Disabled')
        self.assertEqual(host.request.call_count, 1)
        host.request = Mock(side_effect=[response({'Key': 'Disabled'}), response({'Key': 'Enabled'}),
                                        response({}), response({'Key': 'Disabled'}), response({'Key': 'Enabled'})])
        with self.assertRaisesRegex(ValueError, 'still conflicts'):
            host.apply(PATH, 'Key', 'Other', 'Disabled')

    def test_save_failure_preserves_existing_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            app.save(path, {'before': True})
            with self.assertRaises(TypeError):
                app.save(path, {'invalid': object()})
            self.assertEqual(json.loads(path.read_text()), {'before': True})
            self.assertEqual(list(Path(directory).iterdir()), [path])
            app.save(path, {'after': True})
            self.assertEqual(json.loads(path.read_text()), {'after': True})
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_commands_do_not_confuse_uppercase_clear_and_previous_with_skip(self):
        for upper, lower in (('D', 'd'), ('P', 'p'), ('K', 'k'), ('Q', 'q'), ('C', 'c')):
            self.assertEqual(app.normalize_command(upper), lower)
        self.assertEqual(app.normalize_command('S'), 'S')
        self.assertEqual(app.normalize_command('s'), 's')
        self.assertEqual(app.parse_selection('10', [10, 34]), 10)
        self.assertEqual(app.parse_selection('#2', [10, 34]), 34)
        self.assertEqual(app.parse_selection('"D"', ['a']), 'D')
        with self.assertRaisesRegex(ValueError, 'between'):
            app.parse_selection('#10', [10, 34])

    def compare(self, answers, *, pending=None):
        host = self.host('old', 'old')
        host.resources[PATH]['values'] = {'Group.A': 'old', 'Group.B': 'old', 'Other.C': 'old'}
        host.resources[PATH]['metadata'] = {key: {'ReadOnly': False} for key in host.resources[PATH]['values']}
        host.resources[PATH]['pending'] = pending or {}
        host.read_setting = Mock(side_effect=lambda path: {'Attributes': host.resources[path]['values']})
        options = parse_options(['--all'])
        options.failed = False
        shown = []
        with patch.object(app, 'parallel', side_effect=lambda hosts, label, fn: {h: fn(h) for h in hosts}), \
             patch.object(app, 'ask', side_effect=answers), patch.object(app, 'snapshot'), \
             patch.object(app, 'save'), patch.object(app, 'show_attribute', side_effect=lambda *a, **k: shown.append(a[5])), \
             patch.object(app, 'apply_with_history', return_value={host: 'verified'}) as write:
            app.compare([host], options, PATH, [])
        return shown, write

    def test_previous_and_prefix_skip_navigation(self):
        shown, write = self.compare(['', 'P', '', 'S'])
        self.assertEqual(shown, ['Group.A', 'Group.B', 'Group.A', 'Group.B'])
        write.assert_not_called()
        shown, write = self.compare(['s', 'Group.', 'q'])
        self.assertEqual(shown, ['Group.A', 'Other.C'])
        write.assert_not_called()

    def test_uppercase_clear_requires_confirmation_and_sends_empty_string(self):
        shown, write = self.compare(['D', '""', 'CLEAR', 'q'])
        self.assertEqual(shown, ['Group.A', 'Group.B'])
        self.assertEqual(list(write.call_args.args[3].values()), [''])
        shown, write = self.compare(['D', '""', '', 'q'])
        write.assert_not_called()

    def test_power_total_rejects_missing_stale_and_duplicate_sources(self):
        first, second = self.host(), self.host()
        meter = app.PowerMonitor.__new__(app.PowerMonitor)
        meter.hosts, meter.lock, meter.errors, meter.not_before = [first, second], Lock(), {}, 0
        meter.samples = {first: {'identity': 'one', 'watts': 100, 'sampled': 95},
                         second: {'identity': 'two', 'watts': 150, 'sampled': 95}}
        with patch.object(app.time, 'monotonic', return_value=100):
            self.assertEqual(meter.total(), (250, True, 2))
            meter.samples[second]['identity'] = 'one'
            self.assertFalse(meter.total()[1])
            meter.samples[second]['sampled'] = 50
            self.assertFalse(meter.total()[1])
            del meter.samples[second]
            self.assertFalse(meter.total()[1])

    def telemetry(self, watts=123, chassis='/redfish/v1/Chassis/test', chassis_type='RackMount'):
        host = self.host()
        documents = [
            {'Links': {'Chassis': [{'@odata.id': chassis}]}},
            {'ChassisType': chassis_type, 'SerialNumber': 'server-serial',
             'Power': {'@odata.id': '/redfish/v1/Chassis/test/Power'}},
            {'PowerControl': [{'PowerConsumedWatts': watts}]},
            {'PowerControl': [{'PowerConsumedWatts': watts + 1 if type(watts) in (int, float) else watts}]}]
        host.client = SimpleNamespace(get=Mock(side_effect=[SimpleNamespace(
            json=lambda document=document: document, raise_for_status=lambda: None) for document in documents]))
        meter = power.PowerMonitor.__new__(power.PowerMonitor)
        meter.sources = {}
        return meter, host

    def test_actual_telemetry_reads_system_chassis_power_and_reuses_source(self):
        meter, host = self.telemetry()
        first = meter.read(host)
        second = meter.read(host)
        self.assertEqual(first['watts'], 123)
        self.assertEqual(second['watts'], 124)
        self.assertEqual(first['identity'], 'server-serial')
        self.assertEqual([call.args[0] for call in host.client.get.call_args_list], [
            power.SYSTEM_PATH, '/redfish/v1/Chassis/test',
            '/redfish/v1/Chassis/test/Power', '/redfish/v1/Chassis/test/Power'])
        self.assertTrue(all(call.kwargs == {'timeout': 5} for call in host.client.get.call_args_list))

    def test_telemetry_rejects_external_links_and_shared_enclosures(self):
        meter, host = self.telemetry(chassis='https://other/redfish/v1/Chassis/test')
        with self.assertRaisesRegex(ValueError, 'Invalid local'):
            meter.read(host)
        self.assertEqual(host.client.get.call_count, 1)
        meter, host = self.telemetry(chassis_type='Enclosure')
        with self.assertRaisesRegex(ValueError, 'shared enclosure'):
            meter.read(host)
        self.assertEqual(host.client.get.call_count, 2)

    def test_telemetry_rejects_invalid_wattage(self):
        for value in (-1, True, None, '123', float('inf'), float('nan')):
            with self.subTest(value=value):
                meter, host = self.telemetry(value)
                with self.assertRaises(ValueError):
                    meter.read(host)

    def test_package_functions_have_no_undefined_global_names(self):
        root = Path(__file__).resolve().parents[1]
        missing = []
        for path in [root / 'src' / 'lazy_idracer' / 'cli.py', *sorted((root / 'src' / 'lazy_idracer').glob('*.py'))]:
            table = symtable.symtable(path.read_text(), str(path), 'exec')
            defined = set(dir(builtins)) | {'__file__', '__name__'} | {
                symbol.get_name() for symbol in table.get_symbols()
                if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}
            def check(scope):
                for symbol in scope.get_symbols():
                    if symbol.is_referenced() and symbol.is_global() and symbol.get_name() not in defined:
                        missing.append(f'{path.name}:{scope.get_lineno()}: {symbol.get_name()}')
                for child in scope.get_children():
                    check(child)
            check(table)
        self.assertEqual(missing, [])

    def test_get_retries_respect_controller_delay_and_posts_are_never_replayed(self):
        clock = Clock()
        response503 = HTTPStatusError(503, {'error': {'@Message.ExtendedInfo': [
            {'MessageId': 'Base.1.ServiceTemporarilyUnavailable', 'MessageArgs': ['30']}]}})
        response503.response.headers = {}
        client = SimpleNamespace(request=Mock(side_effect=[response503, SimpleNamespace(raise_for_status=lambda: None)]))
        host = server.DellServer('test', client)
        host.read_retry_seconds = 100
        with patch.object(server, 'time', clock):
            host.request('GET', '/redfish/job')
        self.assertEqual(clock.now, 30)
        client.request = Mock(side_effect=transport_error('lost response'))
        with self.assertRaises(transport_error):
            host.request('PATCH', PATH, json={'Attributes': {'Key': 'x'}})
        self.assertEqual(client.request.call_count, 1)

    def test_login_feedback_is_quiet_unless_verbose(self):
        output = StringIO()
        host = server.DellServer('test', SimpleNamespace(headers={}))
        host.request = Mock(return_value=SimpleNamespace(
            headers={'X-Auth-Token': 'secret-token', 'Location': '/redfish/session'}, http_version='HTTP/2'))
        with patch.object(server, 'CONSOLE', Console(file=output, width=100)):
            host.login('admin', 'secret-password')
            self.assertEqual(output.getvalue(), '')
            host.verbose = True
            host.login('admin', 'secret-password')
        self.assertIn('logged in after', output.getvalue())
        self.assertIn('HTTP/2', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
        self.assertFalse(parse_options([]).verbose)
        self.assertTrue(parse_options(['--verbose']).verbose)

    def test_write_results_table_shows_wire_mapping_and_raw_pending(self):
        host = self.host('Disabled', '1', {'ReadOnly': False, 'Value': [
            {'ValueName': '1', 'ValueDisplayName': 'Enabled'}]})
        output = StringIO()
        with patch.object(app, 'CONSOLE', Console(file=output, width=150)):
            app.show_write_results(PATH, 'Key', {host: 'Enabled'}, {host: 'Disabled'},
                                   {host: 'verified pending'})
        text = output.getvalue()
        self.assertIn('Requested → wire', text)
        self.assertIn('"Enabled" → "1"', text)
        self.assertIn('"Disabled" → "1"', text)
        self.assertIn('verified pending', text)
        self.assertNotIn('Changes:', text)

    def test_progress_places_power_above_hosts_in_either_creation_order(self):
        for power_first in (True, False):
            with self.subTest(power_first=power_first):
                output = StringIO()
                progress = ui.FleetProgress(console=Console(file=output, width=100))
                overall = progress.add_task('OVERALL')
                operation = progress.add_task('OPERATION')
                if power_first:
                    progress.add_task('POWER', unit='watts')
                progress.add_task('HOST_A')
                progress.add_task('HOST_B')
                if not power_first:
                    progress.add_task('POWER', unit='watts')
                with patch.object(ui, 'OVERALL_TASK', overall), patch.object(ui, 'OPERATION_TASK', operation):
                    progress.console.print(progress.get_renderable())
                text = output.getvalue()
                positions = [text.index(label) for label in ('OVERALL', 'OPERATION', 'POWER', 'HOST_A', 'HOST_B')]
                self.assertEqual(positions, sorted(positions))

    def test_noop_does_not_add_undo_history(self):
        host, options = self.host(), parse_options([])
        host.apply = Mock(return_value='already pending; no write needed')
        with patch.object(app, 'parallel', side_effect=lambda hosts, label, fn: {h: fn(h) for h in hosts}):
            app.apply_with_history(options, PATH, 'Key', {host: 'Enabled'}, {host: 'Disabled'}, 'test')
        self.assertFalse(getattr(options, 'undo_history', []))

    def test_entry_point_builds_http2_clients_and_dispatches_configurer(self):
        host = Mock(session_path='/redfish/session', errors={})
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        transport = Mock()
        fake = SimpleNamespace(HTTPTransport=transport, Limits=Mock(), Client=Mock(return_value=client))
        with patch.object(cli, 'httpx', fake), patch.object(cli, 'DellServer', return_value=host), \
             patch.object(cli, 'Configurer') as configurer, patch.object(cli, 'logout_hosts') as logout, \
             patch.object(cli, 'parallel'), patch.object(cli, 'PROGRESS', Mock()), \
             patch.object(cli.getpass, 'getpass', return_value='secret'), \
             patch.object(sys, 'argv', ['lazy-idracer', '--hosts', 'test', '--verbose']):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(transport.call_args.kwargs['http2'])
        self.assertFalse(transport.call_args.kwargs['trust_env'])
        self.assertTrue(host.verbose)
        configurer.return_value.run.assert_called_once()
        logout.assert_called_once_with([host])

    def test_reboot_free_jobs_complete_without_power_actions_or_prompts(self):
        host = self.host()
        host.request = Mock(return_value=SimpleNamespace(json=lambda: {'JobState': 'RebootCompleted'}))
        options = parse_options(['--no-power-monitor', '--no-power-throttle'])
        options.failed = False
        records = {host.name: {'original_power': 'Off', 'triggered': False}}
        scheduled = {host: {'jobs': ['/redfish/job'], 'needs_reboot': False}}
        with patch.object(power, 'PROGRESS', Mock()), patch.object(power, 'save'), \
             patch.object(power, 'power_action') as action, patch.object(power, 'restore_power') as restore, \
             patch.object(power, 'ask') as ask:
            power.stagger_reboots([host], scheduled, {host: {}}, options, records, confirm_reboot=True)
        action.assert_not_called()
        restore.assert_not_called()
        ask.assert_not_called()
        self.assertEqual(records[host.name]['status'], 'verified-complete')
        self.assertFalse(options.failed)

    def test_application_overlaps_staggered_starts_and_restores_original_power(self):
        hosts = [self.host(), self.host()]
        hosts[0].name, hosts[1].name = 'first', 'second'
        states = {hosts[0]: 'Off', hosts[1]: 'On'}
        clock, triggers = Clock(), []
        first_waiting, second_started = Event(), Event()
        for host in hosts:
            def request(method, path, host=host, **kwargs):
                if path == power.SYSTEM_PATH:
                    data = {'PowerState': states[host]}
                else:
                    if host is hosts[0]:
                        first_waiting.set()
                        if not second_started.wait(timeout=1):
                            raise AssertionError('Second host must start before first finishes')
                    data = {'JobState': 'Completed'}
                return SimpleNamespace(json=lambda: data)
            host.request = request
        def action(host, reset):
            if host is hosts[1]:
                self.assertTrue(first_waiting.wait(timeout=1))
                second_started.set()
            triggers.append((host, clock.now, reset))
            states[host] = 'On'
        def restore(host, original):
            states[host] = original
        options = parse_options(['--no-power-monitor', '--no-power-throttle'])
        options.failed = False
        records = {h.name: {'original_power': states[h], 'triggered': False} for h in hosts}
        scheduled = {h: {'jobs': ['/redfish/job'], 'needs_reboot': True} for h in hosts}
        with patch.object(power, 'time', clock), patch.object(power, 'PROGRESS', Mock()), \
             patch.object(power, 'save'), patch.object(power, 'power_action', side_effect=action), \
             patch.object(power, 'restore_power', side_effect=restore):
            power.stagger_reboots(hosts, scheduled, {h: {} for h in hosts}, options, records)
        self.assertGreaterEqual(triggers[1][1] - triggers[0][1], 60)
        self.assertEqual([item[2] for item in triggers], ['On', 'GracefulRestart'])
        self.assertEqual([states[h] for h in hosts], ['Off', 'On'])
        self.assertTrue(all(records[h.name]['status'] == 'verified-complete' for h in hosts))
        self.assertFalse(options.failed)

    def test_concurrent_expired_session_reads_renew_only_once(self):
        barrier = Barrier(2)
        seen = set()
        client = SimpleNamespace(headers={'X-Auth-Token': 'old'})
        def request(*args, **kwargs):
            thread = get_ident()
            if thread not in seen:
                seen.add(thread)
                barrier.wait(timeout=1)
                raise HTTPStatusError(401, {})
            return SimpleNamespace(raise_for_status=lambda: None)
        client.request = request
        host = server.DellServer('test', client)
        host.renew_session = Mock(side_effect=lambda: client.headers.update({'X-Auth-Token': 'new'}))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(host.request, 'GET', '/redfish/job') for _ in range(2)]
            for future in futures:
                future.result(timeout=2)
        host.renew_session.assert_called_once()


if __name__ == '__main__':
    unittest.main()
