'use strict';
const $=id=>document.getElementById(id),Q=window.NodeViewQuery;
const names=['status','message','connections','destinations','sources','connections-label','destinations-label','sources-label','pause','search','source','user','window','hide-dns','domain-only','records','empty','empty-title','empty-text','more','coverage','updated','row-count','list-title','mode-live','mode-history','live-window','history-controls','history-navigation','from-date','to-date','history-query','history-prev','history-next','history-page','refresh-note'];
const ui=Object.fromEntries(names.map(id=>[id,$(id)]));
const number=value=>Number(value).toLocaleString('zh-CN');
const clock=value=>new Date(value*1000).toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
const stamp=value=>new Date(value*1000).toLocaleString('zh-CN',{hour12:false});
let accountLabels={},rosterLabels={};
const rowKey=row=>row.account?'key:'+row.account:row.user;
const label=value=>value.startsWith('key:')?(accountLabels[value.slice(4)]||'已删除账号'):value==='UNLABELLED'?'未标记（旧记录）':value+'（旧名称记录）';
const RETENTION=604800;
let activeUsers=null,rosterBusy=false,rosterError='',lastRosterRead=0;
const routeParams=new URLSearchParams(window.location.search),stored=window.NodeViewPanel.restore('connections');
let restorePosition=stored?.position||null,restoreScroll=Number(stored?.scroll)||0;
let requestedUser=routeParams.get('account')?'key:'+routeParams.get('account'):routeParams.get('user')||stored?.controls?.user||null,selectionNotice='';
let data=null,paused=false,busy=false,shown=80,latestFetch=0,mode='live';
let queryEpoch=0,historyState=null,historyBusy=false,controller=null;
const cache=new Map();
const F=window.NodeViewFast,rowNodes=new Map(),optionSignatures=new WeakMap();
const archiveFetch=F.boundedLoad(job=>fetchJSON('history/'+job.file,job.signal),3);
let searchTimer=null,composing=false;
function state(text,kind=''){ui.status.textContent=text;ui.status.className='status'+(kind?' '+kind:'');}
function notify(text){ui.message.textContent=text;ui.message.hidden=!text;}
function element(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function filters(from,to){return {from,to,user:ui.user.value,source:ui.source.value,query:ui.search.value.trim().toLowerCase(),hideDNS:ui['hide-dns'].checked,domainOnly:ui['domain-only'].checked};}
function paint(rows,total,pageMode=false){
  ui.connections.textContent=number(rows.reduce((sum,row)=>sum+row.count,0));
  ui.destinations.textContent=number(new Set(rows.map(row=>row.host)).size);
  ui.sources.textContent=number(new Set(rows.map(row=>row.source)).size);
  ui['connections-label'].textContent=pageMode?'本页连接记录':'筛选内连接记录';
  ui['destinations-label'].textContent=pageMode?'本页目标地址':'目标地址';
  ui['sources-label'].textContent=pageMode?'本页来源 IP':'来源 IP';
  ui['row-count'].textContent=number(total)+' 组';
  const nodes=[],keep=new Set();
  for(const row of (pageMode?rows:rows.slice(0,shown))){
    const key=JSON.stringify([Math.floor(row.first/60),rowKey(row),row.user,row.source,row.host,row.port,row.protocol,row.dns,row.domain]);keep.add(key);
    const displayLabel=row.account?label('key:'+row.account):label(row.user);
    const signature=JSON.stringify([row.first,row.last,row.count,row.dns,row.domain,pageMode,displayLabel]);
    let saved=rowNodes.get(key);
    if(saved&&saved.signature===signature){nodes.push(saved.card);continue;}
    if(saved){
      F.text(saved.time,pageMode?stamp(row.last):clock(row.last));saved.time.title=stamp(row.first)+' — '+stamp(row.last);
      F.text(saved.countValue,number(row.count));F.text(saved.badge,displayLabel);saved.signature=signature;nodes.push(saved.card);continue;
    }
    const card=element('article','record'),origin=element('div','origin');
    const time=element('div','time',pageMode?stamp(row.last):clock(row.last));
    time.title=stamp(row.first)+' — '+stamp(row.last);
    const userLine=element('div','user-line');
    const badge=element('span','user-badge'+(row.user==='SHARED'?' shared':row.user==='UNLABELLED'?' unknown':''),displayLabel);
    userLine.append(badge,element('span','source',row.source));origin.append(time,userLine);
    const target=element('div','target'),meta=element('div','target-meta');
    target.append(element('div','host',row.host));
    meta.append(element('span','protocol',row.protocol.toUpperCase()),element('span','','端口 '+row.port));
    if(row.dns)meta.append(element('span','dns-badge','DNS'));
    else if(!row.domain)meta.append(element('span','','IP 目标'));
    target.append(meta);
    const count=element('div','count'),countValue=document.createTextNode(number(row.count));count.append(countValue,element('small','','条记录'));
    card.append(origin,target,count);nodes.push(card);rowNodes.set(key,{card,time,badge,countValue,signature});
  }
  F.reconcile(ui.records,nodes);for(const key of rowNodes.keys())if(!keep.has(key))rowNodes.delete(key);ui.empty.hidden=rows.length!==0;
  ui['empty-title'].textContent='暂时没有匹配的连接';
  ui['empty-text'].textContent=pageMode?'可以调整时间、用户或域名后重新查询。':'可以调整筛选，或通过节点打开网站后等待刷新。';
  ui.more.hidden=pageMode||total<=shown;
  ui.more.textContent='再显示 '+Math.min(80,total-shown)+' 组';
}
function warnings(snapshot){
  const notes=[];
  if(snapshot.error)notes.push(snapshot.error);
  if(snapshot.importing)notes.push('正在导入服务器现有 access.log，历史记录会逐步出现。');
  if(snapshot.archive_pending)notes.push('历史索引正在分批整理，更早时段会逐步出现。实时连接继续更新。');
  if(mode==='live'&&Date.now()/1000-snapshot.generated>45)notes.push('采集数据超过 45 秒未更新，请检查服务器采集服务。');
  if(mode==='live'&&snapshot.truncated)notes.push('实时窗口仅展示最新 1,000 组。更早记录请切换“7 天历史”查询。');
  return notes.join('\n');
}
function renderLive(){
  if(mode!=='live'||!data)return;
  const end=paused?latestFetch:Date.now()/1000;
  const filter=filters(end-Number(ui.window.value),end+300);
  const rows=data.rows.filter(row=>activeUsers?.has(rowKey(row))&&Q.matches(row,filter));
  paint(rows,rows.length);notify([!activeUsers?'正在同步当前用户名单，读取完成后显示实时记录。':rosterError,selectionNotice,warnings(data)].filter(Boolean).join('\n'));
  ui.coverage.textContent='已显示 '+number(Math.min(shown,rows.length))+' / '+number(rows.length)+' 组 · 完整历史保留 7 天';
  ui.updated.textContent='采集更新 '+clock(data.generated)+(paused?' · 已暂停刷新':'');restoreViewScroll();
}
function setOptions(select,values,all,textFunction){
  const current=select.value;
  const unique=[...new Set(values)];if(current&&!unique.includes(current))unique.push(current);
  const signature=JSON.stringify([unique,all,select===ui.user?accountLabels:null]);if(optionSignatures.get(select)===signature)return;
  optionSignatures.set(select,signature);const fragment=document.createDocumentFragment();
  const option=element('option','',all);option.value='';fragment.append(option);
  for(const value of unique){const option=element('option','',textFunction?textFunction(value):value);option.value=value;fragment.append(option);}
  select.replaceChildren(fragment);select.value=current;
}
function refreshOptions(snapshot){
  accountLabels={...accountLabels,...snapshot.account_labels,...rosterLabels};
  if(mode==='live'&&ui.user.value&&!activeUsers?.has(ui.user.value))ui.user.value='';
  setOptions(ui.user,mode==='live'?[...(activeUsers||[])]:[...(snapshot.users||[])],'全部用户',label);
  if(mode==='live'&&activeUsers&&requestedUser!==null){
    if(activeUsers.has(requestedUser))ui.user.value=requestedUser;
    else if(!requestedUser.startsWith('key:')&&Object.values(accountLabels).includes(requestedUser)){const key=Object.keys(accountLabels).find(k=>accountLabels[k]===requestedUser&&activeUsers.has('key:'+k));if(key)ui.user.value='key:'+key;}
    else selectionNotice='链接指定的账号已删除或未启用，当前显示全部启用账号的连接。';
    requestedUser=null;
  }
  const sources=mode==='live'?[...new Set((snapshot.rows||[]).filter(row=>activeUsers?.has(rowKey(row))).map(row=>row.source))]:[...(snapshot.sources||[])];
  if(mode==='live'&&ui.source.value&&!sources.includes(ui.source.value))ui.source.value='';
  setOptions(ui.source,sources.sort(),'全部来源');
}
async function fetchJSON(path,signal){
  const abort=new AbortController();let timedOut=false;
  const cancel=()=>abort.abort();if(signal)signal.addEventListener('abort',cancel,{once:true});
  const timer=setTimeout(()=>{timedOut=true;abort.abort();},15000);
  try{
    if(signal&&signal.aborted)abort.abort();
    const response=await fetch(path,{cache:'no-store',credentials:'same-origin',signal:abort.signal});
    if(response.status===401)throw new Error('登录已失效，请重新加载页面登录。');
    if(response.status===404&&path.startsWith('history/'))throw new Error('历史文件已刷新或尚未生成，请点击“查询历史”重试。');
    if(!response.ok)throw new Error('读取失败（HTTP '+response.status+'），请检查服务器采集服务。');
    return await response.json();
  }catch(error){if(timedOut)throw new Error('读取超时，请检查网络后重试。');throw error;}
  finally{clearTimeout(timer);if(signal)signal.removeEventListener('abort',cancel);}
}
async function poll(){
  if(mode!=='live'||paused||busy||(data&&Date.now()/1000-latestFetch<2)||(document.hidden||window.NodeViewClient?.inactive))return;
  busy=true;
  const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),8000);
  try{
    const next=await fetchJSON('api/snapshot.json',abort.signal);
    if(mode!=='live'||paused)return;
    if(next.schema!==2||!Array.isArray(next.rows))throw new Error('面板数据版本不匹配，请完成升级后重新加载页面。');
    data=next;latestFetch=Date.now()/1000;refreshOptions(data);renderLive();
    if(data.error||latestFetch-data.generated>45)state('采集异常','error');else state('实时连接中');
  }catch(error){if(mode==='live'){state('连接中断','error');notify(error.name==='AbortError'?'连接超时，将自动重试。':error.message);}}
  finally{clearTimeout(timer);busy=false;}
}
function localInput(ms){const date=new Date(ms);return new Date(ms-date.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function initDates(){const now=Date.now();if(!ui['from-date'].value||new Date(ui['from-date'].value).getTime()<now-RETENTION*1000-65000)ui['from-date'].value=localInput(now-RETENTION*1000);if(!ui['to-date'].value)ui['to-date'].value=localInput(Math.ceil(now/60000)*60000);}
function cancelHistory(){queryEpoch++;if(controller)controller.abort();controller=null;historyBusy=false;ui['history-query'].disabled=false;}
function clearHistory(){
  cancelHistory();historyState=null;paint([],0,true);notify('筛选已修改，请点击“查询历史”。');
  ui['history-prev'].disabled=true;ui['history-next'].disabled=true;ui['history-page'].textContent='尚未查询';
  ui.coverage.textContent='磁盘历史保留 7 天 · 每页 80 组';
}
async function loadArchiveFile(file,signal){
  if(!/^h-\d+-[0-9a-f]{10}-\d+\.json$/.test(file))throw new Error('历史索引中的文件名不正确。');
  if(cache.has(file)){const value=cache.get(file);cache.delete(file);cache.set(file,value);return value;}
  const value=await archiveFetch({file,signal});
  if(value.schema!==2||!Array.isArray(value.rows))throw new Error('历史文件格式不正确。');
  cache.set(file,value);while(cache.size>8)cache.delete(cache.keys().next().value);return value;
}
async function historyPage(index,cursor,epoch,signal){
  historyBusy=true;ui['history-query'].disabled=true;ui['history-prev'].disabled=true;ui['history-next'].disabled=true;
  state('查询中');ui.updated.textContent='正在筛选历史文件……';
  const snapshot=historyState;
  try{
    const result=await Q.readPage({pages:snapshot.pages,cursor,filter:snapshot.filter,limit:80,concurrency:3,
      load:file=>loadArchiveFile(file,signal),cancelled:()=>epoch!==queryEpoch,
      progress:(done,total)=>{if(epoch===queryEpoch)ui.updated.textContent='正在扫描历史文件 '+done+' / '+total;}});
    if(epoch!==queryEpoch||mode!=='history')return;
    snapshot.index=index;snapshot.rows=result.rows;snapshot.next=result.next;snapshot.positions[index]=cursor;
    paint(result.rows,result.rows.length,true);
    ui['history-page'].textContent='第 '+(index+1)+' 页 · 本页 '+result.rows.length+' 组';
    ui['history-prev'].disabled=index===0;ui['history-next'].disabled=!result.next;
    ui.updated.textContent='历史索引更新 '+stamp(snapshot.manifest.generated);
    ui.coverage.textContent='本页 '+result.rows.length+' 组 · 统计仅针对本页 · 历史保留 7 天';
    state('历史查询');notify(warnings(snapshot.manifest));saveView();restoreViewScroll();
  }catch(error){if(epoch===queryEpoch&&error.name!=='AbortError'){state('查询失败','error');notify(error.message);ui['history-prev'].disabled=true;ui['history-next'].disabled=true;}}
  finally{if(epoch===queryEpoch){historyBusy=false;ui['history-query'].disabled=false;}}
}
async function queryHistory(){
  if(mode!=='history')return;
  clearTimeout(searchTimer);
  cancelHistory();const epoch=queryEpoch;controller=new AbortController();const signal=controller.signal;
  const now=Date.now()/1000,start=new Date(ui['from-date'].value).getTime()/1000,end=new Date(ui['to-date'].value).getTime()/1000;
  if(!Number.isFinite(start)||!Number.isFinite(end)||start>=end){notify('请选择有效的开始、结束时间，结束时间应晚于开始时间。');return;}
  if(start<now-RETENTION-65){notify('历史最多保留最近 7 天，请调整开始时间。');return;}
  historyBusy=true;ui['history-query'].disabled=true;state('读取历史');notify('');paint([],0,true);
  ui['empty-title'].textContent='正在查询历史';ui['empty-text'].textContent='正在读取并筛选记录，请稍候。';
  try{
    const manifest=await fetchJSON('history/index.json',signal);
    if(epoch!==queryEpoch)return;
    if(manifest.schema!==2||!Array.isArray(manifest.hours))throw new Error('历史索引格式不正确。');
    refreshOptions(manifest);
    const filter=filters(Math.max(start,now-RETENTION),end);
    historyState={manifest,filter,pages:Q.pagesFor(manifest,filter),positions:[],index:0,rows:[],next:null};
    const signature=pageSignature(historyState),position=restorePosition;restorePosition=null;
    if(position&&position.signature===signature&&Number.isInteger(position.index)&&position.index>=0&&position.index<100&&Array.isArray(position.positions)&&position.positions.every(p=>p&&Number.isInteger(p.file)&&p.file>=0&&p.file<=historyState.pages.length&&Number.isInteger(p.row)&&p.row>=0&&p.row<=500)&&position.positions[position.index]){historyState.positions=position.positions;await historyPage(position.index,position.positions[position.index],epoch,signal);}
    else await historyPage(0,{file:0,row:0},epoch,signal);
  }catch(error){if(epoch===queryEpoch&&error.name!=='AbortError'){state('查询失败','error');notify(error.message);ui['empty-title'].textContent='历史查询未完成';ui['empty-text'].textContent='筛选条件已保留，可检查网络后重新查询。';}}
  finally{if(epoch===queryEpoch){historyBusy=false;ui['history-query'].disabled=false;}}
}
function switchMode(next){
  clearTimeout(searchTimer);
  mode=next;cancelHistory();
  ui['mode-live'].setAttribute('aria-pressed',String(mode==='live'));ui['mode-history'].setAttribute('aria-pressed',String(mode==='history'));
  ui.pause.hidden=mode==='history';ui['live-window'].hidden=mode==='history';ui['history-controls'].hidden=mode==='live';ui['history-navigation'].hidden=mode==='live';
  ui['list-title'].textContent=mode==='live'?'实时连接':'历史连接';ui['refresh-note'].textContent=mode==='live'?'实时每 3 秒刷新 · 时间按本机显示':'历史按需查询 · 时间按本机显示';
  if(mode==='history'){initDates();queryHistory();}else{if(data)refreshOptions(data);renderLive();if(paused)state('已暂停','paused');else poll();}
}
ui['mode-live'].addEventListener('click',()=>switchMode('live'));ui['mode-history'].addEventListener('click',()=>switchMode('history'));
ui.pause.addEventListener('click',()=>{paused=!paused;ui.pause.textContent=paused?'继续刷新':'暂停刷新';if(paused)state('已暂停','paused');else{state('正在连接');poll();}renderLive();});
function applyFilters(){restorePosition=null;if(mode==='history')clearHistory();else{shown=80;renderLive();}saveView();}
for(const name of ['search','source','user','window','hide-dns','domain-only','from-date','to-date'])ui[name].addEventListener(name==='search'?'input':'change',event=>{
  if(name==='user'){requestedUser=null;selectionNotice='';}
  clearTimeout(searchTimer);
  if(name==='search'){if(!composing&&!event.isComposing)searchTimer=setTimeout(applyFilters,160);}else applyFilters();
});
ui.search.addEventListener('compositionstart',()=>{composing=true;clearTimeout(searchTimer);});
ui.search.addEventListener('compositionend',()=>{composing=false;clearTimeout(searchTimer);searchTimer=setTimeout(applyFilters,160);});
ui.search.addEventListener('keydown',event=>{if(event.key==='Enter'&&!composing&&!event.isComposing&&event.keyCode!==229&&mode==='history'){event.preventDefault();restorePosition=null;queryHistory();}});
ui.more.addEventListener('click',()=>{shown+=80;renderLive();saveView();});ui['history-query'].addEventListener('click',queryHistory);
ui['history-prev'].addEventListener('click',()=>{if(historyState&&!historyBusy&&historyState.index>0)historyPage(historyState.index-1,historyState.positions[historyState.index-1],queryEpoch,controller.signal);});
ui['history-next'].addEventListener('click',()=>{if(historyState&&!historyBusy&&historyState.next)historyPage(historyState.index+1,historyState.next,queryEpoch,controller.signal);});
document.addEventListener('visibilitychange',()=>{if(!(document.hidden||window.NodeViewClient?.inactive))poll();});setInterval(poll,3000);

// Keep the live view scoped to current enabled accounts. Historical queries remain complete.
async function syncLiveUsers(){
  if(rosterBusy||(activeUsers&&Date.now()-lastRosterRead<10000)||(document.hidden||window.NodeViewClient?.inactive))return;rosterBusy=true;
  try{const roster=await fetchJSON('manager/api/users');
    if(!Array.isArray(roster.users)||roster.users.some(u=>typeof u.label!=='string'||typeof u.enabled!=='boolean'))throw new Error('当前用户名单格式不正确。');
    const names=Object.fromEntries(roster.users.map(u=>[u.key,u.label]));const renamed=JSON.stringify(names)!==JSON.stringify(rosterLabels);rosterLabels=names;accountLabels={...accountLabels,...names};
    const next=new Set(roster.users.filter(u=>u.enabled).map(u=>'key:'+u.key));
    const changed=JSON.stringify([...(activeUsers||[])])!==JSON.stringify([...next]),hadError=!!rosterError;
    activeUsers=next;rosterError='';lastRosterRead=Date.now();
    if(mode==='live'&&data&&(changed||hadError||renamed)){refreshOptions(data);renderLive();}
  }catch(error){rosterError='当前用户名单同步失败，暂时保留上次成功读取的名单。';if(mode==='live'&&data)renderLive();}
  finally{rosterBusy=false;}
}
document.addEventListener('nodeview:changed',()=>{lastRosterRead=0;if(!window.NodeViewClient?.isHidden())syncLiveUsers();});
syncLiveUsers();setInterval(syncLiveUsers,10000);
document.addEventListener('visibilitychange',()=>{if(!(document.hidden||window.NodeViewClient?.inactive))syncLiveUsers();});

function pageSignature(value){const pages=value.pages;return JSON.stringify([value.manifest.version,pages.length,pages[0]?.file,pages.at(-1)?.file]);}
function saveView(){
 const controls={};for(const name of ['search','source','user','window','from-date','to-date'])controls[name]=ui[name].value;
 for(const name of ['hide-dns','domain-only'])controls[name]=ui[name].checked;
 const position=mode==='history'&&historyState?{signature:pageSignature(historyState),index:historyState.index,positions:historyState.positions.slice(0,100)}:null;
 window.NodeViewPanel.save('connections',{controls,mode,shown,scroll:window.scrollY,position});
}
function restoreViewScroll(){if(restoreScroll){const position=restoreScroll;restoreScroll=0;requestAnimationFrame(()=>window.scrollTo(0,position));}}
if(stored?.controls){for(const name of ['search','source','from-date','to-date'])if(typeof stored.controls[name]==='string'&&stored.controls[name].length<200)ui[name].value=stored.controls[name];
 if([...ui.window.options].some(o=>o.value===stored.controls.window))ui.window.value=stored.controls.window;
 for(const name of ['hide-dns','domain-only'])if(typeof stored.controls[name]==='boolean')ui[name].checked=stored.controls[name];shown=Math.min(1000,Math.max(80,Number(stored.shown)||80));}
let saveTimer=null;window.addEventListener('scroll',()=>{clearTimeout(saveTimer);saveTimer=setTimeout(saveView,200);},{passive:true});window.addEventListener('pagehide',saveView);
if(stored?.mode==='history'&&!routeParams.has('account')&&!routeParams.has('user')){if(requestedUser)ui.user.append(new Option(label(requestedUser),requestedUser,true,true));requestedUser=null;switchMode('history');}else poll();
