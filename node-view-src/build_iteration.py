import ast,base64,gzip,hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parent
helpers=(root/'notify_upgrade_core.py').read_text();tree=ast.parse(helpers)
shared='\n\n'.join(ast.get_source_segment(helpers,node) for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('command','replace','deployment'))
auth=(root/'auth_upgrade_core.py').read_text().replace('from notify_upgrade_core import command, replace, deployment',shared)
files={'_auth_core':auth,'_perf_core':(root/'performance_upgrade_core.py').read_text(),'auth.py':(root/'auth.py').read_text(),'identities.py':(root/'identities.py').read_text(),'system_health.py':(root/'system_health.py').read_text(),'_history':(root/'v8/history.py').read_text(),'_traffic':(root/'v8/traffic.py').read_text()}
for name in ('panel-core.js','iteration.css','system.html','system.js','login.html','login.js','auth.css','session.js','security.html','notifications-v8.js','connections-v8.js','query-v8.js','overview-v8.js','fast-core.js','performance.css'):files[name]=(root/'web'/name).read_text()
files['avatar_store.py']=(root/'avatar_store.py').read_text()
files['identity-v1.js']=(root/'web/identity-v1.js').read_text()
files['_manager_features']=(root/'manager.py').read_text()
files['_experience']=(root/'experience_core.py').read_text()
files['notify.py']=(root/'notify.py').read_text()
for name in ('theme.js','theme.css','experience.css','rule-users.html'):files[name]=(root/'web'/name).read_text()
for name in ('client-boot.js','client-shell.js','client-router.js','profiles.js','client.css','client.webmanifest','client-sw.js','client-icons.json','manager.js'):files[name]=(root/'web'/name).read_text()
for name,source in files.items():
 if name.endswith('.py') or name.startswith('_'):ast.parse(source,feature_version=(3,9))
core=(root/'iteration_upgrade_core.py').read_text().replace('from notify_upgrade_core import command,replace,deployment',shared)
payload=base64.b64encode(gzip.compress(json.dumps(files,ensure_ascii=False).encode(),mtime=0)).decode()
source=core+'\nPAYLOAD=(\n'+'\n'.join('    '+repr(payload[n:n+100]) for n in range(0,len(payload),100))+'\n)\nif __name__=="__main__":main()\n'
ast.parse(source,feature_version=(3,9));output=root.parent/'deliverables/loho-node-upgrade.py';output.write_text(source)
print(output);print('bytes',output.stat().st_size);print('sha256',hashlib.sha256(output.read_bytes()).hexdigest())
