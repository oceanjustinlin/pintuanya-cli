import json
import unittest
from unittest.mock import patch
import subprocess
import http_transport
import urllib.error


class RawTransportTest(unittest.TestCase):
    def test_urllib_redirect_handler_refuses_cross_domain_cookie_forwarding(self):
        import pintuanya
        import urllib.request
        request = urllib.request.Request('https://www.pintuanya.com/api/v1/user', headers={'Cookie': 'sid=secret'})
        opener = unittest.mock.Mock()
        with patch('http_transport.urllib.request.build_opener', return_value=opener) as build:
            pintuanya.api_urlopen(request, timeout=5)
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, http_transport.NoRedirect)
        self.assertIsNone(handler.redirect_request(request, None, 302, 'redirect', {}, 'https://evil.test/'))
        opener.open.assert_called_once_with(request, timeout=5)

    def test_curl_api_redirect_is_rejected_without_following(self):
        data = b'HTTP/2 302\r\nLocation: https://evil.test/\r\n\r\n'
        with patch('http_transport.subprocess.run', return_value=subprocess.CompletedProcess([], 0, data, b'')) as run:
            with self.assertRaises(urllib.error.HTTPError):
                http_transport.api_request('https://www.pintuanya.com/api/v1/user', 'GET', {'Cookie': 'sid=secret'}, None)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('--location', run.call_args.args[0])

    def test_raw_binary_repeated_headers_and_sensitive_url_only_in_stdin(self):
        data = b'HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 200\r\nSet-Cookie: a=one\r\nSet-Cookie: b=two\r\n\r\n\x89PNG\r\n\x1a\n'
        with patch('http_transport.subprocess.run', return_value=subprocess.CompletedProcess([], 0, data, b'')) as run:
            status, headers, body = http_transport.raw_request('https://www.pintuanya.com/auth?code=secret', headers={'Cookie': 'sid=hidden'}, transport='curl')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_all('Set-Cookie'), ['a=one', 'b=two'])
        self.assertTrue(body.startswith(b'\x89PNG'))
        self.assertNotIn('secret', str(run.call_args.args))
        self.assertNotIn('hidden', str(run.call_args.args))
        self.assertIn(b'code=secret', run.call_args.kwargs['input'])

    def test_http_auth_failure_preserves_status_without_exposing_response(self):
        data = b'HTTP/2 401\r\nContent-Type: application/json\r\n\r\n{"message":"private"}'
        with patch('http_transport.subprocess.run', return_value=subprocess.CompletedProcess([], 0, data, b'')):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                http_transport.api_request('https://www.pintuanya.com/api/v1/user', 'GET', {}, None, None, on_headers=lambda h: None)
        self.assertEqual(caught.exception.code, 401)
        self.assertNotIn('private', str(caught.exception))

class CurlTransportTest(unittest.TestCase):
    def test_cookie_and_utf8_body_only_go_to_stdin(self):
        with patch('http_transport.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'HTTP/2 200\r\n\r\n{"code":0}', b'')) as run:
            http_transport.api_request('https://www.pintuanya.com/api/v1/groupon/goods','POST',{'Cookie':'secret-cookie'},{'title':'红胶'},'8.133.164.140')
        args=run.call_args
        self.assertNotIn('secret-cookie',str(args.args))
        self.assertIn('红胶',args.kwargs['input'].decode())
        self.assertNotIn('\\u7ea2',args.kwargs['input'].decode())
        self.assertNotIn('--insecure',args.args[0])
        self.assertEqual(args.args[0][1], '--disable')
        self.assertIn('www.pintuanya.com:443:8.133.164.140',args.args[0])

    def test_resolve_rejects_non_ip(self):
        with self.assertRaises(ValueError):http_transport.api_request('https://www.pintuanya.com/api/v1/x','GET',{},None,'bad --insecure')

    def test_errors_redact_cookie(self):
        with patch('http_transport.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','secret-cookie')):
            with self.assertRaisesRegex(ValueError,'隐藏'):
                http_transport.api_request('https://www.pintuanya.com/api/v1/x','GET',{'Cookie':'secret-cookie'},None,None)
