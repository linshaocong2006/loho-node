'use strict';
const $=id=>document.getElementById(id), api='manager/api/';
let data=null, pending=null, working=false, qrURL=null, activeJob=null,lastRead=0,listSignature='',refreshing=null,readError='';
function notice(message){$('notice').textContent=message;$('notice').hidden=!message;}
function busy(value){working=value;document.querySelectorAll('button').forEach(b=>{if(!['close-credential','copy'].includes(b.id))b.disabled=value||(b.id==='add'&&!data);});$('new-label').disabled=value;$('edit-form').setAttribute('aria-busy',String(value));}
async function request(route,body){
  const options={cache:'no-store',credentials:'same-origin',headers:{}};
  if(body){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':data.csrf};options.body=JSON.stringify(body);}
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);options.signal=controller.signal;
  try{const response=await fetch(api+route,options);if(response.status===401){const error=new Error('网站登录已过期，请重新登录。');error.status=401;throw error;}
    const value=await response.json().catch(()=>{throw new Error('管理接口未就绪，请刷新或检查升级是否成功。');});
    if(!response.ok){const error=new Error(value.error||'请求失败');error.status=response.status;throw error;}return value;
  }finally{clearTimeout(timer);}
}
function button(text,fn){const b=document.createElement('button');b.className='button'+(text==='删除用户'?' danger':'');b.textContent=text;b.onclick=fn;return b;}
function refresh(){if(refreshing)return refreshing;$('refresh').disabled=true;$('refresh').textContent='刷新中…';$('accounts').setAttribute('aria-busy','true');refreshing=readAccounts().finally(()=>{refreshing=null;$('refresh').disabled=working;$('refresh').textContent='刷新';$('accounts').setAttribute('aria-busy','false');});return refreshing;}
async function readAccounts(){
  try{data=await request('users');if(readError&&$('notice').textContent===readError)notice('');readError='';$('summary').textContent=`${data.users.filter(u=>u.enabled).length} 启用 / ${data.users.length} 总数`;
    lastRead=Date.now();const signature=JSON.stringify(data.users);if(signature===listSignature){busy(working);return;}listSignature=signature;const fragment=document.createDocumentFragment();
    for(const user of data.users){const row=document.createElement('article');row.className='account'+(user.enabled?'':' disabled');row.setAttribute('aria-label',user.label);row.dataset.accountKey=user.key;
      const info=document.createElement('div'), title=document.createElement('h3'), name=document.createElement('span');name.className='account-name';name.textContent=user.label;name.title=user.label;title.append(name);
      const state=document.createElement('span');state.className='state'+(user.enabled?'':' off');state.textContent=user.enabled?'使用中':'已暂停';title.append(state);
      const id=document.createElement('code');id.textContent=user.masked;info.className='account-info';
      const avatar=button(Array.from(user.label)[0]||'U',()=>window.NodeViewProfiles?.editUser(user.key,user.label));avatar.className='managed-avatar';avatar.setAttribute('aria-label','更换 '+user.label+' 的头像');window.NodeViewProfiles?.decorate(avatar,'user',user.key);info.append(avatar,title,id);
      if(user.label==='SHARED'){const note=document.createElement('p');note.className='shared-note';note.textContent='旧共享账号 · 切换完成后再删除';info.append(note);}
      const actions=document.createElement('div');actions.className='actions';actions.append(button('查看配置',()=>credential(user)),button(user.enabled?'暂停用户':'恢复使用',()=>edit(user.enabled?'disable':'enable',user)),button('修改标记',()=>edit('rename',user)),button('删除用户',()=>edit('delete',user)));
      row.append(info,actions);fragment.append(row);
    }
    $('accounts').replaceChildren(fragment);busy(working);
  }catch(error){if(error.name==='AbortError'&&window.NodeViewClient?.isHidden())return;readError=error.name==='AbortError'?'读取超时，已保留上次用户列表，请重试。':error.message;notice(readError);throw error;}
}
function edit(action,user){
  if(working||!data)return;
  pending={action,key:user?.key};$('edit-title').textContent={add:'新增用户',rename:'修改用户标记',delete:'删除节点用户',disable:'暂停用户',enable:'恢复使用'}[action];
  const needsLabel=['add','rename','delete'].includes(action);
  $('label-wrap').hidden=!needsLabel;$('new-label').required=needsLabel;$('new-label').maxLength=action==='delete'?128:32;
  $('label-wrap').firstChild.textContent=action==='delete'?'输入用户标记确认删除':'用户标记';
  $('new-label').value=action==='rename'?user.label:'';$('new-label').placeholder=action==='delete'?user.label:'例如 JWX';
  $('apply').textContent=({delete:'确认删除',disable:'确认暂停',enable:'确认恢复'})[action]||'确认应用';$('apply').classList.toggle('danger',action==='delete');
  $('edit-description').textContent=action==='disable'?`暂停 ${user.label} 后，该用户暂时无法通过节点建立新的连接。保留原 UUID、导入链接和历史记录，之后可随时恢复。网站管理员登录不受影响。`:action==='enable'?`恢复 ${user.label}，继续使用原来的导入链接和 UUID，无需重新分发配置。`:action==='add'?'创建独立 UUID，应用后可在列表查看导入链接。':action==='rename'?`${user.label} 的 UUID 保持不变，新日志使用新标记。`:`删除 ${user.label} 后，原链接和二维码失效，用户将从管理列表、首页状态和实时列表中移除。历史记录保留至 7 天到期。请按原样输入 ${user.label} 确认。${user.label==='ADMIN'?' 这可能是你当前使用的代理账号，删除会使该连接失效。':''}${user.label==='SHARED'?' 请确认其他人已换成个人配置。':''}`;
  $('edit-dialog').showModal();
}
async function credential(user){
  busy(true);notice('正在读取专属配置…');
  try{const value=await request('credential?key='+encodeURIComponent(user.key));if(window.NodeViewClient?.isHidden())return;$('credential-title').textContent=value.label+' · 专属配置';$('credential-note').textContent=value.enabled?'账号使用中':'账号已暂停；恢复后此配置才可连接';$('uuid').value=value.uuid;$('import-link').value=value.link;
    if(qrURL)URL.revokeObjectURL(qrURL);qrURL=URL.createObjectURL(new Blob([value.svg],{type:'image/svg+xml'}));$('qr').src=qrURL;$('copy-status').textContent='';notice('');$('credential-dialog').showModal();
  }catch(error){if(!window.NodeViewClient?.isHidden())notice(error.name==='AbortError'?'配置读取超时，请重试。':error.message);}finally{if($('notice').textContent==='正在读取专属配置…')notice('');busy(false);}
}
function clearCredential(){if(qrURL)URL.revokeObjectURL(qrURL);qrURL=null;$('qr').removeAttribute('src');$('uuid').value='';$('import-link').value='';}
async function pollJob(identifier){
  activeJob=identifier;busy(true);let failures=0;
  for(let attempt=0;attempt<100;attempt++){
    if(window.NodeViewClient?.isHidden())await new Promise(resolve=>window.NodeViewClient.whenActive(resolve));
    try{const result=await request('job?id='+encodeURIComponent(identifier));failures=0;
      if(result.status!=='running'){sessionStorage.removeItem('node-manager-job');activeJob=null;notice(result.message);busy(false);await refresh();window.NodeViewClient?.changed();return;}
      notice('正在检查并应用配置……代理可能短暂重连，请稍候。');
    }catch(error){if(error.status===404||error.status===401){sessionStorage.removeItem('node-manager-job');activeJob=null;busy(false);if(error.status===404)await refresh().catch(()=>{});notice(error.status===404?'任务状态已过期。请核对当前列表后再操作。':error.message);return;}failures++;notice('正在等待连接恢复……'+(failures>3?'可稍后刷新页面继续查看结果。':''));}
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  busy(false);activeJob=null;notice('暂时无法确认应用结果。请恢复网络后刷新，系统会继续查询这次任务。');
}
$('edit-form').addEventListener('submit',async event=>{
  event.preventDefault();if(working)return;busy(true);
  try{const value=await request('change',{...pending,label:$('new-label').value,revision:data.revision});$('edit-dialog').close();sessionStorage.setItem('node-manager-job',value.job);await pollJob(value.job);
  }catch(error){$('edit-dialog').close();notice(error.message+' 若刚发生断线，请刷新列表确认后再操作。');busy(false);}
});
$('add').onclick=()=>edit('add');$('refresh').onclick=()=>{if(!working){notice('');refresh().catch(()=>{});}};
$('cancel-edit').onclick=()=>$('edit-dialog').close();$('close-credential').onclick=()=>$('credential-dialog').close();$('credential-dialog').addEventListener('close',clearCredential);
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('import-link').value);$('copy-status').textContent='已复制导入链接';}catch{$('import-link').focus();$('import-link').select();$('copy-status').textContent='请长按或使用 Ctrl+C 复制已选中的链接。';}};
const start=async()=>{const job=sessionStorage.getItem('node-manager-job');if(job){await pollJob(job);}else{await refresh().catch(()=>{});}};
$('add').disabled=true;
$('edit-dialog').addEventListener('cancel',event=>{if(working)event.preventDefault();});
if(window.NodeViewClient)window.NodeViewClient.whenActive(start);else start();
function resume(){if(window.NodeViewClient?.isHidden()||working||refreshing||Date.now()-lastRead<10000)return;refresh().catch(()=>{});}
document.addEventListener('visibilitychange',resume);
document.addEventListener('nodeview:changed',()=>{lastRead=0;resume();});
