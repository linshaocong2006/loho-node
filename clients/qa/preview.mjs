import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(process.argv[2]),output=path.resolve(process.argv[3]);fs.mkdirSync(output,{recursive:true});
const fixture=fs.readFileSync(path.join(here,'fixture.js'));
const mime={'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.webmanifest':'application/manifest+json'};
const server=http.createServer((req,res)=>{try{const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);if(pathname==='/node-view/fixture.js'){res.setHeader('Content-Type','application/javascript');res.end(fixture);return;}if(!pathname.startsWith('/node-view/'))throw Error('route');const file=path.resolve(root,pathname.slice('/node-view/'.length)||'index.html');if(!file.startsWith(root+path.sep))throw Error('path');let body=fs.readFileSync(file);if(file.endsWith('.html'))body=Buffer.from(body.toString().replace('<head>','<head><script src="fixture.js"></script>'));res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');if(file.endsWith('.html')&&!file.endsWith('login.html')){res.setHeader('X-Frame-Options',file.endsWith('client.html')?'DENY':'SAMEORIGIN');res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; base-uri 'self'; form-action 'self'; frame-ancestors "+(file.endsWith('client.html')?"'none'":"'self'"));}res.end(body);}catch{res.writeHead(404);res.end('Not found');}});
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
   let ready=false;for(let i=0;i<100;i++){ready=await evaluate("!!document.querySelector('.client-header') && Array.from(document.querySelectorAll('iframe')).some(f=>!!f.contentDocument?.querySelector('.user-card, .client-user-insight'))");if(ready)break;await pause(100);}if(!ready)throw Error('Client did not load: '+errors.join('\n'));
   await test(evaluate);await pause(300);const image=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(image.data,'base64'));
   if(errors.length)throw Error(errors.join('\n'));const overflow=await evaluate('document.documentElement.scrollWidth > innerWidth + 1');if(overflow)throw Error(name+' overflows horizontally');console.log(name+': layout and interaction checks passed');
  }finally{ws.close();await fetch('http://127.0.0.1:'+debugPort+'/json/close/'+target.id);}
 }
 const base='http://127.0.0.1:'+port+'/node-view/';
 const activeDoc="document.querySelector('iframe:not([hidden])').contentDocument";
 async function until(evaluate,expression,message){for(let i=0;i<100;i++){if(await evaluate(expression))return;await pause(100);}throw Error(message);}
 await screen('phone-dashboard',base+'client.html',390,844,async evaluate=>{
  if(!await evaluate("getComputedStyle(document.querySelector('.client-nav')).display==='flex' && document.querySelectorAll('.client-nav a').length===4"))throw Error('Phone navigation');
  await evaluate("document.querySelector('.client-account').click()");if(!await evaluate("document.querySelector('.client-tools-dialog').open"))throw Error('Phone settings');await evaluate("document.querySelector('.client-tools-dialog').close()");
  await until(evaluate,"document.querySelectorAll('iframe').length===4 && Array.from(document.querySelectorAll('iframe')).every(f=>f.contentDocument?.readyState==='complete')",'Prewarm timeout');
  const prewarmReads=await evaluate("Array.from(document.querySelectorAll('iframe[hidden]')).map(f=>({url:f.src,count:f.contentWindow.__qaFetches?.length||0}))");
  if(prewarmReads.some(p=>p.count))throw Error('Inactive prewarm triggered API requests: '+JSON.stringify(prewarmReads));
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
 await screen('auth-expiry',base+'client.html',390,844,async evaluate=>{
  await evaluate("window.__qaUnauthorized=true;NodeViewClientHost.navigate('manager.html?app=1')");
  await until(evaluate,"location.pathname.endsWith('/login.html')",'Expired session stayed inside a child view');
  if(!await evaluate("document.querySelectorAll('iframe').length===0"))throw Error('Retained private views survived logout');
 });
}finally{chrome.kill('SIGTERM');server.close();try{fs.rmSync(profile,{recursive:true,force:true});}catch{}}
