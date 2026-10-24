"""Explicit macOS Keychain storage for the CLI session cookie."""
from pathlib import Path
import subprocess
import sys
import tempfile

SERVICE = 'pintuanya-cli'
ACCOUNT = 'PINTUANYA_COOKIE'


def load_cookie():
    if sys.platform != 'darwin':
        return None
    result = subprocess.run(['/usr/bin/security','find-generic-password','-s',SERVICE,
                             '-a',ACCOUNT,'-w'],capture_output=True,text=True,check=False)
    if result.returncode == 44:
        return None
    if result.returncode:
        raise ValueError('无法读取本机钥匙串中的拼团呀登录信息')
    return result.stdout.strip() or None


def save_cookie(cookie):
    if sys.platform != 'darwin':
        raise ValueError('钥匙串登录仅支持 macOS；其他系统请使用环境变量')
    cookie = cookie.strip()
    if cookie.lower().startswith('cookie:'):
        cookie = cookie.split(':',1)[1].strip()
    if not cookie or '=' not in cookie or '\n' in cookie or '\r' in cookie:
        raise ValueError('需要单行 Cookie 文本；图片无法用作登录凭据')
    # The temporary executable contains code only. The credential goes through stdin.
    with tempfile.TemporaryDirectory(prefix='pintuanya-keychain-') as folder:
        binary = str(Path(folder)/'keychain-store')
        compiled = subprocess.run(['/usr/bin/swiftc',str(Path(__file__).with_name('keychain_store.swift')),
                                   '-framework','Security','-o',binary],capture_output=True,text=True,check=False)
        if compiled.returncode:
            raise ValueError('钥匙串存储程序编译失败；请检查本机 Swift 工具链')
        result = subprocess.run([binary],input=cookie,capture_output=True,text=True,check=False)
        if result.returncode:
            raise ValueError('钥匙串保存失败；请检查系统钥匙串访问提示')
    if load_cookie() != cookie:
        raise ValueError('钥匙串回读校验失败')
