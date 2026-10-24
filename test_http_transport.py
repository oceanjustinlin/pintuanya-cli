import json
import unittest
from unittest.mock import patch
import subprocess
import http_transport

class CurlTransportTest(unittest.TestCase):
    def test_cookie_and_utf8_body_only_go_to_stdin(self):
        with patch('http_transport.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"code":0}', '')) as run:
            http_transport.api_request('https://www.pintuanya.com/api/v1/groupon/goods','POST',{'Cookie':'secret-cookie'},{'title':'红胶'},'8.133.164.140')
        args=run.call_args
        self.assertNotIn('secret-cookie',str(args.args))
        self.assertIn('红胶',args.kwargs['input'])
        self.assertNotIn('\\u7ea2',args.kwargs['input'])
        self.assertNotIn('--insecure',args.args[0])
        self.assertEqual(args.args[0][1], '--disable')
        self.assertIn('www.pintuanya.com:443:8.133.164.140',args.args[0])

    def test_resolve_rejects_non_ip(self):
        with self.assertRaises(ValueError):http_transport.api_request('https://www.pintuanya.com/api/v1/x','GET',{},None,'bad --insecure')

    def test_errors_redact_cookie(self):
        with patch('http_transport.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','secret-cookie')):
            with self.assertRaisesRegex(ValueError,'隐藏'):
                http_transport.api_request('https://www.pintuanya.com/api/v1/x','GET',{'Cookie':'secret-cookie'},None,None)
