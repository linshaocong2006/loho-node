"""Node-specific settings and atomic config helpers for fresh installations."""
import json
import os
from pathlib import Path
import secrets
from urllib.parse import quote, urlencode

SETTINGS = json.loads(Path('/etc/loho-node-manager/node.json').read_text())
CONFIG = Path('/usr/local/etc/xray/config.json')
XRAY = '/usr/local/bin/xray'
DOMAIN, ADDRESS, PORT = SETTINGS['domain'], SETTINGS['address'], SETTINGS['port']


def managed_inbound(config):
    return managed_inbounds(config)[0]


def checked_inbound(config, tag, transport, security):
    found = [b for b in config.get('inbounds', []) if b.get('tag') == tag]
    if len(found) != 1 or not isinstance(found[0].get('settings', {}).get('clients'), list):
        raise ValueError('没有找到唯一的受管理节点入站，未修改配置。')
    inbound = found[0]
    stream = inbound.get('streamSettings', {})
    if inbound.get('protocol') != 'vless' or stream.get('network') != transport or stream.get('security', 'none') != security:
        raise ValueError('受管理节点入站的协议或安全方式不一致，未修改配置。')
    return inbound


def managed_inbounds(config):
    legacy = checked_inbound(config, SETTINGS['inbound_tag'], SETTINGS['transport'], 'none')
    reality = SETTINGS.get('reality')
    if not reality:
        return (legacy,)
    primary = checked_inbound(config, reality['inbound_tag'], 'xhttp', 'reality')
    if primary['settings']['clients'] != legacy['settings']['clients']:
        raise ValueError('REALITY 和 TLS 入站的用户不同步，未修改配置。')
    return legacy, primary


def link(client, label, path, *, use_reality=True):
    reality = SETTINGS.get('reality') if use_reality else None
    port = PORT
    if reality:
        port = reality['port']
        query = {'encryption':'none','security':'reality','type':'xhttp','mode':'packet-up',
                 'sni':reality['sni'],'fp':reality['fingerprint'],'pbk':reality['public_key'],
                 'sid':reality['short_id'],'path':reality['path']}
    else:
        transport = SETTINGS['transport']
        query = {'encryption':'none','security':'tls','sni':DOMAIN,'type':transport,
                 'host':DOMAIN,'path':path,'alpn':'h2,http/1.1' if transport=='xhttp' else 'http/1.1'}
        if transport == 'xhttp':
            query['mode'] = 'packet-up'
    return 'vless://%s@%s:%s?%s#%s' % (
        client['id'], ADDRESS, port, urlencode(query, quote_via=quote), quote(label))


def credential_links(client, label, path):
    if SETTINGS.get('reality'):
        yield 'REALITY 主节点', link(client, label, path)
        yield 'TLS 备用节点', link(client, label+'-TLS', path, use_reality=False)
    else:
        yield 'TLS 节点', link(client, label, path)


def replace(path, data, metadata=None, mode=0o600):
    temporary = path.with_name(path.name + '.tmp-' + secrets.token_hex(4))
    try:
        with temporary.open('wb') as stream:
            os.fchmod(stream.fileno(), mode if metadata is None else metadata.st_mode & 0o777)
            if metadata is not None:
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()
