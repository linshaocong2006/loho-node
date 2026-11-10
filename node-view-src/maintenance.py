#!/usr/bin/env python3
"""Root-only maintenance CLI; no additional public API or listening service."""
import argparse
import fcntl
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

APP=Path('/opt/loho-node-view')
NODE=Path('/etc/loho-node-manager/node.json')
ROOT=Path('/var/lib/loho-node-maintenance')
SERVICES=('xray','nginx','loho-node-view','loho-node-manager','loho-node-auth','loho-node-traffic','loho-node-notify')


def run(args,timeout=30,input_text=None):
    return subprocess.run(args,input=input_text,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout).stdout


def atomic(path,value):
    temporary=path.with_name(path.name+'.tmp-'+secrets.token_hex(4))
    try:
        with temporary.open('w') as stream:
            os.fchmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def collect():
    now=time.time();settings=json.loads(NODE.read_text());issues=[]
    value={'generated':now,'issues':issues,'renewal':{},'services':{}}
    result=run(['systemctl','show',*[n+'.service' for n in SERVICES],'certbot.timer','loho-node-logrotate.timer',
                '--property=Id,LoadState,ActiveState,Result,NextElapseUSecRealtime'])
    for block in result.split('\n\n'):
        fields=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        name=fields.get('Id','');state=fields.get('ActiveState','unknown')
        if name in {n+'.service' for n in SERVICES}|{'certbot.timer','loho-node-logrotate.timer'}:
            value['services'][name]=state
            if state!='active':issues.append(name+' 未运行')
        if name=='certbot.timer':value['renewal']['next']=fields.get('NextElapseUSecRealtime','')
    renewal=run(['systemctl','show','certbot.service','--property=Result,ExecMainStatus,InactiveEnterTimestamp'])
    fields=dict(line.split('=',1) for line in renewal.splitlines() if '=' in line)
    value['renewal'].update(result=fields.get('Result'),status=fields.get('ExecMainStatus'),last=fields.get('InactiveEnterTimestamp'))
    if fields.get('Result') not in (None,'','success'):issues.append('最近证书续期任务未成功，请运行 renew-test 检查')
    cert='/etc/letsencrypt/live/loho-'+settings['domain']+'/fullchain.pem'
    try:
        expiry=run(['openssl','x509','-in',cert,'-noout','-enddate']).strip().split('=',1)[1]
        value['certificate_expires']=ssl.cert_time_to_seconds(expiry)
        if value['certificate_expires']-now<14*86400:issues.append('证书不足14天有效，请检查续期')
    except (OSError,ValueError,subprocess.SubprocessError):issues.append('无法读取证书到期时间')
    for name,path,field in [('连接采集','/var/lib/loho-node-view/activity.json','generated'),('流量采集','/var/lib/loho-node-traffic/public/users-live.json','sampled')]:
        try:
            sample=json.loads(Path(path).read_text())
            if sample.get('error') or now-sample.get(field,0)>90:issues.append(name+'异常或数据超过90秒未更新')
        except (OSError,ValueError):issues.append(name+'数据不可读')
    disk=shutil.disk_usage('/var/lib');value['disk_free']=disk.free
    if disk.free<1024**3 or disk.free/disk.total<.05:issues.append('磁盘剩余空间偏低')
    return value


class TLS(http.client.HTTPSConnection):
    def __init__(self,settings):
        self.settings=settings
        super().__init__(settings['domain'],settings['port'],timeout=8,context=ssl.create_default_context())
    def connect(self):
        sock=socket.create_connection(('127.0.0.1',self.settings['port']),timeout=self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.settings['domain'])


def probe(settings):
    """Connect a temporary Xray client and fetch public HTTPS through REALITY."""
    from manage_users import managed_inbound
    node=json.loads(Path('/usr/local/etc/xray/config.json').read_text())
    clients=managed_inbound(node)['settings']['clients']
    if not clients:raise ValueError('请启用至少一个代理用户再执行完整链路检查。')
    client=clients[0]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    reality = settings['reality']
    stream = {'network':'xhttp','security':'reality','xhttpSettings':{'path':reality['path'],'mode':'packet-up'},
              'realitySettings':{'serverName':reality['sni'],'fingerprint':reality['fingerprint'],
                                 'password':reality['public_key'],'shortId':reality['short_id']}}
    config={'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'auth':'noauth','udp':False}}],
            'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':'127.0.0.1','port':reality['port'],'users':[{'id':client['id'],'encryption':'none'}]}]},'streamSettings':stream}]}
    with tempfile.TemporaryDirectory(prefix='loho-probe-') as folder:
        path=Path(folder)/'client.json';path.write_text(json.dumps(config));os.chmod(path,0o600)
        with (Path(folder)/'client.log').open('w+') as output:
            process=subprocess.Popen(['/usr/local/bin/xray','run','-config',str(path)],stdout=output,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+8
                while True:
                    if process.poll() is not None:raise ValueError('本机测试客户端启动失败。')
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                    except OSError:
                        if time.monotonic()>deadline:raise ValueError('本机测试客户端未就绪。')
                        time.sleep(.1)
                for url in ('https://example.com/','https://www.google.com/generate_204'):
                    try:
                        run(['curl','--fail','--silent','--show-error','--noproxy','','--proxy','socks5h://127.0.0.1:'+str(port),
                             '--connect-timeout','8','--max-time','18','--output','/dev/null',url],timeout=22)
                        return
                    except subprocess.SubprocessError:pass
                raise ValueError('REALITY 完整代理连通检查失败：请检查服务端线路、目标站点和传输配置。')
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)


def check(with_probe=False):
    settings=json.loads(NODE.read_text())
    run(['/usr/local/bin/xray','run','-test','-config','/usr/local/etc/xray/config.json']);run(['nginx','-t'])
    for path,expected in (('/node-view/login.html',200),('/node-view/api/snapshot.json',401),('/node-view/manager/api/users',401)):
        c=TLS(settings)
        try:
            c.request('GET',path,headers={'Host':settings['domain']+':'+str(settings['port'])});r=c.getresponse();r.read()
            if r.status!=expected:raise ValueError(path+'检查失败，HTTP'+str(r.status))
        finally:c.close()
    if with_probe:
        probe(settings)
    print('Xray、Nginx配置和TLS登录保护通过'+('；完整代理链路通过。' if with_probe else '。'))


def reset_password():
    fd=os.open('/run/loho-node-install.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('已有安装或维护正在运行。')
        path=Path('/etc/loho-node-view/htpasswd');lines=path.read_text().splitlines()
        if len(lines)!=1:raise ValueError('网站登录文件有多个账号，请先检查配置。')
        user=lines[0].split(':',1)[0];password=getpass.getpass('新网站密码（12–128位）：')
        if not 12<=len(password)<=128 or any(c in password for c in (':','\n','\r','\x00')):raise ValueError('密码格式不正确。')
        if password!=getpass.getpass('再次输入密码：'):raise ValueError('两次密码不同。')
        hashed=run(['openssl','passwd','-6','-stdin'],input_text=password+'\n').strip()
        if not hashed.startswith('$6$'):raise ValueError('密码散列失败。')
        from manage_users import replace
        replace(path,(user+':'+hashed+'\n').encode(),path.stat())
        print('网站密码已修改，之前记住的设备将失效；代理用户不受影响。')


def main():
    parser=argparse.ArgumentParser(description='LoHo节点维护')
    parser.add_argument('action',choices=('status','health','check','reset-password','renew-test','links'))
    parser.add_argument('--probe',action='store_true',help='check同时验证完整代理链路')
    args=parser.parse_args()
    if os.geteuid()!=0:raise ValueError('请使用 sudo loho-node 运行。')
    os.umask(0o077)
    if args.action in ('status','health'):
        value=collect()
        if args.action=='health':ROOT.mkdir(mode=0o700,exist_ok=True);atomic(ROOT/'health.json',value)
        else:
            for name,state in value['services'].items():print(name+': '+state)
            print('自动续期最近结果：'+str(value['renewal'].get('result','未知')))
            print('问题：'+'；'.join(value['issues']) if value['issues'] else '服务、采集、证书和磁盘检查正常。')
    elif args.action=='check':check(args.probe)
    elif args.action=='reset-password':reset_password()
    elif args.action=='renew-test':
        settings=json.loads(NODE.read_text());print(run(['certbot','renew','--cert-name','loho-'+settings['domain'],'--dry-run','--run-deploy-hooks'],timeout=300))
    elif args.action=='links':
        from manage_users import link,managed_inbound
        config=json.loads(Path('/usr/local/etc/xray/config.json').read_text())
        for client in managed_inbound(config)['settings']['clients']:
            print(client['email']+' / REALITY\n'+link(client,client['email'])+'\n')


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError) as error:raise SystemExit('检查未通过：'+str(error))
