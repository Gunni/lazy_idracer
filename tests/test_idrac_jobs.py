"""Shared job polling regressions, with no network access or real sleeps."""
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
from offline_support import Clock, fake_httpx

with patch.dict(sys.modules, {'httpx': fake_httpx}):
    from lazy_idracer.jobs import Job, JobGroup, JobFailure, normalize_job


class Tests(unittest.TestCase):
    def server(self, states):
        return SimpleNamespace(request=Mock(side_effect=[SimpleNamespace(json=lambda state=state: state)
                                                         for state in states]))

    def test_refresh_uses_owner_server_and_retains_normalized_oem_readback(self):
        server = self.server([{'Oem': {'Dell': {'JobState': 'RebootCompleted', 'PercentComplete': 100}}}])
        job = Job(server, '/redfish/job')
        job.refresh()
        server.request.assert_called_once_with('GET', '/redfish/job')
        self.assertEqual(job.state, 'Completed')
        self.assertTrue(job.terminal)
        self.assertFalse(job.failed)
        self.assertEqual(JobGroup([job]).progress, (100, 100, 'percent'))

    def test_ready_callback_fires_once_and_downloading_pending_is_not_ready(self):
        server = self.server([{'JobState': state} for state in (
            'Pending', 'Scheduled', 'Scheduled', 'Running', 'Completed')])
        callback = Mock()
        job = Job(server, '/redfish/job')
        data = job.wait(100, clock=Clock(), on_ready=callback)
        callback.assert_called_once()
        self.assertEqual(data['JobState'], 'Completed')
        self.assertFalse(Job(server, '/redfish/job', {'JobState': 'Pending'}).ready)
        self.assertTrue(Job(server, '/redfish/job', {
            'JobState': 'Pending', 'Message': 'Task successfully scheduled.'}).ready)

    def test_mixed_failure_waits_for_other_running_job_before_raising(self):
        server = self.server([
            {'JobState': 'Failed', 'Message': 'Bad firmware'}, {'JobState': 'Running'},
            {'JobState': 'Failed', 'Message': 'Bad firmware'}, {'JobState': 'Completed'}])
        group = JobGroup([Job(server, '/redfish/failed'), Job(server, '/redfish/running')])
        clock = Clock()
        with self.assertRaises(JobFailure) as error:
            group.wait(100, clock=clock)
        self.assertTrue(group.terminal)
        self.assertEqual(server.request.call_count, 4)
        self.assertEqual(clock.now, 5)
        self.assertIn('Bad firmware', str(error.exception))

    def test_completed_task_with_critical_status_is_a_terminal_failure(self):
        job = Job(self.server([{'TaskState': 'Completed', 'TaskStatus': 'Critical'}]), '/redfish/job')
        with self.assertRaises(JobFailure):
            job.wait(100, clock=Clock())
        self.assertTrue(job.terminal)

    def test_timeout_does_not_claim_completion_or_perform_mutations(self):
        server = SimpleNamespace(request=Mock(return_value=SimpleNamespace(json=lambda: {'JobState': 'Running'})))
        job = Job(server, '/redfish/job')
        clock = Clock()
        with self.assertRaisesRegex(ValueError, 'within 7s'):
            job.wait(7, clock=clock)
        self.assertEqual(clock.now, 7)
        self.assertFalse(job.terminal)
        self.assertTrue(all(call.args[0] == 'GET' for call in server.request.call_args_list))

    def test_configuration_readiness_returns_without_waiting_for_reboot(self):
        job = Job(self.server([{'JobState': 'New'}, {'JobState': 'Scheduled'}]), '/redfish/job')
        clock = Clock()
        job.wait(180, clock=clock, interval=2, until_states={'Scheduled', 'Running', 'Starting', 'Completed'})
        self.assertEqual(job.state, 'Scheduled')
        self.assertEqual(clock.now, 2)
        self.assertFalse(job.terminal)

    def test_scheduled_job_does_not_restart_other_running_job(self):
        server = self.server([
            {'JobState': 'Scheduled'}, {'JobState': 'Running'},
            {'JobState': 'Scheduled'}, {'JobState': 'Completed'},
            {'JobState': 'Completed'}, {'JobState': 'Completed'}])
        group = JobGroup([Job(server, '/redfish/a'), Job(server, '/redfish/b')])
        clock = Clock()
        callback = Mock(side_effect=lambda: self.assertEqual(clock.now, 5))
        group.wait(100, clock=clock, on_ready=callback)
        callback.assert_called_once()

    def test_invalid_paths_and_responses_fail_before_any_power_action(self):
        server = self.server([])
        for path in ('https://other/redfish/job', '//other/redfish/job', '/other/job', '/redfish/job?x=1'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Job(server, path)
        for data in ([], None, {'Oem': []}, {'Oem': {'Dell': ['bad']}}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    normalize_job(data)
        server.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
