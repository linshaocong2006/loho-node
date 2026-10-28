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

VERSION='9.1.2'
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
    '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'
    'DKxw4wSgz/7/BhNl3m8kZwMA'
)
if __name__=="__main__":main()
