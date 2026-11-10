"""Install a complete, independent LoHo node on a supported blank VPS."""
import argparse
import ast
import base64
import fcntl
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import types
import uuid
import zipfile

VERSION = '10.3.2'
XRAY_VERSION = '26.10.21'
ASSETS = {
    'x86_64': ('Xray-linux-64.zip', '0eec3ed4163c273d8c8bf821f7d219e257921cf584aa3fb970715e458e631978'),
    'aarch64': ('Xray-linux-arm64-v8a.zip', 'd1304946c486d98e6b886e9b931eaaaebb23835b30e217638037b7f2721d4e9c'),
}
APP = Path('/opt/loho-node-view')
CONF = Path('/usr/local/etc/xray')
SITE = Path('/etc/nginx/sites-available/loho-node.conf')
ENABLED = Path('/etc/nginx/sites-enabled/loho-node.conf')
SNIPPET = Path('/etc/nginx/snippets/loho-node-view.conf')
REPORT = Path('/root/loho-node-install.txt')
LOG = None
SERVICES = ('xray','loho-node-view','loho-node-manager','loho-node-auth','loho-node-traffic','loho-node-notify')
TIMERS = ('loho-node-health.timer','loho-node-logrotate.timer')
OWNED_ROOTS = [APP,CONF,Path('/usr/local/share/xray'),Path('/var/log/xray'),
    *[Path('/var/lib')/name for name in ('loho-node-view','loho-node-manager','loho-node-auth','loho-node-traffic','loho-node-notify','loho-node-maintenance')],
    *[Path('/etc')/name for name in ('loho-node-view','loho-node-manager','loho-node-auth','loho-node-notify')],
    Path('/var/www/loho-acme'),Path('/var/www/loho-site')]
OWNED_FILES = [SITE,ENABLED,SNIPPET,REPORT,Path('/usr/local/bin/xray'),Path('/usr/local/bin/loho-node'),
    Path('/etc/logrotate.d/loho-xray'),Path('/etc/sysctl.d/90-loho-node-bbr.conf'),
    Path('/etc/letsencrypt/renewal-hooks/deploy/loho-nginx'),
    *[Path('/etc/systemd/system')/(name+'.service') for name in SERVICES],
    *[Path('/etc/systemd/system')/name for name in (*TIMERS,'loho-node-health.service','loho-node-logrotate.service')]]

# @@LOW_LEVEL_HELPERS@@


def system_info():
    fields = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key,value = line.split('=',1); fields[key] = value.strip('"')
    distro,version = fields.get('ID'),fields.get('VERSION_ID')
    allowed = {'debian': {'12','13'}, 'ubuntu': {'22.04','24.04','26.04'}}
    if version not in allowed.get(distro,set()):
        raise ValueError('支持 Debian 12/13、Ubuntu 22.04/24.04/26.04，未修改系统。')
    return distro,version


def preflight():
    if os.geteuid() != 0: raise ValueError('请使用 sudo bash 运行安装器。')
    if sys.version_info < (3,10): raise ValueError('需要 Python 3.10+。')
    distro,version = system_info()
    if platform.machine() not in ASSETS: raise ValueError('仅支持 x86_64 和 aarch64。')
    if not Path('/run/systemd/system').is_dir(): raise ValueError('需要由 systemd 启动的 VPS，不支持普通容器。')
    if shutil.disk_usage('/').free < 1024**3: raise ValueError('至少需要 1GiB 可用磁盘。')
    if not Path('/proc/meminfo').exists(): raise ValueError('无法识别 Linux 内存信息。')
    extra = [Path(p) for p in ('/etc/v2ray','/usr/local/etc/v2ray','/etc/xray','/usr/local/bin/v2ray','/usr/bin/v2ray','/usr/bin/xray')]
    for service in (*SERVICES,'v2ray'):
        extra += [Path(base)/(service+'.service') for base in ('/usr/lib/systemd/system','/lib/systemd/system')]
    occupied = [str(p) for p in OWNED_ROOTS+OWNED_FILES+extra if p.exists() or p.is_symlink()]
    if occupied: raise ValueError('此工具只安装空白服务器，检测到已有文件，未覆盖：\n'+'\n'.join(occupied[:12]))
    for folder in ('/etc/nginx/sites-enabled','/etc/nginx/conf.d'):
        path = Path(folder)
        if path.exists():
            others = [p for p in path.iterdir() if not (folder.endswith('sites-enabled') and p.name=='default')]
            if others: raise ValueError('检测到其他 Nginx 站点，未覆盖：'+str(others[0]))
    default = Path('/etc/nginx/sites-enabled/default')
    if (default.exists() or default.is_symlink()) and (not default.is_symlink() or default.resolve()!=Path('/etc/nginx/sites-available/default')):
        raise ValueError('Nginx default 不是发行版标准软链接，未修改。')
    if subprocess.run(['getent','passwd','xray'],stdout=subprocess.DEVNULL).returncode == 0:
        raise ValueError('已有 xray 系统账号，停止使用未知配置。')
    log('系统检查通过：%s %s / %s。'%(distro,version,platform.machine()))
    return distro,version


def free_port(port):
    for family,address in ((socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')):
        try: sock = socket.socket(family,socket.SOCK_STREAM)
        except OSError:
            if family==socket.AF_INET6: continue
            raise
        try:
            if family==socket.AF_INET6: sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
            sock.bind((address,port))
        except OSError as error:
            if family==socket.AF_INET6 and error.errno in (97,99): continue
            raise ValueError('TCP %d 不可用：%s'%(port,error))
        finally: sock.close()


def choose_internal(start,exclude):
    for candidate in range(start,start+50):
        if candidate in exclude: continue
        try: free_port(candidate); return candidate
        except ValueError: pass
    raise ValueError('无法分配本机服务端口。')


def check_reality_target(domain):
    try:
        addresses = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError('REALITY 目标域名解析失败，请检查拼写或换一个域名。') from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('REALITY 目标域名需解析到公网地址。')
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(['h2'])
    try:
        with socket.create_connection((domain, 443), timeout=8) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as tls:
                if tls.selected_alpn_protocol() == 'h2':
                    return
    except OSError as error:
        raise ValueError('无法以 TLS 1.3 连接 REALITY 目标：' + str(error)) from error
    raise ValueError('REALITY 目标未协商 HTTP/2，请选择其他域名。')


def reality_keypair():
    output = run(['/usr/local/bin/xray', 'x25519'], secret=True, timeout=15)
    private = re.search(r'^Private\s*Key:\s*([A-Za-z0-9_-]{43})$', output, re.M | re.I)
    public = re.search(r'^(?:Password(?: \(PublicKey\))?|Public\s*Key):\s*([A-Za-z0-9_-]{43})$', output, re.M | re.I)
    if not private or not public:
        raise ValueError('Xray 未返回可识别的 REALITY 密钥。')
    return private[1], public[1]


def ask_value(prompt, validate, default=None):
    while True:
        value = input(prompt).strip()
        try:
            return validate(default if value == '' and default is not None else value)
        except ValueError as error:
            log(str(error)+' 请重新输入这一项。')


def choice_value(value, options):
    if value not in options:
        raise ValueError('可选值为 '+ ' / '.join(options)+'。')
    return value


def node_domain(value):
    domain = validate_domain(value)
    try:
        records = sorted({item[4][0] for item in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)})
    except socket.gaierror as error:
        raise ValueError('节点域名解析失败，请检查拼写和 DNS 的 A 记录。') from error
    if not records:
        raise ValueError('节点域名没有解析记录。')
    for address in records:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError('域名包含非公网解析，无法申请公网证书。')
        if ':' in address:
            raise ValueError('此安装器使用 IPv4 入口，请移除该域名的 AAAA 记录。')
    return domain, records


def available_port(value, panel_port=None):
    if not value.isascii() or not value.isdigit():
        raise ValueError('端口必须是数字。')
    port = int(value)
    if not 1 <= port <= 65535 or port in (22, 80):
        raise ValueError('端口需在 1–65535，且不能使用 22、80。')
    if panel_port is not None and ((port != 443 and port < 1024) or port == panel_port):
        raise ValueError('REALITY 端口需为 443 或 1024–65535，且须与 HTTPS 面板端口不同。')
    free_port(port)
    return port


def check_acme_port():
    try:
        free_port(80)
    except ValueError:
        # An earlier setup may already have installed and started the distro Nginx.
        default = Path('/etc/nginx/sites-enabled/default')
        if not default.is_symlink() or default.resolve() != Path('/etc/nginx/sites-available/default') or not shutil.which('ss'):
            raise
        main_pid = run(['systemctl','show','--property=MainPID','--value','nginx']).strip()
        listeners = run(['ss','-H','-lntp','sport = :80']).splitlines()
        owners = [re.findall(r'\("([^"\n]+)",pid=(\d+),', line) for line in listeners]
        if (not owners or not all(row and all(name == 'nginx' for name,_ in row) for row in owners)
                or not any(pid == main_pid for row in owners for _,pid in row)):
            raise
        log('TCP80 已由发行版的 Nginx 默认站点监听，继续使用现有 Nginx。')


def website_username(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', value):
        raise ValueError('用户名需为 1–32 位字母、数字、点、下划线或短横线。')
    return value


def website_password():
    while True:
        password = getpass.getpass('网站密码（12–128位，输入不显示）：')
        if not 12<=len(password)<=128 or any(c in password for c in (':','\n','\r','\x00')):
            log('密码需为 12–128 位，且不能包含冒号或换行。请重新输入密码。')
        elif password == getpass.getpass('再次输入密码：'):
            return password
        else:
            log('两次密码不同，请重新输入并确认密码。')


def reality_domain(value, domain):
    target = validate_domain(value)
    if target == domain:
        raise ValueError('REALITY 目标应填写另一个 HTTPS 站点的域名。')
    return target


def prompt_reality_domain(domain):
    return ask_value('REALITY 目标域名 [www.apple.com]：',
                     lambda value: reality_domain(value, domain), 'www.apple.com')


def verify_reality_settings(settings):
    reality = settings['reality']
    target = reality['sni']
    while True:
        try:
            check_reality_target(target)
        except ValueError as error:
            log(str(error)+' 之前输入的其他设置已保留。')
            target = prompt_reality_domain(settings['domain'])
        else:
            reality.update(sni=target,target=target+':443')
            log('REALITY 目标检查通过：'+target+'。')
            return


def settings_prompt():
    if not sys.stdin.isatty(): raise ValueError('请下载或上传脚本后用 bash 运行，安装需要交互输入。')
    check_acme_port()
    domain,records = ask_value('节点和后台域名（A 记录指向本机，普通 DNS 解析）：',node_domain)
    port = ask_value('HTTPS 面板端口 [5000]：',available_port,'5000')
    username = ask_value('网站登录用户名 [loho]：',website_username,'loho')
    password = website_password()
    default_port = '3001' if port == 3000 else '3000'
    reality_port = ask_value(f'REALITY 节点端口 [{default_port}]：',
                             lambda value: available_port(value,port),default_port)
    log('REALITY 目标是供握手使用的 HTTPS 站点。只填域名，不加 https://、端口或路径。')
    target = prompt_reality_domain(domain)
    reality = {'port':reality_port,'target':target+':443','sni':target,'fingerprint':'chrome',
               'short_id':secrets.token_hex(8),'path':'/'+secrets.token_hex(12)+'/',
               'inbound_tag':'loho-node-reality'}
    bbr = ask_value('启用系统自带BBR（可选）？[y/N]：',
                    lambda value: choice_value(value.lower(),('y','n','yes','no')),'n')
    api = choose_internal(10085,{80,port,reality_port})
    settings = {'domain':domain,'port':port,'api_endpoint':'127.0.0.1:'+str(api),
                'reality':reality,'version':VERSION,'xray_version':XRAY_VERSION}
    log('即将安装：HTTPS 面板 %s:%d；域名解析 %s。'%(domain,port,', '.join(records)))
    log('节点：VLESS + XHTTP packet-up + REALITY，TCP%d，初始用户 ADMIN。'%reality_port)
    public_ports = 'TCP80、TCP%d、TCP%d'%(port,reality_port)
    log('需要公网 '+public_ports+'；继续即同意证书服务条款 https://letsencrypt.org/repository/ 。')
    return settings,username,password,bbr in ('y','yes')


def xray_config(settings,clients,reality_private_key):
    if not reality_private_key:
        raise ValueError('缺少 REALITY 服务端密钥。')
    reality = settings['reality']
    stream = {'network':'xhttp','security':'reality',
              'xhttpSettings':{'path':reality['path'],'mode':'packet-up'},
              'realitySettings':{'target':reality['target'],'serverNames':[reality['sni']],
                                 'privateKey':reality_private_key,'shortIds':[reality['short_id']]}}
    inbound = {'tag':reality['inbound_tag'],'listen':'0.0.0.0','port':reality['port'],
               'protocol':'vless','settings':{'clients':clients,'decryption':'none'},'streamSettings':stream,
               'sniffing':{'enabled':True,'destOverride':['http','tls','quic'],'routeOnly':True}}
    return {
        'log':{'access':'/var/log/xray/access.log','error':'/var/log/xray/error.log','loglevel':'warning'},
        'api':{'tag':'loho-local-api','listen':settings['api_endpoint'],'services':['StatsService','LoggerService']},
        'stats':{},'policy':{'levels':{'0':{'statsUserUplink':True,'statsUserDownlink':True}}},
        'inbounds':[inbound],
        'outbounds':[{'tag':'direct','protocol':'freedom'},{'tag':'block','protocol':'blackhole'}],
        'routing':{'domainStrategy':'IPIfNonMatch','rules':[{'type':'field','ip':['geoip:private'],'outboundTag':'block'}]},
    }


def nginx_config(settings,tls=True,modern=True):
    domain,port = settings['domain'],settings['port']
    result = '''server {
    listen 80;
    server_name %s;
    access_log off;
    location ^~ /.well-known/acme-challenge/ { root /var/www/loho-acme; default_type text/plain; }
    location / { %s }
}
'''%(domain,'return 301 '+origin(settings)+'$request_uri;' if tls else 'return 404;')
    if not tls: return result
    http2 = 'listen %d ssl;\n    http2 on;'%port if modern else 'listen %d ssl http2;'%port
    return result+'''server {
    %s
    server_name %s;
    ssl_certificate /etc/letsencrypt/live/loho-%s/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/loho-%s/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:LOHOTLS:10m;
    ssl_session_timeout 1d;
    server_tokens off;
    access_log off;
    error_log /var/log/nginx/loho-node-error.log warn;
    root /var/www/loho-site;
    index index.html;
    include /etc/nginx/snippets/loho-node-view.conf;
    location / { try_files $uri $uri/ =404; }
}
'''%(http2,domain,domain,domain)


XRAY_UNIT = '''[Unit]
Description=LoHo Xray node
After=network-online.target
Wants=network-online.target
[Service]
User=xray
Group=xray
UMask=0027
Environment=XRAY_LOCATION_ASSET=/usr/local/share/xray
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/log/xray
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
'''


def xray_unit(settings):
    if settings['reality']['port'] == 443:
        return XRAY_UNIT.replace('CapabilityBoundingSet=\n',
                                 'CapabilityBoundingSet=CAP_NET_BIND_SERVICE\nAmbientCapabilities=CAP_NET_BIND_SERVICE\n')
    return XRAY_UNIT


def rotate_config(settings):
    return '''/var/log/xray/*.log {
    daily
    maxsize 20M
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    su xray xray
    create 0640 xray xray
    sharedscripts
    postrotate
        /usr/local/bin/xray api restartlogger --server=%s --timeout=5
    endscript
}
'''%settings['api_endpoint']


def maintenance_units():
    return {
        'loho-node-health.service':'''[Unit]
Description=LoHo scheduled health checks
After=network-online.target nginx.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/loho-node health
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/loho-node-maintenance
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
TimeoutStartSec=45
MemoryMax=96M
''',
        'loho-node-health.timer':'''[Unit]
Description=LoHo health every five minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=20
Persistent=true
[Install]
WantedBy=timers.target
''',
        'loho-node-logrotate.service':'''[Unit]
Description=LoHo bounded Xray log rotation
After=xray.service
[Service]
Type=oneshot
ExecStart=/usr/sbin/logrotate --wait-for-state-lock /etc/logrotate.d/loho-xray
TimeoutStartSec=90
''',
        'loho-node-logrotate.timer':'''[Unit]
Description=Check LoHo logs hourly
[Timer]
OnCalendar=hourly
RandomizedDelaySec=120
Persistent=true
[Install]
WantedBy=timers.target
'''}


def socket_json(service,path,secret,auth=False):
    sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); sock.settimeout(4)
    connection = http.client.HTTPConnection('localhost',timeout=4)
    try:
        sock.connect('/run/loho-node-'+service+'/api.sock'); connection.sock=sock
        headers = {'X-LoHo-Auth-Key':secret} if auth else {'X-LoHo-Manager-Key':secret,'X-LoHo-Actor':'installer'}
        connection.request('GET',path,headers=headers); response=connection.getresponse(); raw=response.read(1048576)
        if response.status!=200: raise ValueError('%s接口返回HTTP%d'%(service,response.status))
        return json.loads(raw)
    finally: connection.close(); sock.close()


def until_ready(check,label,timeout=35):
    deadline = time.monotonic()+timeout
    while True:
        try: return check()
        except (OSError,ValueError,KeyError,http.client.HTTPException) as error:
            if time.monotonic()>=deadline: raise ValueError(label+'超时：'+str(error))
            time.sleep(.4)


def tls_request(settings,path,body=None,cookie=None,csrf=None):
    connection=LocalTLS(settings)
    try:
        headers={'Host':settings['domain']+':'+str(settings['port'])}
        if cookie: headers['Cookie']=cookie
        if csrf: headers['X-CSRF-Token']=csrf
        if body is not None: headers.update({'Origin':origin(settings),'Content-Type':'application/json'})
        connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        response=connection.getresponse(); return response.status,dict(response.getheaders()),response.read(2*1024*1024)
    finally: connection.close()


def check_gateway(settings,username,password):
    def check():
        status,headers,_=tls_request(settings,'/node-view/login.html')
        if status!=200 or headers.get('X-LoHo-Login-Version')!=VERSION: raise ValueError('新登录页尚未就绪，HTTP'+str(status))
        for path in ('api/snapshot.json','api/users-live.json','history/index.json','manager/api/users','notify/api/state','auth/api/system'):
            if tls_request(settings,'/node-view/'+path)[0]!=401: raise ValueError('未登录访问没有被阻止：'+path)
    until_ready(check,'等待TLS和登录保护')
    status,headers,raw=tls_request(settings,'/node-view/auth/api/login',{'username':username,'password':password,'remember':False})
    if status!=200 or not headers.get('Set-Cookie'): raise ValueError('网站登录验证失败，HTTP'+str(status))
    cookie=headers['Set-Cookie'].split(';')[0]
    csrf=json.loads(tls_request(settings,'/node-view/auth/api/me',cookie=cookie)[2])['csrf']
    try:
        for path in ('index.html','connections.html','manager.html','notifications.html','system.html','security.html',
                     'api/snapshot.json','api/users-live.json','api/users.json','manager/api/users','notify/api/state','auth/api/system'):
            status,_,raw=tls_request(settings,'/node-view/'+path,cookie=cookie)
            if status!=200: raise ValueError('登录后检查失败：'+path+' HTTP'+str(status))
            if path=='manager/api/users':
                users=json.loads(raw)['users']
                if len(users)!=1 or users[0]['label']!='ADMIN': raise ValueError('初始代理用户不是唯一的ADMIN。')
                status,_,credential=tls_request(settings,'/node-view/manager/api/credential?key='+users[0]['key'],cookie=cookie)
                if status!=200 or 'security=reality' not in json.loads(credential)['link']:
                    raise ValueError('用户管理导出的 REALITY 链接不匹配。')
    finally:
        # Login-check sessions are short-lived, and explicitly revoked on success.
        tls_request(settings,'/node-view/auth/api/logout',{},cookie,csrf)


def verify_files(files):
    required={'collector.py','history.py','manager.py','manage_users.py','traffic.py','notify.py','auth.py','identities.py','system_health.py','maintenance.py',
              '_nginx_snippet','_release','_collector_unit','_manager_unit','_auth_unit','_traffic_unit','_notify_unit',
              'www/index.html','www/connections.html','www/manager.html','www/notifications.html','www/login.html','www/system.html','www/security.html'}
    if not required<=files.keys(): raise ValueError('内置资源不完整。')
    for name,text in files.items():
        if name.startswith('/') or '..' in Path(name).parts: raise ValueError('资源路径无效。')
        if name.endswith('.py'): ast.parse(text,filename=name,feature_version=(3,10))
    if any('http-links' in name or 'http_view' in name for name in files): raise ValueError('意外包含已放弃的HTTP功能。')
    release=json.loads(files['_release'])
    for filename in release['assets']:
        if hashlib.sha256(files['www/assets/'+filename].encode()).hexdigest()[:20] not in filename: raise ValueError('静态资源校验失败。')
    for name,value in json.loads(files.get('_binary_assets','{}')).items():
        if name not in {'www/app-icon-'+str(size)+'.png' for size in (180,192,512)} or not base64.b64decode(value,validate=True).startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('客户端图标无效。')


class Installation:
    def __init__(self):
        self.armed=False; self.created_user=False; self.default_target=None; self.bbr_before=None
        self.old_nginx_active=subprocess.run(['systemctl','is-active','--quiet','nginx'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
        self.old_certbot_enabled=None

    def cleanup(self):
        if not self.armed: return
        log('安装检查未通过，正在清理本次新建的服务和应用文件；诊断日志保留。')
        for unit in (*SERVICES,*TIMERS): subprocess.run(['systemctl','disable','--now',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for path in reversed(OWNED_FILES):
            if path.is_file() or path.is_symlink(): path.unlink()
        for path in reversed(OWNED_ROOTS):
            if path.exists(): shutil.rmtree(path)
        default=Path('/etc/nginx/sites-enabled/default')
        if self.default_target and not default.exists() and not default.is_symlink(): default.symlink_to(self.default_target)
        if self.bbr_before:
            for key,value in self.bbr_before.items(): subprocess.run(['sysctl','-w',key+'='+value],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if self.created_user: subprocess.run(['userdel','xray'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl','daemon-reload'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['nginx','-t'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl','restart' if self.old_nginx_active else 'stop','nginx'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if self.old_certbot_enabled is False: subprocess.run(['systemctl','disable','--now','certbot.timer'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def apply_bbr(transaction):
    subprocess.run(['modprobe','tcp_bbr'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    available=run(['sysctl','-n','net.ipv4.tcp_available_congestion_control']).split()
    if 'bbr' not in available:
        log('当前内核未提供BBR，已跳过；没有安装其他内核。'); return
    old={k:run(['sysctl','-n',k]).strip() for k in ('net.core.default_qdisc','net.ipv4.tcp_congestion_control')}
    transaction.bbr_before=old
    write('/etc/sysctl.d/90-loho-node-bbr.conf','net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n')
    run(['sysctl','-p','/etc/sysctl.d/90-loho-node-bbr.conf'])
    log('已启用内核自带BBR；实际收益取决于线路。')


def install(files):
    global LOG
    preflight()
    settings,username,password,bbr=settings_prompt()
    LOG=Path('/root/loho-install-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)+'.log'); write(LOG,'',0o600)
    transaction=Installation()
    try:
        log('[1/8] 安装系统依赖。')
        run(['apt-get','-o','DPkg::Lock::Timeout=120','update'])
        run(['env','DEBIAN_FRONTEND=noninteractive','apt-get','-o','DPkg::Lock::Timeout=120','install','-y','--no-install-recommends',
             'nginx','certbot','openssl','curl','ca-certificates','python3','libcrypt1','logrotate','iproute2','kmod','procps'])
        verify_reality_settings(settings)
        version=run(['nginx','-V']); match=re.search(r'nginx/(\d+)\.(\d+)\.(\d+)',version)
        if '--with-http_auth_request_module' not in version or '--with-http_v2_module' not in version: raise ValueError('系统Nginx缺少auth_request或HTTP/2模块。')
        modern=bool(match and tuple(map(int,match.groups()))>=(1,25,1))
        run(['nginx','-t']); transaction.armed=True
        transaction.old_certbot_enabled=subprocess.run(['systemctl','is-enabled','--quiet','certbot.timer'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
        log('[2/8] 下载并核验 Xray '+XRAY_VERSION+'。')
        with tempfile.TemporaryDirectory(prefix='loho-xray-') as work: download_xray(Path(work))
        run(['useradd','--system','--user-group','--home-dir','/var/lib/xray','--no-create-home','--shell','/usr/sbin/nologin','xray']); transaction.created_user=True
        uid,gid=pwd.getpwnam('xray').pw_uid,pwd.getpwnam('xray').pw_gid; webgid=pwd.getpwnam('www-data').pw_gid
        for path,mode,owner,group in [(CONF,0o750,0,gid),(APP,0o755,0,0),(APP/'www',0o755,0,0),(APP/'www/assets',0o755,0,0),
            (Path('/var/log/xray'),0o750,uid,gid),(Path('/var/lib/loho-node-view'),0o750,uid,webgid),
            (Path('/var/lib/loho-node-view/identity'),0o750,0,webgid),
            (Path('/var/lib/loho-node-traffic'),0o750,0,webgid),(Path('/var/lib/loho-node-traffic/public'),0o750,0,webgid)]: directory(path,mode,owner,group)
        for name in ('loho-node-manager','loho-node-auth','loho-node-notify','loho-node-maintenance'): directory('/var/lib/'+name,0o700)
        for name in ('loho-node-view','loho-node-manager','loho-node-auth','loho-node-notify'): directory('/etc/'+name,0o700)
        for name in ('access.log','error.log'): write(Path('/var/log/xray')/name,'',0o640,uid,gid)
        client={'id':str(uuid.uuid4()),'email':'ADMIN','level':0}
        state={'accounts':[{'key':secrets.token_hex(12),'enabled':True,'client':client}]}
        private_key, settings['reality']['public_key'] = reality_keypair()
        write(CONF/'config.json',json_text(xray_config(settings,[client],private_key)),0o640,0,gid)
        write('/etc/loho-node-manager/node.json',json_text(settings),0o600)
        write('/var/lib/loho-node-manager/state.json',json_text(state),0o600)
        secret=secrets.token_hex(32); write('/etc/loho-node-manager/key',secret+'\n',0o600)
        write('/etc/loho-node-view/traffic.json',json_text({'endpoint':settings['api_endpoint']}),0o600)
        hashed=run(['openssl','passwd','-6','-stdin'],input_text=password+'\n',secret=True).strip()
        if not hashed.startswith('$6$'): raise ValueError('密码散列格式异常。')
        write('/etc/loho-node-view/htpasswd',username+':'+hashed+'\n',0o600)
        write('/etc/loho-node-auth/settings.json',json_text({'origin':origin(settings),'version':VERSION}),0o600)
        write('/etc/loho-node-notify/node.json',json_text({'origin':origin(settings)}),0o600)
        write('/etc/loho-node-view/state.json',json_text({'version':VERSION,'history_days':7,'config':str(SITE),'nginx_config':str(SITE),'username':username,'nginx_gid':webgid}),0o600)
        log('[3/8] 安装完整管理系统和私有后台。')
        for name,text in files.items():
            if not name.startswith('_'): write(APP/name,text,0o644)
        for name,value in json.loads(files.get('_binary_assets','{}')).items():write(APP/name,base64.b64decode(value,validate=True),0o644)
        # This persisted entry contains all runtime source and supports future diagnostics.
        write(APP/'bootstrap.py',Path(__file__).read_bytes(),0o700)
        write('/usr/local/bin/loho-node','#!/bin/sh\nexec /usr/bin/python3 /opt/loho-node-view/maintenance.py "$@"\n',0o750)
        namespace={'__name__':'fresh_identity'}; exec(compile(files['identities.py'],'identities.py','exec'),namespace)
        namespace['sync_catalog'](state,path=Path('/var/lib/loho-node-view/identity/accounts.json'))
        os.chown(Path('/var/lib/loho-node-view/identity/accounts.json'),0,webgid)
        release=json.loads(files['_release']); release.update(version=VERSION,installed=time.time(),certificate='/etc/letsencrypt/live/loho-'+settings['domain']+'/fullchain.pem',transport='xhttp',xray_version=XRAY_VERSION)
        write('/etc/loho-node-view/release.json',json_text(release),0o600)
        write(SNIPPET,files['_nginx_snippet'].replace('@@SECRET@@',secret),0o600)
        write('/etc/systemd/system/xray.service',xray_unit(settings))
        for name in SERVICES[1:]: write(Path('/etc/systemd/system')/(name+'.service'),files['_'+name.removeprefix('loho-node-').replace('view','collector')+'_unit'])
        for name,text in maintenance_units().items(): write(Path('/etc/systemd/system')/name,text)
        write('/etc/logrotate.d/loho-xray',rotate_config(settings))
        for path in ('/var/www/loho-acme','/var/www/loho-acme/.well-known','/var/www/loho-acme/.well-known/acme-challenge','/var/www/loho-site'): directory(path,0o755)
        write('/var/www/loho-site/index.html','<!doctype html><html><meta charset="utf-8"><title>Welcome</title><h1>Welcome</h1><p>This site is online.</p></html>\n')
        run(['env','XRAY_LOCATION_ASSET=/usr/local/share/xray','/usr/local/bin/xray','run','-test','-config',str(CONF/'config.json')])
        log('[4/8] 申请HTTPS证书（公网TCP80验证）。')
        default=Path('/etc/nginx/sites-enabled/default')
        if default.is_symlink(): transaction.default_target=os.readlink(default); default.unlink()
        write(SITE,nginx_config(settings,tls=False,modern=modern)); ENABLED.symlink_to(SITE)
        run(['nginx','-t']); run(['systemctl','enable','--now','nginx']); run(['systemctl','reload','nginx'])
        # Wait for the new HTTP worker before ACME's external validation begins.
        token=secrets.token_hex(16); challenge=Path('/var/www/loho-acme/.well-known/acme-challenge')/token; write(challenge,token)
        def challenge_ready():
            c=http.client.HTTPConnection('127.0.0.1',80,timeout=3)
            try:
                c.request('GET','/.well-known/acme-challenge/'+token,headers={'Host':settings['domain']}); r=c.getresponse()
                if r.status!=200 or r.read(1024).decode()!=token: raise ValueError('验证路径尚未就绪。')
            finally: c.close()
        until_ready(challenge_ready,'等待证书验证路径'); challenge.unlink()
        certargs=['certbot','certonly','--webroot','-w','/var/www/loho-acme','-d',settings['domain'],'--cert-name','loho-'+settings['domain'],'--agree-tos','--non-interactive','--keep-until-expiring']
        helptext=run(['certbot','--help','all'])
        if '--register-unsafely-without-email' in helptext: certargs.append('--register-unsafely-without-email')
        run(certargs)
        log('[5/8] 启用节点、网站登录、采集和通知服务。')
        write('/etc/letsencrypt/renewal-hooks/deploy/loho-nginx','#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\n/usr/bin/systemctl reload nginx\n',0o750)
        write(SITE,nginx_config(settings,modern=modern)); run(['nginx','-t']); run(['systemctl','daemon-reload'])
        run(['systemctl','enable','--now',*SERVICES,'certbot.timer',*TIMERS])
        run(['systemctl','reload','nginx'])
        def services_ready():
            for service in SERVICES:
                if subprocess.run(['systemctl','is-active','--quiet',service],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode: raise ValueError(service+'没有运行。')
            if not socket_json('auth','/health',secret,auth=True).get('ok'): raise ValueError('登录服务未就绪。')
            value=socket_json('manager','/users',secret)
            if len(value['users'])!=1 or value['users'][0]['label']!='ADMIN': raise ValueError('用户目录异常。')
            if socket_json('notify','/state',secret).get('health',{}).get('reader'): raise ValueError('通知采集尚未就绪。')
            for filename in ('/var/lib/loho-node-view/activity.json','/var/lib/loho-node-traffic/public/users-live.json'):
                value=json.loads(Path(filename).read_text())
                if value.get('error') or time.time()-value.get('generated',0)>30: raise ValueError('采集数据未就绪。')
        until_ready(services_ready,'等待后台服务')
        log('[6/8] 验证TLS、登录、用户管理和数据入口。'); check_gateway(settings,username,password)
        run(['/usr/local/bin/loho-node','check','--probe'],timeout=140)
        log('[7/8] 检查自动续期、日志轮换和可选BBR。')
        run(['certbot','renew','--cert-name','loho-'+settings['domain'],'--dry-run','--run-deploy-hooks'],timeout=300)
        run(['logrotate','--debug','/etc/logrotate.d/loho-xray'])
        if bbr: apply_bbr(transaction)
        run(['/usr/local/bin/loho-node','health']); run(['/usr/local/bin/loho-node','status'])
        namespace={'__name__':'fresh_credentials'}; exec(compile(files['manage_users.py'],'manage_users.py','exec'),namespace)
        report=['LoHo 完整一键部署 '+VERSION,'面板：'+origin(settings)+'/node-view/','网站账号：'+username,'网站密码：安装时设置，不保存明文。','代理用户 ADMIN：']
        report.extend(['REALITY 节点',namespace['link'](client,'ADMIN'),''])
        report.extend(['节点二维码在登录后的用户管理页面查看。','当前用户链接：sudo loho-node links',
                       '诊断：sudo loho-node status','续期演练：sudo loho-node renew-test','安装日志：'+str(LOG)])
        write(REPORT,'\n'.join(report)+'\n',0o600); transaction.armed=False
        log('[8/8] 安装完成：'+origin(settings)+'/node-view/')
        log('仅创建代理用户ADMIN；网站账号 '+username+'，密码为刚才设置的密码。')
        log('查看专属导入链接：sudo cat /root/loho-node-install.txt；二维码在网页用户管理中。')
        log('请用电脑和手机导入节点，验证外部网络实际连接。')
    except BaseException:
        try: transaction.cleanup()
        except BaseException as error: log('清理未完全完成：'+str(error))
        log('未完成安装；诊断日志：'+str(LOG)); raise


def main(files):
    parser=argparse.ArgumentParser(description='LoHo完整一键部署 '+VERSION)
    group=parser.add_mutually_exclusive_group(); group.add_argument('--check',action='store_true'); group.add_argument('--preflight',action='store_true'); group.add_argument('--extract-source',metavar='DIR')
    args=parser.parse_args(); verify_files(files)
    if args.check: print('内置Python代码、静态资源散列和完整性检查通过；没有安装软件。'); return
    if args.extract_source:
        target=Path(args.extract_source).resolve()
        if target.exists(): raise ValueError('源码导出目录已存在，未覆盖。')
        target.mkdir(mode=0o700,parents=True)
        for name,text in files.items():
            path=target/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text)
        for name,value in json.loads(files.get('_binary_assets','{}')).items():(target/name).write_bytes(base64.b64decode(value,validate=True))
        (target/'installer.py').write_bytes(Path(__file__).read_bytes()); print('完整源码已导出：'+str(target)); return
    if args.preflight: preflight(); return
    if os.geteuid()!=0: raise ValueError('请用 sudo bash 运行。')
    os.umask(0o077)
    fd=os.open('/run/loho-node-install.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('已有安装或升级正在运行。')
        install(files)
