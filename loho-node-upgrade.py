#!/usr/bin/env python3
"""Single maintenance entry for the existing LoHo website. Never restart or alter Xray."""
import ast
import base64
import fcntl
import gzip
import grp
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import types
def command(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)

def replace(path, data, metadata=None, mode=0o644):
    temporary = path.with_name(path.name + '.notify-' + secrets.token_hex(5))
    try:
        with temporary.open('wb') as stream:
            os.fchmod(stream.fileno(), metadata.st_mode & 0o777 if metadata else mode)
            if metadata:
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()

def deployment():
    node = Path('/etc/loho-node-manager/node.json')
    if node.exists():
        value = json.loads(node.read_text())
        return value['domain'], int(value['port'])
    tree, values = ast.parse((APP / 'manage_users.py').read_text()), {}
    for item in tree.body:
        if isinstance(item, ast.Assign):
            for name in item.targets:
                if isinstance(name, ast.Name) and name.id in ('DOMAIN', 'PORT'):
                    values[name.id] = ast.literal_eval(item.value)
    return values['DOMAIN'], int(values['PORT'])

VERSION='9.1.0'
APP=Path('/opt/loho-node-view')
DATA=Path('/var/lib/loho-node-view')
TRAFFICROOT=Path('/var/lib/loho-node-traffic')
AUTHROOT=Path('/var/lib/loho-node-auth')
AUTHSETTINGS=Path('/etc/loho-node-auth/settings.json')
SNIPPET=Path('/etc/nginx/snippets/loho-node-view.conf')
PASSWORD=Path('/etc/loho-node-view/htpasswd')
KEY=Path('/etc/loho-node-manager/key')
CATALOG=Path('/var/lib/loho-node-manager/state.json')
PENDING=Path('/var/lib/loho-node-manager/pending.json')
RELEASE=Path('/etc/loho-node-view/release.json')
BACKUPS=Path('/var/backups/loho-node-view')
AUTHUNIT=Path('/etc/systemd/system/loho-node-auth.service')
DROPIN=Path('/etc/systemd/system/loho-node-manager.service.d/30-loho-identity.conf')
MANAGER='loho-node-manager.service'
COLLECTOR='loho-node-view.service'
TRAFFIC='loho-node-traffic.service'
AUTH='loho-node-auth.service'
NOTIFY='loho-node-notify.service'
NOTIFYROOT=Path('/var/lib/loho-node-notify')
ASSET_BEGIN,ASSET_END='# BEGIN LOHO VERSIONED ASSETS V8','# END LOHO VERSIONED ASSETS V8'
MAP_BEGIN,MAP_END='# BEGIN LOHO ACCOUNT TIMELINE V8','# END LOHO ACCOUNT TIMELINE V8'


def encoded(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()


def module(name,source):
    value=types.ModuleType(name);value.__file__=str(APP/(name+'.py'));exec(compile(source,name+'.py','exec'),value.__dict__);return value


def remove_block(text,begin,end):
    if text.count(begin)!=text.count(end) or text.count(begin)>1:raise ValueError('升级标记不完整，停止覆盖：'+begin)
    return re.sub(re.escape(begin)+r'.*?'+re.escape(end)+r'\n?','',text,flags=re.S)


def replace_functions(original,replacement,names,klass=None):
    source_tree=ast.parse(original);target_tree=ast.parse(replacement)
    source_nodes=source_tree.body;target_nodes=target_tree.body
    if klass:
        source_nodes=next(n for n in source_nodes if isinstance(n,ast.ClassDef) and n.name==klass).body
        target_nodes=next(n for n in target_nodes if isinstance(n,ast.ClassDef) and n.name==klass).body
    patches=[];lines=original.splitlines(keepends=True)
    for name in names:
        source=[n for n in source_nodes if isinstance(n,ast.FunctionDef) and n.name==name]
        target=[n for n in target_nodes if isinstance(n,ast.FunctionDef) and n.name==name]
        if len(source)!=1 or len(target)!=1:raise ValueError('组件结构不兼容：'+name)
        old,new=source[0],target[0];start=min([old.lineno]+[n.lineno for n in old.decorator_list])-1
        text=ast.get_source_segment(replacement,new)
        if klass:text='    '+text
        patches.append((start,old.end_lineno,text+'\n'))
    for start,end,text in sorted(patches,reverse=True):lines[start:end]=[text]
    result=''.join(lines);ast.parse(result);return result


def patch_history(text,template):
    if 'RETENTION = 7 * 86400' not in text:raise ValueError('历史组件不是当前 7 天版本。')
    text=replace_functions(text,template,['__init__','add','as_row','export'],klass='History')
    if 'from identities import Resolver' not in text:text+='\nfrom identities import Resolver\n'
    return text


def patch_manager(text):
    text=remove_block(text,MAP_BEGIN,MAP_END)
    for name in ('load','transaction','recover'):
        if not any(isinstance(n,ast.FunctionDef) and n.name==name for n in ast.parse(text).body):raise ValueError('用户管理组件结构不兼容：'+name)
    block=MAP_BEGIN+'''
from identities import sync_catalog as _sync_identity_catalog
_identity_original_load=load
_identity_original_transaction=transaction
_identity_original_recover=recover
def load():
    result=_identity_original_load()
    _sync_identity_catalog(result[2])
    return result
def transaction(raw,state,updated,new_state):
    _sync_identity_catalog(new_state)
    try:return _identity_original_transaction(raw,state,updated,new_state)
    except BaseException:
        _sync_identity_catalog(state)
        raise
def recover():
    try:return _identity_original_recover()
    finally:
        if STATE.exists():_sync_identity_catalog(json.loads(STATE.read_text()))
_IdentityBaseHandler=Handler
class Handler(_IdentityBaseHandler):
    def do_POST(self):
        import fcntl
        try:
            with open('/run/loho-node-install.lock','rb') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                except BlockingIOError:
                    if self.authorize():self.send_json(503,{'error':'网站正在升级，请稍后重试。'})
                    return
        except FileNotFoundError:pass
        return super().do_POST()
'''+MAP_END+'\n'
    match=re.search(r'(?m)^if __name__\s*==',text)
    if match:text=text[:match.start()].rstrip()+'\n\n'+block+'\n'+text[match.start():]
    else:text=text.rstrip()+'\n\n'+block
    ast.parse(text);return text


def patch_auth(text):
    return text+'''
# BEGIN LOHO SYSTEM API V8
from urllib.parse import parse_qs as _status_query
from system_health import snapshot as _system_snapshot,preview as _rule_preview
_BaseSessionHandler=Handler
class Handler(_BaseSessionHandler):
    def do_GET(self):
        parsed=urlsplit(self.path)
        if parsed.path not in ('/system','/preview'):return super().do_GET()
        if not self.trusted():return
        try:
            if session(self.headers.get('Cookie','')) is None:
                self.reply(401,{'error':'请先登录。'});return
            if parsed.path=='/system':self.reply(200,_system_snapshot());return
            query=_status_query(parsed.query,max_num_fields=101)
            self.reply(200,_rule_preview(query.get('account',['']),query.get('q',[''])[0]))
        except ValueError as error:self.reply(400,{'error':str(error)})
        except (OSError,sqlite3.Error):self.reply(503,{'error':'状态暂不可用或查询超时，请稍后重试。'})
# END LOHO SYSTEM API V8
'''


def configure_auth_source(source):
    # The base module invokes main only at the end; append the extension before that guard.
    match=re.search(r'(?m)^if __name__\s*==',source)
    core=source[:match.start()] if match else source
    return patch_auth(core)+'\nif __name__=="__main__":main()\n'


def restore_asset_names(html,registry):
    def restore(match):
        attr,quote,path=match.groups();clean=path.split('?',1)[0].removeprefix('./')
        if clean.startswith('/node-view/'):clean=clean[len('/node-view/'):]
        if clean.startswith('assets/'):
            asset=registry.get(clean[len('assets/'):])
            if not asset:raise ValueError('版本资源索引缺失，请保留原 release.json。')
            return attr+'='+quote+asset['source']+quote
        return match.group()
    return re.sub(r'\b(src|href)=(["\x27])([^"\x27]+)\2',restore,html)


def version_assets(pages,resources,registry,changes):
    registry=dict(registry)
    for name,html in list(pages.items()):
        public=name=='login.html'
        def rewrite(match):
            attr,quote,url=match.groups();source=url.split('?',1)[0]
            if source.startswith('./'):source=source[2:]
            if source.startswith('/node-view/'):source=source[len('/node-view/'):]
            if not re.fullmatch(r'[A-Za-z0-9_.-]+\.(?:js|css|svg)',source):return match.group()
            raw=resources.get(source)
            if raw is None:
                path=APP/'www'/source
                if not path.is_file() or path.is_symlink():raise ValueError('网页引用资源缺失：'+source)
                raw=path.read_bytes();resources[source]=raw
            digest=hashlib.sha256(raw).hexdigest()[:20];stem,extension=source.rsplit('.',1);filename=stem+'.'+digest+'.'+extension
            info=registry.get(filename,{'source':source,'public':False});info['public']=info['public'] or public;registry[filename]=info
            changes[APP/'www/assets'/filename]=raw
            return attr+'='+quote+'assets/'+filename+quote
        pages[name]=re.sub(r'\b(src|href)=(["\x27])([^"\x27]+)\2',rewrite,html)
    return registry


def asset_routes(registry,auth):
    result=ASSET_BEGIN+'\n'
    for filename,info in sorted(registry.items()):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.[0-9a-f]{20}\.(js|css|svg)',filename):raise ValueError('无效版本资源名。')
        path=APP/'www/assets'/filename
        result+='    location = /node-view/assets/'+filename+' {\n'
        if info['public']:result+='        auth_basic off;\n        auth_request off;\n'
        else:result+='        '+auth.GUARD+'\n'
        result+='        alias '+str(path)+';\n        limit_except GET { deny all; }\n        types { text/css css; application/javascript js; image/svg+xml svg; }\n'
        result+='        add_header Cache-Control "private, max-age=31536000, immutable";\n        add_header X-Content-Type-Options "nosniff" always;\n    }\n'
    return result+ASSET_END+'\n'


def static_json_route(path,filename,auth):
    return '''    location = %s {
        alias %s;
        default_type application/json;
        %s
        add_header Cache-Control "no-store" always;
        add_header X-Content-Type-Options "nosniff" always;
        gzip on;
        gzip_types application/json;
        gzip_min_length 512;
    }
'''%(path,filename,auth.GUARD)


def socket_json(path,route):
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.settimeout(4);connection=http.client.HTTPConnection('localhost',timeout=4)
    try:
        sock.connect(path);connection.sock=sock;connection.request('GET',route,headers={'X-LoHo-Manager-Key':KEY.read_text().strip(),'X-LoHo-Actor':'upgrader'})
        response=connection.getresponse();value=json.loads(response.read(1048576))
        if response.status!=200:raise ValueError('管理接口检查失败，HTTP '+str(response.status))
        return value
    finally:connection.close();sock.close()


def pause_manager():
    if PENDING.exists():raise ValueError('用户管理有未完成事务，请先处理。')
    if socket_json('/run/loho-node-manager/api.sock','/users').get('busy'):raise ValueError('用户管理正在应用变更，请稍后重试。')
    command(['systemctl','stop',MANAGER])
    if PENDING.exists():command(['systemctl','start',MANAGER]);raise ValueError('检测到配置事务，已恢复管理服务。')


def backup_database(path,target):
    if not path.exists():return False
    source=sqlite3.connect(str(path),timeout=5);destination=sqlite3.connect(str(target))
    try:source.backup(destination)
    finally:destination.close();source.close()
    os.chmod(str(target),0o600);return True


def restore_database(path,source,metadata):
    temporary=path.with_name(path.name+'.restore-'+secrets.token_hex(4))
    try:
        with source.open('rb') as reader,temporary.open('wb') as writer:
            os.fchmod(writer.fileno(),metadata.st_mode&0o777);os.fchown(writer.fileno(),metadata.st_uid,metadata.st_gid)
            shutil.copyfileobj(reader,writer,1024*1024);writer.flush();os.fsync(writer.fileno())
        os.replace(str(temporary),str(path))
    finally:
        if temporary.exists():temporary.unlink()


def preflight(files):
    required=(APP/'history.py',APP/'collector.py',APP/'manager.py',APP/'traffic.py',APP/'www/index.html',APP/'www/connections.html',APP/'www/manager.html',KEY,PASSWORD,CATALOG,SNIPPET)
    for path in required:
        if not path.is_file() or path.is_symlink():raise ValueError('缺少已安装的管理功能或发现符号链接：'+str(path))
    if PENDING.exists():raise ValueError('用户管理存在未完成事务。')
    if '/node-view/http/api/' in SNIPPET.read_text():raise ValueError('检测到 HTTP 试用仍启用，请先完成此前的回退。')
    command(['nginx','-t']);version=command(['nginx','-V'])
    if '--with-http_auth_request_module' not in version.stdout+version.stderr:raise ValueError('Nginx 缺少 auth_request 模块，未修改网站。')
    domain,port=deployment()
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',domain) or not 1<=port<=65535:raise ValueError('网站域名或端口不正确。')
    auth=module('_auth_upgrade_v8',files['_auth_core']);auth.VERSION=VERSION;auth.APP=APP;auth.ROOT=AUTHROOT;auth.PASSWORD=PASSWORD;auth.KEY=KEY
    auth_module=auth.module_from(files['auth.py']);auth_module.credentials()
    old=json.loads(RELEASE.read_text()) if RELEASE.exists() else {'assets':{}}
    print('当前版本：'+old.get('version','分散升级版')+' → '+VERSION,flush=True)
    return auth,auth_module,domain,port,old


def prepare(files,auth,domain,port,old):
    changes={};web=APP/'www';registry=old.get('assets',{});pages={}
    for path in web.glob('*.html'):
        text=restore_asset_names(path.read_text(),registry)
        if re.search(r'<header\b[^>]*\bmasthead\b',text,re.I):pages[path.name]=text
    for name in ('index.html','connections.html','manager.html'):
        if name not in pages:raise ValueError('缺少分页结构：'+name)
    performance=module('_performance_v8',files['_perf_core'])
    if 'overview-fast.js' not in pages['index.html']:pages['index.html']=performance.patch_html(pages['index.html'],'overview')
    if 'connections-fast.js' not in pages['connections.html']:pages['connections.html']=performance.patch_html(pages['connections.html'],'connections')
    pages['login.html']=files['login.html'];pages['security.html']=auth.security_page(files['security.html'],pages['index.html']);pages['system.html']=auth.security_page(files['system.html'],pages['index.html'])
    links=[('index.html','./','仪表盘'),('connections.html','connections.html','连接记录'),('manager.html','manager.html','用户管理')]
    if 'notifications.html' in pages:links.append(('notifications.html','notifications.html','通知规则'))
    for name,text in list(pages.items()):
        if name=='login.html':continue
        nav='<nav class="site-nav" aria-label="主导航">'+''.join('<a href="'+href+'"'+(' aria-current="page"' if name==page else '')+'>'+title+'</a>' for page,href,title in links)+'</nav>'
        text,count=re.subn(r'<nav\b[^>]*\bclass=["\x27][^"\x27]*\bsite-nav\b[^"\x27]*["\x27][^>]*>.*?</nav>',lambda match:nav,text,count=1,flags=re.S)
        if count!=1:raise ValueError('网页导航结构不兼容：'+name)
        pages[name]=text
    if 'notifications.html' in pages:
        pattern=r'(<p\b[^>]*id="match-preview"[^>]*>.*?</p>)'
        if 'id="preview-rule"' not in pages['notifications.html']:
            pages['notifications.html'],count=re.subn(pattern,r'\1<button class="button" type="button" id="preview-rule">查看最近命中</button><div id="preview-result" aria-live="polite"></div>',pages['notifications.html'],count=1,flags=re.S)
            if count!=1:raise ValueError('通知规则页面结构不兼容。')
    resources={name:files[name].encode() for name in ('panel-core.js','iteration.css','system.js','login.js','auth.css','session.js','fast-core.js','performance.css')}
    resources.update({'overview-fast.js':files['overview-v8.js'].encode(),'connections-fast.js':files['connections-v8.js'].encode(),'query-fast.js':files['query-v8.js'].encode()})
    if 'notifications.html' in pages:resources['notifications.js']=files['notifications-v8.js'].encode()
    for name,text in list(pages.items()):
        if name!='login.html':
            text=re.sub(r'<link\b[^>]*href="iteration\.css[^\"]*"[^>]*>[ \t]*(?:\r?\n)?','',text)
            text=auth.patch_html(text)
            text=re.sub(r'<script\b[^>]*src="panel-core\.js[^\"]*"[^>]*>\s*</script>[ \t]*(?:\r?\n)?','',text)
            text=text.replace('<script src="session.js?v=7.0.0" defer></script>','<script src="session.js?v=7.0.0" defer></script>\n<script src="panel-core.js" defer></script>',1)
            if 'iteration.css' not in text:text=text.replace('</head>','<link rel="stylesheet" href="iteration.css">\n</head>',1)
        pages[name]=text
    if '_experience' in files:
        experience=module('_experience_compile',files['_experience'])
        experience.apply(pages,resources,changes,files,APP)
    registry=version_assets(pages,resources,registry,changes)
    for name,raw in resources.items():changes[web/name]=raw
    for name,text in pages.items():changes[web/name]=text.encode()
    changes[APP/'identities.py']=files['identities.py'].encode();changes[APP/'system_health.py']=files['system_health.py'].encode()
    changes[APP/'history.py']=patch_history((APP/'history.py').read_text(),files['_history']).encode()
    changes[APP/'manager.py']=patch_manager((APP/'manager.py').read_text()).encode()
    changes[APP/'traffic.py']=replace_functions((APP/'traffic.py').read_text(),files['_traffic'],['roster','summaries','run']).encode()
    collector=(APP/'collector.py').read_text()
    if "clock.split('.')[0]" in collector:
        collector=re.sub(r'(?m)^(\s*)timestamp = moment\.timestamp\(\)[ \t]*$',r"\1timestamp = moment.timestamp() + (float('0.'+clock.split('.',1)[1]) if '.' in clock else 0)",collector)
    collector=collector.replace('if signature!=previous or now-last_write>=15:', 'if signature!=previous or now-last_write>=15 or history.archive_pending:')
    changes[APP/'collector.py']=collector.encode()
    changes[APP/'auth.py']=configure_auth_source(files['auth.py']).encode()
    changes[AUTHSETTINGS]=encoded({'origin':'https://'+domain+(':'+str(port) if port!=443 else ''),'version':VERSION})
    changes[AUTHUNIT]=auth.service_unit(grp.getgrgid(PASSWORD.stat().st_gid).gr_name).encode()
    changes[DROPIN]=b'[Service]\nReadWritePaths=/var/lib/loho-node-view/identity\n'
    base=remove_block(SNIPPET.read_text(),ASSET_BEGIN,ASSET_END)
    base=remove_block(base,'# BEGIN LOHO LIGHT TRAFFIC V8','# END LOHO LIGHT TRAFFIC V8')
    base=auth.patch_snippet(base,KEY.read_text().strip())
    base+='# BEGIN LOHO LIGHT TRAFFIC V8\n'+static_json_route('/node-view/api/users-live.json','/var/lib/loho-node-traffic/public/users-live.json',auth)+'# END LOHO LIGHT TRAFFIC V8\n'
    changes[SNIPPET]=(base+asset_routes(registry,auth)).encode()
    certificates=set()
    try:
        config_file=Path(json.loads((PASSWORD.parent/'state.json').read_text())['config'])
        certificates={p.strip('"\x27') for p in re.findall(r'(?m)^\s*ssl_certificate\s+([^;\s]+)\s*;',config_file.read_text()) if '$' not in p}
    except (OSError,ValueError,KeyError):pass
    # Fresh installations already recorded the active certificate; never erase it
    # when an older installer-state file is absent.
    certificate=next(iter(certificates)) if len(certificates)==1 else old.get('certificate')
    changes[RELEASE]=encoded({'version':VERSION,'installed':time.time(),'assets':registry,'certificate':certificate,'features':['sessions','identity-timeline','split-refresh','rule-preview','system-health','filter-memory','versioned-assets']+(['multi-user-rules','theme-switch','unified-header'] if '_experience' in files else [])})
    for path,raw in changes.items():
        if path.suffix=='.py':ast.parse(raw.decode(),feature_version=(3,8))
    return changes


def check_collector(started):
    deadline=time.monotonic()+30
    while True:
        try:
            value=json.loads((DATA/'activity.json').read_text());traffic=json.loads((TRAFFICROOT/'public/users-live.json').read_text())
            if value.get('generated',0)<started or 'account_last_seen' not in value or traffic.get('generated',0)<started:raise ValueError('采集数据尚未更新。')
            if value.get('error') or traffic.get('error'):raise ValueError('采集组件报告异常；未保留此次升级。')
            return
        except (OSError,ValueError) as error:
            if time.monotonic()>=deadline:raise ValueError('等待采集组件超时：'+str(error))
            time.sleep(.4)


def upgrade(check=False):
    files=json.loads(gzip.decompress(base64.b64decode(PAYLOAD)));auth,auth_module,domain,port,old=preflight(files)
    changes=prepare(files,auth,domain,port,old)
    if check:print('兼容性检查通过；未安装升级。');return
    print('本次更新：通知规则多选用户、统一导航状态、深浅主题及账号说明。',flush=True)
    backup=BACKUPS/('iteration-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4));backup.mkdir(parents=True,mode=0o700)
    before={};written=[];paused=False;stopped=[];database_backups=[]
    units=(MANAGER,COLLECTOR,TRAFFIC,AUTH)+((NOTIFY,) if APP/'notify.py' in changes else ())
    active={unit:subprocess.run(['systemctl','is-active','--quiet',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0 for unit in units}
    if not all(active[u] for u in (MANAGER,COLLECTOR,TRAFFIC)):raise ValueError('请先恢复用户管理、连接采集和流量采集服务，再执行升级。')
    enabled=subprocess.run(['systemctl','is-enabled','--quiet',AUTH],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    changes[APP/'upgrade.py']=Path(__file__).read_bytes()
    for n,(path,data) in enumerate(changes.items()):
        if path.is_symlink():raise ValueError('升级目标不能是符号链接：'+str(path))
        meta=path.stat() if path.exists() else None;raw=path.read_bytes() if meta else None;before[path]=(raw,meta)
        if raw is not None:replace(backup/('%03d-%s'%(n,path.name)),raw,mode=0o600)
    map_path=DATA/'identity/accounts.json';map_before=(map_path.read_bytes(),map_path.stat()) if map_path.exists() else None
    try:
        pause_manager();paused=True
        for unit in (COLLECTOR,TRAFFIC)+((NOTIFY,) if NOTIFY in active else ()):command(['systemctl','stop',unit]);stopped.append(unit)
        databases=(DATA/'history.sqlite3',TRAFFICROOT/'traffic.sqlite3')+((NOTIFYROOT/'notify.sqlite3',) if NOTIFY in active else ())
        for path in databases:
            target=backup/(path.name+'.sqlite-backup')
            if backup_database(path,target):database_backups.append((path,target,path.stat()))
        for directory,mode,gid in [(APP/'www/assets',0o755,0),(DATA/'identity',0o750,DATA.stat().st_gid),(AUTHROOT,0o700,0),(AUTHSETTINGS.parent,0o700,0),(DROPIN.parent,0o755,0)]:
            if directory.is_symlink():raise ValueError('部署目录不能是符号链接。')
            directory.mkdir(parents=True,exist_ok=True,mode=mode);os.chmod(str(directory),mode);os.chown(str(directory),0,gid)
        identity=module('identities_v8',files['identities.py']);identity.sync_catalog(json.loads(CATALOG.read_text()),path=map_path)
        for path,raw in changes.items():
            if path==SNIPPET:continue
            if before[path][0] is not None and path.read_bytes()!=before[path][0]:raise ValueError('文件被其他程序修改，停止覆盖：'+str(path))
            replace(path,raw,before[path][1],mode=0o600 if path in (AUTHSETTINGS,RELEASE) else 0o700 if path==APP/'upgrade.py' else 0o644);written.append(path)
        auth_module.initialize();command(['systemctl','daemon-reload']);command(['systemctl','restart',AUTH]);auth.ready(KEY.read_text().strip())
        started=time.time()-1
        for unit in (MANAGER,COLLECTOR,TRAFFIC):command(['systemctl','start',unit])
        if active.get(NOTIFY):
            command(['systemctl','start',NOTIFY])
            deadline=time.monotonic()+30
            while True:
                try:
                    value=socket_json('/run/loho-node-notify/api.sock','/state')
                    if value.get('health',{}).get('reader'):raise ValueError('通知采集尚未就绪：'+str(value['health']['reader']))
                    if any(not isinstance(r.get('accounts'),list) or not r['accounts'] for r in value.get('rules',[])):raise ValueError('通知规则迁移未完成。')
                    break
                except (OSError,ValueError,http.client.HTTPException):
                    if time.monotonic()>=deadline:raise
                    time.sleep(.4)
        check_collector(started)
        replace(SNIPPET,changes[SNIPPET],before[SNIPPET][1]);written.append(SNIPPET)
        command(['nginx','-t']);command(['systemctl','reload','nginx']);auth.verify(domain,port,auth_module,changes[SNIPPET].decode());command(['systemctl','enable',AUTH])
        if socket_json('/run/loho-node-manager/api.sock','/users').get('busy'):raise ValueError('升级期间有新用户管理任务，请核对状态。')
    except BaseException:
        print('升级检查失败，正在恢复本次文件与采集数据库……',file=sys.stderr,flush=True)
        for unit in (MANAGER,COLLECTOR,TRAFFIC)+((NOTIFY,) if NOTIFY in active else ()):subprocess.run(['systemctl','stop',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for path in reversed(written):
            raw,meta=before[path]
            if raw is None:
                if path.exists():path.unlink()
            else:replace(path,raw,meta)
        for path,target,meta in database_backups:
            for suffix in ('-wal','-shm'):
                side=Path(str(path)+suffix)
                if side.exists():side.unlink()
            restore_database(path,target,meta)
        if map_before:replace(map_path,*map_before)
        elif map_path.exists():map_path.unlink()
        command(['systemctl','daemon-reload'])
        for unit in units:
            if active[unit]:command(['systemctl','restart',unit])
            elif unit==AUTH:subprocess.run(['systemctl','stop',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if not enabled:subprocess.run(['systemctl','disable',AUTH],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        command(['nginx','-t']);command(['systemctl','reload','nginx']);raise
    print('升级完成：v'+VERSION+'。刷新原网站即可，仍使用原网站账号密码。')
    print('右上角可切换深浅主题；通知规则可多选用户，每人独立计算冷却与无连接间隔。')
    print('后续环境检查：sudo python3 /opt/loho-node-view/upgrade.py --check')
    print('未修改代理配置、证书、用户 UUID 或通知设置；未重启 Xray。')


def main():
    if os.geteuid()!=0:raise SystemExit('请使用 sudo python3 运行。')
    if sys.version_info<(3,9):raise SystemExit('统一升级需要 Python 3.9+。')
    if sys.argv[1:] not in ([],['--check']):raise SystemExit('用法：sudo python3 本文件 [--check]')
    os.umask(0o077);print('LoHo 统一网站升级 v'+VERSION,flush=True)
    fd=os.open('/run/loho-node-install.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('另一个升级正在运行。')
        try:upgrade(bool(sys.argv[1:]))
        except KeyboardInterrupt:raise SystemExit('已中止，并尝试恢复本次变更。')
        except subprocess.CalledProcessError as error:raise SystemExit('系统检查失败：'+(error.stderr or error.stdout or '命令失败。'))
        except (OSError,ValueError,KeyError,StopIteration,http.client.HTTPException,sqlite3.Error) as error:raise SystemExit('统一升级未完成：'+str(error))

PAYLOAD=(
    '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'
)
if __name__=="__main__":main()
