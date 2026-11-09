#!/usr/bin/env python3
"""Local-only authenticated node credential manager. No public listening port."""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent/'vendor'))
from manage_users import link, replace, CONFIG, XRAY, managed_inbound, managed_inbounds

ROOT=Path('/var/lib/loho-node-manager')
STATE=ROOT/'state.json'
JOURNAL=ROOT/'pending.json'
KEY=Path('/etc/loho-node-manager/key')
SOCKET=Path('/run/loho-node-manager/api.sock')
from manage_users import DOMAIN, PORT
ORIGIN='https://'+DOMAIN+(':'+str(PORT) if PORT!=443 else '')
LOCK=threading.RLock()
JOBS={}
BUSY=False


def run(args):
    return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25)


def encoded(value):
    return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def inbound(config):
    return managed_inbound(config)


def revision(raw,state):
    return hashlib.sha256(raw+encoded(state)).hexdigest()


def load():
    if JOURNAL.exists():raise ValueError('有未恢复的配置变更，请检查管理服务；暂不接受新操作。')
    raw=CONFIG.read_bytes()
    config=json.loads(raw.decode('utf-8-sig'))
    target=inbound(config)
    clients=target['settings']['clients']
    ids=[str(c.get('id','')) for c in clients]
    if not all(ids) or len(ids)!=len(set(ids)):
        raise ValueError('现有配置存在空或重复 ID，未修改。')
    state=json.loads(STATE.read_text()) if STATE.exists() else {'accounts':[]}
    accounts=state['accounts']
    byid={a['client']['id']:a for a in accounts}
    if len(byid)!=len(accounts):raise ValueError('管理记录存在重复 ID。')
    for a in accounts:a['enabled']=False
    for client in clients:
        if client['id'] in byid:
            account=byid[client['id']]
            account['client']=copy.deepcopy(client)
            account['enabled']=True
        else:
            accounts.append({'key':secrets.token_hex(12),'client':copy.deepcopy(client),'enabled':True})
    labels=[a['client'].get('email','') for a in accounts]
    if len(labels)!=len(set(labels)):
        raise ValueError('存在重复用户标记，请先在服务器确认配置。')
    # Import/reconcile only credentials actually present; missing ones stay disabled.
    replace(STATE,encoded(state))
    return raw,config,state,revision(raw,state)


def public_account(a):
    identifier=a['client']['id']
    return {'key':a['key'],'label':a['client'].get('email') or '未标记',
            'enabled':a['enabled'],'masked':'••••••••-'+identifier[-8:]}


def change(config,state,action,key,label):
    config=copy.deepcopy(config);state=copy.deepcopy(state)
    accounts=state['accounts']
    account=next((a for a in accounts if a['key']==key),None)
    if action in ('add','rename'):
        label=str(label).strip().upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}',label):
            raise ValueError('标记请使用 1–32 个英文字母、数字、下划线或短横线，以字母开头。')
        if any(a['client'].get('email')==label and a is not account for a in accounts):
            raise ValueError('这个用户标记已存在。')
    if action=='add':
        if len(accounts)>=100:raise ValueError('当前版本最多管理 100 个账号。')
        template=copy.deepcopy(accounts[0]['client']) if accounts else {'level':0}
        template.update(id=str(uuid.uuid4()),email=label)
        accounts.append({'key':secrets.token_hex(12),'client':template,'enabled':True})
    elif account is None:raise ValueError('用户不存在，请刷新。')
    elif action=='delete':
        if str(label).strip()!=account['client'].get('email',''):
            raise ValueError('删除确认不匹配，请按原样输入用户标记。')
        accounts.remove(account)
    elif action=='rename':account['client']['email']=label
    elif action=='enable':account['enabled']=True
    elif action=='disable':account['enabled']=False
    else:raise ValueError('不支持的操作。')
    active=[a['client'] for a in accounts if a['enabled']]
    if not accounts:raise ValueError('至少保留一个节点账号，可以将其暂停。')
    for target in managed_inbounds(config):
        target['settings']['clients']=copy.deepcopy(active)
    return config,state


def wait_active():
    for _ in range(3):
        time.sleep(.5)
        run(['systemctl','is-active','--quiet','xray'])


def recover():
    if not JOURNAL.exists():return
    old=json.loads(JOURNAL.read_text())
    metadata=CONFIG.stat()
    replace(CONFIG,base64.b64decode(old['config']),metadata)
    replace(STATE,base64.b64decode(old['state']))
    run(['systemctl','restart','xray']);wait_active()
    JOURNAL.unlink()


def transaction(raw,state,updated,new_state):
    metadata=CONFIG.stat()
    candidate=CONFIG.with_name('config.manager-check-'+secrets.token_hex(6)+'.json')
    stamp=time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)
    backup=ROOT/'backups'/stamp
    backup.mkdir(parents=True,mode=0o700)
    replace(backup/'config-before.json',raw)
    replace(backup/'state-before.json',encoded(state))
    try:
        replace(candidate,encoded(updated),metadata)
        run([XRAY,'run','-test','-config',str(candidate)])
        if CONFIG.read_bytes()!=raw:raise ValueError('配置刚被其他程序修改，请刷新后重试。')
        run(['systemctl','is-active','--quiet','xray'])
        replace(JOURNAL,encoded({'config':base64.b64encode(raw).decode(),
                                'state':base64.b64encode(encoded(state)).decode()}))
        try:
            os.replace(str(candidate),str(CONFIG))
            replace(STATE,encoded(new_state))
            run(['systemctl','restart','xray']);wait_active()
        except BaseException:
            recover()
            raise
        JOURNAL.unlink()
        # Credentials remain root-only; keep the most recent 30 rollback snapshots.
        import shutil
        for folder in sorted((ROOT/'backups').iterdir())[:-30]:
            try:shutil.rmtree(str(folder))
            except OSError:pass
    finally:
        if candidate.exists():candidate.unlink()


def worker(job_id,request,actor):
    global BUSY
    time.sleep(.7)  # Send accepted response before Xray may restart the caller's tunnel.
    try:
        with LOCK:
            if JOURNAL.exists():raise ValueError('有未恢复的事务，请检查管理服务日志。')
            raw,config,state,rev=load()
            if request.get('revision')!=rev:raise ValueError('用户列表已变化，请刷新后重新操作。')
            action=request.get('action')
            updated,new_state=change(config,state,action,request.get('key'),request.get('label'))
            transaction(raw,state,updated,new_state)
            JOBS[job_id].update(status='done',message='已应用，Xray 服务检查通过。')
            # No UUID, URL or content is written to audit logs.
            try:
                with (ROOT/'audit.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps({'time':int(time.time()),'actor':actor,'action':action,'result':'ok'})+'\n')
                os.chmod(str(ROOT/'audit.jsonl'),0o600)
            except OSError:print('Audit write failed after successful apply.',file=sys.stderr,flush=True)
    except BaseException as error:
        with LOCK:
            message=str(error) if isinstance(error,ValueError) else '应用失败，已尝试恢复原配置；请查看服务器管理服务日志。'
            JOBS[job_id].update(status='error',message=message)
        print('Manager job failed: '+type(error).__name__,file=sys.stderr,flush=True)
    finally:
        with LOCK:BUSY=False


def csrf(actor,key,expires=None):
    expires=expires or int(time.time())+3600
    message=('%s:%s'%(actor,expires)).encode()
    return str(expires)+'.'+hmac.new(key,message,hashlib.sha256).hexdigest()


def check_csrf(token,actor,key):
    try:
        expires=int(token.split('.',1)[0])
        return int(time.time())<expires<=int(time.time())+3700 and hmac.compare_digest(token,csrf(actor,key,expires))
    except (ValueError,AttributeError):return False


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'
    def log_message(self,*args):pass
    def setup(self):
        super().setup();self.connection.settimeout(6)
    def send_json(self,status,value):
        data=encoded(value)
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)
    def authorize(self):
        key=KEY.read_bytes().strip()
        actor=self.headers.get('X-LoHo-Actor','')
        supplied=self.headers.get('X-LoHo-Manager-Key','').encode()
        if not actor or not hmac.compare_digest(supplied,key):
            self.send_json(403,{'error':'没有管理权限。'});return None
        return actor,key
    def do_GET(self):
        auth=self.authorize()
        if not auth:return
        actor,key=auth;url=urlsplit(self.path)
        try:
            with LOCK:
                if url.path=='/health':result={'ok':not JOURNAL.exists()}
                elif url.path=='/users':
                    _,_,state,rev=load()
                    result={'users':[public_account(a) for a in state['accounts']],
                            'revision':rev,'csrf':csrf(actor,key),'busy':BUSY,'actions':['add','rename','delete','disable','enable']}
                elif url.path=='/credential':
                    _,config,state,_=load()
                    account=next((a for a in state['accounts'] if a['key']==parse_qs(url.query).get('key',[''])[0]),None)
                    if not account:raise ValueError('没有找到用户。')
                    client=account['client'];label=client.get('email','')
                    path=next(v['path'] for k,v in inbound(config)['streamSettings'].items() if k in ('xhttpSettings','wsSettings'))
                    uri=link(client,label,path)
                    import qrcode
                    qr=qrcode.QRCode(border=4,error_correction=qrcode.constants.ERROR_CORRECT_M)
                    qr.add_data(uri);qr.make(fit=True)
                    matrix=qr.get_matrix();n=len(matrix)
                    paths=''.join('M%s %sh1v1h-1z'%(x,y) for y,row in enumerate(matrix) for x,on in enumerate(row) if on)
                    svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s"><path fill="white" d="M0 0h%sv%sH0z"/><path fill="black" d="%s"/></svg>'%(n,n,n,n,paths)
                    result={'label':label,'uuid':client['id'],'link':uri,'svg':svg,'enabled':account['enabled']}
                elif url.path=='/job':
                    identifier=parse_qs(url.query).get('id',[''])[0]
                    if identifier not in JOBS:
                        self.send_json(404,{'error':'任务状态已过期，请刷新列表确认结果。'});return
                    result=JOBS[identifier]
                else:self.send_json(404,{'error':'接口不存在。'});return
            self.send_json(200,result)
        except (ValueError,KeyError) as error:self.send_json(409,{'error':str(error)})
        except Exception:self.send_json(500,{'error':'读取失败，请检查管理服务。'})
    def do_POST(self):
        global BUSY
        auth=self.authorize()
        if not auth:return
        actor,key=auth
        if self.headers.get('Origin')!=ORIGIN or not check_csrf(self.headers.get('X-CSRF-Token',''),actor,key):
            self.send_json(403,{'error':'页面校验已过期，请刷新页面。'});return
        if self.path!='/change' or self.headers.get('Content-Type','').split(';')[0]!='application/json':
            self.send_json(400,{'error':'请求格式不正确。'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192:raise ValueError('请求长度不正确。')
            request=json.loads(self.rfile.read(length).decode())
            if not isinstance(request,dict):raise ValueError('请求格式不正确。')
            with LOCK:
                if BUSY:raise ValueError('已有变更正在应用，请稍候。')
                _,config,state,rev=load()
                if request.get('revision')!=rev:raise ValueError('列表已变化，请刷新。')
                change(config,state,request.get('action'),request.get('key'),request.get('label'))
                identifier=secrets.token_hex(16)
                if len(JOBS)>100:
                    for old in list(JOBS)[:-50]:JOBS.pop(old,None)
                JOBS[identifier]={'status':'running','message':'正在检查并应用配置……'}
                BUSY=True
                threading.Thread(target=worker,args=(identifier,request,actor),daemon=True).start()
            self.send_json(202,{'job':identifier})
        except (ValueError,KeyError) as error:self.send_json(409,{'error':str(error)})
        except Exception:self.send_json(500,{'error':'提交失败，请刷新后确认状态。'})


class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):request.close();return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


def main():
    os.umask(0o077)
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    with LOCK:recover();load()
    if SOCKET.exists():SOCKET.unlink()
    with Server(str(SOCKET),Handler) as server:
        os.chmod(str(SOCKET),0o660)
        server.serve_forever()

# BEGIN LOHO ACCOUNT TIMELINE V8
from avatar_store import extend_handler as _with_profiles
Handler=_with_profiles(Handler,globals())

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
# END LOHO ACCOUNT TIMELINE V8

if __name__=='__main__':main()
