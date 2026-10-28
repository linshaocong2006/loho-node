'use strict';
const $=id=>document.getElementById(id);
let state=null,working=false,editing=null,settingsDirty=false,readFailure='';
const readGate=window.NodeViewPanel.readGate();let refreshTimer=null,refreshFailures=0,previewController=null;
const delivery={pending:'等待重试 / 发送',sending:'正在发送',sent:'已提交 ntfy',failed:'发送失败',cancelled:'已取消'};
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
function busy(value){working=value;if(value){readGate.cancel();if(previewController)previewController.abort();}document.querySelectorAll('section.viewer button, #rule-dialog button').forEach(button=>button.disabled=value||(!state&&button.id!=='refresh'));document.querySelectorAll('#channel-form input, #rule-form input, #rule-form select').forEach(input=>input.disabled=value||!state);$('channel-form').setAttribute('aria-busy',String(value));$('rule-form').setAttribute('aria-busy',String(value));}
function readError(error){readFailure=error.message+(state?' 已保留上次数据，将自动重试。':' 请检查网络后点击刷新。');notice(readFailure);}
async function request(path,body,signal){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
  const cancel=()=>controller.abort();if(signal){signal.addEventListener('abort',cancel,{once:true});if(signal.aborted)controller.abort();}
  try{
    const response=await fetch('notify/api/'+path,{cache:'no-store',credentials:'same-origin',signal:controller.signal,
      ...(body?{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},body:JSON.stringify({...body,revision:state.revision})}:{})});
    if(response.status===401)throw new Error('管理员登录已过期，请重新登录。');
    let value;try{value=await response.json();}catch{throw new Error('通知接口暂不可用（HTTP '+response.status+'），请检查通知服务。');}if(!response.ok)throw new Error(value.error||'操作失败，请刷新确认。');return value;
  }catch(error){if(error.name==='AbortError')throw new Error('请求超时，请刷新确认操作结果。');throw error;}finally{clearTimeout(timer);if(signal)signal.removeEventListener('abort',cancel);}
}
function el(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function action(text,handler,danger=false){const button=el('button',text,'button'+(danger?' danger':''));button.type='button';button.onclick=handler;return button;}
function members(rule){return Array.isArray(rule?.accounts)?rule.accounts:rule?.account?[rule.account]:[];}
function selectedUsers(){return Array.from(document.querySelectorAll('#rule-users input:checked'),input=>input.value);}
function selectionChanged(){const count=selectedUsers().length;$('rule-selection-count').textContent='已选 '+count+' 人';preview();}
function renderSelection(rule){
 const selected=new Set(members(rule)),roster=new Map(state.users.map(user=>[user.key,user.label]));
 const keys=[...roster.keys(),...[...selected].filter(key=>!roster.has(key))];
 $('rule-users').replaceChildren(...keys.map(key=>{
  const available=roster.has(key),label=el('label',undefined,'rule-user-option'+(available?'':' unavailable'));
  const input=el('input');input.type='checkbox';input.name='rule-account';input.value=key;input.checked=selected.has(key);input.dataset.available=String(available);
  input.addEventListener('change',selectionChanged);label.append(input,el('span',roster.get(key)||'已停用或删除（不触发）'));return label;
 }));
 selectionChanged();
}
function renderSettings(){
  const s=state.settings;
  $('channel-state').textContent=s.enabled?'规则通知已启用':'规则通知已暂停';
  $('server').value=s.server;$('topic').value=s.topic;$('enabled').checked=s.enabled;$('details').checked=s.details;
  $('token').value='';$('clear-token').checked=false;$('token-state').textContent=s.token_set?'已有令牌；留空可保留，服务地址变更时请核对令牌。':'未设置令牌；需要登录的 ntfy 服务请填写令牌。';
}
function renderRules(){
  const roster=new Map(state.users.map(user=>[user.key,user.label]));$('rule-count').textContent=state.rules.length+' 条';
  const nodes=state.rules.map(rule=>{
    const keys=members(rule),active=keys.filter(key=>roster.has(key)),labels=keys.map(key=>roster.get(key)||'已停用或删除'),hasUser=active.length>0;
    const row=el('article',undefined,'account'+(!rule.enabled||!hasUser?' disabled':'')),info=el('div');
    const title=el('h3',labels.slice(0,3).join('、')+(labels.length>3?' 等 '+labels.length+' 人':''));title.title=labels.join('、');title.append(el('span',!hasUser?'不再触发':rule.enabled?'启用':'暂停','state'+(!hasUser||!rule.enabled?' off':'')));
    info.append(title);const start=rule.kind==='start',match=el('p',start?'开始使用节点时提醒':'域名包含 ');if(!start)match.append(el('span',rule.keyword,'rule-word'));
    const period=start?'每人连续 '+rule.cooldown/60+' 分钟没有连接后，再次使用触发':'每人独立冷却 '+rule.cooldown/60+' 分钟 · 新连接命中时提醒';
    info.append(match,el('p',hasUser?period+' · '+active.length+'/'+keys.length+' 人可触发':'可删除此规则或编辑为其他用户','rule-meta'));
    const actions=el('div',undefined,'actions');actions.append(action('编辑',()=>edit(rule)),action('删除',()=>remove(rule),true));row.append(info,actions);return row;
  });
  $('rules').replaceChildren(...(nodes.length?nodes:[el('div','还没有规则。选择用户，设置域名或开始使用提醒。','empty')]));
}
function renderEvents(){
  const nodes=state.events.map(event=>{
    const row=el('article',undefined,'alert-event');const moment=el('time',new Date(event.created*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}));moment.dateTime=new Date(event.created*1000).toISOString();
    const info=el('div');info.append(el('strong',event.kind==='test'?'测试通知':event.user+' · '+(event.kind==='start'?'开始使用节点':event.host)));
    if(event.kind==='match')info.append(el('p','关键词：'+event.keyword));if(event.kind==='start')info.append(el('p','按账号连接活动判断，也可能来自 App 后台请求。'));if(event.error)info.append(el('p',event.error));
    row.append(moment,info,el('span',delivery[event.status]||event.status,'delivery '+event.status));return row;
  });
  $('events').replaceChildren(...(nodes.length?nodes:[el('div','暂无提醒记录。发送一条测试通知，确认手机可以收到。','empty')]));
  $('reader-state').textContent=state.health.reader||'采集正常 · 提交成功仅表示 ntfy 已接受，手机接收还取决于网络与通知权限。';
}
let lastRefresh=0,rulesSignature='',eventsSignature='';
async function refresh(forceSettings=false){
 if(window.NodeViewClient?.isHidden())return;
 return readGate.run(async(signal,isCurrent)=>{
  const value=await request('state',null,signal);if(!isCurrent())return;state=value;
  if(readFailure&&$('notice').textContent===readFailure)notice('');readFailure='';
  if(forceSettings||!settingsDirty)renderSettings();else $('channel-state').textContent=value.settings.enabled?'规则通知已启用 · 有未保存修改':'规则通知已暂停 · 有未保存修改';
  const nextRules=JSON.stringify([state.users,state.rules]);if(nextRules!==rulesSignature){renderRules();rulesSignature=nextRules;}
  const nextEvents=JSON.stringify([state.events,state.health]);if(nextEvents!==eventsSignature){renderEvents();eventsSignature=nextEvents;}lastRefresh=Date.now();refreshFailures=0;
 });
}

async function mutate(value,message){if(working||!state)return;busy(true);try{await request('change',value);await refresh();notice(message);}catch(error){notice(error.message);await refresh().catch(()=>{});}finally{busy(false);}}
function edit(rule=null){
  if(!state)return;
  editing=rule?rule.id:null;$('rule-title').textContent=rule?'编辑通知规则':'新增通知规则';
  renderSelection(rule);
  $('rule-kind').value=rule?.kind||'match';$('rule-keyword').value=rule?rule.keyword:'';$('rule-cooldown').value=rule?rule.cooldown/60:10;$('rule-enabled').checked=rule?Boolean(rule.enabled):true;
  $('rule-error').hidden=true;kindChanged(false);$('rule-dialog').showModal();
}
function preview(){if(previewController)previewController.abort();if($('preview-result'))$('preview-result').replaceChildren();const term=$('rule-keyword').value.trim();$('match-preview').textContent=$('rule-kind').value==='start'?'首次观察到该账号连接，或连续一段时间无连接后再次使用时提醒；不要求访问某个域名，也会统计直接连接 IP 的活动。':term?'匹配任意包含“'+term+'”的域名；也可能命中包含相同字样的无关域名。':'填写一段域名关键词，不需要填写 https:// 或 *。';}
function kindChanged(resetPeriod=true){
  const start=$('rule-kind').value==='start';$('keyword-wrap').hidden=start;$('rule-keyword').required=!start;
  $('period-label').textContent=start?'连续无连接时长（分钟）':'冷却时间（分钟）';
  $('period-hint').textContent=start?'每个用户分别判断。例如 15：该用户连续 15 分钟无连接后，下次连接提醒一次；App 后台连接也算活动。':'每个用户独立计算冷却；同一用户命中不同子域名时共用冷却时间，其他用户不受影响。';
  if(resetPeriod)$('rule-cooldown').value=start?15:10;preview();
}
async function remove(rule){const label=rule.kind==='start'?'开始使用提醒':'关键词“'+rule.keyword+'”的通知规则';if(!window.confirm('删除'+label+'？历史提醒仍保留至到期。'))return;await mutate({action:'delete_rule',id:rule.id},'规则已删除。');}
$('channel-state').setAttribute('aria-live','polite');
$('channel-form').addEventListener('input',()=>{settingsDirty=true;$('channel-state').textContent=(state?.settings.enabled?'规则通知已启用':'规则通知已暂停')+' · 有未保存修改';});
$('rule-dialog').addEventListener('cancel',event=>{if(working)event.preventDefault();});
$('channel-form').addEventListener('submit',async event=>{
  event.preventDefault();if(working||!state)return;busy(true);
  try{await request('change',{action:'settings',settings:{server:$('server').value.trim(),topic:$('topic').value.trim(),token:$('token').value.trim(),clear_token:$('clear-token').checked,enabled:$('enabled').checked,details:$('details').checked}});settingsDirty=false;await refresh(true);notice('推送设置已保存，可以发送测试通知。');}
  catch(error){notice(error.message);}finally{busy(false);}
});
$('rule-form').addEventListener('submit',async event=>{
  event.preventDefault();if(working)return;busy(true);
  try{const accounts=selectedUsers();if(!accounts.length)throw new Error('请至少选择一个用户。');await request('change',{action:'save_rule',id:editing,kind:$('rule-kind').value,accounts,keyword:$('rule-keyword').value.trim(),cooldown:Number($('rule-cooldown').value)*60,enabled:$('rule-enabled').checked});$('rule-dialog').close();await refresh();notice('规则已保存，从新连接开始匹配。');}
  catch(error){$('rule-error').textContent=error.message;$('rule-error').hidden=false;}finally{busy(false);}
});
$('test').onclick=()=>{if(settingsDirty){notice('请先保存推送设置，再发送测试。');return;}mutate({action:'test'},'测试通知已加入队列，请查看手机和下方发送状态。');};
$('add-rule').onclick=()=>edit();$('cancel-rule').onclick=()=>$('rule-dialog').close();$('rule-keyword').addEventListener('input',preview);
$('rule-kind').addEventListener('change',()=>kindChanged());
$('rule-select-all').onclick=()=>{document.querySelectorAll('#rule-users input[data-available="true"]').forEach(input=>input.checked=true);selectionChanged();};
$('rule-select-none').onclick=()=>{document.querySelectorAll('#rule-users input').forEach(input=>input.checked=false);selectionChanged();};
$('refresh').onclick=()=>{if(!working)refresh().then(()=>{if(!working)busy(false);}).catch(readError);};
function start(){busy(true);refresh(true).catch(readError).finally(()=>busy(false));}
if(window.NodeViewClient)window.NodeViewClient.whenActive(start);else start();
function schedule(){clearTimeout(refreshTimer);refreshTimer=setTimeout(async()=>{
  if(!working&&!(document.hidden||window.NodeViewClient?.inactive)&&!$('rule-dialog').open){try{await refresh();if(state&&!working)busy(false);}catch(error){refreshFailures=Math.min(refreshFailures+1,3);readError(error);}}
  schedule();
 },Math.min(60000,10000*2**refreshFailures));}
 document.addEventListener('visibilitychange',()=>{if(document.hidden||window.NodeViewClient?.inactive){readGate.cancel();if(previewController)previewController.abort();}else if(!working&&Date.now()-lastRefresh>5000){refresh().then(()=>{if(!working)busy(false);}).catch(readError);}schedule();});schedule();
 $('preview-rule').onclick=async()=>{
  if(working)return;
  const output=$('preview-result');output.replaceChildren();if($('rule-kind').value!=='match'){output.append(el('p','活动提醒按无连接间隔判断，不使用域名命中预览。'));return;}
  if(previewController)previewController.abort();const controller=new AbortController();previewController=controller;
  const timer=setTimeout(()=>controller.abort(),10000);$('preview-rule').disabled=true;
  try{const accounts=selectedUsers();if(!accounts.length)throw new Error('请先选择要预览的用户。');const query=new URLSearchParams({q:$('rule-keyword').value.trim()});accounts.forEach(account=>query.append('account',account));
   const response=await fetch('auth/api/preview?'+query,{cache:'no-store',credentials:'same-origin',signal:controller.signal});let value;try{value=await response.json();}catch{throw new Error('命中预览暂不可用。');}
   if(!response.ok)throw new Error(value.error||'预览失败。');if(previewController!==controller||controller.signal.aborted)return;
   output.append(el('p',value.note,'hint'));for(const row of value.rows){const line=el('p');line.append(el('strong',row.host),el('span',' · '+row.count+' 条连接'));output.append(line);}
   if(!value.rows.length)output.append(el('p','样本中没有匹配域名。这不代表今后不会触发。','hint'));
  }catch(error){if(previewController===controller&&!controller.signal.aborted)output.append(el('p',error.message,'hint'));}
  finally{clearTimeout(timer);if(previewController===controller){previewController=null;$('preview-rule').disabled=false;}}
 };

