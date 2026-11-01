"""Optional system curl transport; TLS verification is always enabled."""
import ipaddress
import json
import subprocess
from email.parser import BytesParser
import io
import urllib.error
import urllib.request
import urllib.parse


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def raw_request(url, method='GET', headers=None, body=None, *, transport='curl', timeout=30, resolve_ip=None):
    """Return status, repeated headers and bytes. Never follow redirects implicitly."""
    headers = headers or {}
    if transport == 'urllib':
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, OSError):
            raise ValueError('HTTP/TLS 网络请求失败（详细响应已隐藏）') from None
        with response:
            return response.code, response.headers, response.read()
    if transport != 'curl':
        raise ValueError('PINTUANYA_TRANSPORT 仅支持 urllib 或 curl')
    args = ['curl', '--disable', '--silent', '--show-error', '--include', '--max-time', str(timeout),
            '--request', method, '--config', '-']
    if resolve_ip and urllib.parse.urlsplit(url).hostname == 'www.pintuanya.com':
        ip = str(ipaddress.ip_address(resolve_ip))
        # curl accepts bracketed IPv6 literals in --resolve.
        address = '[' + ip + ']' if ':' in ip else ip
        args += ['--resolve', 'www.pintuanya.com:443:' + address]
    config = ''.join('header = ' + json.dumps(k + ': ' + str(v), ensure_ascii=False) + '\n'
                     for k, v in headers.items())
    config += 'url = ' + json.dumps(url) + '\n'
    if body is not None:
        config += 'data = ' + json.dumps(body.decode('utf-8'), ensure_ascii=False) + '\n'
    result = subprocess.run(args, input=config.encode('utf-8'), capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f'curl HTTP/TLS 网络请求失败（退出码 {result.returncode}；详细响应已隐藏）')
    remaining = result.stdout
    status, parsed = None, None
    # curl may emit a proxy CONNECT response or 1xx headers before the final response.
    while remaining.startswith(b'HTTP/'):
        head, separator, remaining = remaining.partition(b'\r\n\r\n')
        if not separator:
            raise ValueError('curl 响应头格式无效')
        first, _, rest = head.partition(b'\r\n')
        try:
            status = int(first.split()[1])
        except (ValueError, IndexError):
            raise ValueError('curl 响应状态无效') from None
        parsed = BytesParser().parsebytes(rest + b'\r\n\r\n')
    if status is None:
        raise ValueError('curl 缺少 HTTP 响应状态')
    return status, parsed, remaining


def api_request(url, method, headers, body, resolve_ip=None, *, on_headers=None, timeout=30):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    status, response_headers, raw = raw_request(url, method, headers, data, resolve_ip=resolve_ip, timeout=timeout)
    if not 200 <= status < 300:
        raise urllib.error.HTTPError(url, status, '接口 HTTP 错误', response_headers, io.BytesIO(raw))
    if on_headers:
        on_headers(response_headers)
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError('curl 响应不是 JSON') from None


def upload(request, timeout=60):
    """Upload multipart bytes without API cookies or credentials in argv."""
    import io
    if request.full_url != 'https://upload.qiniup.com/':
        raise ValueError('非预期图片存储域名')
    headers = dict((k.lower(), v) for k,v in request.header_items())
    if set(headers) != {'content-type'}:
        raise ValueError('图片存储请求包含非预期请求头')
    result = subprocess.run(['curl', '--disable', '--silent', '--show-error', '--fail-with-body',
        '--max-time', str(timeout), '--header', 'Content-Type: ' + headers['content-type'],
        '--data-binary', '@-', request.full_url], input=request.data, capture_output=True, check=False)
    if result.returncode:
        raise ValueError('图片存储上传失败；请回查上传及审核状态')
    return io.BytesIO(result.stdout)
