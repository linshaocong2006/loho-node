(function(){
 'use strict';
 if(!document.documentElement.classList.contains('client-mode')||window.NodeViewClient?.embedded)return;
 const outlet=document.getElementById('client-outlet');if(!outlet)return;
 const routes={overview:['index.html','节点概览'],connections:['connections.html','连接记录'],users:['manager.html','用户管理'],notifications:['notifications.html','通知规则'],system:['system.html','系统状态'],security:['security.html','登录设备']};
 document.documentElement.classList.add('client-host');
 const pages=new Map();let current='',paused=false,prewarm=null,serial=0,loggingOut=false;
 const status=document.createElement('div');status.className='client-view-status';status.setAttribute('role','status');status.hidden=true;
 const statusText=document.createElement('p'),retry=document.createElement('button');retry.type='button';retry.textContent='重新加载';retry.className='button';status.append(statusText,retry);outlet.append(status);
 const hidden=()=>paused||document.hidden;
 function routeURL(name,query=''){const q=new URLSearchParams(query);q.delete('app');q.delete('view');q.delete('tab');q.set('app','1');q.set('view','1');return '/node-view/'+routes[name][0]+'?'+q;}
 function keyQuery(query){const p=new URLSearchParams(query);for(const k of [...p.keys()])if(!['account','user'].includes(k))p.delete(k);p.sort();return p.toString();}
 function sync(){for(const [name,page]of pages){const active=name===current&&!hidden();try{page.frame.contentWindow.NodeViewClient?.setActive(active);}catch{}}}
 function showStatus(page){status.hidden=page?.ready===true;if(!status.hidden){statusText.textContent=page?.failed?'页面暂时没有加载完成，请检查网络后重试。':'正在打开'+routes[current][1]+'…';retry.hidden=!page?.failed;}}
 function create(name,query=''){
  const frame=document.createElement('iframe');frame.className='client-view-frame';frame.title=routes[name][1];frame.hidden=true;frame.referrerPolicy='no-referrer';
  const page={frame,query:keyQuery(query),ready:false,failed:false,used:++serial,timer:null};pages.set(name,page);
  frame.addEventListener('load',()=>{try{if(frame.contentWindow.location.pathname.endsWith('/login.html')){host.login();return;}if(!frame.contentWindow.NodeViewClient){page.failed=true;if(current===name)showStatus(page);}}catch{page.failed=true;if(current===name)showStatus(page);}});
  page.timer=setTimeout(()=>{if(!page.ready){page.failed=true;if(current===name)showStatus(page);}},15000);
  frame.src=routeURL(name,page.query);outlet.append(frame);return page;
 }
 function select(name,query='',historyMode='push'){
  if(!routes[name]||loggingOut)return false;
  const clean=keyQuery(query);let page=pages.get(name);
  // Explicit account deep links replace that view; ordinary tab switches retain its filters.
  if(page&&clean&&clean!==page.query){clearTimeout(page.timer);page.frame.remove();pages.delete(name);page=null;}
  if(!page)page=create(name,clean);
  current=name;page.used=++serial;
  for(const [id,p]of pages){p.frame.hidden=id!==name;}
  sync();showStatus(page);
  if(historyMode!=='none'){
   const params=new URLSearchParams({tab:name});const q=new URLSearchParams(page.query);for(const[k,v]of q)params.set(k,v);
   const url='/node-view/client.html?'+params;if(location.pathname+location.search!==url)history[historyMode==='replace'?'replaceState':'pushState']({clientTab:name},'',url);
  }
  document.dispatchEvent(new CustomEvent('nodeview:route',{detail:{name,title:routes[name][1]}}));
  // Four primary tabs remain resident. Settings/device pages occupy one extra slot.
  const extras=[...pages].filter(([n])=>!['overview','connections','users','notifications',current].includes(n)).sort((a,b)=>a[1].used-b[1].used);
  for(const[n,p]of extras){clearTimeout(p.timer);p.frame.remove();pages.delete(n);}
  scheduleWarm();return true;
 }
 function parse(url){try{const u=new URL(url,location.href);if(u.origin!==location.origin||u.hash||!u.pathname.startsWith('/node-view/'))return null;let name;
  if(u.pathname.endsWith('/client.html'))name=u.searchParams.get('tab')||'overview';else name=Object.keys(routes).find(key=>u.pathname==='/node-view/'+routes[key][0])||(u.pathname==='/node-view/'?'overview':null);
  return routes[name]?[name,u.search]:null;
 }catch{return null;}}
 function scheduleWarm(){clearTimeout(prewarm);if(hidden()||navigator.connection?.saveData||/^(slow-)?2g$/.test(navigator.connection?.effectiveType||''))return;
  prewarm=setTimeout(()=>{if(hidden()||!pages.get(current)?.ready)return;const next=['overview','connections','users','notifications'].find(name=>!pages.has(name));if(next)create(next);},1200);
 }
 function dismissDialog(doc){const dialog=doc.querySelector('dialog[open]');if(!dialog)return false;
  const event=new doc.defaultView.Event('cancel',{cancelable:true});if(dialog.dispatchEvent(event))dialog.close();return true;
 }
 const host=window.NodeViewClientHost={isActive:view=>!hidden()&&pages.get(current)?.frame.contentWindow===view,
  navigate(url){const route=parse(url);if(!route)return false;return select(...route);},
  ready(view){const entry=[...pages].find(([,p])=>p.frame.contentWindow===view);if(!entry)return;const[name,page]=entry;clearTimeout(page.timer);page.ready=true;page.failed=false;if(name===current)showStatus(page);sync();scheduleWarm();},
  changed(source){for(const page of pages.values()){try{if(page.frame.contentWindow!==source)page.frame.contentDocument.dispatchEvent(new Event('nodeview:changed'));}catch{}}},
  setPaused(value){paused=!!value;window.NodeViewClient?.setActive(!paused);sync();if(!paused)scheduleWarm();else clearTimeout(prewarm);},
  login(){if(loggingOut)return;loggingOut=true;outlet.hidden=true;const next=location.pathname+location.search;window.setTimeout(()=>{host.clear();location.replace('/node-view/login.html?next='+encodeURIComponent(next));},0);},
  clear(){clearTimeout(prewarm);for(const page of pages.values()){clearTimeout(page.timer);try{page.frame.contentWindow.NodeViewClient?.setActive(false);}catch{}page.frame.remove();}pages.clear();},
  back(){if(dismissDialog(document))return true;const page=pages.get(current);try{if(page&&dismissDialog(page.frame.contentDocument))return true;}catch{}
   if(current!=='overview'){select('overview','','replace');return true;}return false;
  },get current(){return current;}
 };
 retry.onclick=()=>{const page=pages.get(current),query=page?.query||'';if(page){clearTimeout(page.timer);page.frame.remove();pages.delete(current);}select(current,query,'replace');};
 document.addEventListener('click',event=>{if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;const a=event.target.closest?.('a[href]');if(a&&!a.download&&!a.target&&host.navigate(a.href))event.preventDefault();});
 window.addEventListener('popstate',()=>{const route=parse(location.href);if(route)select(...route,'none');});
 document.addEventListener('visibilitychange',()=>{sync();if(!hidden())scheduleWarm();});
 window.addEventListener('pagehide',()=>{host.setPaused(true);});window.addEventListener('pageshow',()=>host.setPaused(false));
 window.addEventListener('online',()=>{sync();const page=pages.get(current);if(page?.failed)retry.click();});
 const params=new URLSearchParams(location.search),initial=params.get('tab')||'overview';select(routes[initial]?initial:'overview',params,'replace');
})();
