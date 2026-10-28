"""Compile current runtime components into one fresh-server installer."""
import ast
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import activity_upgrade_core as activity
import auth_upgrade_core as auth
import build_unified
import installer_core as setup
import iteration_upgrade_core as iteration
import notify_upgrade_core as notify
import pages_upgrade_core as pages
import performance_upgrade_core as perf
import traffic_upgrade_core as usage

ROOT = Path(__file__).resolve().parent
VERSION = '10.1.7'
PLACEHOLDER = 'd' * 64


def payload():
    old = build_unified.payload()
    # Start from established components, excluding abandoned HTTP capture assets.
    files = {name: text for name, text in old.items()
             if not name.startswith('_') and not name.startswith('www/http-') and name != 'README.txt'}
    files['collector.py'] = files['collector.py'].replace("LABELS = {'ADMIN','JWX','ZSM','GYP','LJY','TEMP','SHARED'}", "LABELS = {'ADMIN'}")
    files['manage_users.py'] = (ROOT/'fresh_credentials.py').read_text()
    manager = files['manager.py'].replace('from manage_users import link, replace, CONFIG, XRAY', 'from manage_users import link, replace, CONFIG, XRAY, managed_inbound')
    manager = iteration.replace_functions(manager, 'def inbound(config):\n    return managed_inbound(config)\n', ['inbound'])
    manager = manager.replace("inbound(config).get('streamSettings',{}).get('wsSettings',{}).get('path','/3dd024/')", "next(v['path'] for k,v in inbound(config)['streamSettings'].items() if k in ('xhttpSettings','wsSettings'))")
    files['manager.py'] = manager
    files['traffic.py'] = (ROOT/'traffic.py').read_text()
    files['notify.py'] = (ROOT/'notify.py').read_text()
    files['maintenance.py'] = (ROOT/'maintenance.py').read_text()
    files['www/connections.html'] = pages.connections_page(usage.patch_index(files['www/index.html']))
    files['www/index.html'] = pages.overview_page((ROOT/'web/overview.html').read_text())
    files['www/manager.html'] = pages.management_page(files['www/manager.html'])
    files['www/notifications.html'] = notify.notify_page((ROOT/'web/notifications.html').read_text(), files['www/index.html'])
    snippet = old['_nginx_snippet'].replace('@@SECRET@@', PLACEHOLDER)
    snippet = perf.patch_snippet(usage.patch_snippet(snippet))
    snippet = notify.patch_snippet(snippet, PLACEHOLDER)
    # The v8 patcher is also the canonical compiler for current assets/session routes.
    packaged = {}
    for name in ('auth.py','identities.py','system_health.py'):
        packaged[name] = (ROOT/name).read_text()
    packaged.update(_auth_core=(ROOT/'auth_upgrade_core.py').read_text(), _perf_core=(ROOT/'performance_upgrade_core.py').read_text(),
                    _history=(ROOT/'v8/history.py').read_text(), _traffic=(ROOT/'v8/traffic.py').read_text())
    for name in ('panel-core.js','iteration.css','system.html','system.js','login.html','login.js','auth.css','session.js','security.html',
                 'notifications-v8.js','connections-v8.js','query-v8.js','overview-v8.js','fast-core.js','performance.css'):
        packaged[name] = (ROOT/'web'/name).read_text()
    packaged['system.html'] = packaged['system.html'].replace('升级前记录按原名称保留；升级后的连接按固定用户 ID 关联。', '连接按固定用户 ID 关联。')
    packaged['avatar_store.py']=(ROOT/'avatar_store.py').read_text()
    packaged['identity-v1.js']=(ROOT/'web/identity-v1.js').read_text()
    packaged['_manager_features']=(ROOT/'manager.py').read_text()
    packaged['_experience']=(ROOT/'experience_core.py').read_text()
    packaged['notify.py']=(ROOT/'notify.py').read_text()
    for name in ('theme.js','theme.css','experience.css','rule-users.html'):packaged[name]=(ROOT/'web'/name).read_text()
    for name in ('client-boot.js','client-shell.js','client-router.js','profiles.js','client.css','client.webmanifest','client-sw.js','client-icons.json','manager.js'):packaged[name]=(ROOT/'web'/name).read_text()
    with tempfile.TemporaryDirectory() as folder:
        stage = Path(folder); app = stage/'app'; app.mkdir()
        for name, text in files.items():
            path = app/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        password = stage/'htpasswd'; password.write_text('compiler-only')
        key = stage/'key'; key.write_text(PLACEHOLDER)
        snip = stage/'snippet'; snip.write_text(snippet)
        with patch.multiple(iteration, APP=app, PASSWORD=password, KEY=key, SNIPPET=snip, VERSION=VERSION):
            with patch.object(auth, 'VERSION', VERSION):
                changes = iteration.prepare(packaged, auth, 'install.invalid', 5000, {'assets': {}})
        binary={}
        for path, raw in changes.items():
            if path.suffix=='.png' and app in path.parents:
                binary[str(path.relative_to(app))]=base64.b64encode(raw).decode()
                continue
            text = raw.decode().replace(str(app), '/opt/loho-node-view')
            if app in path.parents:
                files[str(path.relative_to(app))] = text
            elif path == snip:
                files['_nginx_snippet'] = text.replace(PLACEHOLDER, '@@SECRET@@')
            elif path == iteration.RELEASE:
                value = json.loads(text); value.pop('installed', None)
                files['_release'] = json.dumps(value, ensure_ascii=False)
        files['_binary_assets']=json.dumps(binary)
    # Only ship resources referenced by current HTML, not accumulated old scripts.
    keep = {'www/'+name for name in ('index.html','client.html','client.webmanifest','client-sw.js','connections.html','manager.html','notifications.html','system.html','security.html','login.html','icon.svg','manifest.webmanifest')}
    files = {name: text for name,text in files.items() if not name.startswith('www/') or name in keep or name.startswith('www/assets/')}
    files['_collector_unit'] = setup.service_unit('xray', 'www-data', 'xray')
    files['_manager_unit'] = (ROOT/'manager_unit.txt').read_text().replace('@@GROUP@@','www-data').replace('UMask=0077', 'UMask=0077\nEnvironment=XRAY_LOCATION_ASSET=/usr/local/share/xray').replace(' /run/loho-node-manager /var/log/xray', ' /run/loho-node-manager /var/log/xray /var/lib/loho-node-view/identity')
    files['_traffic_unit'] = usage.service_unit('www-data')
    files['_notify_unit'] = notify.service_unit('www-data')
    files['_auth_unit'] = auth.service_unit('www-data')
    files['README.txt'] = (ROOT/'FRESH_README.txt').read_text()
    for name,text in files.items():
        if name.endswith('.py'): ast.parse(text, filename=name, feature_version=(3,10))
    return files


def build():
    files = payload()
    # Embed proven low-level file/download helpers without an old installer path.
    original = (ROOT/'unified_core.py').read_text(); tree = ast.parse(original)
    names = {'log','run','write','directory','json_text','validate_domain','validate_port','origin','download_xray','LocalTLS'}
    helpers = '\n\n'.join(ast.get_source_segment(original, n) for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names)
    core = (ROOT/'fresh_core.py').read_text().replace('# @@LOW_LEVEL_HELPERS@@', helpers)
    files['_installer'] = core
    for name,text in files.items():
        if name.endswith('.py') or name == '_installer': ast.parse(text, filename=name, feature_version=(3,10))
    compressed = gzip.compress(json.dumps(files,ensure_ascii=False,sort_keys=True).encode(),mtime=0)
    encoded = base64.b64encode(compressed).decode()
    shell = (ROOT/'fresh_wrapper.sh').read_text().replace('@@PAYLOAD@@', '\n'.join(encoded[i:i+100] for i in range(0,len(encoded),100))).replace('@@HASH@@',hashlib.sha256(compressed).hexdigest())
    target = ROOT.parent/'deliverables/loho-node-onekey.sh'; target.write_text(shell); target.chmod(0o755)
    print(target); print('bytes:',target.stat().st_size); print('sha256:',hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == '__main__': build()
