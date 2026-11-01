"""CLI login commands. Credentials use .env / environment only; no Keychain access."""
import os
import subprocess
import sys
import time
import uuid

from login import CookieState, LoginSession, scan_login


def normalize_cookie(cookie):
    cookie = cookie.strip()
    if cookie.lower().startswith('cookie:'):
        cookie = cookie.split(':', 1)[1].strip()
    if not cookie or '=' not in cookie or any(c in cookie for c in '\r\n\x00'):
        raise ValueError('需要单行 Cookie 文本（内容已隐藏）')
    CookieState(cookie)
    return cookie


def run_command(args, config, client_class):
    deadline = time.monotonic() + args.timeout if args.command == 'auth-login' else None
    def verify(cookie, device):
        client = client_class(cookie)
        client.device_id = device
        if deadline is not None:
            client.timeout = deadline - time.monotonic()
            if client.timeout <= 0:
                raise ValueError('登录验证超时；原登录配置未覆盖')
        client.check()
        return client.cookie

    if args.command == 'auth-status':
        print(f'配置文件: {config.path if config.path else "已禁用"}')
        print(f'凭据来源: {config.source}')
        cookie = config.environ.get('PINTUANYA_COOKIE')
        device = config.environ.get('PINTUANYA_DEVICE_ID')
        print('Cookie: ' + ('已设置' if cookie else '缺失'))
        print('设备 ID: ' + ('已设置' if device else '缺失（请求将使用临时 ID，建议运行 auth-login 固定）'))
        if not cookie:
            raise ValueError('未配置登录信息，请运行 auth-login')
        client = client_class(cookie)
        if config.source == '配置文件':
            client.persist_cookie = lambda updated: config.save(updated, client.device_id)
        client.check()
        print('登录有效')
        return
    if config.path is None:
        raise ValueError('--no-env-file 模式不保存登录；请使用 --env-file')
    device = config.environ.get('PINTUANYA_DEVICE_ID') or str(uuid.uuid4())
    if args.command == 'auth-login':
        session = LoginSession(device, os.getenv('PINTUANYA_TRANSPORT', 'curl'), os.getenv('PINTUANYA_RESOLVE_IP'))
        scan_login(session, device, config.save, verify, timeout=args.timeout, qr_output=args.qr_output)
        return
    if args.clipboard:
        result = subprocess.run(['/usr/bin/pbpaste'], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError('读取系统剪贴板失败；请检查进程的剪贴板访问权限')
        cookie = result.stdout
    else:
        if sys.stdin.isatty():
            import getpass
            cookie = getpass.getpass('Cookie（输入隐藏）: ')
        else:
            cookie = sys.stdin.read()
    cookie = normalize_cookie(cookie)
    cookie = verify(cookie, device)
    config.save(cookie, device)
    print(f'Cookie 已验证并保存至配置文件: {config.path}')
