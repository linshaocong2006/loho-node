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

VERSION='9.1.1'
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
)
if __name__=="__main__":main()
