"""CLI-only WeChat QR login. No browser automation, JS evaluation or secret logging."""
from email.message import Message
import html
import http.cookiejar
from http.cookies import SimpleCookie, CookieError
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

from http_transport import raw_request

SITE = 'https://www.pintuanya.com'
API_CHECK = SITE + '/api/v1/groupon/batch?page=1&size=1'
WX = 'https://open.weixin.qq.com'
POLL = 'https://long.open.weixin.qq.com/connect/l/qrconnect'


class CookieState:
    def __init__(self, cookie=''):
        self.jar = http.cookiejar.CookieJar()
        if cookie:
            parsed = SimpleCookie()
            try:
                parsed.load(cookie)
            except CookieError:
                raise ValueError('Cookie 格式无效（内容已隐藏）') from None
            if not parsed:
                raise ValueError('Cookie 格式无效（内容已隐藏）')
            headers = Message()
            for name, value in parsed.items():
                headers.add_header('Set-Cookie', f'{name}={value.coded_value}; Path=/; Secure')
            self.receive(SITE + '/', headers)

    def header(self, url=API_CHECK):
        request = urllib.request.Request(url)
        self.jar.add_cookie_header(request)
        return request.get_header('Cookie', '')

    def receive(self, url, headers):
        if headers is None:
            return
        request_url = urllib.parse.urlsplit(url)
        if request_url.hostname == 'www.pintuanya.com':
            # A Cookie header carries no domain metadata. Treat root cookies from
            # the site's valid domain aliases as replacements, not duplicate names.
            for raw in headers.get_all('Set-Cookie', []):
                parsed = SimpleCookie()
                try:
                    parsed.load(raw)
                except CookieError:
                    continue
                for name, value in parsed.items():
                    domain = value['domain'].lower().lstrip('.') or 'www.pintuanya.com'
                    path = value['path'] or request_url.path.rpartition('/')[0] or '/'
                    if domain not in ('www.pintuanya.com', 'pintuanya.com') or path != '/':
                        continue
                    for old in list(self.jar):
                        if (old.name == name and old.path == '/'
                                and old.domain.lstrip('.') in ('www.pintuanya.com', 'pintuanya.com')):
                            self.jar.clear(old.domain, old.path, old.name)
        class Response:
            def info(self):
                return headers
        self.jar.extract_cookies(Response(), urllib.request.Request(url))


def site_url(url, paths=None):
    parsed = urllib.parse.urlsplit(url)
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname == 'www.pintuanya.com'
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or (paths is not None and parsed.path not in paths):
        raise ValueError('登录回调或跳转地址不在允许范围，已停止')
    return parsed


class LoginSession:
    def __init__(self, device_id, transport='curl', resolve_ip=None):
        self.device_id, self.transport, self.resolve_ip = device_id, transport, resolve_ip
        self.cookies = CookieState()

    def get(self, url, *, follow=False, timeout=30):
        deadline = time.monotonic() + timeout
        for _ in range(6):
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError('登录请求超时；原登录配置未覆盖')
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != 'https' or parsed.hostname not in
                    ('www.pintuanya.com', 'open.weixin.qq.com', 'long.open.weixin.qq.com')
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                raise ValueError('登录请求地址不在允许范围')
            headers = {'User-Agent': 'Mozilla/5.0 pintuanya-cli/0.2', 'Accept': '*/*'}
            if parsed.hostname == 'www.pintuanya.com':
                headers.update({'X-Device-Id': self.device_id, 'X-Nonce': str(uuid.uuid4()),
                                'X-Timestamp': str(int(time.time())), 'Origin': SITE,
                                'Referer': SITE + '/login'})
            cookie = self.cookies.header(url)
            if cookie:
                headers['Cookie'] = cookie
            status, received, body = raw_request(url, headers=headers, transport=self.transport,
                                               resolve_ip=self.resolve_ip, timeout=left)
            self.cookies.receive(url, received)
            if follow and status in (301, 302, 303, 307, 308):
                location = received.get('Location')
                if not location:
                    raise ValueError('登录跳转缺少地址')
                url = urllib.parse.urljoin(url, location)
                site_url(url, {'/', '/auth', '/groupon/list', '/login'})
                continue
            if not 200 <= status < 300:
                raise ValueError(f'登录接口 HTTP {status}（响应内容已隐藏）')
            return status, received, body
        raise ValueError('登录跳转次数过多')


def qr_identifier(page):
    match = re.search(r'["\x27]/connect/qrcode/([A-Za-z0-9_-]+)["\x27]', page)
    if not match:
        raise ValueError('微信二维码格式变化，无法提取二维码')
    return match.group(1)


def poll_result(script):
    status = re.search(r'\bwindow\.wx_errcode\s*=\s*(\d+)\s*;', script)
    code = re.search(r'\bwindow\.wx_code\s*=\s*["\x27]([A-Za-z0-9_-]+)["\x27]\s*;', script)
    if not status or (status.group(1) == '405' and not code):
        raise ValueError('微信轮询响应格式变化（内容已隐藏）')
    return int(status.group(1)), code.group(1) if code else None


def image_suffix(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    raise ValueError('微信二维码不是支持的 PNG/JPEG 图片，已停止')


def callback_url(params, code):
    parsed = site_url(params.get('redirect_uri', ''), {'/auth'})
    state = params.get('state')
    if not isinstance(state, str) or not state or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', code):
        raise ValueError('微信登录缺少有效的 state 或 code')
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if 'code' in query or 'state' in query:
        raise ValueError('微信回调包含冲突参数')
    query.update({'code': [code], 'state': [state]})
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def scan_login(session, device_id, save, verify, *, timeout=180, qr_output=None, output=None):
    output = sys.stdout if output is None else output
    if timeout <= 0 or timeout > 600:
        raise ValueError('--timeout 必须在 1 到 600 秒之间')
    deadline = time.monotonic() + timeout

    def get(url, **kwargs):
        left = deadline - time.monotonic()
        if left <= 0:
            raise ValueError('扫码登录超时；原登录配置未覆盖')
        try:
            return session.get(url, timeout=min(30, left), **kwargs)[2]
        except ValueError:
            if time.monotonic() >= deadline:
                raise ValueError('扫码登录超时；原登录配置未覆盖') from None
            raise

    try:
        payload = json.loads(get(SITE + '/api/v1/wx/login-params?redirect_uri=%2F'))
        params = payload.get('data')
        if not isinstance(params, dict) or not all(isinstance(params.get(k), str) and params[k]
                                                 for k in ('appid', 'redirect_uri', 'state')):
            raise ValueError()
        callback_url(params, 'validation')
    except (ValueError, AttributeError, TypeError):
        raise ValueError('无法取得有效的微信登录参数（详细响应已隐藏）') from None
    query = urllib.parse.urlencode({k: params[k] for k in ('appid', 'redirect_uri', 'state')})
    page = get(WX + '/connect/qrconnect?' + query + '&scope=snsapi_login&response_type=code').decode('utf-8')
    identifier = qr_identifier(html.unescape(page))
    image = get(WX + '/connect/qrcode/' + identifier)
    suffix = image_suffix(image)
    temporary = qr_output is None
    if temporary:
        fd, path = tempfile.mkstemp(prefix='pintuanya-login-', suffix=suffix)
        path = Path(path)
    else:
        path = Path(qr_output).absolute()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(image)
        print(f'二维码文件: {path}', file=output, flush=True)
        print('请打开图片，用微信扫码并确认；不会自动打开浏览器。', file=output, flush=True)
        if not temporary:
            print('显式输出的二维码文件会保留，请在登录结束后删除。', file=output, flush=True)
        last = None
        while True:
            query = {'uuid': identifier}
            if last is not None:
                query['last'] = last
            status, code = poll_result(get(POLL + '?' + urllib.parse.urlencode(query)).decode('utf-8'))
            if status == 405:
                get(callback_url(params, code), follow=True)
                cookie = session.cookies.header()
                if not cookie:
                    raise ValueError('回调未取得可用 Cookie；原登录配置未覆盖')
                cookie = verify(cookie, device_id) or cookie
                if time.monotonic() >= deadline:
                    raise ValueError('登录验证超时；原登录配置未覆盖')
                save(cookie, device_id)
                print('扫码登录成功，凭据已验证并保存。', file=output, flush=True)
                return
            if status == 403:
                raise ValueError('微信登录已取消；原登录配置未覆盖')
            if status in (400, 402):
                raise ValueError('微信二维码已过期，请重新运行 auth-login')
            if status not in (404, 408):
                raise ValueError(f'微信登录返回未支持的状态 {status}；原登录配置未覆盖')
            if status == 404 and last != 404:
                print('已扫码，请在微信中确认。', file=output, flush=True)
            if status == 404:
                last = status
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    finally:
        if temporary:
            path.unlink(missing_ok=True)
