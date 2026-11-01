"""Non-executable .env configuration and atomic, conflict-checked credential storage."""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

DEFAULT_ENV = Path(__file__).resolve().with_name('.env')
CREDENTIALS = ('PINTUANYA_COOKIE', 'PINTUANYA_DEVICE_ID')
ASSIGNMENT = re.compile(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def parse_env(text):
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = ASSIGNMENT.fullmatch(line)
        try:
            if not match:
                raise ValueError()
            key, value = match.groups()
            if value.startswith('"'):
                value, end = json.JSONDecoder().raw_decode(value)
                rest = match.group(2)[end:].strip()
                if rest and not rest.startswith('#'):
                    raise ValueError()
            elif value.startswith("'"):
                end = value.find("'", 1)
                if end < 0:
                    raise ValueError()
                rest = value[end + 1:].strip()
                if rest and not rest.startswith('#'):
                    raise ValueError()
                value = value[1:end]
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
            if '\n' in value or '\r' in value or '\x00' in value:
                raise ValueError()
            values[key] = value
        except (ValueError, TypeError):
            raise ValueError(f'.env 第 {number} 行格式无效（内容已隐藏）') from None
    return values


def read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError('无法安全读取配置文件；拒绝符号链接或无法访问的路径') from None
    with os.fdopen(fd, 'rb') as handle:
        return handle.read()


class AuthConfig:
    def __init__(self, path=DEFAULT_ENV, environ=None):
        self.path = Path(path).absolute() if path is not None else None
        self.environ = os.environ if environ is None else environ
        self.snapshot = None
        self.source = '环境变量'

    def load(self):
        self.snapshot = read_private(self.path) if self.path else None
        if self.snapshot is None:
            return
        try:
            values = parse_env(self.snapshot.decode('utf-8-sig'))
        except UnicodeError:
            raise ValueError('.env 必须为 UTF-8 文本') from None
        self.source = '配置文件'
        for key in CREDENTIALS:
            self.environ.pop(key, None)
            if values.get(key):
                self.environ[key] = values[key]
        for key, value in values.items():
            if key not in CREDENTIALS:
                self.environ.setdefault(key, value)

    def save(self, cookie, device_id):
        if self.path is None:
            raise ValueError('--no-env-file 模式不保存登录；请指定 --env-file')
        if not isinstance(cookie, str) or (cookie and '=' not in cookie) or not device_id or any(
                c in cookie + device_id for c in '\r\n\x00'):
            raise ValueError('登录凭据格式无效（内容已隐藏）')
        lock_path = str(self.path) + '.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if read_private(self.path) != self.snapshot:
                raise ValueError('配置已被其他进程修改，未覆盖；请重新运行登录命令')
            text = self.snapshot.decode('utf-8-sig') if self.snapshot is not None else ''
            replacements = dict(zip(CREDENTIALS, (cookie, device_id)))
            lines = []
            for line in text.splitlines():
                match = ASSIGNMENT.fullmatch(line.strip())
                if match and match.group(1) in replacements:
                    continue
                lines.append(line)
            for key, value in replacements.items():
                lines.append(key + '=' + json.dumps(value, ensure_ascii=False))
            data = ('\n'.join(lines) + '\n').encode('utf-8')
            tmp_path = None
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(prefix='.' + self.path.name + '-', dir=self.path.parent)
                with os.fdopen(tmp_fd, 'wb') as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if read_private(self.path) != self.snapshot:
                    raise ValueError('配置已被其他进程修改，未覆盖；请重新运行登录命令')
                os.replace(tmp_path, self.path)
                self.snapshot = data
                self.environ.update(replacements)
                self.source = '配置文件'
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
