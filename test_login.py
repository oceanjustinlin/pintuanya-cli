from email.message import Message
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import login


def response(body=b'', cookies=(), status=200):
    headers = Message()
    for cookie in cookies:
        headers.add_header('Set-Cookie', cookie)
    return status, headers, body


class LoginTest(unittest.TestCase):
    def test_extract_qr_requires_expected_path_and_safe_identifier(self):
        self.assertEqual(login.qr_identifier('<img src="/connect/qrcode/abc_123-foo">'), 'abc_123-foo')
        for text in ('<img src="https://evil.test/qr">', '<img src="/connect/qrcode/../escape">'):
            with self.assertRaises(ValueError):
                login.qr_identifier(text)

    def test_qr_supports_real_wechat_jpeg_and_rejects_html(self):
        self.assertEqual(login.image_suffix(b'\xff\xd8\xff\xe0jpeg'), '.jpg')
        self.assertEqual(login.image_suffix(b'\x89PNG\r\n\x1a\npng'), '.png')
        with self.assertRaises(ValueError):
            login.image_suffix(b'<html>error</html>')

    def test_poll_parser_does_not_evaluate_javascript_or_echo_payload(self):
        self.assertEqual(login.poll_result('window.wx_errcode=405;window.wx_code="ABC_123";'), (405, 'ABC_123'))
        self.assertEqual(login.poll_result('window.wx_errcode=404;'), (404, None))
        for text in ('private response', 'window.wx_errcode=405;window.wx_code=evil();'):
            with self.assertRaises(ValueError) as caught:
                login.poll_result(text)
            self.assertNotIn(text, str(caught.exception))

    def test_callback_restricted_to_site_auth_and_state_required(self):
        self.assertIn('state=state-test', login.callback_url({'redirect_uri': 'https://www.pintuanya.com/auth?redirect_uri=/', 'state': 'state-test'}, 'CODE'))
        for url in ('https://evil.test/auth', 'https://www.pintuanya.com.evil.test/auth',
                    'http://www.pintuanya.com/auth', 'https://www.pintuanya.com:444/auth',
                    'https://evil@www.pintuanya.com/auth', 'https://www.pintuanya.com/other'):
            with self.assertRaises(ValueError):
                login.callback_url({'redirect_uri': url, 'state': 'state-test'}, 'CODE')

    def test_cookie_domain_expiry_and_deletion(self):
        cookies = login.CookieState('sid=old; preference=keep')
        url = 'https://www.pintuanya.com/api/v1/user'
        cookies.receive(url, response(cookies=['sid=new; Path=/; Secure; HttpOnly'])[1])
        self.assertIn('sid=new', cookies.header(url))
        self.assertIn('preference=keep', cookies.header(url))
        cookies.receive(url, response(cookies=['stolen=no; Domain=evil.test; Path=/'])[1])
        self.assertNotIn('stolen', cookies.header(url))
        self.assertEqual(cookies.header('https://open.weixin.qq.com/'), '')
        cookies.receive(url, response(cookies=['sid=deleted; Max-Age=0; Path=/'])[1])
        self.assertNotIn('sid=', cookies.header(url))

    def test_imported_cookie_is_replaced_by_explicit_domain_update_or_deletion(self):
        for domain in ('.pintuanya.com', 'www.pintuanya.com'):
            with self.subTest(domain=domain):
                jar = login.CookieState('sid=old; preference=keep')
                jar.receive(login.API_CHECK, response(cookies=['sid=new; Domain=' + domain + '; Path=/; Secure'])[1])
                self.assertEqual(jar.header().count('sid='), 1)
                self.assertNotIn('sid=old', jar.header())
                self.assertIn('sid=new', jar.header())
                jar = login.CookieState('sid=old')
                jar.receive(login.API_CHECK, response(cookies=['sid=gone; Domain=' + domain + '; Path=/; Max-Age=0'])[1])
                self.assertEqual(jar.header(), '')

    def test_rejected_domain_cannot_remove_imported_cookie(self):
        jar = login.CookieState('sid=old')
        jar.receive(login.API_CHECK, response(cookies=['sid=gone; Domain=evil.test; Path=/; Max-Age=0'])[1])
        self.assertEqual(jar.header(), 'sid=old')

    def test_session_redirect_cannot_leak_cookie_to_other_domain(self):
        headers = Message()
        headers['Location'] = 'https://evil.test/'
        session = login.LoginSession('device', 'curl')
        session.cookies = login.CookieState('sid=secret')
        with patch('login.raw_request', return_value=(302, headers, b'')) as request:
            with self.assertRaises(ValueError):
                session.get('https://www.pintuanya.com/auth?code=CODE&state=STATE', follow=True)
        self.assertEqual(request.call_count, 1)

    def test_redirects_share_one_timeout_budget(self):
        headers = Message()
        headers['Location'] = '/groupon/list'
        session = login.LoginSession('device')
        with patch('login.time.monotonic', side_effect=[0, 1, 3]), patch('login.raw_request', side_effect=[(302, headers, b''), response()]) as request:
            session.get(login.SITE + '/auth?code=CODE&state=STATE', follow=True, timeout=10)
        self.assertEqual([c.kwargs['timeout'] for c in request.call_args_list], [9, 7])

    def test_complete_flow_checks_before_save_and_cleans_temporary_qr(self):
        events = []
        class Session:
            cookies = login.CookieState('sid=new')
            def get(self, url, **kwargs):
                if '/login-params?' in url:
                    return response(b'{"code":0,"data":{"appid":"app","state":"state","redirect_uri":"https://www.pintuanya.com/auth?redirect_uri=/"}}')
                if '/connect/qrconnect?' in url:
                    return response(b'<img src="/connect/qrcode/qr-id">')
                if '/connect/qrcode/' in url:
                    return response(b'\x89PNG\r\n\x1a\nimage')
                if '/connect/l/qrconnect?' in url:
                    return response(b'window.wx_errcode=405;window.wx_code="CODE";')
                if '/auth?' in url:
                    events.append('callback')
                    return response()
                raise AssertionError('unexpected request')
        def verify(cookie, device):
            self.assertEqual((cookie, device), ('sid=new', 'device'))
            events.append('check')
        def save(cookie, device):
            events.append('save')
        output = io.StringIO()
        login.scan_login(Session(), 'device', save, verify, output=output)
        self.assertEqual(events, ['callback', 'check', 'save'])
        path = Path(output.getvalue().split('二维码文件: ')[1].splitlines()[0])
        self.assertFalse(path.exists())
        self.assertNotIn('CODE', output.getvalue())
        self.assertNotIn('sid=new', output.getvalue())

    def test_cancel_does_not_verify_or_save_and_explicit_qr_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'qr.png'
            class Session:
                def get(self, url, **kwargs):
                    if '/login-params?' in url:
                        return response(b'{"data":{"appid":"app","state":"state","redirect_uri":"https://www.pintuanya.com/auth"}}')
                    if '/connect/qrconnect?' in url:
                        return response(b'<img src="/connect/qrcode/qr-id">')
                    if '/connect/qrcode/' in url:
                        return response(b'\x89PNG\r\n\x1a\nimage')
                    return response(b'window.wx_errcode=403;')
            with self.assertRaisesRegex(ValueError, '取消'):
                login.scan_login(Session(), 'device', lambda *a: self.fail('saved'), lambda *a: self.fail('verified'), qr_output=target, output=io.StringIO())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_timeout_never_saves(self):
        session = unittest.mock.Mock()
        with patch('login.time.monotonic', side_effect=[0, 2]):
            with self.assertRaises(ValueError):
                login.scan_login(session, 'device', lambda *a: self.fail('saved'), lambda *a: None, timeout=1)
        session.get.assert_not_called()

    def test_verification_failure_preserves_old_credentials(self):
        # The same successful handshake must not save when the account check fails.
        class Session:
            cookies = login.CookieState('sid=new')
            def get(self, url, **kwargs):
                if '/login-params?' in url:
                    return response(b'{"data":{"appid":"app","state":"state","redirect_uri":"https://www.pintuanya.com/auth"}}')
                if '/connect/qrconnect?' in url:
                    return response(b'<img src="/connect/qrcode/qr-id">')
                if '/connect/qrcode/' in url:
                    return response(b'\x89PNG\r\n\x1a\nimage')
                if '/connect/l/qrconnect?' in url:
                    return response(b'window.wx_errcode=405;window.wx_code="CODE";')
                return response()
        with self.assertRaisesRegex(ValueError, 'account denied'):
            login.scan_login(Session(), 'device', lambda *a: self.fail('saved'),
                             lambda *a: (_ for _ in ()).throw(ValueError('account denied')), output=io.StringIO())
