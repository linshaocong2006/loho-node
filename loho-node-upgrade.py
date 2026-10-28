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

VERSION='9.1.3'
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
    print('在“系统状态 → 外观”切换深浅主题；通知规则可多选用户，每人独立计算冷却与无连接间隔。')
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
    'H4sIAAAAAAACA9S9e3sb1bUw/lWGUDqaeCTLzhXZ49RNDPjXJM4bO1yOraNnLI1tNbKkaqQ4ruv3CZRLuITQcishFEKhcChNKG2B'
    'JqQ8z/tReiLZ+atf4V2XvWf2nossJ7Tn/Z0eYs3Mvq699lprr70uG3tKbqe9Uio3Wt6egrHnwQeGO35reLFaH/bq54zmenulUd+3'
    'UF/Yg/+brvttt1Yz1rxFv9r2DN/z/Wqj7htrVSjXaRvlFbe+XK0vG81W4/y6UW7Ul6rLnZbbhlJGo2W0PGig1cYST7bc9Rw3u1Cv'
    'rjYbrbbh+u3g96Lrewf3B49L5Xq7Fjwt/7zaDB9a4e8V11+pVRfD53a7mSvXql49bPmnfqMePDT8hfpSq7FqNN021jTE+1PwGBRq'
    'ecFP3yu3vLYfPjfKZ72wbd8PB+l3FgEMZYBR+Go9/N2urobNttebHnyqeEsAtNVVt17JuK1l3yos1A34P+iy06orLeZanTqVsAHm'
    'XvmsM9fqeLbhtyuwDI5S7tT0qSl677Va8fdt73xbVMXhYN19eQuWm0bS8po1t+xlEDS2UXHbrm2sem0XfzknG3WotdqoeE6+cXD/'
    'fjnUtoczclvrhkMwzSFulOruKreTw1/GkGHm6o12dWk9a8KDAGqu3Tjr1Usr3vnMAcsSzbXWRcP4f9hW2EOu0fTqGXNt0bQAdWCS'
    'Lc9dVUrj/zX83FJ5BYaZ4c+5pWrNqzcyVjiVnN8u4TyMHxr5xqFDh4zqUvDN8Gq+R7O09HaVMpEew14ba/X+vXaqFf3FcrUS6UfU'
    'X2vBfstgseTvS7WOv5Kx4nP31+vl6CCUYlBErjIUygSgtRBnWrRisvhStQ57X10NgEG4Ft75qt/2M1YEGmGBTr1WrZ/NBNhVgX4b'
    '66uwM4NKdVwFh/Zexhz22uXhWmOlkcXXWdgT7rLXGsaHHG5hU4wLRkHvEgZwzq11sEEsnqs13IqfoaIAjUoJcV+DhdhkVGnerDRW'
    '3WrdLNpGFUYoXuJeNYsBanqwA+iLD50A8co13ZbvZTKTp04Zw4bJQy51fK/l55rrpqV1bBsbmwKyQBlheVehJ2o0t9ioROBc9atI'
    'e+uwTljSpt4mgfou16MQx9Zok0FrWDYHFHcZdlcCmuoNYyVu+CT8gi1Vr1BDuWoF28qYx2ZOTE6fNG3DPDVzes60EloMoO7Pi6pF'
    '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'
    'LVfr2cVGG5hJYfRA8/ym7Ab92DeaboWCjY2OQmH0I0f/n3ql8ODIodFDo/kx2KMVr1UYaZ43YItVK4AarUw2S3lDxcdsy61UO35h'
    'ZD/2B52tVSvtlUJe62jCB3Joq28MfxU2TjDJxVqjfHZsCfZGFgMBFEagS9gqNWAr3OVqh7kc9Jxd8XC7FEZyhzf1JmFj1ZdT2xw9'
    'DG3S4xo3cCCf1/pYbnle3RJAIwAa+TFUQS/VgK2vtQCqbn19bcVreUHHQjfrB6DMG6MACWNEhbUoNMiSwgIaIyN5bOFh+JeX8iD8'
    'kO2PHKZhCeC3G82U1XGBptQpM6VfQGW511LBOxofnQRfBELxYrRwCWujNI8jR81HlkZRaGFjmznB5UXdB8v7vAPugc0cVZcvF/e5'
    '+w97mw+K7NFZpoIbYro1b6ldGA3m++CBfYf27y+PhbDHiUXRSMeZh2F9z8vH0VEcqVzigttpN6JdF2CF2uFurDfqsdFJyMl57a94'
    'i/vG4nCExcjW3XMbAUqdpx5pQLxrRvL5h8KChgucwzuf9VeAFp7FLcXXI1nMhrkRAbeE3yH34cOLmz9a9SpVNxO2fCgPhayNKJHp'
    'S1dGk+jKw2k0ZOQQIu2+yGcNq3gb7ovjnrp9ENuTt0/6jiEw4ugOJ+E1ISxV5jqFkeHsiIqfiFYa2IM54UY8HEMp5DpIw7mj3Ep7'
    'tYYUfPyBSqOMzMDANxPj+C/IrPVlZ2HPz1eyR08u7IGXwKEmxslepbyCbLoNX0mLi1/pPQqY8BIxDDnNwh5ySUUBcGEPraZT8XBm'
    'vLQ2eiBW3VrWL7s1zxmxZb3sUhXzrGDWT2i5XW3XvImtP9+Co/LWS1/1Ljxt/J+vDeSJBjLF8WEuME6WbS1gkAt7qtAtdL7S8pbE'
    'U84/t4yNKYX89nrN81c8rx0W5XfI2AYoDBCvLgeccIAKGufE8uxWaPitMtbgNfkpfEHzOa81MT7MBeAHA3+xUVk3yJ8TynP4ilWA'
    'bpYifQrkwd+0IC5593NZGEithm8fyGaNx6Ym8d42m4UBCPdYWa6KIh6NDNhe8NZb9xZBkIT3s0/Nzk2dMIaxjeNzj8EAoRygxoi2'
    'PjDckYnx5kTv/Y+3rr4sVQBPi6uxX7+y/ddnezdf6176YOuNT//7wjOsQdi6+fvejcuobdr65Ne9d//Se+uLf377Sve1V7uXv7h7'
    '7a933/uwd+WZ7tNXOa85VBsfbiKEeAYT45XqObRjlHDMylPWnhBgwRsQYz02tWp3ENwr1UrFgzaGoZE4UHTKA8XVboK36liiLSBi'
    'MzLjKPW3WVxa2LLiKwBzdIIhdeebVxl2AM9RgKfWL8cMr0AtVlZu37jVvfwWw4SaWeyA6BQOgR8jgxepP6ARhqpcPi4sW4pCVpI+'
    'BbSxL7LuTjAx0DzPbeGhLys1w9Cu26q6WdJj1LzKIprFKeVovyvg4sElFOh+9Nb2J88I6E10n77c/fj2ne/e6/7xN6hCp3zRvb9e'
    '3v7kYvedTwOEokbZNEAOFQ5nq8HwYHRLcOgK36ImAJfhN3/f+ujmnW9u3f3wN+P8lgeml6MLuCy/EyeqifEGRUwS56CFPRW3dRZb'
    '/PpP2y9+OT7MX2Ol6OiFxf76nFZsmLuCHzQLdQ1cvwpUM9jW54Ef1YkgxbY8BrfKMhmdqMptLjBQWlAQN0cwX3ph6+YnAMzt6190'
    'b7/Ze+XF7qvvc/5tqRZ8V5R57dWtKyLzNxZ792b3+pUwwET3uT9vP/0GrARrTTkYJucNB+pgHIyQhuu/7b39FTfGysbupTe3Xv48'
    'QiBozhPjS3CQAnST8+NHOe2JkzPHpozHp6eeANJ2fOaxGTlj+rd39fOtP7yzdf3a1mvPd196f/uXt2ESvFVEpvJbv9u+9mnvxZd7'
    'V29C4Ts3b/Ke7L1+6c7tq7KtYe4TfiBphj9IzZG0I8vV2DKrXSI6Fj6pB4fGHziwFhPAtjurwittqkbM4Mfr0xhdxLKBDWXa7rKN'
    '4oJdrvlYnetiCCMnqFqGo2fbE7WxhkXHS6gFJ8LgQGhhpRy+PSpYOv4m3Qw0TR8JtidRBIBXgb4BvoxtjgXjpmS+dWdCHD3reOqs'
    'd1YXvZZ5xPzHhTfMQn3CGckfAvl45PDo/iOZ+v9l7t2/26quhdF/RS6ttzbeVvxOIkX2hZCcZhQIh4TT71zHhyFLcqxGlnwkOYmP'
    '7TFMS14lr7ZASggNaQO0BZLQB4QkkDHu/U84lmz/1H/hztd67i3bAXrP+b4eYu31XmuuueZ77jK/wkyrfrByulxKD2LC+X95NshS'
    '+cie0d1jXuELzwZmVMSVOGptAiU4z6GAqvY0ZgbHRs9jnKbyEeLb0wHRO0G0hDZNg0PZGVIZoxDu/8ZYfZ6R7UqYDWzBeJBDqRfK'
    'cFDHTW0d8RgSlmlCTyxeiEpANlWqoX0w5Wo6KDRalWK1HER6/6PAok2DMEc7LrbN2AJhLIioazx6dHZG8jUQMQZ/QnoyUGOG7hHB'
    'NAuo1LalNZIYCuVDsqblZQ03/GiKGC2n1txqLJRzP0wH7gsThBiBjsM6UQ1e73aSNdp3TBvZOsrGEHQJ4hLAaHAIz5IkbUasBAPP'
    'wx/lfOFUodJKkSUYbO1Ca3ZXYb6yi2cIJ22CwjWzaDNd7q8D31eB7SQFRDao1ftJJhNELJjLxmR8KyEdPO11DmfASJoHVvMgyXda'
    'S4BYOIfrViYvFgnFFIANWqRpzK3AUfToDusnQr8Xln2RyG95OWCqgLts3/5s428fcDeWSBIoojw3KpYbGJEIw4BPvID2H8DqQY/p'
    'WGmGQ6o3+1VaC7GNC3dRbLOQJW00hoEEIZTIBokMI/bPVqqlRrmWpksRMH5dv3AeMCiG2Ar6uHeVOwAtgtbur8pDcgbtgoIwkrZ/'
    'eG393d+2L/xp7at38JD+S4LYINCdmMgACMINEI0cU5vtB79JBX12TXiW0caStcih6pn3vn32DBANTs9z5bl6Y3Eio+N/wgCd1Ydc'
    '0+1aVSXPKt3zxt3X1r78sH3+XufGzSDCU8iTXHUiQMuxu2fb5z8OsvB538BE0P7iLxuPz2FF/ALIrX37T3KznVOjEDBdjwtx1bX3'
    'O399i3vvnH+bwYNnokJPO8ChCDqBEPUzM1eYT8sP/aoAHG6Hu5Q9IKCe+ikHewmykgpkeSQ4i/BajypAS5VyifaofefCxh/OYD4T'
    'LqL4f7CJbBRZhueE9alr9z9NqFSp6Wqwu8BXdD79vVctcgft7e0+klIDZQOSCQUOvg1coFC9MFiEBg/DnuRWZPuBxEzLEbAnXX0m'
    'JWdAv5vht9h1gjJuLxn5SnzRWVRCf5IYNe1d7H63USg3vNshcmU8w76Ar1tgH6aUE46aCJjF4WBPcABcpuP3wkkze/P4q42/f84B'
    '0+kSmNty/ecI1TwMloyPwY3h3xtf/6Jz7SM4FaHHiHR0J7C8rJrwyWX1WbpvJt4sfxfCnL4Q82gmhqkfcis+3pMqCYgvk9E3KrTe'
    'TOkfGtjkVsCzX3twGQDJuugG+wJe+H++SAlfQzvV/voMby80UcJ+boXhF1FsTv8ixstbdJiFu2L1fILHQRdI2MJkkQkTjGF9iU4V'
    'GjXk7/KTDjZhvWgEm5G2ak9kKs3mQrm5vIyBE9HEDq2Qnq0D516oyaDwFlotlN7HQK1VaL6OozuEmgqfWwBcCLAS7dc+aD/4ovOH'
    '1c7NDzY+PwMYd3C0ff7s5q9vArYR2An9Z02ECt5h6f5/Vq/U0gEwQIF9wqYRE1H5Ht2gWq4db80mrS/8HvGsvD4b5/7c/uUfgXPC'
    'p8W+ovbOAaSWTxWqExlRXgKgNBeKaGUSIB5e3Xj8q7WHD5FaOX8V2CO8bST34Y1cf/ibzu9u+AjR2mkFzYmHhXcs8SrJmsyNW7E0'
    'ouHSdufD9QhF4XqI8CT6CV+Nu190Pvs5AACABFJLnIQsIx3hLYMHgxnbtfu/7HwiFjBYdasjZmZgRfSuS3ByhYaiaonMDXMu37Al'
    'ES29oYoV/i+par1WrFaKJ/LyJafp9kKpdACzOD9PcZHhUgUnK83KdKUKLH0RGRsAKaUV7olT+8IT4EtlfqAyFDU1cAPT8jVCDzei'
    'yVfCtNIdAhqp1LqJnY/V/ncJnu2elcCmWm84nT81ODA4PViw5NTvPGx/9da2Euok8TByJigZnjiZ350ZyAzEJMS8ez+zKnQRFMNO'
    'OqJiapgsGeaiamER4JAG9MR0XE6BjrG44BVMo4eBWYI535hESTpanJuuY+GLLFv5ZvX3Wj5iiVy0PGYfoYzx6cXU8/Uf16EO/dRN'
    'Cq5Elcco1ucXuwxvJNn/fviVl1MvHjj608Mv/yRKPXM09UzqX55/5sX9ByyZdvvd33XeQ1QG//vHo4v7phvjyIGcP9e+9xWSE5cv'
    'dq7cIkGTyLvJXO6/V19jTmPt/mUJFPs3QI9/lB6gIXSydh/p18333uncP9N559769dfXHv5549YfgY0BepURqCuSjK8TsyM0RHzH'
    'ElNBNQCumLcYtqACrSvf8z8iJE84WwSSfi1Zs2VqL7186N8wWsn+wy8eOfz8AXPk3aTDCmJr5aovNpdDBiZq20P+6YHn9x9+4UDq'
    '2Wf2/0Sf65Dc0c7lj9rnP29fuafE695qFqaVHHntq8fIBF6+idBw/fX1r36F8kA62vbVS+1fX2zfPbv+/mvqvLS8nDuKKyLwovfv'
    'VBuBoSns7vA36ZocEbWUVcrVkhZQo7kdoi+orSe7r1KbXxDBtClXWM76gopJlfbDLQFmgYkU+Dw8hPMv/+dChdIcYEJ1eDCaKaJz'
    'Z+vVUhknsvH1b4Bh97aNdAQiod5+LfNQBM8jHjlvtnvwqli1GrfXadqqdVpf8Plxv3grF1uofquGvQNDo2P2FnRfOE8bpyaaGVYO'
    '1I8fr5btzmNL4ipmqlqTY5Qk8JXVD2oQLptvUDpZFHdTygelpLAUPHIPE8+hAU8eMgT9/HZ6u6pKzcTI72m6fhoXgX8Crc3YHKNk'
    'f3Wl8+ntjTtft2+fQx1f+/afBLWvPTyD/B1Qol98hsoBqgKEVfuNrzdXL8QwvszU3kN9X+cqLf+WzTcqorSTSapqPhJIMUAlotFv'
    'zl7T4+udw3sYxxqI/UQtgjwSrQHoSMYga/f/hn8/ur5x9z00lb6xuvnW49TgUKp97woTm/RAXLm79vCDzev4CXbmm9UbPFXemW9W'
    '34MHAqnq9262z3y+9vBt/o54iaq5r4an9rDQar+nAkG1R8q8u/ZrvP7Ra2sPHmzcebx57Q5SNj8+evSlI3G1hkbm3RUcioJJ0G8w'
    'czO9eKi0nWYDKmsZdancbFVYh5UOjXYEo8WTSPmVl58/UsYMlS8VGoW5ZhpzMpAGvklfJUg/Vg/QnHCXye5A2gPfRlG6TGODSPfF'
    '4mJSmzALyh+As/CqKBYV3b847yUF7fsppWS1xw57e3t2VpXE2Un1Ma2U1DZUWRCGtqmkrt2nOHLcFPmBSSe0teKxmk7r6O3RinUa'
    '8qaR8ihcwrNMB84j6LFhpEJKrqYYY6oCY3AtD11abE7aqLfQBTnP9a2KdP+B19OfcslVsPVEgMMCJ+tXjo9vL4ebbl6/unHtCrLB'
    'hGy7NwVG5pkWwP80OcEbbB5EdkeM0nV38jN8kl7lHQgi0XFh92iJCqeXpK6yDwSxHLLbMZ6R8WgQsc6ojIWsnqQ/M2hnBv8+J5HW'
    '6W7IOIkaI3tM6dpXGSngClgI831rkEaVBgnvPFmi70SJRDMOoiXgFWfrpWzw0uEjR4PoCTRKyK9hZuylQKCo/yhAIRx3YX6+WmHs'
    'sYtcJlYixKdZ38hYUWZZ3kL1E7aPrzHUnIP1KQjJxqCeFZHqOZdy9RPK5S1fCbfSfuGGfQ8aMH6TY7oveCw37n7B3neb5y5t3H1L'
    'tGE47JNpxHgI1oV16xZ7Td6mfBDkNE5XDr7OG0StXWGUAtwdypx4ViKGZCcye3qeOIoN7HcsVNr6nmmpEp2oD+uYjOIJlKUr4tWg'
    'bxBJlOyT2m4nV5SfAyH3FSVEUnISJCKyaCDP9qv9ZGVfzqL1TK6/n8g/xM7Zp8oj5VJpRn0jOX/2qb1jhcLekvpIFtTZp4pj5T2F'
    'Peoj2t9mnxoqjgwM717JGCHKkhhaD7iW5ygJ2uPYZes5iI3xTGGuUl3Mkqgs6scbXu5nwV30LAqHXigUj9DPg3UMEvKDI+Xj9XLq'
    'lUPHfgA/XqhgwN76TCv174UflyvwrVmoNVEoWpmxZ5d6egmIcLT5pGzcbH8MX5w6hSWyIjUiBLIPlslXarPQaUs1YOHQEprIK2Pk'
    'gYHSydncTqzCMwNjaBg+01C9kZRgSaW8yzbKUL9ysmxsmfnl152jIXGOrIl1Ro0s927vPtryF6pwjPAvmrGUq9XKPMa3wowkaJwc'
    'PTU8MDIzPJQaiFoN2Lf5AsWhHt3zozBCp4E92mmgv8FrNMbaBAEj2lh7BA3ZB4d+lBomW19LELbkTJos4NG5IMGiHXAl/NMP0yiS'
    'G0PGM/8e9iz+x0YHnJG280JAw2pvjEHjVoHG96PeCCOeT4G5K6E+OhLeuc4AdHH7RSCqVseW2yNo/S8QQ3/HnDKeGh3aPTiyN8kh'
    'Q6zCS4WZscJu557BORaGh6zFDg/GF9uPfcSAzF0I+XYYSCxMw6RgvTk+/73oCWDvE9uAWxbrM8PF0T1Ff2R9TiiGTIDz/+qv1Erl'
    '09lB5a6B8obUgIau3Wi5DTA7YgGXSLSWtj7goYzxPXmqMFYcKkzbc0nNDlodAEc4N59GCI6GM6MnT0Wj8KfvpjIS29bBBE8UWQd5'
    'jzirT80veSCtJjc4vbsw4gwGk3cwhLCnVgd7Tfu9A4C7B2MAnrG2zGC2hBMWt6KxUdrEGVjq0I/k2PEvtX48glz8Sg94V5qQE7Bb'
    'uTpOpLWYzQwPxaeRqpBPBJy7C+0wRmqUjtx5TnBvCg2D0J6a3lMq7ilHT42N7BkZHRkcDNU0h9HzIj5atgb3t4iq5vRwLVySyrtH'
    't6s8YiqPDm1XedRU3rttz0O1vkFdfcRUJ3HE0g7wfSIC3uErgG8A4P6nBkvDA0NjQ0POKzA2Sq8APeAagNFFxDi3WL4uw3hF7dtr'
    'N0nNDlkwO5xwTfsze7pfosG9fPuHDCQrQfRSF+zseUf5nmaeK92w1TMJS3ey8RrsvaGM39BIcdT32Rv0BkqR8HDJ8rTLWdurwGKP'
    '7883NjQyOJPwdAwPjuwenvYu017L3wxxqDvlEc81zyLPgMAhao+8pOKzzmYtwa5ylxrbs7u8eyDmnBZvTDJx5QmmUODuYmEPYiL0'
    'kiqgp+UAooAU+u49tXemOLa7PAhYxBVsJzwn9un51WXHZUOErhkbmLcrsgCh6yu4W17BPeYpHx4yT7mDsqwbpRF9oTi8t0xbpKjK'
    'HKcMyFLoF6AVrOOimXlS56Ud4l8Sj/nwqV4mcohMDVtvd7FQ2lvc480lNrgCWelGsHShiGSOOsjiQGlmb1GB8qjZJ/YE83vcAd0m'
    'He+e2bt7z26bZNuNHdpibRs/Ofv0swVgpGYWNV2GuKfcP11unQI2J4keta9ccbg8XBhLuHJSIPMbLAImHdjiAuIGpNC5bkvCFslO'
    '7xTsJWZnkTFYsudXGizPTI951RQPu6Qf4dFR1THKIPS9FAH9krWvQx6esA+hvHdsz6jviem8XIpSsTrEO/YkB0JgPWpGHZvZs3f3'
    '3pxL+8TJedkCEQUY3z8EVJuILpeKQ8DqOkT02NB0kpv0U2PAaQz7FPmeOOp3d2S3j/wH/NlNMls35fqjKqf6/ulCI+GmDyUzT/5m'
    'agJMPaJEiaasHdgzArd92j9jakfe0fgfZzKpQuT8ZLXPkovIxHF1ujRccHaXWGmNITXVQu7PHvZL4sLdiegfoohUcMb4mcmP+EyT'
    '7oO3QL5ZScuUO6egB9jm0l7TuFmGmaD7DwkSMvon+Q5qIBxmbDnEfq1OHc2IWOSPx805BxlrPx+ZLyXJzViR0+kKons18zBY2L13'
    't9WpOOC58LcDsOtyh12oSrwY/tAu0Uhe83EnbNUGM94+QYyFRF9o3NgVexPpqXWDKsQproGR4eE9PrM+nESwlb2ACu5AqdnhJY8s'
    '8+HheziLoW5nQZTW4J6EHUhllLZ/ulA6XtZ+8SwV9LGHJzyJb9jI3rHhkUEHAezRO6s2cCxpHg7jbHH1IrSMBwagdY16t0U6E9xl'
    'vU+D7qAIUakMhwvwvc0t73/X2z/nBQPYwvu/q0+/I190aCJPWqgAEz3sMRhIygJRwYSaEpGbFpffWQJMV5i2lEzCi7hLMX0WBbw7'
    'doqDg66YyJJEsBzIRYua3xvoLqEZ2h3DYcOjGjOOcT9JHJaLSVyBTJQgY4niIhP3kbY66CqQ2pNAoWR2z592mXxnG/ggVfQVWWSx'
    'UC2mWb6c6k8NEaA4LLm+w1tz3ti53qwBRzKFYV28dSStV/PdHgYf8xaVQP9RVAfFduGHYVeKuCeJ0UkMeWORRwapOXhoJUYg0OWb'
    'LjQrTb6o8Xgm7lvSNTpF9xdicO8Wz7qG3oSHLmMRUglhKXglO3KAZbVToXoQtVR5jlbE0d4z0/AjzV/YL0+FIaPoYxiLbT+FaB6I'
    'SLeJ2jHlsWa5i7LCb1w7jNLv78dp1IxUZtMWx1NXvGj/ef66aoCY1678Fffd1RYeBPcY3M1X3dlmIcbeZIIMcYI+CvtcfuXlQ/vr'
    'c/OwJTBx3YO2QfFtc1i3aZ9snqwN0G8G7orl0Owp6R3Q0LVzxphnoVHVpjziFoNVJgemyLuBDjVYXpZvOv4sVIMWE/I5K/9moDdj'
    'C4SG0HQgWtHJdp3Q8cjAYG8vVN7CPAhLtzcOCuUUtHWOCbxGQ/KZed68hfkKhwBHK4LQsnD4/kwoBnbshOsbMKF6Oeij6X0f7rjk'
    'TITrnPAMM3ZubREF/6d//5GXD/YfxVjnQValfy02GzPJlhi0ryvZpZVQOfH9DxhEPJkxBAcosB2DbeuwXNyvBrrvasUQG0scbm2r'
    'BnuudnDCLd1ilJeLCdE3Wz/1gmNqloQnPcPBQL9F2sjMcmaPV+Y3VdfNJeNT+urbqsnF0mmUBMkr5yrxRly/dWfjzm0+4W9WP4T/'
    'QZH3lgdhxPYBpo8C1LI9xaEKP85Nr45lNAp1AGVgMgFdhd9hqLe5uto+94Brs+2LCg6FLh2BFSkqyMlAqkg2VAp5BDaiU92rj64l'
    'l94hmMqCmRLHJWg6Dm0SB7ofa8Iq4LIdPw7IyOyoeJUntZHKARo/YeBEM1DlZNdBmKazTcwKJwuAiLu8vy8eSQezrdZ8dteuU6dO'
    'ZU4NAyo/vgujEexqnkRHO/wvdWY8eid1dMkpdOs9PP2zcrGVgc4oBPYSYsVn66ezAbKpQyPwvyCaqVSriPhqiPQA150ANCggsh85'
    'QxyIPjODBQhtMDNmPiIXUSzMw2di0t0CdFG0SiwLaPiK5wXDowIDDzAbEGkDJEzI2+KaG5qwmWb7EOV+280rVhro4Ygqrh1uXhH2'
    'DfMlFRezwZ4gamSD4cwoThensf1kAbMsoIqn+W1njE8YzFf345lj4ga/MApsTWF3ajfpewZTgyOpAWgi+ymOjTjdSPeCtAvBsirm'
    'ymKuyCXdrUmDjb992L7yxcaVG+1Lb/3j0XUbe/z36ms2osCQUy4+UN0TK5R3ugpydFfUnHTyJ7FoEmQhmCfM4e1SdaVXvpgesJSt'
    'u0YpA46UOdhyOuDizBxQ8PgnYsdqvdUd5XP9frnfAdFl2CLE/6i54LzCHKWBgmJuEspIThULbdGjsAVCsQTfQagsVzOVUj72sOhC'
    '9/zQJ4iuKdKOVhfy1BAajc92Bn2ktWUgzHcLp08ymw4iZbmLvsZ4QJiqpbe3h/5GugoTTaXZphcg7jjmftb1lL3gdkPBTaMUrfZg'
    '3CWmvAQq4kATsFMZ49aqrtFq0h1FASLhIiSZeFh5YJQZODMIyBkkvjxIhyGJpAIcMzVGXCB0yb+QUA64NXB0K8w3GAQEU7YQD/xq'
    'pt3IwGEII8B3h36nIMFh6MUQbpTn6ifLFEYY4xlren6H3FWgWijyzCaMPDNRfzvk6JI4BdmOJTGuVTyy8hxQv3OaeU6bXVectrWV'
    'aIucM0w38tKZWv0U2oQLxnDjHGBmAe1EG6go3Bk0bLZaMDpS9PGFzlvnOzdubpz7q3InV81U7BMPkdm9ctwEF7Mlb5faAQkMLo7N'
    'qZibiAVWuFH6aqCYCc+L7kcaKeX9C8DKzPHvAM8YjzgrYALgxzRRdql+IosAvLISejT6CgdNt4Dg+xmNFkzDJdPpjuBEUegeIKF0'
    'KW2zmvbuEGzIGwEA0hWHi+O2YHAXVNIqSJAiTNktry8IAyvCFKAw+03pyhfgdMX6W0daUJFv1AAY+Ib/ZlmUVdUiv5MiM7iSNKRl'
    'CRB1o9nhQJ5MGYxeK/miA7pjGxPQwWUrOPKHUPxR4KgydFwfKyAExyHTvcxrpgHYtc1rfwNKQe6SmgRRQqUwdNtwCAjl4+y1aWJg'
    'aq8BxzzqMoi6r6GJ8dHjbYJcNWYxEjgado7rfCq8FXyRojDHf3g8inyMPyFSEH9CLAhulE8CIQ13p1LKyjwlU3iltEKEoMbIhNlo'
    'bht3PxCvPeWyF6jXh+/MzvG6P0eN162D5jrQqRVoiHh7Tp1A9yMpOA1lU5c7NYF/Zyfdk+fQO4R9/fU4EK9VtEE4FW5x13kz++tA'
    '3TWatmNCj3sLm/W5sr6GPnjkBBmJmJj6yu90SLxz/HfI/ySAhRTsACxUt0RGuKBguW4CWDDRjUEtrTu89vi9zsXXvjuI+PO1n355'
    '603+iSWzAQItajgv6UT3AekERGwbJwaJfjMRRgw50K9phPExkiT+c+bGZKORJWGKoYVqeb8QPZaY05x4QiSU3t7EmQ+RG5kBBkVa'
    '7viwVrwJ5VZUIJVYiRNZxRHG/K+P6f0dQquID/n/xhDgEyfzwypwy3eN9WJUYdtGe9kmLHiS2c63DhDuqvy2iBT+zH5KZZ7alXru'
    'wL8d2n/giB0rnEI9rn31vnZa50N144YLKiSyHCryM8PVAVGyfzw/pygWP3uJfexNCAFyswe8KUiW4hF33v+yc+mODvq89vhW57W7'
    'HA6Rgo1fXLu/qv3psdurF03lR9fb5ylwIjnibhWB3JM0f9vQH07PsROw4oZTyHCzPRLLgOZik88YymD1Te24j1FPtowS7jxiCH7y'
    'gozzc8VvWOfCKoytDrBb5HCbxrbjhnvfWWxVOUkhOeqYAA2XNx+vb9EUXgh0N1gCitWtcAjmsLbtk4NWaHDiYA32kyxhLP7x6F07'
    'rkNCUIfO/TOb79zWcJTaX6+fqJT/e/W1tftIM/Nm4k/qYOP3H3d+fXvzzdXO+bfXHt/pvPmlHT+kffUyAj+Q3B++tnnuEsZsd6I+'
    'fLcw15u3/r728IxEYuVwLeq+4WX46rP2bzDYNZZSGJe1+5c5YSZrep4g0nWtLtFJMZVe/8k9W8WE+OEOAkKgqo3DcpKO33F6jcql'
    'SkvxqpGKMv9cBXORK593pazkJEzKnMBJvJQxuc3IqEDinB0l7Sh1LV8OAg+9AH/kByJJt7LfS52lxiuVEdYbi/klpNPRsCJY//QC'
    'RW1EPRmcUvvKrzZXX8PAqFKu8r/qz60s8hWdK1fXHtxO1Vozi0E0AxMol+A71WL1HmpPMbMaF3zxF7gpnc/PByt2MA08lKJSqf0Q'
    'JQQtYlXjai+70Nd+6e6mF5qLacmipQ6EhRYqUEao015lVN43LIvtWhj7onO/rSTLip+pVomzp6dZUg0wbopSTzWAiOpHT6H68ZRi'
    'DTMz9caBApBowlaO+4yVqApggbEw2TAykKFauS5q6X+Ojt0Eupaead/yydVJ3i2T4X8TCHKqq8AjWgJ+p8yyJqs5dwi8f8IYK0oC'
    'Jtmft1D7061fdLX9vk6/u/q/q7ZfZT78/rX+hFK20Pljijc6coRPJLOkhfq54hoEpLrZhcR155Ju4Fe/lYAFKgK01qPbqF+FJf0e'
    'LA42V6+v3/wAgw5c+YNrcXAew/6kgj5vAX3BPx5dsGMWSA9ks/CdrRK4Y8lOQgrzf4adQiz6gj0gT4fjmD6h5YK5fnKNWOC/1QWk'
    '+2RjUUCKCbZh/yssw6xZFjhBKM1ytlArwe2MShhLtMFPbExoZ+R11Eb96ktzq4kgxX/ALUXZ5U4EdzKumiOX5tz4RGhw2UzjAxCq'
    'HJDPNBqFxUylSf9S0URG58mewN/6Z9YpnZi0C6eyk1POYJyGpVzCzLHNtDfcTKM+l97i6eI3ihLmihOhhGEJwoh+58fpn4x5lryB'
    '4a/9FM21lFabT/PMe/NSEYfhSacxdetkkTsJqy6gXgZL+4LU2oMHQU7e57Q7kQYQLeXGEdWhbLtRtstE6Ek8UsZ8u9bxhBFnnqfS'
    'FzDePGFWziGMYnj8Kz8+SdmaUZdPf5DKeyq0lLWooctPAp7m7lhjF6KF2CTF3+Y56AjTqJEc75G6s4UmKebCqRxH2TWnkhzOGzun'
    'yVE3FgGg0wTkva4jDuCHF0LU9ZbSQA/Xz8l94HrojiYCuBqphZr+oET8YnxEQILd0l8o2SR44RukQvUF8pXQJA+nlOU5C77yMFP5'
    'LWCooUgvRMoxjwKQMSYvQl5sZE2iBJol105QS0sEYh+IUYU4bRkbYHMrYLXsKkZzk8ywHNWf+JS32+ffBw4MnjB4zjY++hDoYniz'
    'AqNKo65Jbsx7GL9BXqAzBdnMSjg6rmaeAVXxGbTaH/LC0FqcSv3AYZlyjajMiWDjo9fb59/hNxSXcJWirGf975wLLVCdY2RsCtbE'
    'x9XM8Ae81K36fKVoldBvLJAhTYQnMw0sVtZYdrF8U6O2iEiyQiPhMvEZ7FclqqmOaE0FXfaAyl6FfaOUDJ0bF9Ye3l6/cBEY7PW3'
    '3ln/0wMMjKjTzHMuuRv32u+ttq/8FqOzX/sc3/H377fvfskNKVoSZdO48/X6V3d0b8w9M/EE/D0xTSnuEJ/+Wx+3z75jekg8+Zfh'
    'prjH/p2QlcIsSRhXCEocUKs3U533bgXWZcfnuOnUxDHxL6WltJGhg2YjzmGRJ8xl40AfBTKiauZdFLeTewdPFvSCz02eB5N1jA/k'
    'HJZhi7j2Cin1AR1JmYwZTpeXe6RnpBeEU2OKIcIMZtQfGueEzkhK88pKV15XpllF3ncgGg4laP9/o21lX1qKZcrDMBCw6PD+Od/l'
    'HWRahZW0PIjUsnrMeTpcneJEr4TFmYyngqy9XrgYCh8IAmAToTJujLSHPXGbpOozMzwzzYXA1jhKYJ3CHC1W8tQcuPUSG8MXkDid'
    'Q+qatmweLRDhG8zl0Wr7ozdY7MpiILiDnStXN8/+GsZr38QccO2LZ9pXP06xYq2HGobUV3wDeNjyIoYlkKePQ6+5hzdfblTqpbzM'
    'oXP3CoYmffy79YefIm+CfRTr9SraG+0aG8Bg/ZzB4S+3UEqpwqmhsPfspc4nt2T2stfS3fobn6x//Eb77BftS3/bqlNUQgCbIL3+'
    '6iuM3K13IGGvaeGRbKI6bl4P26AEfc4F6Qt2UWJxF8oADer5wt98xzC4Lr0QcOvWH7298fWv1u6LgJZFdYFsKWpe/C1l4r2pb4t7'
    '86gMlY/8lzZBZJou4NFYoYeCNkW9qXLBAVTO7I8gHpQxhE6GCtwradd0suKIslqeHcJwyfRXmjChUlnTD9RZy7KCjce/FUCgvQL0'
    'juF53/gTbxGyfvRSMOgi/rIgnI+VVdvkKsm67KTXgUga93mwMTSZvjEOteJm7gwPAn/T6qdWgbq0c3XyIMImyHYGkfZYYhs7Mdv4'
    'fjyXwhwPh3ReGbnd/DajHTpyWIZyYc5Dz/Y1sROViJmgIKNWudnCpCN/f2Pj7ltMCwVZrmIMuQAXuq0YhSXhK9V4Fl4xCz3OeB3Q'
    'vQ1Cf45ky3Lmr5tv3tm4e4kMWbRRIyKukHBe0lQSe+pcvCAmaJymjOxp2udvd97+FMXvX97Ee/+LrzrvfbBx7s+pZ+bnU5wbl4UX'
    'JJiwBmQ5SMI4drFasHUF+XTp8bSoayWnnuTWLPiZWl62f0aBqpVSG8EF4RZXma/Ct7rLYnxCt5ITj2LiUJJ4r91fBQLJhhLYQRHm'
    'UJJODrHdefPz9vl7CTeasQzbDieSqnSNOU9UhusB4cOZllA+f/8+vQwkkuekOGsPz2Byh9sPmNhE8hYlbNeQkuW0oZdxOoCf2lfe'
    'bp/969qDy6h/eXiDDLJJnvbeLzbfuWoI0i65GmfqjWJZ8SVK9GKCN2txe2OhJvYNIkOtNPeL/YzDt7oiRBZ0C8kRsTqF5Vv0vOs+'
    '0irMdI51MkZMB9WcKQK14ihkQp+zYsPsbfgnFb2aG23DTNHZ3LgAfAHnAmYlWzcWq0ttWozDC+Rc3J+LKYOYv+TTi53f3EJL+5hG'
    'yhjEybvZQwtX+0q6FXpBLcsjfUCKiRbhkCpV2XpEz2NsThzZqZR6dileH15gVi0FpXkx2OVWHDGmIg1IA6aNi91V4UelrCNRG5Fe'
    'lRKnm1NcEpGsHgRQbaFF+AT5NJGme/te+/e/cz5ap+dJp2waox+xtmZvWfSHn+Cu84ugZyQY361r07PZILCYPKYkE2pbRGZ2cEC3'
    'iHPq1ESSoaVtcj/MKg8rvYoyS70d1wFchpJtyGmp6qwXw0DiaKRULxWqMemHlvYtPaGyDqrDMFLez/nE4NlK+BZ7EBS1A9zmXL7L'
    'vkusa1wKHVC/9OrBSuLxWnTC5odvA2ew8dHP23dvwgOBxprWw4wo+/zbzHHgQ3Pn72zJCo+RZi9s3kJzBGQocGnjw9dQ10C5FDo3'
    'r2IeIKI2+Y1fe3R9/eHNjTu31t/9G7pOUoepQy+l0ACGKAISauAmAD1z8cvNM5fWHj7svH6F+axvVm8EfVjYF3yz+h60UX2/q+kH'
    'ZlO4+vq799tXL7Y/vdZ5nyxsrr0P5Aw3YdmJyEJolcLPGXoHTQJYlMLVUugY1czu2pWC/Uk9TU+VDTU2zMExl1svMSdHWMwWoBET'
    'uvUh4RHL4VN4FwPeHAcuDiAqQ0uemVB1Q5j7Ek+p2CuPHCaftD5dPOy3Hv/j0XlmAVGOCPtErKKyaLaKvGFmK0niHcXH3v+zJMch'
    'KyMm/WAX175+o/3hz1ODo/9A+5APuI5wvIOjir+1oI+O5g0EYZ4xQR8e4ie3ABIsypGzVH15c/3ONQu4nLkwK4wAeecarxKTuZOd'
    'lEyW4IlNpdqfXhXW6drn7TOfUeJce2Mu2gwpaRavsU2JkDVKP6pAI+yKMnnXBkcRSxrdQzJhZFhOUYOwxD1ByuGyCEaSoWCerpeN'
    '1tU1cx8XfNbEgAQGnKk05jQXzFIjVJnelBz3fDgPL7Fkc+PcX9k6nul5eRb57RUSYYkZ5CyS2+VW+VWcTxDBGylv5UokhAwSPDSq'
    'jpVvUVFd8zqwtoDNc11TGXo4iIDZST9d80NsnyBiC1pHmRp0oXf03qiZB9rcJ7vEIvFsTFquMiSQYDzrS8xN6YlyLesLvVUpSb1f'
    '1XUSheCRPNHZJNm7JGVvZpMk7+jqlmS25NJkvENCvQWdy38ETkjkGZSvEohXvILE+jCfZHNIVkKFHVCDyfSe5A1Qt/afARtbwISS'
    'ZbGq1ldz0q1UhcJUJloC4BW89yuWC3GiPsZXtgX+FoBXOGldSaFlI0Qz2aQXLVIzihShuDVpEylMmH2RcvWmu2HI8OmxARvgkunI'
    'lQSyr1itN8vprkyDQS4apNYeXjZSUEKgTJl0AymfMLWfQwfOcl1IWC+VahcYJGGRlyAIDTIcdnNJL+vuF+0z53lN9t1hSbF9X2wD'
    'lNyKj5VpVMDB9t1CTPxLoK0+2PwtEJTXxFiGk+SyPOLX9GK//SWPo1yzaft4MXCJ+gms3AURV0WHyNYjSXW6nnAc1Lo+B/LG2rdb'
    'wLi7BhfHtmm+0G7Nt7Mf0wF7R/QkRhGTqG3uN6pmlbFtyrLnc6wlFP/EmCNBzbsSnyNFXfgOk9xuLgK5W0wmltNXO6UYrOjy5eIH'
    'k4i8uV8LgToPyE46yMjNI/bfun1OZjblrYLWJ7ZplG02q8c+6ps/Gt8bJSiQpfb2xj1xemIgzi7kNqWg0xY7qMgX0rxQaM1m5iq1'
    'tFfQNxgNaxTo7Ea3dNCIN+6d5QTbToQewodmc0gqFOlxyfOJIyk9PfT00940JCTW95LKGbZNQc/SE4KPNXukTJy12Ly8i4tiR+o9'
    '6YYDBADAe5IgFshxUYJ0gEULsQe2x4jvl6StL3cnzofp8M7FC5qRAo5l8/qbRgB/XxxMhA8mtmfz969vfHReZO/qRZDVPYlcZGdG'
    'wrm4NblpYm3fE8brip9YLFrP90lfwSvLxBV6MND+oReQRWLxOIRYE1M4Lv3nNkTSCuoqZSIK88qH/Dj1q2BA2xUoOkypRHaU+012'
    'bSLoo06/F1vmFfYt+G42vDZw2ja8FkH2hFnTuC87UFgShMNlMwtaXo4tTluRWzc+lXgreXSM0x4FLDSxYwDhTOszksIS/ZI1Z1+p'
    'lcVIgVPZJKkUUdtFCj9LwSWKwwax7GzX2HlP5Cc4tjtJ7NneRzMRBfhdUM37X3RufIJWAqSEZipZy9s2Hr+DSObhHzZu/XHt4S/b'
    'Vy/jz0fXWdnPaiq1F4kWz3H0YJ8H4PvuB5I4Xwfvm7Fp3dtYPW89lXCpi1/MFqhIOS3jc5lDBQp6EUGXNSaavrMPUfSvvtPPv+Kl'
    '1h2ghWQzP6ki8kSBCuATBdYsAtSC6hx2+LNZX2hghAunmo7MZFfWH6WJ/j1fgBWRQz1iQV0B/kAqE/7hiRN4lIAI4mnU0feqv16r'
    'LqLpB4b2LTW1olP+FVWK/oVJT2FlhLsCctblFS7Mo7ofg1cBkIuVWhSgz57uYQ7j06D+V/09W6EYw1TvJHoH6zly7GEBh6b1yXjT'
    'wke0kO4vkaIxaNXVX6oqYVzrN4KN3ROvRJfyMoTM6Ue8Ekzpo12o5CWaD455QEKJ0YGTkQZCzmSlFP0Q4YQ1xAITxIOrQLHCkTP7'
    '3cXOwrQtYpTrHcWYxdu1pbUG/sgGQ0D9HkepCpCRC62y9aEJh18rmQ8rZhYAzXPz3znS7YrywJNX9Hm2FFxaMY519VM/KS/m0bRl'
    'nKwNxIIehfhZxrvyKYt/U6wf1ZbFtTJH0fRakZWoh3Ai7Yw9KfXIpm8knBK7RBKHsjInCLNap/DKi88/8+yB558/8Bwal9z4c+f9'
    'cxt37v3j0fnOtY/YtIDE/FQfZbf4nf2JTame7ssHjh548eihwy/mxwZG9gwM6K1Bwy4ilsRZkCwnnwXuSbwU+QO9vmhLa7YOTpNJ'
    'n0RySLCWHzw3IhKk1M2TkUrTDupShyiFL0lGoDx3NJFRKYKWl8XZkQqOFBGB5wX4Vd0mfQ2Xlwd0pyQxY3Ixb62JE3DrCG0GIrrV'
    'yMaKCAvCWGpwhVkmCI5kupq9fpGYGt5hnBmKEPhECM/KSxNNm4NBRWgtv2cgwvjYzRYHnB6IEMvlCbmprggpHZivU7HgniPGKVW+'
    'WEde7OIXSoSktiU21/Wgf5wHC80WEjQvknmZbhCxu8ARfORbxNhi0U/LhRNufwgpMH9e0cHMNIaALJeerxdK6Z/Vp/PjRPGiy1ta'
    'odJdQR+UoKFwOcI/xPwDuWNxwiXwM06xjkiARGXk70OKlwCYsoWKmAnFPZJMkfFEUu9vXxp7mAiAaCPhahC4rifsaSiurNCRip+X'
    'NIgXWy/uyFo2HlVRsdqMusecTfbAgjZxj6rmE/hmdfO6YnvtZhrfrahV1z5GS/KBzMyzsEQyN2eJMxMP+JH/ks8Eu/SVDtBhqOgR'
    'OFVu7C+g3DBCKuO5F49A5UlDcUwZTQZRHoeB8KAaNiGiK604y5iHCi0KNgRzbhXwJh4vv4AbawyY4JXOWNjK2SN+hzlaEWCABcD5'
    '6ebCHF4M4HzhL0PYRwNCPUN3NvGV1J9yS6J+yaBePV9sL5jB+PW6N6HanqwjbuR1NZlAKk45/ar9wdcKnfYl0DI9RgH6kL8LLHb7'
    '7Bnnu+49gejcuvv1d+/Ak8jOFti9/VP36lKtW3fYee+DzoOrqUMvobZZ/627MkTmVNJ2EoyEwKKtP3w97gUxORWdKJfntVMZ72qM'
    'eUzrOVFgKzoa5QJACD8MQ991Iu95/06S2G6mWq8T8AFWbDRbu8YG0HUN6R38SH/T/YvMgWselP7A8Dj8R6PeqhfrVfoBl4r/pfsz'
    'FeZwVSjxY28re2pNhedjE9Szoq6q8lrIVYiPoi/elO1QjDqtUl69MtrXw1jLUoXeXvonYyaTz+u/Uc+CbecXmrNcPYNpODj+GNCr'
    'EsfQ9Kb2PpU6SCAgjZDD1JOcIOo1rVYWZommNr/DnGkk7himBe0KAtE3q2/CI+J1lUscnbbt3whdGpSTUYIjf/n6r9x2a+fBVuwj'
    'xRoJAQWFkQvgheccAm4I2kDES767y5wfq1YsxXe+k9zft99J4bTgGjzPMhpnNuTiiFIVb+YUujAWPp1qc1TDvrS6XUjHH/nxMy8T'
    'DZ9qzhYa6AlklzpUfmqhdqKG+lKkHCKbJSH8lY4zJmGWS1SXelPUonS0PZxZ5E9a8ew21s/xcRlXHABu1Zt3hBR6N3aI9BXmj04d'
    'yYXi76HOj77F4kdyA8RHlnRMtcEOukWcVAgrcNAXkAuvQH0hF8LYVmCIxo/vtq/8QSRuiP9s43vBS+FWI0O5gEAUAC1iPFpUHOce'
    'g9W27Aj+h+Z19KSZXtyNwvbuebAw2dtAYVAigyZ8wvAo4BJEoukE9EH/qhEtTGMmjJlDecYom+RH3XjyAMJQrRmsIl4ETye0kRCj'
    'H43Qm4zQUX7dKJHvh7WESKMxsdonNHUwg4ioVgQ2IA20j4iXIhrDltRKsGQ9FDtfY4DkHnrOtHehrsFWTfzAQM8kmFKUuSVhBaJ5'
    'QNMLtizLpRjYS+BzW96KEZ1Yruu3R5lRN7qFzWY27v2i85aYs2HkdNJaoJ8Q64QoF0fn7Xudmx9s3P29mFBaDZkuYwPSzdXrG4/P'
    'idvchd+0H61KmKerlzkEEIeg0JZxyKcAe6u2Qs1seZmooX15olmcmq7b/tlLHLYM7pxWNgJLS637meAxFJVNnJ8qNGpkit+sFeah'
    'Yst1a2oRyaWM91QdcTChYnn43JJYg8ocogFUB1qNVLSju4/aZySiSPudP65fvgenicImYNwwExfqWsmaji/F2qPrm6tXO59+gHHs'
    'L98z8VHs8YT1fVUCLzmjcmfrf/t9+9FbMoPzZzsXvsQjvHoWzw/djf+EgHXn7/HR2nd+h87IBGTrDz9C61Go/7Y7EZIe5EV+YAeT'
    '3IVyt349z+Oo10Xh6/jIqDNJcTchZfPG50Dqn0uNjKbWP/o1CrBoPDsoi948aWZFZ0mekJ5AqwGQQHF/nS2iNa7/+Rqg8bWHZ9qf'
    'vQWw1bmxCsOmBiNYAYISxj6jneJzobgq5zqXfv/N6o3dGDqN9/mb1ffMhQnFNF/4XRyQfWePHWOyJu5t9zxM2EQrx2X0qGUsL/eg'
    'eCeuYYaGeRb3TFhCnax3ClYD5rTziuGG9v0i8oLrJkIZlvxGUNY37AZpokDSOBNWEombNXGBllxwgtChxTqEvb3/miHtdZm+R9xQ'
    'cL7FNduqp5yIPiZ77K7VTbp6ESGVIiuK/S7gLcwKcZGj57XvXESPpauXGVsIKCv3KiSotJDSF6lFGlPQnuuIGuKmENoHqblmpWSI'
    'BRnR2EqeSo20CFc5K0bEtcvUtIsYpaFyDxf21t8Y4thaI0UwqLGr6DjcichtoeuEMU+ILKaD1Pcy7EsLJJESUTsOMf4m+jInslKU'
    '2LG8NOZSAW/wYZLZYTYB3FZWgTYjePNJ3nRQajpRxSTYPrfIGC8robZrlf8EGgTDnChmmDsNp0gqxa17e3u4YqZSK1YX4A1WJWEo'
    'BUw0WOGVt+U7uSFNnicHmEXEUBOOoJ48e6Y47LInsyRGk1uHDi9p3eZYm6ZuYwgXZVIwA4BGrrIeVfac/Dwo5Wl7jTyCIfUkEAuS'
    'YbA6IOHnOcScCoGhBtG0GJX7QgiqjpQRb5QSJm4xmA0DE/YPUThlxdfLmQ9DRLcpsSUSw45vUKPaKLs06VBlPvDxMOnVNAA7hIKn'
    'E8JsfPYXDMRjHmUueZWjJqx0eZoceSaAbwyFOhUAiu2fLPhP2RdOyiNnmAm8Nmmr6+XlyalwKkuf9YQXTAn6Av9x8xd/VO72tIZu'
    'r6vVb2+voxuBt8s4yFFTqy4/EHZ1f3VOYYwlsgsTlGi9vaIHZTThqtRCgx+8GSwZAZmdFcVrPYNJOU/gg2fr6U5gxkWnP2dzaMHC'
    'i59g/2bkDLwTlfLyopIi0YpjGp/N33yN7jAXz7XvXEcugPSAWi0INDlQTuwiqgOxyytIB8tFOh4x03eaPk95Ki6VUJT5JcGXLCLN'
    'xwFN4WcDWfiKEWDtnFoIk2XLPszKLBhqu18xWzsAl0yJtzUQeFUIEO0PyRdN5J+qtyZbosnVYVFwkOzIY3RRFDgyHmOTbFm6WM6h'
    'GBMZ29LhhZaO+ZoQQpP6SIie+W2CZ25jkLek50NWdu7IK9HgqJNM00hHaSZAn7sGPG7zbSNxftvYmzyMNlV74qCWKpZl+/ZnnbfO'
    '27Es2798f+OrrzZv/X3zvd/H41p2GWKktxdX4qAyragM4+OLz9Xb59Yefk4XH0k05N/vXYe7v/7mTaB9JbXmz79sn3sIjAqzJppT'
    'sU159dS2NKQLhLCWcJY7jaK5DcOmeaRky8BE+zAFcN2maAfA5ElwDAIvx6gT/XIHtmDfNgKmd//niWzuwuYxBb68jFp7K6snC0ts'
    'gpHU+tqsdVvEkRyX17ls0Z6EiyrSEUx+bF071qSj6ahGxhx11r5WFlwlLtIxnsRaOEoGjbDnClB5CHheN6IllZOVZPzg4bJ13nvM'
    'QoT1C+fJOvESy8kkECrzg5fOrT/4SMu37LtqgySbUqDC2raU8Jlqj1okZjFns/JmacRpiQ2q1WW/y4GhZIRNC4Rfaz/6efv+fTRr'
    'Y78hjiEhVWzpzNr9T4Mul8V5EMMlaaybdd7+1OpfWO6uIWcn/FS4Ca4BWd/Fbtu7ZUxU/JixaAxUPYReJuk5bRxLSc21jdecibBk'
    'fSMzO2S8cKj/qtfKh2dmkKVCd7J4QB+tMB0cc60vMAkI9thMGzOJU07mN0CaKHk1Rn5TTDUsL+vZJJaHanLpcB901a+trchcrX+M'
    'Xs3ElnlrU+ItzZSUraEaMOGb3RXJJYrlShU73cW7JJvlbAnjth/z4wTbYmyF+vqIGzfWscVEBwHHUChmSpRjYwzbOHIqZjxrTwfh'
    'ycyGffScGeZiBkw5ljhNTkUDkXE2BbhXBgfoB4PRUbZ+Qg3GsOdMBpxTnuuBXaHG4vnuFcjI0xP/88POEwi2ljmt/+G19Xd/GxcQ'
    'UQCYu1cA26X2DFgCcj/XXb1QekbMqVArQhZSNpGK8LXrP2b7jx0r9fVPDvTvLfTPTC0NDqzQl2PH6Cn44a4MYjlq3ZV+YcE0ICGM'
    '2cC0zNVLgLc7n/5h/dYdR6hLFB4xCtyjk6WQC1HKQoU5/i0KGPtTU6qoeDIJCQjj6f0s07L0krUbK3pyYqPZ/eWyDPy3puU67z9q'
    'P7qSsAXJ8z81iyckZZX/Ko/vCZ218w/WVWUQ7tLKq9aPOx6HA4HGlzC1E3C85dNRcaEBbE5UxrvuQIR9iWPQHLvDcXj/VjeGWDJ+'
    'zPha8BvYTQAqqcr5fls7LgnLbWGgkDR5G2/kuuQGWKgqwuhfKYAWbdcS3uBmVtNG9FNtH3PBppB/R9XKXKWV3TOAppQsnCouZod1'
    'RgC8lVk8+vz4VhfUyo1BXrV4VACRBkfrDucb9eMw/2Y2XYLXkdVn4mBHrfJ2q3DrXe1c+Lhz5Yq9q5i8FLoVeTZ1rvLcSly62MyW'
    'lxWVqAzuXQLRKNgQGPP035wjZcjzedDfpoRoVykhks4ci9gBNyeps6k8n5CMJ1oJ02Vk/S1i+cg47nuPQAIKX//kEwzpR2P1DaJE'
    'H1ExReVCUzJmpbwBbNuwrZ8Z3hVUJm91c3rsjVCdJh+toztUegO2idE7OFeoVWYAzVtKBNNr8uu07WJxRzh00NrDM5u/Pt+++6W0'
    'QaVEF7WHRgUChPxO6nc9pvrVMxdzp3/juCvJ6o2knAyxK9Lba4jmHi9Pw5KDp1Qam2SiW5PN3x1DuoR38sVe+nYEWCJjS5VdUszi'
    '/ZJutU+piU6TpmkmmdvWn9TS4lTzMecwx7LylM/GcRDg/I6pdW6EeleniU9tew0UqdDDylagDA4ib1FOcyRhoBj8AhiBXAGgeDyP'
    'P5YUkYqiJnI8lVRp11/nYBVoyMEZY1WYIvtn+8GbnXeurz24zJWV7YcdAkKZFuCYLlMC/IgZX+7YjdX27et8FTlzLl9IppnZTmTr'
    'ob4F2aC4V5ZE0TzMBUIFZYyyT3q31eWPizSUzI2wqcg0YtKM+NsVE2VoxLgFUajroAdScxsiuSthmPKVVhqzOcJTT+3PSujCaT7r'
    'yDlrUvyr1g7vtKT6FiImYjrnX5nAOQhz9iqEkX5js3AmtKvZgYgz8E5FiLVIb7rSxQoY+9U60bQ9F9Nz3nPzyfluP1qBwd7s8rm3'
    'V/2VbOjb26sv5CF4uo4DntENaB2h1QN9GM8P+J/2wdXv7XWPXdfQW2P3pD9icGXAifP58fmkqZDjCjakP3ho+nOfQ7byycj7mtgP'
    'KlywLfwrvcBf+/KjwPYnTWvSXeCUfkBkOFUvH28qgXhsvsLtLNp+OJf5cLRk8d4HImLXGOCyAyte4/+ph/2f/iwbW4xTFVjdC2Sn'
    'SZ4+ODDJA4n+819fJckwHrBTyHE+02o1KtMLuE7KajmPbAP60Ir0zBUwMuHiOM4+US+aTNCWNSQtVuaCfjUazfbLndqyZsxnN1ad'
    '3eAc2so483aprTbO8iN2aX9XT+pIbtGykk3+xJaT/EYc/97tu+rcvZIaRpM5VgQRU8GxPC9eAOIZwzKT/lcPBt83b6wy2Hav7etU'
    '9dksWXLRnEv25VbwNi6JzDvcXkaOOJn1AYqE10lhxGO8pATfrDkRGbEPqQnxmzBaC8eNsW7CVnC64z4cKNUwumXzJfHK7OF/c7qV'
    'a6fLBlhsbaktrxxDrCfYMY2fOK2sRIHIqX10zoG4c2PYg+gO7Xomv4u3fpLr+xQKorH3hO1WGgcZckKidmWdUFyKsVCV2Wt2KcFq'
    'IcFVloV8tg7C8vO0Vd2YUXGR41MlEhRJ18KVRcsJiM+tC/QrFtOprYO81Ye2B6odUQpnFg2ODcitoN+SRZ2hUTwf4+B4orxIsfUi'
    'FarQxPUvL+LQBwAMG0Fvb2xp/iWXkcg+e2uwpw3oi+2AvQFh4uPWtVt7LjyPGKe89ZRg1TbV0tvbYz23vb0ORcOk3UBoExbxCv2D'
    'UTIdNJlUdyqyBHIxdlXvboy9//5WReTA1kvq85aETXYy7+8lpleoUBTFC0VqtQFsdRq/RsOipj5W27Ur9ZNyeT7Vmi2nEKunMOBK'
    'qlmsz5dLqVZdmZWmJFykDvmUSTH0VIqFKkktKuUmxtelPPL1uXmUnWdi4g389bwyWjLyDRP+oKu+3oqQkBCIShJuxXhR4KXgZBoU'
    'oUky9VmWGl6yR85eRdXC5WX7Z6ZZnyunF/LjmDAPDTU5W1cPBQtG0itYXtZFslNYOM3WzonWJzGz6y3YUzvqTEJ0EmeqlGAMs4tx'
    'ajGTV0zJ27Gbkm+kS32HMGfvu2u3l9uB/Sb15DKjJD42fs/WZMWcDear9y1UKxBLPlqHl6OIQT+2iGQrzYRVsZXd6dYUJiMrcZSN'
    'nh7LnF1Gc+J0kO+9H5bDMs6wLOaQROvtTcs0l5fVIABVvP2hDrO3BVW3EueunPHjICQW/SpZLvs7OeEIKQEJi35Q5kWtyGwxeRFh'
    'fEKK6fJDlghJ6d1wB/U4ZSri3PeF7fyBVxi/WXEFbDmIZByXLGLI4EveEPpbafZ8sOEqWuh0EkaCriNbQMA/JgemJjguBpcVWun+'
    'wZA/TXkJWvUjbpKhKtYKo+Z8O3IymWpU/TLtmNdEpHYWSBqsC2XavTcJ62Cyrmoxk08SuS/qxJKWHGW3klqxCCz+2lqCsmRCwpio'
    'AOCtZJVQKzEqDh6LGxInWlILJuNwDgwQcXCbrPTBv/5dz2Ql7qUU044ssT2jiZ+jwdKTznFpzo21gyQhEe3P1CpzxGEfbMApkGma'
    'M6mjdVi46jJkEQoJqt04OeHSzgAuGcDQsJDfQe4348GIeTB7exNryC3aN8SWOxZ45hPr4/nBoIjQje8V+zQ0p/jZrufH6zplhNeJ'
    'tCB7Zdtxq0u9J74iO9gPTSP496fLgpkZ0h5QpEnRoug9A5ETfCnDzpzLy3sGOFifCuFgxeSRdSdEZie4EZTr8nuqB+ZBYuyVwmnR'
    'EFk+RcC3N5uAm5U1dNch8drjZgaR6gIvkAWmcQzSY4eAIg8BHR0qqZA5Xbl0tueCciMQvxgkV/htTourv1M7cn6SnkTnMExgopPE'
    'HizlUQIZjmFIvIGJXphWeAOoJuVC40U0TFlpo+LOgUuOebCKwzAuOgwiIlO9vbpgnyoAwr+3V5k5pHvkK+5Pank5lbZ+J4VfswIl'
    'wFk5lSUGW7bHqtPba0VmsGqHIc5Nj86oB8c3Dg2mgTg4JE1bQhRhyx4VTsDp2QQpUr1LrICk3uiIaBdUdIS+QGIX8Bz0T/RhNj8k'
    'HoL+QCt0gykZfwp7rNBytXeomURtkXvirn5Mkdv4Iz+O/wWiuxSDBipgBebO4IEaMEHvr2DLc+SBfF8S90DDUA2bmakWWhiyzJo+'
    'bYO1P7F8L2JNJC+CG8SHTVXyrOCQGhn+yLQbqjy87/BphXRvys/dBIHQJkcGOQ8bzDwY6Z5MzeXlwTCMxOvcCfGGHVsptkgJrqPg'
    '4BOEiJztVawZ51hjpsdXQzoEqlW/z5pLyI37+sw4zCXL9Ah9stYuVJ/Qlo5Jr5cAdirNMgYTrFeRWci0Zsu1tJY1Wh3qfVBmVr29'
    'sU/pBI5ZFwYmIo8F6qoPNO1yFy6mSazus5quyCQlmmSawyuvhJFEPE8v0R8rJhmojssj/4jtoNnQfUk7bjY0afV4N5LWn9rRBgho'
    'CHOPawfCgcUg6pDQptOaYphTBcrA0S7baqbf4ZygO56bRHzgRvYnXdWwZHkpN67MsT1HPStWdeItWwAnkYPKcGPKctH9C25d7Jzf'
    'EN3ndPeTpmZf31TOuST206s2Sfnn+/BvTRWeLw22aLsYKmBeImM50u3z5FdckMcuyBEbVc82SHtwmkrZt70vZ5aQH0g6a2XRGKb8'
    'LzaYRIlQ7kauii/EGCms2CqCCppE0O5F6lGLFN7m6kj7uIGY89BMSfyFvgbCcKFKymUdPzHAq8XfM+XT+BrDutzf0tFKqLphytTt'
    'ZoI/Zo9X69OF6lEg4BS5htZ5FKD6ySg2ZU6CNkr5mpYq1pCw5bgFgWVnILZMNSBna+NybPppmF5EBaJIM4xkk/uW7yJCTQXfrL4Z'
    'uCG4oBbGsX4WGJef4H9ewP/8C/7n6LPBFHlKVjSk8M2jPsfzgwNDI729lX3UhYBA/2DIOHQXFecqAHEuOKQraF45QQ9UAwOMOg7r'
    '2OvAhASbrB+snAYEMyBl+sMghXgI+mjgycpUAnFEYrtKazFNMe5qcCehpNxoAUlnB5GkrdIly8tq16ABcEzUWBywe3vlhwaK3l69'
    'yRTZDF2SgLCGphTuQ+/5UosyZ6sAY+S5nw3aX5/hDMTBiiWOpo7cdhi3TNpwiuONx7/q3LjJaSoCV8qKGFOTGwNkj2QHXUPn8ePl'
    'ffm9MD13ELjfZT01nUSwffYMDLPxxS/sOWIPw2OxHhYB36PYSbpIte9dwax/0MONC95cu65v4+tfrH15BrM42w3cgxVlBIv4WAAJ'
    'XFdhZqZSdEm76UWMMK0IqrTUmbBc9C3Rs5bciwJPSyhY9OxL1KdCz+synaRHQC4I/826Y2kxd2+vq0twBu4yIz2VNEreF6IFNIbJ'
    '0lrplef5cozjlUS2oVEGGhqFuepmJCANgWcHHAMO0QMHKlBoYmZpizMKLN7cBgSl1r4xDUIBGz6qfKrX2+fxf0G8iQ13fiOJY8XR'
    'N6UBxt7EhDwMzRcuJXS5Z2zkCfukSWCvDOGm1yfphIfFXmBPVRf+o0iYPVKoLLJBP1LHuJJz3sbnCs3Z6ToGi9zZ+0g6avt1THwc'
    '7V6U0D3v9BMPqXQw70wMI1PbAZeqeTd8MsqX/ocCKNue+ehikT+dHz/d2yt34XRmYT60fqHmP5zAr338QwuRU3YMfehDNYHWp8cH'
    'JrSd8+nkUPph1rzOTj4Buyv73Ty9ZRd2lG30GXSQYwXlifnkrGXpIEPFgSRxx78tnayXKiMgDNRfUnBnspSKD7BqN10vLfJ5oayP'
    'AtcGulU/p2X2sYlEv8EMnUWJfBMbLidlmUopHyvVhQmmcyqdhkTt+/trm+eurN2/LG+PF2+zPk/zUOFJcRT4FoTRLNCpyLmr0jAn'
    'X5xUMypQKbY7USmeIE3N84f+7UDqyCsvvPDMy/8ecPaZ2SEO6Ljx+OrGrYudD3++8dH5wNN8ks6wBvR4Pta1LsKYkK6xEsxWzUgm'
    'GOnabv+Koo0tWBV4ezNXRroWaMjJYO3hLzvXPuisPuTdhJZr93+59uh92Na1+29sfPVV++r5jTu3gqloUlSX/Hlz9eb65XNQnQLW'
    'bZ4713n/i/aXf22/d669+ohqa6IHqZFIUKyV7vjGBVmp6VryOkTyk8O16DgvwdQUPaXpSbI0jErlZrFRIaZmypZYCDFRP03bYQLv'
    '8pbw2uHwWIBgpxXCY8TLGOagcQIwANGDA0tTzj2kI3pas8GwsLLxOqJs/XTM85ClGc7BiLtOXqUb4inLV5iX/OXdjUad1idx9U0t'
    'O128PQpaazqgUm70Y+DQpgcmmN0Nll4sW5U181Rk5Awb2UJ1s3fndTUqVJV+7N08q9486u/6Bc5NtFurlX0g6tIx0G58/kb7l48D'
    'JxsU3Uh0zADoRjtPhup/PHqDwde/oeSxVa3un0WPGDO9UqFSXewv4teUWTn+DPiMVZutsFX7zB/XAJ4tvT/MbO3+L/De8aXzsOh8'
    'uVx6uX4qaZuoDM4pWpg/gn8mw2+Ej9wW5UpUqZVik5TRgQBzipRj5rLjtgVqOB8LBGakqSmlAVU3j3Fr16uEJIRy51VLdq6LE02b'
    'IOEgkCf6brjnwRbDCiLWHl4W5EH+dJgsnRxy1v92F35KEBTsUQdutgAtsg82UlOL9Azc01KmVaSA7nYLVCU4GvXnzu9CxPZQz1dq'
    'JxijSQDmO7fWr55l1Jj65uyvYUmmYma2UZ7JK0uqzGxrrhrkvKGTLpS8rPKgWiObhHysaZe2XpcRohZ3f6rl4wo92C8Tf04ERfLK'
    'RTsfgUThdINvLtzWrwc9GoqBNezvN6u/UXws1SBOFRp+oL5iiDDghDjysgFZoNTmzNsMVBT8trenotBkqd4it95aGS9ZcmRogmwM'
    'WHXcAjDs0QPpGRuahRqAT3gKlD147ev3MDAsPZJ8ImuP7gH4/uPRuwjQV+5hkJ/Pfr725U1MMQdQfgZjmfEF6Fy80L54be3BJ+IC'
    '99WvAdugcZFK1ZDqvHVP56rbuPN1+/Y5+GKbryWeNy8Knhj1NDgXKHLaGDpP1anPRwrIVajRSPcUzdDdIuI1U5hB1lvas02Q2TjO'
    'ESsgReliKY45Cjl1/tgc/9EVLwMzP71otWPnBRd0uQsEbSDp6ZbQn1Yr+RAyYfDTRsHQnGEklY5SsH25ZJgB1HwlAtidQU73tD01'
    '+sqRAy+nXnr58MFDzx/wxvMs7zBdNE0BtqFVr1kroCL4/f9ew7yd+CODXGNe1ZRvWz5vf9289qk8bHc/7PzijO7JTnYs56EyV9t7'
    'a6Lny9IjquUlEgBmxMIiav742az9CAcWU5dKVVKUERBpLXjRXxDyN96bEMaEppmFP7IwN1doLMa6pOfGAxfqnEKuo3hWKGtFVmtq'
    'RH8hj5Pz+qf94KZ2peSBZZKXEzaMJz2w35mOhbOwd6U7xerRqiKeS3r31CbRQ0bEH1WNP3n6gLipvoVuA+ehGvaIO0VCecSfR++R'
    'C72FEANtWitDbUH5mflsS+oxk2PTdp2bnwE2b7/7tenFQajuSiN7IJ/OYIDcyX6rujvfcK9F8o7LQwT8HMdNT6S4h0bkqSXvKu1X'
    '5e26Gs/fdvVd77xTcQdb/+Bi+9Jr7Xff5TnY2ZOAQmjf/0Xgz0CsohOmwCFsrTl4lI+zY5Ez0cjtPUrEHnpL37rXvnimffVj+1WX'
    'fNX0PK9/9SvMuPT3K5gMmLOTn397/cbN9p3fcUJr3N23HksSa9e+obowV0vCdFICRyimHDuBKzaw2UlVl6HhEcxDuCUsyoRsxCtU'
    'md1PEoSaPFReJzpOAnfwvMv/8td+JF3NIM5Z2wNHVifdzrV9/lr73AOBSDqh9euvp4YGAKn/uX3mEzhPmCSmGaBY+hyiEWi4zptf'
    'tq/8FoowvNzt6wgLRJgBhde+cnfjF1+1z3wGfXFb/6jlbHa4yTm7etJetu/f15nSu3DW7dt/crKT6cstfXvbLDaWjULthBnf2WZ7'
    'TpHVCfZL0OqdBzV2gDLpQW7+dBuhilutq9REqjk86BbUAGuPniUqSrNtTFSlVDOug7t94VL761V7NzHsCzN13CnJYmX5DrETudN3'
    'SRyfjInsJyhy8FokexxZ64zsRWiSWp2WRcFFOD08VFty7NTTASalj24OXTH/PMmblM9LPxYNJH1B8bMoXYfV769WoM3LAOvsX8td'
    'FOnr/9mH1FC1PNNaXna+j+P3RuX4rFfw79QAmBbvM9WfrsOezIUeVWsiPVHGVPZvosyhBHT8pw6zJUlYi+UKZmcbiIDBBO4IzXHz'
    'mKm02XqZO0AL8GoVQViymZLYH5pYWWiVdwsPpH+psfQH0y08pJg1J99/qEa6ikWr7JWmbclvnGZMDjA/XSlDjZeuNNEuTuBrP7vZ'
    'UNoix6+D/I48Hw4hVNG32htH57IDCCHfPiFZ2bslVlslSqKqirzlmEGO71P9FDEt6nAmzNH0pefLjZl6Yw4Npux8L6pumJiDRItD'
    'YI+PlAG/kOXsuDqfCStwfrP1ahNqTGQmyfoWpjs1MUF/0tlO2Ek0J8gCeGLCCvWhjMqON1/R9hO4lh411vJyT4+by8f5otP4LC/T'
    'GpIT2XgAAfM6iLoJGol+YlIKyslKI2htHH9rFtDXsITGMTSE83Xf0KhvpCnL5SFQIe9sgu7FSS+qZ4uxSHwgrANqeonRKEHPkr5T'
    'GIM+BjZkR59Wnl7PMQRLEFRBAdijQgdov/9CvVSoprXIB/EB0bT2rUJV1MlFSS2MfzrX9Mf4JX5LZ2BsumEDBMUvsT+Jtz6eqGV9'
    'am5uM59kkEEnELKCKW/OE262scJxYCodesZ/yn80L8fPpsVsYzQ+oH5zcBeyqeAhdS/Ce08OuGEfVLcTbDTFvagUsbVoAeCtxnH2'
    '0tIloOxSYTHEPLFZ23pK2wdi+Ib9ara04p3OdiLTIAdjBcwyIH5kLXN8OYPucpzBt1xTrHNcUF+wqxl4q1IjDXkj0cFSbhg5sgnf'
    'fdMxuUKslF4II7pLqKeukc8GS19DE6kO4xrau9VlNsNJs5kQrbfd3j8kUTV037XY4SSdxsJ8GO58exfmu22u1m+4sUgSDzfBGlYE'
    'Ul1V8VpnR6EtVP1wyfztjEvAikEyUKvBGmcM/pmVL8ZCLWd1YDT4rfrx44DwJExP1EPdaRRl+ek2MMZJwdw9x0lZ7RxcsmZIphQR'
    'NflJPL2u7soxKla1e/J5FyU6RsAuslRtvl0iIHfo58jRV/8pgITLyY+7VmG0RHSqXbSMf3d71sEqNjc0130iFsPtgq9o+AX/ZOH/'
    'cMuY2Mm6QImprhck0XXaRWY0NmVeKeXHS/gTaWHuKwyXlwcQ0a2ErnW9ivZ2ujK3MJe33BkyGe6Q5ob9ibGp09ooY6AuKmFoG5Zs'
    'kUJcUkafA8WqSYpZmSemRrge9PF1xG9Cz0nduISS+6RaQeQ3s0V3xROxmdBXYFmnC41YGXxDqWgBPWUWgfGdLSPZnzedT+i9Go70'
    'x12ykWgEFGbRBuxHQY6GMQljG/5qqNy3EeDtEIeq0VDlbEoH/VCO+S7CWKon7tW6pKkUu6D5FV32lhJioiqJMlGyLJKRBKcKcCQI'
    '+P8cbXb3XFIxU3WtFvWinXZRQGm5LYu8WEiOk+lLywtLxk8YCmjj3J8xJZyONck0Ipd69lhYQRlUoYWepfuVHJ70JVEPrP1zlG1N'
    'Esb95toVDCGe4vSMFBQWX4prb2BgWyZ0J2Sv7V2GOpLPQGNmPGE1UBwzV5qorEBzDoWcXdys4rkaRyZJX8Lmq6pYuRloznB5WVkX'
    'cQBJy9ymff7axq0/unuh8k0xXW34wvGxAW+MQDqxIhlwAAMTz4AjJVNgrW3iGTivhGFkl5cFNNgNxZ1ArJqdntRei+JKeF+9VfAF'
    'YTMmScU5FEvFyVDTXn2EK7S+cyiyzdWb7Qcf6oOOrccQ+N7gVoFd32Hdl5cNp1j2P5jEq27HCTVUGgsOhsZZWYGCwGMiDTNrnmF9'
    'mETk/O3O25/S/ekyF3+r1S5PYErX5qtdj8suNi5CIn+zb5+dWEv1E05JGspU3NSpRw/Gr7V3eyi5MVydCDMX69gmFgrQDx85SaIx'
    'N8WVifkmUXSxfAL5TF7GdGsM3xTmKFEymk8qht5GvMgZkvGeFquwWXhCVbzpWDWJ/pB085aKV9t14auMuaU9xS5+wnAhzTJnwUP6'
    'TY3Kpbb+1uaYk+Zmx7ux3l0kIWGf+mkyWIbqqZMwasO1hYRS/gzlFcpg1Vq0lJyki8HNsRWdYUTncLRwPNYXFZDGgoW1tgGHboeC'
    'pm5GHIHnr6hGMrLqlukFyHeZsraTUPPVUwxzuHqtPaK16pUmbSQ6CSoFSMJeiiQ3SQGMRgbUNlH36yqmtRLYzj6OM42kD1v070yg'
    'ANcXjW++OXst8AhIAmfORS4Lje9H5Owgz2IlZ+5A07oD0UmJKRDzmNtBHlPsg4bn0YixVX/iyExeCtFtSdlYihBOeReQrDr1cD12'
    'VlPnVh6k3rm6bIKZiolpAjtn1zT7ZCq7NcyWmTW4Ndg6dZtlxQ8so0HcmOhbl4lj4Mv+5agBnGCsLhtIWTVjwxCgbaXIvfnB+o03'
    'RMjAO4Dx8a+/bpuYwIOlR6ENMF0z59G9Gwb8/4ZXTttCrd2/7Oi7cu4hmyNOBkF6Vvh509NIoJQpPZEt+FhC1E+Eeh6VjzGbfpuS'
    'Z1kj/R1ykyR6XwMC/RTVwISYlnT+cqtz44JnVM3ksZ1a2qYKMUEIRvPV6+MJGGbkILAK6I6OrwKuIaK6oRX/CK6vekj5YnNSFgyy'
    '0kPvInqoU7AwU0V8nJ0XZibGYgit8v6XZCWHxuaS20us5C569m1MpmEulAeoK+fkRmLwyMCCitXXGAYAJADS1h+eZXBBmztKPy3k'
    '0w3Ymhu2vg4TC976u8mCbtNEtoTYlyA77ree+FbXSpDibiHEZWkBCWrYE03Jth1CjUAxXHLM0Zzt1clV1z96uPnO7SBHSj1bO2qH'
    'tNc6SqnA+peuvZtLaQ7Y0jWK+aotzkcTVpV4Oh/0lWtFAJVXXj6EUkHAM7Ukikm2K2/wX1TMu7qDyPx9+Cd5T9WA4tAEifhOqD9P'
    'Vm77/lvKW2dPDDrrozzkmCnEcvzzh9CmhLaYjzGxa/eWjI9tazVf5u68HfrJyDlNBrdscqpcPuG3GNqyxRx8nrX2yWnqCZOVzJ7F'
    '8UrMqwW/KMdNfXP2V6mUkjktWCJ6Kf0N8+botmKkpmpwT23uKiQmjP2DSVlfJETYKDVR/tB57xbjBj5GXQXjzlCFtft/Xn/3Tuf9'
    'c+0b99rvrQLC4Hh/Yql09szmh28Do0xSM848U4Rb3Wi2QhSlvcuWJU4peWz2BYzOWRBhc3qdz851Vh86iMkze7CX2LMAa7wrL4GI'
    'likNW/veZ+sP/0yD9IhkxOacPb5YUi8DBlGMM7e0r5zLkPI07W6oBW0uTOzVynxzPD8Mg356Wwzfbc/n9rkH65fvbZz7a/ver1LD'
    'uMn6iUcunUxo1t/8TFKiku0NZo69fV3MbMhSZ3P1fU4hC68H1Ie3BQe7c7t957pgxDOfrT34mDc66GI80tN9c52HwVWFB4qmjwCy'
    'SeYa5zxJzCzF6BIdzRVObyHg5etVIjlxyGL6JxKZfxdRsDAmMoUC+jgPdBcPi+GrLSWmq0wnQSacdhHd5CeSH//TZMcxubGWGf//'
    'Ii+mM91CWEwqaqO/CJ36W8mMAQXhDbKut8iMHWNYX1JctC1XXeLXh3aFZINosoj2D+Izbblesw0FeW9PJdwFCo9l4gVYnXCwALwD'
    's/nxyVkKpBXNKhQNfVEao+SR+PyaedYEYXit9JJoOodGVqL0q1El5EBWLKiBnvqHhvsqrIuZLxdOePeR+yOgaj7R5eOGGbiDBwrF'
    '2TRDGo2ujKAcMNWGqzuE1BGBVJwyg+oQgyq2TFAaMnT0iLwAK/Et1hJ6sxVPD1P+1ASPaVEFyZPIfdkvptgSYvdN57as+LbAPuRh'
    'C3v3XBPcWHVHJuIvIWEBaPsjiwCsu4Tnnw2G+kuV45VWEM1VasDJmg8roW8zKS6t7JsU7uyGiJ0jXxB4+yL4R76598Gxdt0RtQK9'
    'CSGC9rIU3IQf38CxlWUzr2/3YkjIOHg0zLTpYi6ZOE0xK1yUKYXk/mX5EOJPG2Uh6U+7yo1gMaGHOSVdHxcTbYQxHo2DIUl+XBCQ'
    'CJzcxCXm8Lys/VCdUBgnD9WapU4ojJvQMhnvcpgOpln4HgjGtUyOY3Bs9b4zoBJ72QCBSf5OwK1opruDg4+Sdcxp07dBxaRvVhub'
    'DDKTFTHnJd+/WC9lCZ2eqIB3wUlZGxM4TRdKx8u2jBhX1y9Bm1QenkofhvVzIE93g19RMI1ugk6MjEMiwIXrSX/NLIoVM9H6wKkp'
    'abjlm4O9TFDEdYzRYCo7ojIb5LEqyX9My9C/EhbRYHXZldqRZXnkTsV5Nk5VSq3ZvFqPHJyjC+9C2Nhzo1Ln6tFhRP4FVOfFo+jr'
    '6A5OlxgB07mBiTI5uYsCPvouOq23uoUsBeJbCCTooZdSKnH85RSGJVWZdy+x4yXwCJ0Pbyg1pjZvT7yxNIeEu2oZXnu6dAl08YDJ'
    'MGbIkL+69sH6WxfXHzyGIlvZnhoYyA4MAJ9olO2Wpr1z41L7l7c2z1F4heuvc5/QavP6O8wibb52rfPoCvBQ6L4k0i+SpX11AwZC'
    'w6O/fYmsEC1WXFiYb7t7Zf3Bh6nhAaMkR77r0rn1Bx+1r15GmSPXvnih/e4DYKhY+JY69FyqfeavG6+9iVI2rn1Bth6rXsaTWP/o'
    'HrPIbCsAz1pKBpWUjxYvbHKyMkPch/xx+XihuPiqABK0bl/5sHPjgv0kulUE9XeufcSja1DYuPsFykqJX2WR3/qNN3CzH8Aef9X+'
    '6tftz34H7Ov6wytGEQycYpg1KiU5ci/uKSZdTM8X7FiITmD5rllAoxZHcLaDOaPKLp6FPtot0fuV7YZOxVFuzsMfZTsZB80lWqJU'
    '2tmgVu+ncM7wbjTKhOsK1WY2aAIy6q83gOjRCSOzsQQpK4S91BgZFkvl8/mRgcF4JEpkzz9+Z/2dhygJ+eIvG4/PdW7clH2nPFAI'
    'SlTKKnYyf1Bd10/EO2RTDDF8EKuE8z8+evQlzsVrzwmp0wuo6f8j3I7LcEc27sooigluFWSLdEs6Nm1LvoRVxJKfDcHngS4tm8Wz'
    'yzCzDsFzpKVcXgbQIFPxFUNOqIwNTghvOWhNdSQDEgentuOXiv9ALE/L8nLMmH2fMipWQYGU74HlEGO7N4iBskrFJzqHIwvFYrlc'
    '0gn6fAG2416AsOqp7ZQdgRX6rUuKFCC3ALLThWgaepGnHM3vwkyV6HaUDxca5bQUTWORrf6yU9G8UGlSVMu8FwHXWNgsL/fYomDX'
    '2yCmlCS5O9mrK6Pbk/nxk1auGY5rSEuDksmTrOz8bkuzpiGzc7c7jalVurl69PZ6B8FrUOHxErKySAW262BzCettMxLVeEohHj+t'
    '8qMYQCJIizuqxEA1Z+EvuZV0GRMTCCWkDiLrBzXnOB6KZflpP/p5+/59SefLrjl2H9aU834cUMCEhBLCCfVXzEvEy1Lj3yN2UpG8'
    'Mm5OmbJKsSkZNmm/aTtUgGfAJEfKrRbG/p004BH3ZOmP7fp4HrNODSwvp80VsMbGVKk76WQU3x3VUl0zuD8TNiBkY/GoI2uyBEF0'
    'MU1WY4PgEidhe1DIQsKlmGtFHK6ssexx7AeTZZtpG+o0tHH65DAk2EDA41ioOiPyYCwjMlfoBokc34ifscR8U+xGwl76jh9Jzp2+'
    'sdBSPLjQnFqIRq2Tc1JhBE32YqM4lqRNo4pZ6+6rRtIvKXc5wOUER79EUWRWlZrwlyS0tnRxiXWs4pXsQrhi896EbZysS/ZG8A6i'
    'TtcmV9nK7Qrqw5FO/eIvTtIl2m7S+1Ki0YtAJp/j7JRGM+HPwDIzjGdEY48c50hWwnTo9pGEzjRgUcZPgS6iqBj7bAFYO8JxEqmA'
    'FuzreZIsk3ygYajZ6rF2YYcnJcBjz9CJlBoHAJQsLKyEoRdWWzlQJvvl5Syvym+BlR13SpvV1JjYqmEj4nAr7NXtlJXMcpsztvbM'
    '+AvqZHaJpRhDtE5J6IIEBEO3QuwpuoCB9lrl7nO+Z6n1NrlF3qaoPZlyrBG81y7s6orq0zIJVtCakMAUJe57s8PXaoQeCj4c+4HK'
    'faeZGtJeefAZ0OntdTZtQieDUHX7BqORMDuQ0y5/sYX06bqmNQmxI3x8nx56+mlVjjqpnONSnPOwk47/LV8l3Y2dFm7bBJY0rOUT'
    '/p3yxC1Zro5qgBUrVjLKOVUTjEPPScQoL15dwqhtMZHnDr8gwpbnKXkByY8brWipDrur0h+RHblEZv0O0ednAF76i5g99ttkCyLj'
    'LrSxSvCZFjbA+Er7gXR7xEU6FmCXsiT6MaeVWReyGDVl2LVkebNyIhYuZusEEnHFIrphSxTnSg8yMTgb7iGUjiRVAc7lSGUaYVNy'
    'X/MAFWCeG61nKa8C74A0zyWk9XD2JaFj6bNRnqufZMGcaijJFqwtcfel6ScjTE46KIfgtp3GKAXlEsIYJdaIKH1EftgN9BtLLIO5'
    'Z4HWnOTI/pzdMu7xO78wN28x/bwbXHkfjdPbS/1o40Me7Wf16Tx/b85WZhC0uU1fX26L7DCUrgWacpZP/o4/pWrEf+NrQ9ZoKMhg'
    'hMGd9/f76UxUXzleBmr7VpIyU6BB27gj2bDy3NBkpKKdRyXBJmye10abLOtMp9XsZeb5cd4YMnxcQjrGrYBOd3ZOHawh/ep1qNnw'
    'Z4VdE0OHk0RcX7tIA1pkgY0XOhwjdH+vUcO/LVKzH6Nik/Darqc5RTHno4M9SkEfp2G/MKUcgyelL0bBGyDiSrEpsUFSlSa552dS'
    'T+86Vsu4gf2y2elC8USpUZ9fUn9IFJVsDa3b+k+Vp09UWv1JhbDhokhYKqi0i6rg/5orwwDp+UZ5Buls9sss9c/VqRL/DJcy2hIk'
    'ytja9iijfS2AvCvUOF0j9d3DrjeFWgt5c9yrV3EXGpUy7BXu0zH6/yjK0TuCGQpfOZQq1EqAM1uVmUqxwBkySiVOiYnINbUwf7wB'
    'j1WT6pExFuwwOo8BzcydHqvx4GioU280F5v6Q6OMERWP4cmmqmgc8CpVUXk9GEyZxWXWtIr5zhTx1jg+zZ8B5uApkFxMYT4/xs5W'
    'QcAeOsWnh2imRZgY96Y6iI5H0/l0pdZKQ1+TsFN9Q1PR4Fi4a2h0lJrUsEl6IBoCqkcazUbVqJlXS8lAw1db9Vdnq800dSe1nko9'
    'o/BsBIxKtTJNqoDqYmpuAUhCeE0A37TKuVSp0DiB2w17DSRQFQFGDC4QJBcx3joccUZlH0hV92UGh7IqYcBTpdHS3tJgYBcPjZri'
    '8mB5pFR0iofHTHFxuDg9XXKKR6ziveVCYe8eq3g8s2e3KR4eHRkaLpji5jiObYsrU7P7MgO7U7CRs+OZvSNWz0Oju0fKgVMXau7L'
    'Q4PBPabenuGx6ZFRr97IPqg1ZvU2Uto9MLbXqzW4B3vLZ5x6Y6OjQ4Gbc+Gp0eLYntGZQNa/W2DnqZExmCKuTgEoHRIDaFPU3RH/'
    'UpD6VIrSAJbKxWqhwXdFjhLe+RTdSERP5VIudeg5ON+F4myq0Ew9BReKajTF5r4px42jWp1xoqjQ2mGswDeG4rpmXSlAo3AqTxFk'
    'jzfqC/OG5dF7BBUkA+MpysB4sgBEqN+LuYINjL+JqVzTjeCp9ORA/95C/8wz/Qen+sIggr5CNMZ1bqj9/3irOAvyVD5+2+MtVOLJ'
    'wim3TGZTODU5mO0yoIMQEHvRPR6OxsKs6RUPDDAtcdx8OtUFNO9MNWcL8L40dyns3XQ7f7KVKDjDze3vr53sD/qoWl8QoeKncKov'
    'CAPP2Iq2emEa9hlaHTvG7WTDp/qiY8eaTz9l7/+xY+Gy/WFpONqzcuzYdMAAGhHgTAZsiDgVxiTzUo7PTuV0MMUTdLN/qgmlJ17a'
    'x/XGJ3NLUziVyf5jx07hJJpPZ0Ms53xMk/+RW1oh2LBgWExlQnOtWBHF0YoF9KD/k5X6QtOBuH2lyskUmYPlj/1gnpM1tArsFHTs'
    'B+OZpyf27YIq4xzSGyiYzJHQ7e5VUfg5vaLeH3Zq8j/Gp56Gfysl6J3rHfsBfeSesRp0rbpSV4qHQXBTJYw+XoQnV0LHI+mvd2/n'
    'i5DQ5NFMtXCcpqxWw5ohZw28hdYq5oAgw4/q03gaeg737eKKCVtUmaFLQn2rG4J1NEPA4UYITAanFLZ7uTy9ADxLinJ/whqmgXIA'
    '2msWbkcEPM/JMry59fnFlFaEq2DOu8QjADsuAD2mMd403MKSu7iCtS7euUkgKY6dHto9Nfkf6i8spLZUWX+1auqTLIzjraAF2TtQ'
    'K5x0x4UP1shASpX7rU/cF3xI7k32k6aEzx/+gMrZRgHI7xR5Eyit8I0/b9w92z7/cfv8PQ6w2f7Na+sXLlLii4ftu482zrPkizv+'
    '7iCcMFuu/OockCIL83n+pSCcHnY+LWaD3ctkV/OKFBGmwLZSy7vwz9DYjyUA+0Ef7ZbqsA+2S/+d2K5Vr1dhhSlar3wT+ILu+B7J'
    'bdJgLHL7PG+a09SUQp/eUE4ZprmwdjjF1qfHfmAbDKAnwZU/2PJM2w8M/rt+9SwGT2XdHrmCwZw33/t9573HysDnU3VkavZVpg7y'
    'aZm9mqRGIQvN/ma13rLmxNaeAFTQNRvZbL57lkWrPC3aeOf8YbNlXOvspcw70nKr0OVIoQTPIKGI+yvjxhl3SyjWZk2x2ZnN79N7'
    'gJOMne/CPCaSL+UVQtRDK3SYoluskSN2jsDXh9PFHhV6tDA3/mcyS4gvI0K3qT4ZqI8KuQztuMLslPNmMgJV71xhfr66SMGVmsTA'
    'kyllxBLHJka4xIRd8/PqDSTuA/2fsRekXtCtkZtnkKhD60OLUtOTNbb5G4/f2Tx3ESO+PvPcC4deTHV+e3fjl+huyP4jaF5CELv2'
    '8BK7nLQv3+y89wF6uFJKh/avfmvsW6LAbguV1x5cZjBW2coQY9lXQGq+8cn6x29s3Pl6/as7FhrrMmWMPIs+j5ckCu1ZmNwHmK7k'
    '+utoPfb4Gk+Fq7WvXkbjs7fvGRMty5pLzKBi9lzUlWNLJS5P5/4KGLhz46Y3S3zlUDqXggNLqXNLEX+OrHNT8sMSU1w7TkxCCsW1'
    'qTSsqlzgR1HxyGHGW74hDDhrj0LjzUYR4JY4jmPHMj9rwpt27NgPAL0LQkdaC64ptRmfTB071pp6Oj2RPXasMXHsWC2cMKlNug5Y'
    'rdROqOHIzfHYD6AHGnLZiAZCGL7YjI3/7YbE+0VTh7rqR01WnrKWDAsmDC7rg0pCsbDX5eDWQMT3GEkoWiOmLSR0vQg3bLZcRvQo'
    '6zXLxDXCkDidbdrwBE11Ndhg92WbdfDKY+sKHc4Vb30PKgyOo/ksZkzJUoc2oRxrgMlfLRlNUxp6ZgMeBCDqHe9cPKcvzT7mQPld'
    'bCxUyxRiAd+88n8uVBrlEh4KVYE/uHnAqGsy0NVl7Cl/aeo26VuBKG663ppN1auImmvHKT40TYDwHEmUxL84NbdQbVWc4kzC4lxI'
    '2Fy90HnjT2xCi+kh3ryzcfdS++KXm2cuIeY4/7a9do7IxJiQcR2q269c3Tz7a8Y6a/dX0T3ko9fb59FCVTq/fR2dKKkHQDNrDx9i'
    'tQurUCrd0nC6Kxz37pW1Bw8UYry1fucaWb2+6SEezj6OZzuVN6S4fjkmA3VVgqm8HIH5kmG/ZC0pM61cmDdt/e9eD8zz5pdWFO1/'
    'qFasLpRYUlgothYKVSX2AzKfLk6Kbg5wAs06nGKzBTxCCU6mvojaLyUAo0NWfTYX5lEsCB0gRAD70CQpI7rn4ziVBkvL0Gwa2ElE'
    'sgghonhPFViEqHrOmHdUPaEMNiySSduvp3pssRLcD3Rch4XAHVGIcRI43Ew/8ri4OyHhvQmFFMOJYz+Qi5yNiSHkNmu0EbA8FQcT'
    'sYQ+nATBC5q+5mG9u4JTp04Fu7BZvJIwG1g3U2m+SqFxwgRWQ+L3vw+gfmX90YP27c/YBwrt+JOkLwpmGAape9RpvsqOjKFHp6BA'
    'RZEpum0SqSKbgiSTyJ1oX0K9Mc52Zf15OGI4lGCVygyoShjnQa4lfGjmlUg4EEkMNekLskEfwU5fkAtYVExiE4YnWFSToDLNA+g1'
    'hV2uJN+rdDBJhDl9AxiizgBclqiXfjKkKGfpay7os2fZF6zg09Dn3Grq1F8bbGUS2tewnhXycpJAiGouZuYXza23Plldo3pAl6By'
    '4KmeXQvNxq7pSm1XuXYyNb/Ymq3XhoGyJfn+c+RkgDqxU/UG8PjVcqPVzKSOlOdxReVUaaFB+B6VKqzWjFKk0qITf6VWOd3frBdP'
    'lFupZ1465OsMkNJGc2MUlJZa+vNsoTlbrUyb33OFovnRas1LlHT9rTIPFBrmotdf0LZF/6jDdxJjIJhDxyn5/hL8dNQV8mezXAQq'
    '33TGC/B+si2P+fifVQCdYfO7WTV/W1qR1myDk3mYLxW89zQ/Wht3rOb4bKFZRjvyl+GRLjdbP4ZdreKwVH+hUYXlsPW3agDfmvMw'
    'F6miuSpVTpVfBUJID1pBm/tFVf4cH+jBepWEr8jhvHz48NFUnrYrHew6WWjsglF3Veuz9X5UkvczOOH7duTw/p8csOo2Fmqxemgx'
    'm8E9xAY/OfDvpna5VbRqKwNbAD2suf+Zo888f/hftpqHasGRKNi86Vjt/+w//OLBQ1ZDhHYybqYBTzcKi7uADZ2pHNdNXjz83IFu'
    '05JFkH2Dqh+fV/04d1woFjEII6ULw3r7fwIVNQxkXn4e9gFv5ZGjh1/GIVm2ePjlQ/8CTF2euPEfH3jm+aM/hh9LQYNTgWVTOpjO'
    'XcyhF6UC4VqhaGDFsKWcL3S6zGHw0rAvx8tRim0XtVqDNKiWv0VEd1k8VOQRnqu3ytobAWOwVOBxry+0yFu/Ujheq5N2Vas20KoO'
    'F7o4XxZTycyrryKKf/VVjdwqTaIp4KXnKhFd2gwaOTxHWP+APU+r3z7YF3jcsGJq/ZNPUj9qpjZuXZS/2uevsfnq5pvvbNy9G6R+'
    'lOLuMwjytbosPwP3olYXTFuuJk7n8JFtZgD1avX8j5o4CPdKX5zXkL8jRsble1SA0x1wu+fWHn6eD1J9/1933/7lRnEu+K80+Pi0'
    '2rRkSfO0xrLvAAPxxg/WM+TCjufo9Eg9M7rWqBU9PDazc45hY2I2EJOFACFwCTeEyybBzhtjE/hf7lpj+6f8C/u9qruqH5LGsHv2'
    '7OHgkVrVVV9VffW96/swLidnvumsVkrzxbWRAAsNKowB2xhgkn6/719JdLnR9EGMq8r8vO4mJniJJowPTIOSrAe/hw4bOyxYCfiL'
    'hxz+MF3HT80G/usD82nhhx5IfZhzET9LygXL7gcXsWgjNOPLDvhRjogzaqXfRwHpDz8bfv7u/s2/WrgcBJW2BhriYqhAJGg9wqKK'
    'wYgOHzSCxtexsXodH7ymeSLrXjtoA8tvmY5y6YWOSGOw3ZFkE4DO7d6g69e8Xr3ZrD5D12NIpKlhDqzqShfb9Jhbo5CfA2UHlqli'
    'O7qbBo4vIlmKbx62GKfFP1DwUSfnFLr8wS7YTqEOHGoDZHolt/S7V3SBULHmQrNTk48JV5pyowmq+Jfrfqefvuodr9czzPAMJfA3'
    'zSd4wirPTOEzr30lx+IzigHcBj9jk9kpkgbpJxU7UCDWSfPSMSgGnzlDtUzcgQhagL9tDxZH5FebtkdpgDI/EI5CMpc9mF4ej/y6'
    'oMIMWi0ONu3aq17+pWL+WA30F1JcCtoDB92QsiThWVR7LhKdueWyphoJECyD/TYWJKF/RCr4nbfv3b4DtGz/g99rWq+5SgqP4rgj'
    '42fOUX0CdW236M4d2wsnOBlwbAGwSv9x9a35onXv7288+MkfAVSkBLfe+F8UmAyf4cP9V76Ef+/d/snw+v+4f+cbNOd/9Pn+Z7+F'
    'z2gJuP3G8OPfDV/9BWhdww+vwq8Pr77/8Nob93//qTZlfeeidWfxrgZi2gGWPoHfM6XyyBm3+xtXLE78lHabQ3ynKAoiEVcyo3Ew'
    'TSwn2bAqrxTw2/izmgHW/d/dGt74dRpAsAY5GYFVKOsxoK4oFQOPFU+b/K5IlsVHGB+hfSr2CKkFYrnJDPTf+1thBIGNhPGo7Wi/'
    'U7ZE7bvKUZ7oDwkNnqa6Yx23pqbiIUnYJMeqSo/GQzlPBV9ZJet4lRcY/s7OzEzNOKPxGa+pEv5ZvM9c7g3VhGWLr17z1qPh/uuf'
    'DD99hTSLXuXoUWxf6G0xDj/49OWHv/jR8Pan927fffhv7wEaP/ji1vDrH2XgcEj1j9oa71BcPLS5cCbPKrMqDOLs5UR2ZwsDhRc7'
    'oekJBW+ztQjsWmsiqhivag/6G/n5fK+5aTthMNYZJBNkq/IvY5Rfe9MSTcDuaYIthuW4tN6eZqvCaud11LFpA44Cky4Y8T6ge2NM'
    '4CDyb3PAD4gJm00QOhasQZtS6Pko1VBUWM/aHlBIch8dB5zeuRAKF/QWTHjVIyTxyASBS7YaCUQkSHl8fVfJNs6aQSdUT0nGsSoN'
    'L7aDnTaqDd6qkqvWVlGuWosGVr2oO74UOYvv7EUGIJkVNhc1ieBSk4VDs7oWMwGhxCC/c+NON+gHIHLjCcNLaxLXEx29Sy2QC0i4'
    'w4scCfEN/drN9sA3rXo8K+zAGEyTFXf3HH7GTZPAEsCcVaMJSndV+pQpNuxk6JHWWqCnha6kBUXFgaawdrSsxwYKxdzR48FYskNI'
    'OfjjavT7GlJL6j4FmBSm+Obr925flXxzH3yGPO3N14fXfoNew9ufM6fkh3KD61c/fnCTUu69+6v9P/98ePO14bXPJBVBaEmPwZ8C'
    'o4BoIDM3G0nyMFnfa18Pr/8BPZEEj1GbHtM7kDEeXaI/OL20vGwdtX5wBnDJ4inx5f79X1/d/+g32OzvN/ffuzV881OA/t6dn8pa'
    'kGfAoH69QQvZ3uouaSgVC84SfliDveJcwhU1x8QpW9uL70Li2NExT5zOcJujE59D5o8HmQfVTjCDuOfQ3mPiD/ruJBiU6oCgH/u6'
    'LjtnjzqGS+lowyZpFJR+/MbwkzeSHIZ7ZLZCrn1rGTNBVKJgyVoNL9TVangbfMMlW54+Pj4tNNZRXxE1WGILckiNqLVrgVBRv1jD'
    '4OcaG2FYY3IS3RS6wU5tw6uTZSzq8nywk2zqX/brlGL6ufOLz55ZtP4lgAl5rdo23kD558XTtjP+nfVB70pNpdjAS00jXmIPI4gr'
    'trnLT51fWlxZslYWnzy9ZJ16xjp7bsVaeuHU8sqypUiilWs2rFNnV5aeparup84snn/R+v7SiyJFWytLL6zQe2efP33aWZi4f7b8'
    'Uvecq4h70kfoBByxHg6vhnGTxIrjzExo3Eazh8HvfiPZQz8YwM42LADw9KOAj+5NXpsE2MKTY7Aoc7j5NI0FgHKDbDgBsrD15A8g'
    '/9f9R56JGhBmg5NKzufbLhVVKc3EI2vx+ZVzp87C+2eWzq64nNErNljKMlElDHMtUb6faNGjaY5CqPRdbLbjvUl0m/HQenrpmcXn'
    'T69YttzEsdNG4HudvcSGhm8X3cYgtrEuWYuyRrMztuXU2aeXXkjdlhqOcO6sfMv1pHjvwD/ABqs7J1bOWDQdh/A66rc6azW0r2Wi'
    'aHJbDtp9KKnnUrAjHReSO6qBRN2oF52DQpN6Jg8MTurBdScC0rbNoKdFuuMDYuR2c5PdkhVkw+jnQ988AUnKUhdUow7J2CEDobAN'
    'mBBqWRhUEDT0WI0Eezt1dnnp/Ip17rx16tmz6OKA43EutknLS6eXnloBQTdci2fOnzvDRHkk7xzRebTm0nu9QMvTVTVw3XpBrSiN'
    'Fr1Qt/4TkDDhCV08Tt1Cs1HlDnR4UBxBvRI9M0pLq1higrXZzoO+mpjunUY9cJP6QadZx/bkayJ7MvshC2Tnrm35l3OlWSe0e0NL'
    'VBkkhQF8RVvv3oGXK9zZHyyefn5p2cqV3JMOdJyLDNEyUcd1RuzGhceff+5pPAfCIZaXVlRAc0g3rX/+3hKMqx735PGFx1P6rQfb'
    '281+Lv5LWGUKlp2WOva7hBRF2FUNFeJEG97iEQ3CtYk04rANZT3odAdt1JeLKLYqQVW9RYJqihlZM3Uk9kiwlaUxQswQBl47wMSS'
    '7RQoeVrQ9nMOJmTSR6dJpQytNJlGk1It7TgkzWOSBjRAJDZTAKHeCkfcp84tglL11FIuIt8FpNOuTUZa27EWly0i3NHZtU4vPbMS'
    'HSYh+uf0b3SmqjwISBTnzj8NnPPJFy0SgtxmAzBjzdT5BWCeT/xOVLBjGFFgtl30StF7adNUy52gOxH68KoTmCcj+ORnPCY0KOpt'
    'cDrWkrew8FdcFwSnatkUZmyn6efUkCUcAl2X7hM6UrjZPuY3CNpZqCaRFEB1vPLMrHakV2kpQkx1XF4bRh5Hi0VxCkB3Gs1Nv0dn'
    'MRoaq9CIKiaJuwxlLDw4sYG0QEqkYtBgHShvTjUodIJOTgicTm2EqIT464zfVOAnIn9SFhiSKJUMSTKGCHuuEt1EGiMkkPHCDQfk'
    'fBrw3zp96sypFWumaOt7rdLyRZanikVgqu8uTxbpSZU+IRVn/lYxFt6ltAxdfCzlVpKYYjNs0IQ/kNOT0SDsTaEFdrjle63+lgKJ'
    'owmcPX0rtwdof5TNpLQ5anQqUo+2an1z4Rn69kFZLeA/fD8EHzZ7bNcmX1NbV5WbG9QvW7tCaMnqEAM4cc0xYfumcPPhF38a3nhv'
    '+Po7saSNWjpFM0t3gKgWwcCPbDPWVrWrav7mGDwBOb0zcVr3dEWDmQbJZBRhqtsHd8uZyJAn5qxMN49OPvHGGVtcNZ+5EiHShlPO'
    'cPYA4HSwC8dRjgw8vZVUgSbN5PjV1eG1P4+HlESh0FlIa8jSUbqJNLaA1DTym6U5FBfz/yX0Ke6Wym6pWERXIr052aKzy0T5E8v/'
    'cfUt6AM9iklfYoYX8fXh3TsPbt7kGwoP37+x/8Ed7jR1QZhWAvqtCnVM8hpttSihZ03IaCVNmeD+4iYkCn4wVn1cD9Q4A6as7YGm'
    'k24POnpL7kypzNuDpHNibHtw85uH7968d/eT+6+9Ph7nJIIqJs3L9NSDNUPglh/Vg7V0wZ5odqgNxN2/ikLQzw5L96wHCBqH0j79'
    'jdmUE7xPZPCQ/6IUzreOT+oCpCHey8xN8Z41xf+MkZwNS5JraY6tHoYl9THjAamQPkBneZTuK9IZYXPRZtcKMEBFgVRIoK1a+McI'
    'vVN29yCaRh1RrEV0zeekYQ+vvn9fmfwp/d7N/be/jGsinTRNhA6DzhO8Sz7pDPYoB1JE9tF9hIuQosaVYysdXjCuZoozR3TJOtzK'
    'k7iV0fiwhZpqkFjqGGyoy4cjC/OeiPjJPQngwdd/9fAXn+g8OOVkqdJwMQYcBn+tJp6CdJUEPhejI6pbl8LSQ1pCznR0ZYQV6fAB'
    'EOf0w6k5OrS+vYOwD3eemIfnaA5dNfZkzARWj2+cUGQKMRJ172T0ekrsOmYGioZMSCy1bR9zEpIa+50qRAnk20velkfYkpsZtkEc'
    'xN3SwHROlOhxhCPK04qIMtGCYjktxlGqAG9e5ImQle/OYsX2V9+4/9U7D77+WcpyCwcz1hhRKtWdRmLznvVf9WU/IA5IERdVQjO8'
    'fjT86Uc8p3u3P8ej9/IH9NM7fAbDlsMbt1ikQGmDqiqm+mV5f6shMq0WY8yblHn9xJIWC/xIFP7EItELoUOfW7lK4z3oGpjXunCW'
    '2g2uTHfzIQsTRsGc0PHutzCWMsx8ABsUxgwhXSdbp4/XPkFWwvD5HurX6HNsJC6fdbdhJVTAnLYioq47Tjh/5BM8dYm4+yeefzyc'
    'QdxC+vqqhylLSxK4+j0UvZvMY/HjbBExMuwWPs/PTgMZodsu8vAwtJosMODVL4Zv/IVrHty7fYdp0vR0UYrgpR8RAjGaTBi1Mk5R'
    'yGQuE6oK6n6UTk+yqNpT554/u5I74hj2ZdOUZp2oWuXiZOu0/8FVICwsWeBLVniDMAVOurW4RSiHdNh21WWlk3wpqWawjVjw8qDT'
    'wktAfop4ELPUNQ0DXGR7i2KX0ADnxexuBzbUMRPwlDHdgjcBkif0ST4BspS1ePZpdW5AAMVvYw2J0k4N9Vj15IXHgc0cidg9nkeX'
    'jpuriV5jJB+1hJPtrHbHk6nt/gfqyucXf7p/9wbWOE4QJOYszEdCYh1S4Qe3fhNxdINeMIUQejHJ0Pd/eZtCdYQ6Ys0TGusAADhj'
    '9IjIV3B+6bnTi09pvhVy/ipXTWhQU2RGObbZj+2EXoWTbvifY2crSpzIWJMpFKtS2x6Og+SPCI6mnTlksHK+xeQE9zWonZiQQ/s2'
    'doin4VCChnJQ+Wlkr9soqCrgU/xoMaBXzWVMkU7XDjKLyEH26DNIWZfvpNuDaYSh9mLqgmLjR+KTVAuz4YlriZxs9KB64gGXbaQG'
    'eNAtYJT/Tnf1O8D2/++xj+X27xj7+iDC2smL6plSQ8lwdzAoSOCq3BPBI16UE8yI0dCft6aKpqlhMrb61588uPVzNsjs37qhiniR'
    'GDW88bOHaCq9uv/7j7MYlN+mm8UIgsvO7/j/BLSxPD1/rEcBxcu3b+2//jKmj3kLS5DFAEi6pCPfiYKJnSfK4UQRTS4pHIpxUfhB'
    'xM14kUXmiUVPZ22WKb8aeyZxSyiHRVFKoYPdAVksKedi7YFKaj7DNMd6CgMlHqQCwrJ9bXpcCjveMCIpTTIYKx3kDrbA4YY4yctm'
    'GC2hbyRoEFToKNulmRJd4KYEFLiZMQTKqWZ6/BLOpAgmr9EQeGiVv0t3HN0huCRmWvqwapPT/AS9+0RSWeR1i57h/UK5XhCzgqQH'
    'YnCsswolBmIlo+JO2nqwdKfbDLqxkAvqW5QNTkfQSzr/R1hPKWSNjk0Y3yY6DHfKRF8hzwhdgoBblYZrtKk7cvMVIYgliNQME1ij'
    'UstS16PpS45qI83DUWbweMmk4w3w1pF13s+TjEtpA8x+Q4s8SIcDDFhGWztlv2v5+X6QlwsDUcZoMsv7SCMsTPdsSfXLiUVmPXYo'
    'XMu4vByeQg2zxnJn4aJhp8hHcd+qZxZfyOEH6Dxt17QxwvPvOAlsTz2VMWtF/DActxS7G3caKF61Gt2klV7wO0ydLkZfCu978Jqr'
    'rCjqJMpvbUyXkrg6TSZbDMyLny/ClhhwxFYotAkPSqGJ9TR6vlSAgPZ6lEy6PYVbhYqVsjOpjhPLxO1J57PXJr4LE5khsRQC5jnu'
    '5mQIx0HsZorJN0b4SAzazR/Gu2luyPiTBNporhbzLKf6LuPukfjM81EL4KgMRWjIW8twVSaFGYMURvIM90fBRRFjUxbVSRdZdpQQ'
    'VJBIjzQSiy3+PPG+TeCuMoIrR8jaJGEaxzll0qNtO4btj8+reoSU+fgk25K5dGP3ikWQ2Kqmbp0zgW9zpG0iWkFDdnIyVi00huhC'
    'zqCtRBz8MZa5idYuHt14fLwoMFLRMuRUkceO007zeHPWEbZWjwwvNbVBuW7CfQqqfcs+Y3IBLsC37NGYeLNBQdsooIfxaZOEmpWo'
    'oKA9eYCsEZdKUp+6qdsZgATR29Ki0mhohQScm+k5oAUrp5fZn0/XpbCgAb5ZtyR9wwLIDEQ2NihHEGBDo9kF7kJsAasK+e1LzW7Q'
    '5lRw3eBy04+qRaTfO9eiIhR0qyooIhQcZHjia5yDCTkmPm22N4Jc7GK4q99VR8imp6dcclNU5WVMVlRbXjm/tHjGTD8QDaR7h9MS'
    'WXir02tULLXZq3HFkkgKDntxJr2pP/zm2sOP7z7491/v/+ubw+t/4Gvd/JN5SV5IkMbu4nq/RGy4nCwYHQ6ng+8FFhZ1sbCqi6Xr'
    '4liw++2/YYbUzz/Z/9v1/ZdvPfj4s/uf3Nn/6Wegk2Nqg5ffwoRDXJcUOFB6N7aWQCUJoVi52T+jtjgMndFBDwhxqlajX1AJwgqY'
    'qQo/cPlt6V2ICZI8aIs/v4QcQj43/Fbfy20FQCuq845DyUs2SEOyD7+YP7ydP9ywDn+vcvhM5fCykbpWrVg0y6St304sNb6hfLXv'
    'H+5duNBWuaHfT2afhF/DmuKHe//46rpeWv0fX70Gvw+vfzK8+f69rz/Egpzv3Xr46Tv7v/84zMe7f/0dtJXc/Ov+u7+SepP0LpdA'
    'x7y50vKX9778iEuu73/4mwc//q212OlY6PG+8Yf9v9wd/vfPyB1xOJSiFVvjXdATJP2/vWuZu8Cu5PCbcpqoB+O24d7da3wYeOPu'
    '3fn3+29/hA6Ya3/mJc5YP0P+D79qAoKxwFl4R7DL/up4F7Nr0auuvgZRH2Jv/Nnf793+HM/u/gevAXoAkiQ7F/8RZXjm29Wc85nL'
    'z+NEFf2+gtcXKFRORatF2MFPUIolsDByjcGzBT54EpInm2RwYLvwcCpp+LH7HkVTr9qgALSo0zpwoovwSHKkPWHZlIstj/HoR1Py'
    'JYoRQUqiE8xSki6/AswAr9Fg9m556ShezlgAxoe8o1+ldBO4as9j7aXFTbymXWFamsclzuMSH50pFNUwxjmRoEhtowSKVXtx0N+C'
    'eb9Eg3K8/5M+6Dddiy/+xLqI57hHTTPKg1igzB9Phb9OyArltvG8uojt7YQJ6JL5Xza87WbrikuMF5moa6mcDq5VcxWzgw5CtmdE'
    'lHDnwnj5Ty67S8d4kY1jXJx+PvaTuuMtozpGeXRZDhoPR++1Ckx8aiAOeYMWVnZpczqSwk7X69QEMOjYVYGaag2rsTV14nMzjT7a'
    '6F3O3Jiznzu3vMIpZlxMoXelGoVgyokKr11wnhPbcRXGSILonmHHlHR8VX00kIjUD7poiOOhsUp+otwqudLs1PyMaSgJG4hB+bG0'
    'cIhD1lmq/4F13XqYAB1otO9txxMEutbz50/3JKqaZCnC5h5lQEDhJ57lOUM4evDN28Nf/iult7E42V4MSiflnpN2yQrnnpgltBLb'
    'D1JmvooQEijSk7U2TNLkukKMzkUxglF79OhVDjC34avXhje/vHcbE0gN37z+8N9+ZOaql/KGlVT8wgBbY6+5+JOeaShZOyp6K8wV'
    '2ex10PRSQ2ZMcVCuZrDGGq1VUSCSZugDmqAxMbCFuTB1wZjNtzhuqu/jSKbTQ7teiLaExsA/rl/VwvAW1qSU+yrLvCsm3PRA2HTP'
    'iJjlNRt/eL3OlcmkXRO55LWalDwxXY6nbFERokVWOBSe5R0VKUvpQrIjEdNyKzSSy2yb7sCDRQ9F5hz8KiYdAbpaYgNPIc1iSz7y'
    'SDgKp2TsT9KQy6tHxw4187hYaZ2wpmYTNCsFsx7Ba0sxl5geTgXksBMXs82TBPXgmx/DybU17zwhnYAotwQzAEvq8wlv1SNNRjkB'
    'w3tuVfXhidKkgGYAaYoITCUS5gW9F7pvZ0la0QZdk0L4+mLvDCmEZJVboj9UlrKHzyrxvvB9rDgNO6XpxWp+Nrk5p8SYHrrTo9NL'
    '9/04wSX07sSz0l6uuxrFVkZ5lpgl89onf3zwF9CyrqOgfPcDwAy0nWC46luYCHH41SugNHN+zyiYTwwzIOflSjOuNVt00b9A/zir'
    'WFE7MQ34Td03TKOdyeOciQ0nBaFPog9WvxNC5zK5QeHhRJSYACNizlVJadrrbuQol46LDgkXBYcm1ibXeYg8Q6oonyj3WD+nsRUH'
    '1gzPdiyxKu4evYK/2wWUoDFleKHt71DBXAt0x16FRQcFh3ojuunqxm7LJm6+ymQohRBNSW4yRTNTczHPRTQzmg6+FOUZda2SXOGO'
    'nfjE1I+HHR2vpi7MXLFI1J3mDtuC7uyaTEBAzdoJx8xMmouw3rUW+/1ucx3QKpED2OSJUeYmSVKeS89dHhUCZGG/JrUg8Cxj66Ml'
    'UKd0o3Ur2KyJZCam6yOYXthJZIU1ruAPOolL0b1BB7POFfjXuOFUVxgihWPWvFzvY50nBkIdlkT+UYzsQqE8lsg3lt4/HBfJZi2U'
    '2RMSbdRGasSY6qs7Xn2dqLPTVLHbpnsvlG0MZ+E4o9/14DDksYcup/ZrB3miC4kho7d6ifnvYIpr0L+afQ4gNyMgRE/2E5sJ6AuL'
    '/P2lF426FSqzrHH5l4k8jiUwsJT+Qp70+EVsEL9ZitVKQLdujHrxDCeZzH+fc1fbaTscJbgDIERTSDufdBjVoHHdQa5II+pNF0Fz'
    '37WJQFMe+D99jAHBZKzZ//C/PfzFm6g27KULEaaKKg8jaqCveyOoPbu0klh03A61JNHWpMwXfhvjM1K+NUrBisoXp+23R8192pw7'
    '1eHDFIWfvzf84LPsiWuGMoMwZ/HT+E1uSs5foIQHUbZTJ6M9SKJAZMmkQ9T2CK6FkzmpMnL+eD7sVPEnllzXNHrGMv3bD27dHd4Q'
    'oZTt0Cyq2Cr9fyKqBRhKaJXDE1nFAti9PmA9YMdGa9DbomTi2TOZMVFTOttzYmiFVpD/G3hlnNhz3eZmUxIOiOFQDqPGztMO+lPL'
    '55/Jr6g882iQ4f2c/ITyHbP9X3388Levg8LAGkL8xuQ4vE0eFi7+YvP90jjccQ4BpEkEjgWbov+wizjbGHnyisacMJPmH19JuZxz'
    '8PPXIr4jslH2TEL2ZBdtJz1kpQgSkvSG959Kx8qT3jmDuTz8+TfDO5+OumgkXD2evKdL3IvMaTx4mPPdGZuogFNtTJ7oIXPZnclp'
    'GtMyyfjBAGTStDiZ2rWDiyrPVJJaGUKjKhnhWoiDoj8llLjJgKVsnK0WFuzOOSNw9JiOowfT6YY3XsHSpKMROk6V/w9NZCZ22ICI'
    '37vzidI0I7oBED64+YkCUsubSobrnF6lqLCiKs6caV4+1XaNEkYFLNK0TBZcflUhY8Pzt4O2JEpF9SUyQqTkYyWZHP4cubgTl85Z'
    'cmwFlMAnKn7zJKZs9hvL/rbXAYrvG8Z9JauHo8T714NaAiyyU1OGdong5W+hayIR69LX4Cp4daqBmFtvwcpgpnHOC5vgNdRn0uQ6'
    'Ga1Tc4oDnAA1gXOoSWXhnTYLucuagAwpSeZ6yQZPsGwTz0h1OWJiSZN29lyU6k2CfisIOmyfjqrpcqkqQK5Y8arc6XPPih3W1Qpf'
    'CRgce4tIXXlUCZGtICp0fKRwqHBujOk1me6tbI+5aMEumWea3V5flWkkrm5RyEXP8oBmnXumAIp8sN2sW1jtGRbrCYmigj3sXsIQ'
    'Ybz9jO0L6SOoVS7Ugg6oh86YVuwSAn3Rv5gDelYe175G2TUYpKzOU8ye+k2ERCbDsmuvB4BL/S5WlWzQNYgxPacbYQ1ICQu9S16z'
    'haiW1lRhBcba6UaaZEvOxlXgFCg5rl1EVjE3LHid3sGkGsJ45B3BlIxpp9K8iZQQKu6MxSUogkJM56O0ETkusjiSM4zLOqHkKgNm'
    'nIRHVmUy9yPsa/SW0NNey/c7ubKj13tppxGtCYmPBEBXNSEtJpZmuu0ml1yimkqxeZS0eWDEekh+JNaNxBpXNCr+JegVBtteDw59'
    'UJybExjCenaa9Iw19oyCIKt2wFqaWLqx6mBh+2KjidnbMdRdVbOiKh+14KJ8pTTrxWCuWJTRVC09SiCfo+KFRy1VDVMyuWvBdFyz'
    'sEC9mpVj5ZdBG8tGhz6PUH5hoSoHRBNQtBoyJ+B2IJ9Uc7w4jityFKOcqjM/uq8QZw7QF510JfuB8MvAwyvK7IokgiU+bYqwXfUt'
    'WEHzlWIwO1s07HQkKNKfGlZEx0EEN5ooCXJpP/Jg1mqIKbWaUicZb7juqKphjGVHcxuDttxIABrt7EJrDJ5CwR0UIhvTF9cD4GZE'
    'puD4gUZRpdS4XMacGqgumJQ1cs4unKBdlS0Tui1Qncdl7gEpyam+v52LenSqVegUy7TaJ+VvxcY6xPbCXh2PlepMPdzTR0UN+ork'
    '6+ugEbvXr26Q3IizEfC5RCzn4MocrBHUBxgOVlAfllo+fwcNDfhagXuhfymxc3MjJyM6NOXEXHuJubr0tqMmtheBSLBw5xGINEwI'
    'FxVCWva5lOhiq2UUwM3X2QS8hpJK0F3y6ls5eVQ9sSufuDSzTGHPGdF9zt72+x5VBFY12fNUV/fC4zDCSZxa6JPAtClkIbC5oO9J'
    '+1BjpnGsUYKlPVQqltZLHqdRx+nyfilccQwMwuN3Jccb9x3MWk0aBN8ldKCdhp3y22g0F8ONm3OqJxggY4HcPh5tSd+dAjDOAzY/'
    'BJHgXkZtnrYu8PjCZ/h7EoCnz50Rq8ppaI0+burD3Q1AQa70SblfILVY1gSHHdEfRt+hbbTSAz6J0WwuuQxh5yMME+9j2AucRMHw'
    'J6+cauRstvnkIxcMbBrM8jHxhvAhpCXhJwXkGTKLKl+e4qDUQnDxpI32tg+/IWMbumoBFfgJf2WnrL0gPZHSjLMp9IPNTRDoRPN2'
    'H4v162S/0ezlaa1k6vo7uGX0Lx3R5OrJ+YwWDabNnVwEgletarQqjg3U756T22m2G8EOfolobL1HRPboEWs52Ohb26D1dIFhH7VA'
    'ed62SHyyvEtBs2G1kOWgONL38YKVx9do1rtUHnuz6/voIqgj+y1YR45eaGNQZUbx63x+fbMi528hn+94bb9VOeSX/OlGXX0vQ4Nj'
    'jY3GFDxAXaxyaL1Rn1ufg6+4p5VDUzPT5SkPvm7D8W5UDs3UZ+dnNuA7wVI5NO3Pzs3MwHfvhwMPvjbmirPH4GsXGx8rz8xN+wso'
    'zG520bwQgkMERPUOhyh9ChQn52b8BsS4t7UeeN0GBZvuPvoo5Evwu1kDNZpYciwTDAwxzmMplBEtYIPzQEWBlWY2AqEYGMTojnqg'
    'Z2F0bh6PeN3PbBeAQIBzyoPW124YKyPbvx50+YTTAsmerweX870tD7C30gb5dcSC9QA9823vktF1faMxWz8W7/pYfXp9aoKuLG9X'
    'Xpkpz5amNyZ6ZdXrNr283L2ENliJHVFfBws+H1svh7gwPd2YOshUQYEBFT0bC9H/kJdGxrgwjfWZ+dhyyEP5tlHcKPvr4wevbOGO'
    'TgQCNzUAmZ6daUyvTzwm8PE+VrVFvmh2BHRipu6pjubL0+vTU/Htrh+rz49azgRrCXd9HlZmesSbcAab7XzLuxIM+u7oRkTaUgjC'
    '2N6p9LqJ1Mca5XrpoEjNvaniyp1sgJm2TNQG6TFyhMy5oygD886D9LPNPQJu93OrdMmKHGuA9WuOuS51v9SYMQllfLLT9ZLXGDvZ'
    'etC5Ym2VxuwMkjdrq7w7KWUm1FYaf1Yj+T1G5xpw7tXE5ooz9Sk/PrGN9dKxUTjRAKnhCtBQ6Db77Mk9RW5mos56Y7o+Nbb/dc88'
    'ZfPFY3NzM6PWRA0Zf3Nuan599tiIN1U5sHwTzl+BCiVmTezQD82+NzZGUGTG0EoFX2h0g47xZnluqliem53Nfr2yAYJoL48559db'
    '/i6cbxRFTJJJGfLHEBXUbUHqiUgMyvEkBDFr2mORzL+MlXJ8EK81uewpDACpg2i8BWesYQF3aW6S2TZ0yFkgHw+41CMWxABJU0U9'
    'IrQsjRW2vV4fx2cepb7tolkIKFeF/BmPcdEUr91fAEEQwMV5VIoLm14H/sV7jdB7pWgVrVKxc1kh7XoAZHm7UupctnoBRtxe8ro5'
    'FtqwfMDoNeg1X/IrM2XoLXV5SuVysVRfUFBudpuNBfwnjxGPWEw1T6Jo5cLj61261Y28l1flwuOxlrBrg+12r7LtXc6LMojyLnzN'
    'Fd3SRtextF8WPNjEdh7WartXQanW79IylKYBVG1xZuH7XubyGghwokAw7hJUCHaFvlNvO81Gf6tSnLinUMyJOoNvC/8y6KHxKo/u'
    'kYqPfYPQ3mzD5vEAQLQCfbwFnDF/LhWLhxdkUryBKTvlsHjSfAnxINz+yyFq4OLgKk3BX30B0cjfr2/p8nbqGOubzqgl0CQygbTu'
    'teq5bHCtPGGqo+FuCWHbCLCgL6JeqWzuZzFjhnTupU2pUA4xcqPlX07DFbUTgk7yWDsO/SBo9bTtE6w9cMfYLo9bzds4Yuuifc9u'
    'pJ9X0E13HxkabUnLs50QQ2DahB3aOEyq/N6jj0Uf8F5WBf8hBIQhmUbpyG7uoZIteDFIiXR0xCgaUGpioYKz2aYOs8AVMBZIY873'
    'gIb7IPUQhI82cGXdR5Pqrpq/bcuez8AoMi38iAARkZ6JaHTXazQHvcoMnPHEGWQIUocsKIOFGjvxMinbGS+TfWRXn2wXB8pqmTlG'
    'V8AjHsvokkff70F2Io4Maf2lLLDCFo3dtfyN/lhmp5Dakptnk4KqzR21PxlTZ74msYqjtIlGaYhmomN322stoF620Qp2+Ax57Ss7'
    'W37XHzGb+EKRoprVOAsNoHuheXmUX3ZV3dcKVUlvXvIXXso32w3A5XJRn1cJz9Q4WjeWkenD97vNTdQoH42mfwfA6Hv6yLy3qJAl'
    'FTdTCAFWQ4DdutLyGdf0HUKVzpmEXS+wJ77SCSh51ALZnDhvrLJfxNfZ6l3aNOVOWcLydETI6DNRsoxeKpX8jr9+sdnPS5aBPMcr'
    'hD0zUsbf8jYAShNxU/oWU4WulBmHSH8UUcB4L6nKQ6Uc2x5+f0HpFsHGRs/vE480zscqRlOsnUggbWKP2HrqLGRCHz95YpQIz5+3'
    'DuBBy4WuEBnk2SRooYxoPWHNo0QlUuP8PNLVUIpUzS5RMrSyLnrNhSR0Igylc54+OUc3krEuYpUBDutQsVicSp8emijJ66BscjEM'
    'jBFOk67OphFSNa9jImpYBLESuIH9Tk+mIo0gv8lpnPDctKdsGRtLwDRJX6M4U/ORfFaZV4JTyHnMbZmNcZUEoSBSgqtJO2iJ8XjQ'
    'XMCWeQx5lUqy1JAeEqQV5KsxapK1BmJ1zF6JpK3ROBt7I9YwKa5kjwLrTZdXdwNgp83+lQoKlTyDHa/ZR/Dx6mqeiCIC0tk9yAko'
    'RvhUwn2JpICSiNCx3q2Wv4lm9Zh6Eyk/M2m7Z3bEOYLHS+OhUBWTvhlghfHzcTile2XM3dXxUNMTWIAvm6paKbOzLUCXXRmZ5DNU'
    'dM22QcecV4opQRkIun7H9/q5smtYB5xQokdyp9gULYE6wWrYQzQsWTsTEEy0sPOmUjpd1iiLsUTHEiL+XCbZLDtxvTed8iQP4aj5'
    'sD03Lt/qbL00G7F1+hwqKCV9x4vRDNmPmE/nsnEACoN2GGQXHcS5vfjhOKFjCaEuQFDUSIy1NcUyq586mz2t3a7JH9AG+E/bfqPp'
    '5SJWODeLlofdCcxf07gMo6xaoUXLuvA4mj/g/2zrVqZFS51YK2nz0LXzMkoeBzNCGWYnZevRmE1ccCbtI7/u93dgU/cmMfcQypQ0'
    'SjYbn0KMTmR2KjY4WItjSUNQy+/3UX5FLEIyO7Yjslfv8lRR3FF4PmWSrjL1pBtWRtlHZuOy3yS8P0Yu9iZ1uri6o2dkC+Xm0dkL'
    'gZoksumoCRhpwf970WnpdP0Nv4s2hsag7jfy24FogPjV2T3iHhFVEz6w5N6rYxAq4M6Wd6kJ1AEprma2jtIXk3wR/bJHZvoCxvd6'
    'XbzhkVdRwIpvEO7r7LKEkh3pIQdVDA38pnNXFqNOyvjodNJWNDRP7ohFp1jUhcqZ7H46uzEtOUak0oRY1TP2ycFK0t1k/L/8KPYt'
    'YePGcFJqZJw2M5HcNJcqorJIWjZE0rgQLCTLYEtzIhCnkvgik/g0rAqxiDqYFcM4ug/2YistDH8vfUVCMhoTCYom9cJ+xYsUHkdO'
    'bIZupOPk8IWeLYpJql54PMYcLzx+4jgLjyc4R6PKjXd9eOMWFq+6+to/vnrt+FFpc7zRvJTSlchj2BlTJU6jCWyMvgH/Uu+ED5oN'
    '1QPPF5SBFrw/vPYZDHn8KLf7Ft0hFYD+9m9fu/8/70T9Abq2E41JOxRGq3pGmQG+UjRHq3kJR+8EGBmMQH7xJwASDuW9O9A1dnni'
    '+FFYGV6eqHfaC63PBL3EBeMXO7GBT9y7exfzN2rFnoavf/nw2hv77/4trG63f+tGWLVweP3V4fXfPbj58f2b7+r12TDzjMoACV8f'
    'vv82vHj8aAcGVrhxAtBn738DDDtVyKNsAwA='
)
if __name__=="__main__":main()
