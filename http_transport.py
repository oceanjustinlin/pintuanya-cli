"""Optional system curl transport; TLS verification is always enabled."""
import ipaddress
import json
import subprocess


def api_request(url, method, headers, body, resolve_ip=None):
    args = ['curl', '--disable', '--silent', '--show-error', '--fail-with-body', '--max-time', '30',
            '--request', method, '--config', '-']
    if resolve_ip:
        ip = str(ipaddress.ip_address(resolve_ip))
        # curl accepts bracketed IPv6 literals in --resolve.
        address = '[' + ip + ']' if ':' in ip else ip
        args += ['--resolve', 'www.pintuanya.com:443:' + address]
    config = ''.join('header = ' + json.dumps(k + ': ' + str(v), ensure_ascii=False) + '\n'
                     for k, v in headers.items())
    if body is not None:
        config += 'data = ' + json.dumps(json.dumps(body, ensure_ascii=False), ensure_ascii=False) + '\n'
    result = subprocess.run(args + [url], input=config, text=True, capture_output=True, check=False)
    if result.returncode:
        message = result.stderr
        for key, value in headers.items():
            if key.lower() in ('cookie', 'authorization') and value:
                message = message.replace(value, '[隐藏]')
        raise ValueError('curl HTTP/TLS 请求失败: ' + message[:300])
    try:
        return json.loads(result.stdout)
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
