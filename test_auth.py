import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
from email.message import Message
import os
import pintuanya


class AuthCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.env'
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_auth_save_validates_pair_before_persisting_without_keychain(self):
        out = io.StringIO()
        with patch('sys.stdin', io.StringIO('Cookie: sid=new')), patch.object(pintuanya.Client, 'check', return_value=1) as check, redirect_stdout(out):
            self.assertEqual(pintuanya.cli(['--env-file', str(self.path), 'auth-save']), 0)
        check.assert_called_once()
        self.assertIn('PINTUANYA_DEVICE_ID=', self.path.read_text())
        self.assertIn('sid=new', self.path.read_text())
        self.assertNotIn('sid=new', out.getvalue())

    def test_invalid_import_preserves_old_config(self):
        original = 'PINTUANYA_COOKIE=sid=old\nPINTUANYA_DEVICE_ID=device\n'
        self.path.write_text(original)
        with patch('sys.stdin', io.StringIO('sid=new')), patch.object(pintuanya.Client, 'check', side_effect=pintuanya.LoginError('expired')), redirect_stderr(io.StringIO()):
            self.assertEqual(pintuanya.cli(['--env-file', str(self.path), 'auth-save']), 2)
        self.assertEqual(self.path.read_text(), original)

    def test_status_reports_missing_without_network_or_keychain(self):
        out = io.StringIO()
        with patch.object(pintuanya.Client, 'check', side_effect=AssertionError('network')), redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(pintuanya.cli(['--env-file', str(self.path), 'auth-status']), 1)
        self.assertIn('凭据来源', out.getvalue())
        self.assertIn('Cookie: 缺失', out.getvalue())

    def test_no_env_file_login_rejected_before_network(self):
        with patch('login.raw_request', side_effect=AssertionError('network')), redirect_stderr(io.StringIO()):
            self.assertEqual(pintuanya.cli(['--no-env-file', 'auth-login']), 1)

    def test_file_loaded_before_group_default_and_is_independent_of_cwd(self):
        self.path.write_text('PINTUANYA_COOKIE=sid=file\nPINTUANYA_DEVICE_ID=device\nPINTUANYA_GROUP=group\n')
        client = unittest.mock.Mock()
        client.groups.return_value = [{'groupon_uuid': 'group', 'title': 'shop'}]
        client.inventory.return_value = {}
        with patch('auth_config.DEFAULT_ENV', self.path), patch.object(pintuanya, 'Client', return_value=client) as factory, redirect_stdout(io.StringIO()):
            self.assertEqual(pintuanya.cli(['list']), 0)
        factory.assert_called_once_with('sid=file')
        client.inventory.assert_called_once_with('group')

    def test_response_updates_cookie_and_persists_once(self):
        headers = Message()
        headers['Set-Cookie'] = 'sid=new; Path=/; Secure'
        class Response(io.BytesIO):
            pass
        response = Response(b'{"code":0,"data":{"list":[],"total":1}}')
        response.headers = headers
        client = pintuanya.Client('sid=old')
        saved = []
        client.persist_cookie = saved.append
        with patch('pintuanya.api_urlopen', return_value=response):
            self.assertEqual(client.check(), 1)
        self.assertIn('sid=new', client.cookie)
        self.assertEqual(saved, [client.cookie])

    def test_save_failure_after_server_success_warns_without_repeating_write(self):
        headers = Message()
        headers['Set-Cookie'] = 'sid=new; Path=/; Secure'
        class Response(io.BytesIO):
            pass
        response = Response(b'{"code":0}')
        response.headers = headers
        client = pintuanya.Client('sid=old')
        client.persist_cookie = lambda c: (_ for _ in ()).throw(ValueError('private-cookie-value'))
        err = io.StringIO()
        with patch('pintuanya.api_urlopen', return_value=response) as request, redirect_stderr(err):
            self.assertEqual(client.request('POST', '/groupon/goods/offline', body={}), {'code': 0})
        self.assertEqual(request.call_count, 1)
        self.assertIn('未保存', err.getvalue())
        self.assertNotIn('private-cookie-value', err.getvalue())

    def test_write_auth_failure_is_not_retried(self):
        import urllib.error
        error = urllib.error.HTTPError('https://www.pintuanya.com/api/v1/test', 401, 'denied', {}, None)
        with patch('pintuanya.api_urlopen', side_effect=error) as request:
            with self.assertRaises(pintuanya.LoginError):
                pintuanya.Client('sid=old').request('POST', '/groupon/goods/offline', body={})
        self.assertEqual(request.call_count, 1)

    def test_invalid_external_header_rejected_without_echoing_cookie(self):
        os.environ['PINTUANYA_COOKIE'] = 'sid=private-secret\r\ninvalid'
        err = io.StringIO()
        with patch('pintuanya.api_urlopen', side_effect=AssertionError('network')), redirect_stderr(err):
            self.assertEqual(pintuanya.cli(['--no-env-file', 'check']), 1)
        self.assertNotIn('private-secret', err.getvalue())
