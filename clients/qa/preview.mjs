import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createFixtureServer} from './server.mjs';
const root=path.resolve(process.argv[2]),output=path.resolve(process.argv[3]);fs.mkdirSync(output,{recursive:true});
const {server,qaUsers,bumpRoster}=createFixtureServer(root);
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port,profile=fs.mkdtempSync(path.join(os.tmpdir(),'loho-chrome-'));
const chrome=spawn(process.env.CHROME_BIN||'google-chrome',['--headless=new','--no-sandbox','--disable-gpu','--hide-scrollbars','--no-first-run','--disable-dev-shm-usage','--disable-background-networking','--password-store=basic','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe']});let chromeLog='';chrome.stderr.on('data',d=>chromeLog+=d.toString());
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));let debugPort;
try{
 for(let i=0;i<600;i++){const p=path.join(profile,'DevToolsActivePort');if(fs.existsSync(p)){debugPort=fs.readFileSync(p,'utf8').split('\n')[0];break;}if(chrome.exitCode!==null)throw Error(chromeLog);await pause(100);}if(!debugPort)throw Error('Chrome start timeout: '+chromeLog);
 async function screen(name,url,width,height,test){
  const target=await(await fetch('http://127.0.0.1:'+debugPort+'/json/new?'+encodeURIComponent(url),{method:'PUT'})).json();const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});let id=0;const pending=new Map(),errors=[];
  ws.onmessage=event=>{const data=JSON.parse(event.data);if(data.id){const job=pending.get(data.id);if(job){pending.delete(data.id);data.error?job.reject(Error(JSON.stringify(data.error))):job.resolve(data.result);}}else if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.exception?.description||data.params.exceptionDetails.text);};
  const rpc=(method,params={})=>new Promise((resolve,reject)=>{const next=++id;pending.set(next,{resolve,reject});ws.send(JSON.stringify({id:next,method,params}));});
  const evaluate=async expression=>{const result=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
  try{await rpc('Page.enable');await rpc('Runtime.enable');await rpc('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<700});await rpc('Page.navigate',{url});
   let ready=false;for(let i=0;i<100;i++){ready=await evaluate("(!!document.querySelector('.client-header') && Array.from(document.querySelectorAll('iframe')).some(f=>!!f.contentDocument?.querySelector('.user-card, #accounts .account'))) || (!document.querySelector('#client-outlet') && !!document.querySelector('#accounts .account'))");if(ready)break;await pause(100);}if(!ready)throw Error('Client did not load: '+errors.join('\n'));
   await test(evaluate,rpc);await pause(300);const image=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(image.data,'base64'));
   if(errors.length)throw Error(errors.join('\n'));const overflow=await evaluate('document.documentElement.scrollWidth > innerWidth + 1');if(overflow)throw Error(name+' overflows horizontally');console.log(name+': layout and interaction checks passed');
  }finally{ws.close();await fetch('http://127.0.0.1:'+debugPort+'/json/close/'+target.id);}
 }
 const base='http://127.0.0.1:'+port+'/node-view/';
 const activeDoc="document.querySelector('iframe:not([hidden])').contentDocument";
 async function until(evaluate,expression,message){for(let i=0;i<100;i++){if(await evaluate(expression))return;await pause(100);}throw Error(message+' '+JSON.stringify(await evaluate("({url:location.href,current:window.NodeViewClientHost?.current,frames:Array.from(document.querySelectorAll('iframe')).map(f=>({src:f.src,ready:f.contentDocument?.readyState,reads:f.contentWindow.__qaFetches}))})")));}
 await screen('phone-dashboard',base+'client.html',390,844,async evaluate=>{
  if(!await evaluate("getComputedStyle(document.querySelector('.client-nav')).display==='flex' && document.querySelectorAll('.client-nav a').length===4"))throw Error('Phone navigation');
  await evaluate("document.querySelector('.client-account').click()");if(!await evaluate("document.querySelector('.client-tools-dialog').open"))throw Error('Phone settings');await evaluate("document.querySelector('.client-tools-dialog').close()");
  await until(evaluate,"document.querySelectorAll('iframe').length===4 && Array.from(document.querySelectorAll('iframe')).every(f=>f.contentDocument?.readyState==='complete')",'Prewarm timeout');
  const prewarmReads=await evaluate("Array.from(document.querySelectorAll('iframe[hidden]')).map(f=>({url:f.src,count:f.contentWindow.__qaFetches?.length||0}))");
  if(prewarmReads.some(p=>p.count))throw Error('Inactive prewarm triggered API requests: '+JSON.stringify(prewarmReads));
  const dialogStyles=[];for(let i=0;i<4;i++){await evaluate(`document.querySelectorAll('.client-nav a')[${i}].click();document.querySelector('.client-account').click()`);dialogStyles.push(await evaluate("({background:getComputedStyle(document.querySelector('.client-tools-dialog')).backgroundColor,color:getComputedStyle(document.querySelector('.client-tools-dialog')).color,radius:getComputedStyle(document.querySelector('.client-tools-dialog')).borderRadius,profile:!!document.querySelector('.client-profile-card')})"));await evaluate("document.querySelector('.client-tools-dialog').close()");}
  if(new Set(dialogStyles.map(x=>JSON.stringify(x))).size!==1||dialogStyles[0].radius!=='20px'||!dialogStyles[0].profile)throw Error('Account dialog styles differ across tabs');
  await evaluate("document.querySelectorAll('.client-nav a')[0].click()");
  await evaluate("window.__qaDelay=1200;window.__qaSessionCount=document.querySelectorAll('iframe').length;window.__qaHostToken={};window.__qaLoads=Array.from(document.querySelectorAll('iframe')).map(f=>[f.src,f.contentWindow.performance.timeOrigin]);document.querySelectorAll('.client-nav a')[1].click()");
  if(!await evaluate("NodeViewClientHost.current==='connections' && document.querySelectorAll('.client-nav a')[1].getAttribute('aria-current')==='page'"))throw Error('Nav should respond before slow API completes');
  await until(evaluate,activeDoc+".querySelectorAll('.record').length>0",'Connections did not load');
  await evaluate(activeDoc+".querySelector('#search').value='media';"+activeDoc+".querySelector('#search').dispatchEvent(new Event('input',{bubbles:true}));document.querySelectorAll('.client-nav a')[2].click()");
  await until(evaluate,activeDoc+".querySelectorAll('#accounts .account').length===6",'Users did not load');
  await evaluate("window.__qaUserRow="+activeDoc+".querySelector('#accounts .account');document.querySelectorAll('.client-nav a')[3].click()");
  await until(evaluate,activeDoc+".querySelector('#topic').value==='demonstration-only'",'Notifications did not load');
  await evaluate(activeDoc+".querySelector('#topic').value='unsaved-draft';"+activeDoc+".querySelector('#topic').dispatchEvent(new Event('input',{bubbles:true}));");
  const timing=await evaluate(`(async()=>{const values=[];for(let n=0;n<24;n++){const i=n%4,t=performance.now();document.querySelectorAll('.client-nav a')[i].click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));if(NodeViewClientHost.current!==['overview','connections','users','notifications'][i])throw Error('Wrong active tab');values.push(performance.now()-t);}return values;})()`);
  await evaluate("document.querySelectorAll('.client-nav a')[1].click()");
  if(!await evaluate(activeDoc+".querySelector('#search').value==='media'"))throw Error('Search lost after switching');
  await evaluate("document.querySelectorAll('.client-nav a')[2].click()");
  if(!await evaluate(activeDoc+".querySelector('#accounts .account')===window.__qaUserRow"))throw Error('Unchanged user DOM was rebuilt');
  await evaluate("document.querySelectorAll('.client-nav a')[3].click()");
  if(!await evaluate(activeDoc+".querySelector('#topic').value==='unsaved-draft'"))throw Error('Notification draft lost');
  if(!await evaluate("window.__qaHostToken && window.__qaLoads.every(([src,time])=>Array.from(document.querySelectorAll('iframe')).some(f=>f.src===src && f.contentWindow.performance.timeOrigin===time))"))throw Error('Tab caused document reload');
  await evaluate("window.__qaDelay=0;NodeViewClientHost.setPaused(true)");await pause(200);
  const counts=await evaluate("Array.from(document.querySelectorAll('iframe')).map(f=>f.contentWindow.__qaFetches?.length||0)");await pause(10500);
  const after=await evaluate("Array.from(document.querySelectorAll('iframe')).map(f=>f.contentWindow.__qaFetches?.length||0)");
  if(JSON.stringify(counts)!==JSON.stringify(after))throw Error('Background polling continued: '+JSON.stringify({counts,after}));
  await evaluate("NodeViewClientHost.setPaused(false);document.querySelectorAll('.client-nav a')[0].click()");
  const sorted=timing.toSorted((a,b)=>a-b),report={simulatedApiLatencyMs:1200,switches:timing.length,medianPaintMs:sorted[Math.floor(sorted.length/2)],p95PaintMs:sorted[Math.floor(sorted.length*.95)],maxPaintMs:Math.max(...timing),retainedDocuments:true,inactivePrewarmApiReads:0,pausedPollingRequests:0};
  fs.writeFileSync(path.join(output,'performance.json'),JSON.stringify(report,null,2));if(report.p95PaintMs>300)throw Error('Warm tab paint regression: '+JSON.stringify(report));console.log(JSON.stringify(report));
 });
 await screen('ipad-users',base+'client.html?tab=users',1194,834,async evaluate=>{
  await until(evaluate,activeDoc+".querySelectorAll('.client-user-choice').length===6",'Tablet master list');
  if(!await evaluate("getComputedStyle(document.querySelector('.client-sidebar')).display==='flex'"))throw Error('Tablet sidebar');
  await evaluate(activeDoc+".querySelectorAll('.client-user-choice')[1].click()");
  if(!await evaluate("Array.from("+activeDoc+".querySelectorAll('#accounts > .account')).filter(n=>!n.hidden).length===1 && "+activeDoc+".querySelector('#accounts > .account:not([hidden])').getAttribute('aria-label')==='LoHo'"))throw Error('Tablet detail selection');
  if(await evaluate(activeDoc+".documentElement.scrollWidth>innerWidth+1"))throw Error('Tablet child overflow');
  await evaluate("NodeViewClientHost.navigate('system.html?app=1')");await until(evaluate,activeDoc+".querySelector('[data-theme-control]')",'System view');
  await evaluate(activeDoc+".querySelector('[data-theme-control]').value='light';"+activeDoc+".querySelector('[data-theme-control]').dispatchEvent(new Event('change'))");
  await until(evaluate,"document.documentElement.dataset.theme==='light'",'Theme not synced to host');
  await evaluate("NodeViewClientHost.navigate('client.html?tab=users')");
  if(!await evaluate(activeDoc+".documentElement.dataset.theme==='light'"))throw Error('Retained view theme');
  await evaluate(activeDoc+".defaultView.localStorage.setItem('loho:theme','dark');"+activeDoc+".defaultView.dispatchEvent(new StorageEvent('storage',{key:'loho:theme'}));");
  await until(evaluate,"document.documentElement.dataset.theme==='dark'",'Restore dark');
 });
 async function chooseAvatar(evaluate,rpc){const dom=await rpc('DOM.getDocument');const query=await rpc('DOM.querySelector',{nodeId:dom.root.nodeId,selector:'.avatar-editor-dialog input[type=file]'});await rpc('DOM.setFileInputFiles',{nodeId:query.nodeId,files:[path.join(root,'app-icon-192.png')]});await until(evaluate,"document.querySelector('.avatar-preview img')?.src.startsWith('blob:') && !document.querySelector('.avatar-editor-actions .primary').disabled",'Image processing did not finish');await evaluate("document.querySelector('.avatar-editor-actions .primary').click()");await until(evaluate,"!document.querySelector('.avatar-editor-dialog').open",'Avatar save failed');}
 await screen('phone-profile',base+'client.html',390,844,async(evaluate,rpc)=>{
  await evaluate("document.querySelector('.client-account').click();document.querySelector('.client-avatar-edit').click()");await until(evaluate,"document.querySelector('.avatar-editor-dialog')?.open && !document.querySelector('.avatar-editor-dialog input').disabled",'Admin avatar editor');
  await chooseAvatar(evaluate,rpc);if(!await evaluate("!!document.querySelector('.client-account img') && !!NodeViewProfiles.value.admin.version"))throw Error('Admin avatar not applied');
  await evaluate("NodeViewClientHost.navigate('manager.html?app=1')");await until(evaluate,activeDoc+".querySelectorAll('.account').length===6",'User list');
  await evaluate(activeDoc+".querySelector('.account[aria-label=LoHo] .managed-avatar').click()");await until(evaluate,"document.querySelector('.avatar-editor-dialog')?.open && !document.querySelector('.avatar-editor-dialog input').disabled",'User avatar editor');await chooseAvatar(evaluate,rpc);
  if(!await evaluate("!!document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.account[aria-label=LoHo] .managed-avatar img')"))throw Error('User avatar not applied');
  await evaluate("Array.from("+activeDoc+".querySelectorAll('.account[aria-label=LoHo] button')).find(b=>b.textContent==='暂停用户').click();"+activeDoc+".querySelector('#apply').click()");
  await until(evaluate,activeDoc+".querySelector('.account[aria-label=LoHo] .state').textContent==='已暂停'",'Pause did not update');
  await evaluate("Array.from("+activeDoc+".querySelectorAll('.account[aria-label=LoHo] button')).find(b=>b.textContent==='恢复使用').click();"+activeDoc+".querySelector('#apply').click()");await until(evaluate,activeDoc+".querySelector('.account[aria-label=LoHo] .state').textContent==='使用中'",'Resume did not update');
  await evaluate("NodeViewClientHost.navigate('client.html')");await until(evaluate,activeDoc+".querySelector('.user-card[data-user-key=demo-1] .profile-photo')",'Overview avatar did not sync');
  await evaluate("document.querySelector('.client-account').click()");
 });
 await screen('ipad-profile-sync',base+'client.html?tab=users',1194,834,async evaluate=>{
  await until(evaluate,"!!document.querySelector('.client-account img') && !!"+activeDoc+".querySelector('.account[aria-label=LoHo] .managed-avatar img')",'Avatar not loaded on another device context');
  await evaluate(activeDoc+".querySelectorAll('.client-user-choice')[1].click()");
  if(!await evaluate(activeDoc+".querySelector('.client-user-choice[aria-pressed=true] .profile-photo')"))throw Error('iPad master-list avatar missing');
  await evaluate("document.querySelector('.client-account').click()");
 });
 await screen('web-users',base+'manager.html',1440,1000,async evaluate=>{
  await until(evaluate,"!!document.querySelector('.account-trigger img') && !!document.querySelector('.account[aria-label=LoHo] .managed-avatar img')",'Desktop avatar sync');
  if(!await evaluate("Array.from(document.querySelectorAll('.account[aria-label=LoHo] button')).some(b=>b.textContent==='暂停用户')"))throw Error('Desktop pause control');
  await evaluate("document.querySelector('.account-menu').open=true;document.querySelector('.account-avatar-action').click()");await until(evaluate,"document.querySelector('.avatar-editor-dialog')?.open",'Desktop avatar editor');
 });
 // Exercise the screenshot's short names, plus long Latin and Chinese labels.
 qaUsers.splice(0,qaUsers.length,...['JWX','ZSM','GYP','LJY','GULING','LIWEI','ABCDEFGHIJKLMNOPQRSTUVWXYZ123456','中文用户名测试'].map((label,i)=>({key:'demo-'+i,label,enabled:i!==4,masked:'••••••••-ab123456'})));bumpRoster();
 await screen('responsive-user-list',base+'client.html?tab=users',1024,900,async(evaluate,rpc)=>{
  await until(evaluate,activeDoc+".querySelectorAll('#accounts > .account').length===8",'Responsive roster');
  const report=[];
  for(const width of [320,390,768,1024,1194,1440]){
   await rpc('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<700});await pause(150);
   const result=await evaluate(`(()=>{const frame=document.querySelector('iframe:not([hidden])'),doc=frame.contentDocument,win=frame.contentWindow,rows=Array.from(doc.querySelectorAll('#accounts>.account')),issues=[];for(let i=0;i<rows.length;i++){
    if(win.matchMedia('(min-width:1000px)').matches)doc.querySelectorAll('.client-user-choice')[i].click();
    const row=rows[i],name=row.querySelector('.account-name'),avatar=row.querySelector('.managed-avatar'),info=row.querySelector('.account-info'),actions=row.querySelector('.actions'),range=doc.createRange();range.selectNodeContents(name);const lines=new Set(Array.from(range.getClientRects()).map(r=>Math.round(r.top)));const n=name.getBoundingClientRect(),a=avatar.getBoundingClientRect(),b=info.getBoundingClientRect(),c=actions.getBoundingClientRect();
    if(lines.size!==1)issues.push(name.textContent+' wraps');if(name.textContent.length<=6&&name.scrollWidth>name.clientWidth+1)issues.push(name.textContent+' clipped');if(n.left<a.right+8)issues.push(name.textContent+' overlaps avatar');if(name.title!==name.textContent)issues.push('Missing full label');
    if(c.top<b.bottom-1&&c.left<b.right-1)issues.push('Actions overlap identity');if(row.scrollWidth>row.clientWidth+1)issues.push('Row overflow');
   }if(doc.documentElement.scrollWidth>win.innerWidth+1)issues.push('Document overflow');return {width:innerWidth,issues};})()`);
   report.push(result);if(result.issues.length)throw Error('User identity layout: '+JSON.stringify(result));
  }
  fs.writeFileSync(path.join(output,'responsive-users.json'),JSON.stringify(report,null,2));
  await rpc('Emulation.setDeviceMetricsOverride',{width:1024,height:900,deviceScaleFactor:1,mobile:false});await pause(100);await evaluate(activeDoc+".querySelectorAll('.client-user-choice')[0].click()");
 });
 await screen('responsive-connection-filters',base+'client.html',390,844,async(evaluate,rpc)=>{
  await evaluate("NodeViewClientHost.navigate('connections.html?app=1')");await until(evaluate,activeDoc+".querySelectorAll('.record').length>0",'Filter data');
  const report=[];for(const width of [320,390,768,1024,1440]){
   await rpc('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<700});await pause(100);
   const result=await evaluate(`(()=>{const doc=document.querySelector('iframe:not([hidden])').contentDocument,win=doc.defaultView,controls=['search','user','source','window','from-date','to-date'].map(id=>doc.getElementById(id)),issues=[],styles=controls.map(node=>{const s=win.getComputedStyle(node);return {id:node.id,size:s.fontSize,font:s.fontFamily,weight:s.fontWeight};});
    if(new Set(styles.map(s=>s.size)).size!==1)issues.push('Different value font sizes');if(new Set(styles.map(s=>s.font)).size!==1)issues.push('Different font families');if(new Set(styles.map(s=>s.weight)).size!==1)issues.push('Different font weights');
    const boxes=Array.from(doc.querySelectorAll('.filters>.search,.filters>.select-wrap'));if(new Set(boxes.map(n=>Math.round(n.getBoundingClientRect().height))).size!==1)issues.push('Different control heights');for(const box of boxes){const input=box.querySelector('input,select'),r=input.getBoundingClientRect(),b=box.getBoundingClientRect();if(r.left<b.left||r.right>b.right)issues.push('Value escapes control');}
    if(doc.documentElement.scrollWidth>win.innerWidth+1)issues.push('Horizontal overflow');return {width:innerWidth,styles,issues};})()`);
   report.push(result);if(result.issues.length)throw Error('Filter typography: '+JSON.stringify(result));
  }fs.writeFileSync(path.join(output,'responsive-filters.json'),JSON.stringify(report,null,2));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 });
 await screen('auth-expiry',base+'client.html',390,844,async evaluate=>{
  await evaluate("window.__qaUnauthorized=true;sessionStorage.setItem('qa:unauthorized','1');NodeViewClientHost.navigate('manager.html?app=1')");
  await until(evaluate,"location.pathname.endsWith('/login.html')",'Expired session stayed inside a child view');
  if(!await evaluate("document.querySelectorAll('iframe').length===0"))throw Error('Retained private views survived logout');
 });
}finally{chrome.kill('SIGTERM');server.close();try{fs.rmSync(profile,{recursive:true,force:true});}catch{}}
