import base64
import copy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

# Test the actual extracted installer runtime, without touching host services.
import sys,types,os
stage=Path(sys.argv.pop(1)).resolve();sys.path.insert(0,str(stage))
helpers=types.ModuleType('manage_users');helpers.CONFIG=Path('/unused-test-config');helpers.XRAY='/unused-test-xray'
helpers.DOMAIN='test.invalid';helpers.PORT=8443
helpers.link=lambda *args:'vless://fixture.invalid'
def replace(path,data,metadata=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.test-tmp');temporary.write_bytes(data)
    os.chmod(temporary,metadata.st_mode&0o777 if metadata else 0o600);os.replace(temporary,path)
helpers.replace=replace;helpers.managed_inbound=lambda config:config['inbounds'][0]
sys.modules['manage_users']=helpers
import avatar_store as avatars
import manager as manager
import identities
class RuntimeFixture:
    def fixture(self,folder):
        root=Path(folder);config=root/'config.json'
        config.write_text(json.dumps({'log':{'access':'unchanged'},'inbounds':[{'tag':'loho-node','protocol':'vless','port':33150,'settings':{'clients':[
            {'id':'11111111-1111-4111-8111-111111111111','email':'ADMIN','level':0},
            {'id':'22222222-2222-4222-8222-222222222222','email':'LOHO','level':0}]},'streamSettings':{'network':'xhttp','xhttpSettings':{'path':'/test/'}}}],'outbounds':[{'protocol':'freedom'}]}))
        identities.PATH=root/'identity/accounts.json'
        return {'ROOT':root,'STATE':root/'state.json','JOURNAL':root/'pending.json','CONFIG':config}


def png(size=4,extra=b''):
    header=struct.pack('!IIBBBBB',size,size,8,6,0,0,0)
    pixels=(b'\0'+b'\x52\x89\x67\xff'*size)*size
    return avatars.SIGNATURE+avatars.chunk(b'IHDR',header)+extra+avatars.chunk(b'IDAT',zlib.compress(pixels))+avatars.chunk(b'IEND',b'')


class AvatarTests(unittest.TestCase):
    def test_canonical_image_drops_metadata_and_rejects_invalid_or_bomb_images(self):
        clean=png();self.assertEqual(avatars.normalize_png(clean),clean)
        image=png(extra=avatars.chunk(b'tEXt',b'location\0not retained'))
        self.assertEqual(avatars.normalize_png(image),clean)
        header=struct.pack('!IIBBBBB',4,4,8,6,0,0,0)
        bomb=avatars.SIGNATURE+avatars.chunk(b'IHDR',header)+avatars.chunk(b'IDAT',zlib.compress(b'X'*1000000))+avatars.chunk(b'IEND',b'')
        for invalid in [b'<svg onload="alert(1)"/>',b'\xff\xd8not-a-png',clean[:-1],clean+b'extra',png(257),bomb,clean[:30]+b'bad'+clean[33:]]:
            with self.assertRaises(ValueError):avatars.normalize_png(invalid)

    def test_storage_is_private_atomic_conflict_checked_and_scoped_by_stable_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            store=avatars.Store(Path(folder)/'profiles');encoded=base64.b64encode(png()).decode()
            a=store.write('user','stable-key','admin',encoded,'');self.assertTrue(a['version'])
            self.assertEqual(store.info('user','stable-key','other-admin'),a)
            self.assertEqual(store.path('user','stable-key','admin').stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):store.write('user','stable-key','admin',None,'')
            self.assertEqual(store.info('user','stable-key','admin'),a)
            admin=store.write('admin','','admin',encoded,'')
            self.assertNotEqual(store.path('admin','','admin'),store.path('admin','','another-admin'))
            self.assertEqual(store.info('admin','','another-admin')['url'],'')
            for kind,key in [('user','../../outside'),('admin','another-admin'),('unknown','')]:
                with self.assertRaises(ValueError):store.path(kind,key,'admin')
            self.assertEqual(store.write('user','stable-key','admin',None,a['version'])['url'],'')
            self.assertFalse(list(store.root.glob('*.tmp-*')))

    def test_real_http_auth_csrf_cross_device_sync_and_upload_never_mutates_proxy(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture=RuntimeFixture().fixture(folder);fixture.update(KEY=Path(folder)/'key',BUSY=False)
            with patch.multiple(manager,**fixture),patch.object(manager,'run') as run:
                manager.KEY.write_text('a'*64)
                Handler=avatars.extend_handler(manager.Handler,vars(manager))
                class Server(manager.Server):address_family=socket.AF_INET
                server=Server(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                def request(method,path,actor='loho',body=None,token=None,origin=None,trusted=True):
                    headers={'X-LoHo-Actor':actor,'X-LoHo-Manager-Key':'a'*64 if trusted else 'wrong'}
                    if body is not None:headers['Content-Type']='application/json';headers['Origin']=origin or manager.ORIGIN;headers['X-CSRF-Token']=token or ''
                    c=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5);c.request(method,path,json.dumps(body) if body is not None else None,headers);r=c.getresponse();result=(r.status,dict(r.getheaders()),r.read());c.close();return result
                try:
                    self.assertEqual(request('GET','/profiles',trusted=False)[0],403)
                    status,_,raw=request('GET','/profiles');self.assertEqual(status,200);profile=json.loads(raw)
                    _,_,state,_=manager.load();target=state['accounts'][1];before=manager.CONFIG.read_bytes();state_before=manager.STATE.read_bytes()
                    image=base64.b64encode(png()).decode();body={'kind':'user','key':target['key'],'version':'','image':image}
                    self.assertEqual(request('POST','/profile',body=body,token=profile['csrf'],origin='https://evil.invalid')[0],403)
                    self.assertEqual(request('POST','/profile',body=body,token='invalid')[0],403)
                    status,_,raw=request('POST','/profile',body=body,token=profile['csrf']);self.assertEqual(status,200,raw);info=json.loads(raw)
                    self.assertEqual(manager.CONFIG.read_bytes(),before);self.assertEqual(manager.STATE.read_bytes(),state_before)
                    # Another device/administrator sees the same node image via an independent request.
                    public=json.loads(request('GET','/profiles',actor='another-admin')[2]);self.assertEqual(public['users'][target['key']],info)
                    self.assertNotIn(target['client']['id'],json.dumps(public))
                    path=info['url'].removeprefix('/node-view/manager/api')
                    status,headers,raw=request('GET',path);self.assertEqual(status,200);self.assertEqual(headers['Content-Type'],'image/png');self.assertEqual(raw,png())
                    self.assertEqual(request('POST','/profile',body=body,token=profile['csrf'])[0],409)
                    renamed=copy.deepcopy(state);renamed['accounts'][1]['client']['email']='RENAMED'
                    config=json.loads(manager.CONFIG.read_text());manager.inbound(config)['settings']['clients'][1]['email']='RENAMED';manager.CONFIG.write_bytes(manager.encoded(config));manager.STATE.write_bytes(manager.encoded(renamed))
                    self.assertEqual(json.loads(request('GET','/profiles')[2])['users'][target['key']],info)
                    # Removing an avatar has no dependency on node enabled state.
                    body.update(image=None,version=info['version']);self.assertEqual(request('POST','/profile',body=body,token=profile['csrf'])[0],200)
                    self.assertEqual(request('GET',path)[0],404)
                    admin=dict(body,kind='admin',key='',version='',image=image)
                    self.assertEqual(request('POST','/profile',body=admin,token=profile['csrf'])[0],200)
                    self.assertEqual(json.loads(request('GET','/profiles',actor='another-admin')[2])['admin']['url'],'')
                    unknown=dict(body,key='removed-key',version='',image=image)
                    self.assertEqual(request('POST','/profile',body=unknown,token=profile['csrf'])[0],409)
                    self.assertNotIn(b'profiles',before);self.assertNotIn(b'avatar',state_before)
                    run.assert_not_called()
                finally:server.shutdown();server.server_close();thread.join()

    def test_pause_all_and_resume_keep_original_credentials_after_reload(self):
        with tempfile.TemporaryDirectory() as folder,patch.multiple(manager,**RuntimeFixture().fixture(folder)):
            raw,config,state,_=manager.load();originals={a['key']:a['client']['id'] for a in state['accounts']}
            for account in list(state['accounts']):
                updated,new_state=manager.change(config,state,'disable',account['key'],None)
                with patch.object(manager,'run'),patch.object(manager,'wait_active'):manager.transaction(raw,state,updated,new_state)
                raw,config,state,_=manager.load()
            self.assertEqual(manager.inbound(config)['settings']['clients'],[])
            self.assertTrue(all(not a['enabled'] for a in state['accounts']))
            self.assertEqual({a['key']:a['client']['id'] for a in state['accounts']},originals)
            updated,new_state=manager.change(config,state,'enable',state['accounts'][0]['key'],None)
            with patch.object(manager,'run'),patch.object(manager,'wait_active'):manager.transaction(raw,state,updated,new_state)
            _,config,state,_=manager.load();self.assertEqual(manager.inbound(config)['settings']['clients'][0]['id'],next(iter(originals.values())))


if __name__=='__main__':unittest.main()
