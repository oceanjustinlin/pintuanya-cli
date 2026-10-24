import subprocess
import unittest
from unittest.mock import patch
import auth

class KeychainTest(unittest.TestCase):
    def test_load_never_prints_secret(self):
        with patch('auth.subprocess.run',return_value=subprocess.CompletedProcess([],0,'cookie-secret\n','')):
            self.assertEqual(auth.load_cookie(),'cookie-secret')

    def test_missing_item_is_not_login_expiry(self):
        with patch('auth.subprocess.run',return_value=subprocess.CompletedProcess([],44,'','not found')):
            self.assertIsNone(auth.load_cookie())

    def test_save_uses_stdin_not_process_arguments(self):
        with patch('auth.subprocess.run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            with patch('auth.load_cookie',return_value='foo=secret'):
                auth.save_cookie('foo=secret')
        self.assertNotIn('foo=secret',str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['input'],'foo=secret')
