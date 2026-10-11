"""Offline checks for diagnostic redaction and append-only storage."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'diagnostics_under_test', Path(__file__).resolve().parents[1] / 'src' / 'lazy_idracer' / 'diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class Tests(unittest.TestCase):
    def test_nested_credentials_and_known_secrets_are_redacted(self):
        data = {'Password': 'password', 'nested': [{'X-Auth-Token': 'token',
                'Message': 'password and token', 'JobState': 'Downloaded'}]}
        result = diagnostics.redact(data, ['password', 'token'])
        self.assertEqual(result['Password'], '[REDACTED]')
        self.assertEqual(result['nested'][0]['X-Auth-Token'], '[REDACTED]')
        self.assertEqual(result['nested'][0]['Message'], '[REDACTED] and [REDACTED]')
        self.assertEqual(result['nested'][0]['JobState'], 'Downloaded')
        self.assertEqual(data['Password'], 'password')

    def test_log_appends_timestamped_sanitized_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.jsonl'
            log = diagnostics.DiagnosticLog(path, ['test-password'])
            log.record({'event': 'job', 'Message': 'test-password'})
            log.record({'event': 'request', 'status': 200})
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]['Message'], '[REDACTED]')
            self.assertTrue(entries[0]['observed_at'].endswith('+00:00'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            log.clear()
            self.assertEqual(log.secrets, [])
