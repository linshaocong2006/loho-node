import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(process.argv[2]),output=path.resolve(process.argv[3]);fs.mkdirSync(output,{recursive:true});
const fixture=fs.readFileSync(path.join(here,'fixture.js'));
const mime={'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.webmanifest':'application/manifest+json'};
const server=http.createServer((req,res)=>{try{const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);if(pathname==='/node-view/fixture.js'){res.setHeader('Content-Type','application/javascript');res.end(fixture);return;}if(!pathname.startsWith('/node-view/'))throw Error('route');const file=path.resolve(root,pathname.slice('/node-view/'.length)||'index.html');if(!file.startsWith(root+path.sep))throw Error('path');let body=fs.readFileSync(file);if(file.endsWith('.html'))body=Buffer.from(body.toString().replace('<head>','<head><script src="fixture.js"></script>'));res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');res.end(body);}catch{res.writeHead(404);res.end('Not found');}});
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
   let ready=false;for(let i=0;i<100;i++){ready=await evaluate("!!document.querySelector('.client-header') && (!!document.querySelector('.user-card') || !!document.querySelector('.client-user-insight'))");if(ready)break;await pause(100);}if(!ready)throw Error('Client did not load: '+errors.join('\n'));
   await test(evaluate);await pause(300);const image=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(image.data,'base64'));
   if(errors.length)throw Error(errors.join('\n'));const overflow=await evaluate('document.documentElement.scrollWidth > innerWidth + 1');if(overflow)throw Error(name+' overflows horizontally');console.log(name+': layout and interaction checks passed');
  }finally{ws.close();await fetch('http://127.0.0.1:'+debugPort+'/json/close/'+target.id);}
 }
 const base='http://127.0.0.1:'+port+'/node-view/';
 await screen('phone-dashboard',base+'client.html',390,844,async evaluate=>{if(!await evaluate("getComputedStyle(document.querySelector('.client-nav')).display==='flex' && document.querySelectorAll('.client-nav a').length===4 && getComputedStyle(document.querySelector('.masthead')).display==='none'"))throw Error('Phone navigation');await evaluate("document.querySelector('.client-account').click()");if(!await evaluate("document.querySelector('.client-tools-dialog').open"))throw Error('Phone settings');await evaluate("document.querySelector('.client-tools-dialog').close()");});
 await screen('ipad-users',base+'manager.html?app=1',1194,834,async evaluate=>{if(!await evaluate("getComputedStyle(document.querySelector('.client-sidebar')).display==='flex' && document.querySelectorAll('.client-user-choice').length===6"))throw Error('Tablet master list');await evaluate("document.querySelectorAll('.client-user-choice')[1].click()");if(!await evaluate("Array.from(document.querySelectorAll('#accounts > .account')).filter(n=>!n.hidden).length===1 && document.querySelector('#accounts > .account:not([hidden])').getAttribute('aria-label')==='LoHo'"))throw Error('Tablet detail selection');});
}finally{chrome.kill('SIGTERM');server.close();try{fs.rmSync(profile,{recursive:true,force:true});}catch{}}
