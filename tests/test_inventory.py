"""Inventory validation, override semantics, and secure TLS defaults."""
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from offline_support import fake_httpx

with patch.dict(sys.modules, {'httpx': fake_httpx}):
    from lazy_idracer.cli import parse_args
    from lazy_idracer.inventory import load_inventory


class Tests(unittest.TestCase):
    def test_inventory_relative_ca_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.toml'
            path.write_text('hosts=["one.example.test"]\nuser="admin"\nca="ca.pem"\n'
                            'update_repository="https://firmware.example.test/"\n')
            args = parse_args(['--inventory', str(path)])
            self.assertEqual(args.hosts, ['one.example.test'])
            self.assertEqual(args.user, 'admin')
            self.assertEqual(args.ca, str(Path(directory) / 'ca.pem'))
            self.assertFalse(args.insecure)
            args = parse_args(['--inventory', str(path), '--hosts', 'two.example.test', '--user', 'other',
                               '--ca', 'override.pem', '--update-repository', 'https://other.example.test/'])
            self.assertEqual(args.hosts, ['two.example.test'])
            self.assertEqual(args.user, 'other')
            self.assertEqual(args.ca, 'override.pem')
            self.assertEqual(args.update_repository, 'https://other.example.test/')

    def test_no_implicit_targets_or_repository(self):
        for argv in ([], ['--hosts', 'one.example.test', '--discover-updates']):
            with self.subTest(argv=argv), patch('sys.stderr', StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    parse_args(argv)
                self.assertEqual(exc.exception.code, 2)
        args = parse_args(['--hosts', 'one.example.test'])
        self.assertIsNone(args.update_repository)
        self.assertFalse(args.insecure)
        self.assertTrue(parse_args(['--hosts', 'one.example.test', '--insecure']).insecure)

    def test_rejects_passwords_tls_bypass_and_bad_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.toml'
            for text in ('hosts=["one"]\npassword="secret"', 'hosts=["one"]\ninsecure=true',
                         'hosts="one"', 'hosts=[]', 'hosts=[123]', 'hosts=["one"]\nca=false'):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        load_inventory(path)

    def test_rejects_malformed_inventory_and_tls_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.toml'
            for text, flags in (('hosts=[', []), ('hosts=["one"]\nca="ca.pem"', ['--insecure'])):
                path.write_text(text)
                with patch('sys.stderr', StringIO()), self.assertRaises(SystemExit) as exc:
                    parse_args(['--inventory', str(path), *flags])
                self.assertEqual(exc.exception.code, 2)
