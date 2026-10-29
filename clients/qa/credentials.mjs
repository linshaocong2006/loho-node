import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {fileURLToPath} from 'node:url';
import {createFixtureServer} from './server.mjs';

const root=path.resolve(process.argv[2]),output=path.resolve(process.argv[3]),baseline=process.argv.includes('--baseline');
const {chromium,webkit}=await import(pathToFileURL(path.resolve(process.env.LOHO_PLAYWRIGHT,'index.mjs')).href);
const {default:jsQR}=await import(pathToFileURL(path.resolve(process.env.LOHO_PLAYWRIGHT,'../jsqr/dist/jsQR.js')).href);
const fixture=JSON.parse(fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)),'credential-fixture.json'),'utf8'));
const {server,qaUsers}=createFixtureServer(root);await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base='http://127.0.0.1:'+server.address().port+'/node-view/';
fs.mkdirSync(output,{recursive:true});const report=[];
function geometry(){
 const frame=document.querySelector('iframe:not([hidden])'),doc=frame.contentDocument,win=doc.defaultView;
 const rect=node=>{const r=node.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
 const modal=doc.getElementById('credential-dialog'),m=rect(modal),qr=rect(doc.getElementById('qr')),body=modal.querySelector('.credential-body')||modal;
 const visible=id=>{const b=doc.getElementById(id);if(!b)return false;const r=rect(b),hit=doc.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return r.top>=m.top&&r.bottom<=m.bottom&&r.top>=0&&r.bottom<=win.innerHeight&&r.height>=44&&!!hit&&b.contains(hit);};
 return {modal:m,qr,viewport:{width:win.innerWidth,height:win.innerHeight},outerScroll:modal.scrollTop,bodyScroll:body.scrollTop,bodyOverflow:body.scrollHeight>body.clientHeight+1,detailsCollapsed:!modal.querySelector('details')?.open,copyVisible:visible('copy'),closeVisible:visible('close-credential'),topCloseVisible:visible('credential-dismiss')};
}
try{
 for(const [engine,type,options,native] of [['chrome',chromium,{channel:'chrome'},false],['webkit',webkit,{},false],['android',chromium,{channel:'chrome'},true]]){
  const browser=await type.launch({headless:true,...options});
  try{
   for(const [width,height] of [[320,568],[360,640],[390,844],[844,390],[768,1024],[1024,768]]){
    const context=await browser.newContext({viewport:{width,height},deviceScaleFactor:2,isMobile:native||engine==='webkit'||width<700,hasTouch:true,...(native?{userAgent:'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/144.0.0.0 Mobile Safari/537.36 LoHoNodeAndroid/1.0.6'}:{})});
    await context.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{if(window.__qaClipboardReject)throw Error('Clipboard unavailable');window.__qaCopiedText=value;}}}));
    const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(String(error)));
    try{
     const originalLabel=qaUsers[0].label;qaUsers[0].label=width===320?'配置用户-ABCDEFGHIJKLMNOPQRSTUVWXYZ':originalLabel;
     await page.goto(base+'client.html?tab=users');const view=page.locator('iframe:not([hidden])').contentFrame();
     await view.locator('.account:not([hidden])').first().getByRole('button',{name:'查看配置',exact:true}).click();await view.locator('#credential-dialog').waitFor({state:'visible'});
     await page.waitForFunction(()=>{const q=document.querySelector('iframe:not([hidden])').contentDocument.getElementById('qr');return q.complete&&q.naturalWidth>0;});
     const initial=await page.evaluate(geometry);
     const bitmap=await view.locator('#qr').evaluate(img=>{const r=img.getBoundingClientRect(),canvas=document.createElement('canvas');canvas.width=Math.round(r.width*devicePixelRatio);canvas.height=Math.round(r.height*devicePixelRatio);const c=canvas.getContext('2d');c.drawImage(img,0,0,canvas.width,canvas.height);return {width:canvas.width,height:canvas.height,data:Array.from(c.getImageData(0,0,canvas.width,canvas.height).data)};});
     const decoded=jsQR(new Uint8ClampedArray(bitmap.data),bitmap.width,bitmap.height),qrDecodes=decoded?.data===fixture.link;
     if([390,844].includes(width))await page.screenshot({path:path.join(output,engine+'-'+width+'-credential.png')});
     const issues=[];
     if(initial.modal.top<0||initial.modal.bottom>initial.viewport.height+.5||initial.modal.right>initial.viewport.width+.5)issues.push('Dialog escapes viewport');
     if(!initial.copyVisible||!initial.closeVisible||!initial.topCloseVisible)issues.push('Common actions require scrolling or are clipped');
     if(initial.qr.width>=240||!qrDecodes)issues.push('QR is oversized or unreadable');
     if(!initial.detailsCollapsed||initial.outerScroll!==0)issues.push('Long credentials expanded by default');
     if(height>=568&&width<700&&initial.bodyOverflow)issues.push('Default phone dialog needs scrolling');
     if(!baseline){
      await view.locator('#copy').click();const copied=await view.locator('#import-link').evaluate(input=>input.ownerDocument.defaultView.__qaCopiedText);if(copied!==fixture.link)issues.push('Copy changed the credential');
      await view.locator('.credential-details summary').click();const expanded=await page.evaluate(geometry);if(!expanded.copyVisible||!expanded.closeVisible||!expanded.topCloseVisible||expanded.outerScroll!==0)issues.push('Expanded details hide actions');
      await view.locator('#import-link').evaluate(input=>{input.ownerDocument.defaultView.__qaClipboardReject=true;});
      await view.locator('#copy').click();const fallback=await page.evaluate(geometry);if(!fallback.closeVisible||!fallback.copyVisible||fallback.detailsCollapsed)issues.push('Clipboard fallback hides close action');
      if(width===390)await page.screenshot({path:path.join(output,engine+'-'+width+'-credential-fallback.png')});
      await view.locator('#close-credential').click();await page.waitForFunction(()=>{const d=document.querySelector('iframe:not([hidden])').contentDocument;return !d.getElementById('credential-dialog').open&&d.getElementById('uuid').value===''&&d.getElementById('import-link').value===''&&!d.getElementById('qr').hasAttribute('src');});
      await view.locator('.account:not([hidden])').first().getByRole('button',{name:'查看配置',exact:true}).click();await view.locator('#credential-dialog').waitFor({state:'visible'});await view.locator('#credential-dismiss').click();await view.locator('#credential-dialog').waitFor({state:'hidden'});
     }
     issues.push(...errors);report.push({engine,width,height,initial,qrDecodes,issues});console.log(engine+' '+width+'x'+height+': '+(issues.length?issues.join('; '):'compact credential, QR decoding and actions passed'));
     qaUsers[0].label=originalLabel;
    }finally{await context.close();}
   }
  }finally{await browser.close();}
 }
}finally{server.close();fs.writeFileSync(path.join(output,'credentials.json'),JSON.stringify(report,null,2));}
if(!baseline&&report.some(item=>item.issues.length))throw Error('Credential dialog layout or interaction failed');
