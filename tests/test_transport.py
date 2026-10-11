"""Offline integration tests using actual HTTPX request/response types."""
import ssl
import unittest
from unittest.mock import Mock, patch
import httpx
from lazy_idracer import cli
from lazy_idracer.server import DellServer


class Tests(unittest.TestCase):
    def test_persistent_client_preserves_token_and_patch_body(self):
        received = []

        def handle(request):
            received.append(request)
            return httpx.Response(200, json={'Attributes': {'Test': 'value'}})

        with httpx.Client(base_url='https://idrac.example.test', transport=httpx.MockTransport(handle),
                          headers={'X-Auth-Token': 'test-token'}) as client:
            server = DellServer('idrac.example.test', client)
            self.assertEqual(server.request('GET', '/redfish/v1/Test').json()['Attributes']['Test'], 'value')
            server.request('PATCH', '/redfish/v1/Test', json={'Attributes': {'Test': 'new'}})
        self.assertEqual(len(received), 2)
        self.assertEqual(received[1].headers['X-Auth-Token'], 'test-token')
        self.assertEqual(received[1].content, b'{"Attributes":{"Test":"new"}}')

    def test_rejected_mutation_is_not_replayed(self):
        handle = Mock(return_value=httpx.Response(400, json={'error': {'message': 'Rejected'}}))
        with httpx.Client(base_url='https://idrac.example.test', transport=httpx.MockTransport(handle)) as client:
            server = DellServer('idrac.example.test', client)
            with self.assertRaises(httpx.HTTPStatusError):
                server.request('POST', '/redfish/v1/Test', json={})
        handle.assert_called_once()

    def test_tls_verification_default_and_explicit_bypass(self):
        for flags in ([], ['--insecure']):
            with self.subTest(flags=flags):
                host = Mock(session_path=None, blocked=True, errors={})
                with patch.object(cli, 'DellServer', return_value=host), \
                     patch.object(cli, 'parallel', return_value={}), \
                     patch.object(cli, 'Configurer'), patch.object(cli.getpass, 'getpass', return_value='secret'), \
                     patch.object(cli.httpx, 'HTTPTransport', return_value=httpx.MockTransport(
                         lambda request: httpx.Response(200, json={}))) as transport, \
                     patch('sys.argv', ['lazy-idracer', '--hosts', 'idrac.example.test', '--read-only', *flags]):
                    cli.main()
                verify = transport.call_args.kwargs['verify']
                if flags:
                    self.assertIs(verify, False)
                else:
                    self.assertIsInstance(verify, ssl.SSLContext)
                    self.assertTrue(verify.check_hostname)
                    self.assertEqual(verify.verify_mode, ssl.CERT_REQUIRED)
