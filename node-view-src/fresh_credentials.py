"""Node-specific settings and atomic config helpers for fresh installations."""
import json
import os
from pathlib import Path
import secrets
from urllib.parse import quote, urlencode

SETTINGS = json.loads(Path('/etc/loho-node-manager/node.json').read_text())
CONFIG = Path('/usr/local/etc/xray/config.json')
XRAY = '/usr/local/bin/xray'
DOMAIN, PORT = SETTINGS['domain'], SETTINGS['port']
REALITY = SETTINGS['reality']


def managed_inbound(config):
    found = [b for b in config.get('inbounds', []) if b.get('tag') == REALITY['inbound_tag']]
    if len(found) != 1 or not isinstance(found[0].get('settings', {}).get('clients'), list):
        raise ValueError('没有找到唯一的受管理节点入站，未修改配置。')
    inbound = found[0]
    stream = inbound.get('streamSettings', {})
    if inbound.get('protocol') != 'vless' or stream.get('network') != 'xhttp' or stream.get('security') != 'reality':
        raise ValueError('受管理节点入站的协议或安全方式不一致，未修改配置。')
    return inbound


def link(client, label):
    query = {'encryption':'none','security':'reality','type':'xhttp','mode':'packet-up',
             'sni':REALITY['sni'],'fp':REALITY['fingerprint'],'pbk':REALITY['public_key'],
             'sid':REALITY['short_id'],'path':REALITY['path']}
    return 'vless://%s@%s:%s?%s#%s' % (
        client['id'], DOMAIN, REALITY['port'], urlencode(query, quote_via=quote), quote(label))


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
