#!/usr/bin/env python3
"""Update the bounded Unix-socket listener used by LoHo Node login checks."""
import argparse
import fcntl
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time

AUTH = Path('/opt/loho-node-view/auth.py')
KEY = Path('/etc/loho-node-manager/key')
SOCKET = Path('/run/loho-node-auth/api.sock')
BACKUPS = Path('/var/backups/loho-node-view')
SERVICE = 'loho-node-auth.service'
LOCK = '/run/loho-node-install.lock'


def patch_source(source):
    changes = (
        ('class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):\n    daemon_threads=True\n',
         'class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):\n'
         '    request_queue_size=64\n    daemon_threads=True\n'),
        ('        if not self.slots.acquire(False):request.close();return\n',
         '        if not self.slots.acquire(timeout=0.5):request.close();return\n'),
        ('        self.end_headers()\n        if raw:self.wfile.write(raw)\n',
         '        try:\n            self.end_headers()\n            if raw:self.wfile.write(raw)\n'
         '        except (BrokenPipeError, ConnectionResetError):\n            self.close_connection=True\n'),
    )
    for original, replacement in changes:
        if source.count(replacement) == 1:
            continue
        if source.count(original) != 1:
            raise ValueError('当前登录服务的文件结构与此补丁不兼容。')
        source = source.replace(original, replacement, 1)
    compile(source, str(AUTH), 'exec')
    return source


def check_service(*arguments):
    subprocess.run(['systemctl', *arguments, SERVICE], check=True, timeout=30)


def health():
    deadline = time.monotonic() + 15
    key = KEY.read_text().strip()
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection('localhost', timeout=2)
        try:
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(2)
            connection.sock.connect(str(SOCKET))
            connection.request('GET', '/health', headers={'X-LoHo-Auth-Key': key})
            reply = connection.getresponse()
            if reply.status == 200 and json.loads(reply.read(4096)).get('ok') is True:
                check_service('is-active', '--quiet')
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.25)
    raise ValueError('登录校验服务健康检查超时。')


def atomic_write(path, data, metadata):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.auth-update-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def upgrade(check_only):
    metadata = AUTH.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('登录服务源码不是普通文件。')
    original = AUTH.read_bytes()
    fixed = patch_source(original.decode()).encode()
    health()
    if fixed == original:
        print('登录校验已包含此修复。')
        return
    if check_only:
        print('登录校验补丁兼容性检查通过。')
        return

    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix='auth-socket-', dir=BACKUPS)) / AUTH.name
    backup.write_bytes(original)
    if AUTH.read_bytes() != original:
        raise ValueError('登录服务源码已被其他程序修改。')
    atomic_write(AUTH, fixed, metadata)
    try:
        check_service('restart')
        health()
    except BaseException:
        atomic_write(AUTH, original, metadata)
        check_service('restart')
        health()
        print('登录校验升级失败，已恢复原文件。')
        raise
    print('登录校验修复完成。备份：' + str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅检查兼容性')
    args = parser.parse_args()
    if os.geteuid():
        raise SystemExit('请使用 root 用户运行。')
    os.umask(0o077)
    fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        upgrade(args.check)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit('登录校验升级未完成：' + str(error))
