#!/usr/bin/env python3
"""Apply the history-filter fix to an installed LoHo Node panel."""
import argparse
import base64
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import time

APP = Path('/opt/loho-node-view')
DATA = Path('/var/lib/loho-node-view')
RELEASE = Path('/etc/loho-node-view/release.json')
SNIPPET = Path('/etc/nginx/snippets/loho-node-view.conf')
BACKUPS = Path('/var/backups/loho-node-view')
COLLECTOR = 'loho-node-view.service'
PAYLOAD_URL = 'https://raw.githubusercontent.com/linshaocong2006/loho-node/ee13a9a762da6112bda498379dc8c255b0a1c0f1/loho-node-upgrade.py'
PAYLOAD_SHA256 = 'bcb7374d3527ff108d23f6a237e990719a86477133c53c3bb113010143ce41d7'


def read_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('文件缺失或不是普通文件：' + str(path))
    return path.read_bytes()


def load_payload(directory):
    path = directory / 'payload.py'
    subprocess.run(['curl', '-qfsSL', '--proto', '=https', '--proto-redir', '=https',
                    '--connect-timeout', '10', '--max-time', '60',
                    '-o', str(path), PAYLOAD_URL], check=True, timeout=65)
    if hashlib.sha256(path.read_bytes()).hexdigest() != PAYLOAD_SHA256:
        raise ValueError('升级包 SHA256 校验失败。')
    helpers = runpy.run_path(str(path))
    files = json.loads(gzip.decompress(base64.b64decode(helpers['PAYLOAD'])))
    return helpers, files


def changes_for(helpers, files):
    history_path = APP / 'history.py'
    history = read_file(history_path).decode()
    legacy = all(name in history for name in ('known_users', 'known_sources'))
    fixed = all(name in history for name in ('last_by_user', 'last_by_source'))
    if 'RETENTION = 7 * 86400' not in history or 'from identities import Resolver' not in history or not (legacy or fixed):
        raise ValueError('历史采集组件与此补丁不兼容。')
    replace_functions = helpers['replace_functions']
    fixed_history = replace_functions(history, files['_history'],
                                      ['__init__', 'add', 'prune', 'export'], klass='History')
    fixed_history = replace_functions(fixed_history, files['_history'],
                                      ['read_available'], klass='DurableFollower')
    compile(fixed_history, str(history_path), 'exec')

    html_path = APP / 'www/connections.html'
    html = read_file(html_path).decode()
    names = re.findall(r'assets/(connections-fast\.[0-9a-f]{20}\.js)', html)
    if len(names) != 1:
        raise ValueError('连接记录页面未找到唯一的版本化资源。')
    old_name = names[0]
    asset_directory = APP / 'www/assets'
    old_asset = read_file(asset_directory / old_name)
    if hashlib.sha256(old_asset).hexdigest()[:20] != old_name.split('.')[1]:
        raise ValueError('现有连接页面资源的散列不匹配。')
    pattern = r'(?ms)^function setOptions\(.*?(?=^function refreshOptions\()'
    old_options = re.findall(pattern, old_asset.decode())
    new_options = re.findall(pattern, files['connections-v8.js'])
    if len(old_options) != 1 or len(new_options) != 1:
        raise ValueError('连接页面的筛选函数结构不兼容。')
    if old_options != new_options and 'unique.push(current)' not in old_options[0]:
        raise ValueError('筛选函数包含其他改动，请检查当前版本。')
    fixed_asset = old_asset.replace(old_options[0].encode(), new_options[0].encode(), 1)
    changes = {}
    if fixed_asset != old_asset:
        new_name = 'connections-fast.' + hashlib.sha256(fixed_asset).hexdigest()[:20] + '.js'
        new_path = asset_directory / new_name
        release = json.loads(read_file(RELEASE))
        old_info = release['assets'].get(old_name)
        if old_info != {'source': 'connections-fast.js', 'public': False}:
            raise ValueError('连接页面资源的访问控制配置不兼容。')
        snippet = read_file(SNIPPET).decode()
        route_pattern = r'(?ms)^    location = /node-view/assets/{} \{{\n.*?^    \}}$'
        routes = list(re.finditer(route_pattern.format(re.escape(old_name)), snippet))
        if len(routes) != 1:
            raise ValueError('连接页面资源的 Nginx 路由结构不兼容。')
        route = routes[0].group()
        new_route = route.replace(old_name, new_name)
        if new_route not in snippet:
            end = routes[0].end()
            changes[SNIPPET] = (snippet[:end] + '\n' + new_route + snippet[end:]).encode()
        if release['assets'].get(new_name) != old_info:
            release['assets'][new_name] = old_info
            changes[RELEASE] = helpers['encoded'](release)
        changes = {new_path: fixed_asset, **changes}
        changes[html_path] = html.replace(old_name, new_name).encode()
    if fixed_history != history:
        changes[history_path] = fixed_history.encode()
    return changes


def wait_for_index(command, started):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            value = json.loads((DATA / 'history/index.json').read_bytes())
            if value.get('generated', 0) >= started and not value.get('error'):
                command(['systemctl', 'is-active', '--quiet', COLLECTOR])
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise ValueError('历史索引未及时更新。')


def apply(helpers, changes):
    command, replace = helpers['command'], helpers['replace']
    command(['nginx', '-t'])
    command(['systemctl', 'is-active', '--quiet', COLLECTOR])
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix='history-filters-', dir=BACKUPS))
    originals = {}
    for path in changes:
        if path.exists() or path.is_symlink():
            originals[path] = (read_file(path), path.stat())
            replace(backup / path.name, originals[path][0], mode=0o600)
        else:
            originals[path] = (None, None)
    written = []
    try:
        for path, data in changes.items():
            old_data, metadata = originals[path]
            current = read_file(path) if path.exists() or path.is_symlink() else None
            if current != old_data:
                raise ValueError('文件在升级时发生变化：' + str(path))
            replace(path, data, metadata)
            written.append(path)
            if path == SNIPPET:
                command(['nginx', '-t'])
                command(['systemctl', 'reload', 'nginx'])
        if APP / 'history.py' in changes:
            started = time.time()
            command(['systemctl', 'restart', COLLECTOR])
            wait_for_index(command, started)
    except BaseException:
        print('升级未通过检查，正在恢复原文件。', flush=True)
        for path in reversed(written):
            raw, metadata = originals[path]
            if raw is None:
                path.unlink()
            else:
                replace(path, raw, metadata)
        if SNIPPET in written:
            command(['nginx', '-t'])
            command(['systemctl', 'reload', 'nginx'])
        if APP / 'history.py' in written:
            command(['systemctl', 'restart', COLLECTOR])
        raise
    print('历史筛选修复完成。请刷新连接记录页面并重新查询。')
    print('原文件备份：' + str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅检查安装兼容性')
    args = parser.parse_args()
    if os.geteuid():
        raise SystemExit('请使用 sudo python3 或 root 用户运行。')
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='loho-history-upgrade-') as temporary:
        helpers, files = load_payload(Path(temporary))
        fd = os.open('/run/loho-node-install.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            changes = changes_for(helpers, files)
            if not changes:
                print('此服务器已包含历史筛选修复。')
            elif args.check:
                helpers['command'](['nginx', '-t'])
                helpers['command'](['systemctl', 'is-active', '--quiet', COLLECTOR])
                print('历史筛选升级兼容性检查通过。')
            else:
                apply(helpers, changes)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        raise SystemExit('历史筛选升级未完成：' + (getattr(error, 'stderr', None) or str(error)))
