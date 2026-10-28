import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createFixtureServer} from './server.mjs';

const root=path.resolve(process.argv[2]),output=path.resolve(process.argv[3]),baseline=process.argv.includes('--baseline');
const {chromium,webkit}=await import(pathToFileURL(path.resolve(process.env.LOHO_PLAYWRIGHT,'index.mjs')).href);
fs.mkdirSync(output,{recursive:true});
const {server}=createFixtureServer(root);await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port,base=origin+'/node-view/';
// Give the account button an actual uploaded image, rather than only its SVG fallback.
await fetch(base+'manager/api/profile',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':'fixture'},body:JSON.stringify({kind:'admin',key:'',image:fs.readFileSync(path.join(root,'app-icon-192.png')).toString('base64'),version:''})});

function snapshot(){
 const rect=node=>{const r=node.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
 const header=document.querySelector('.client-header'),account=document.querySelector('.client-account'),photo=account.querySelector('img'),nav=document.querySelector('.client-nav'),sidebar=document.querySelector('.client-sidebar'),outlet=document.querySelector('#client-outlet'),frame=document.querySelector('iframe:not([hidden])'),doc=frame.contentDocument,win=frame.contentWindow;
 const h=rect(header),a=rect(account),p=photo&&rect(photo),f=rect(frame),o=rect(outlet),n=getComputedStyle(nav).display==='none'?null:rect(nav),s=getComputedStyle(sidebar).display==='none'?null:rect(sidebar),issues=[];
 const inside=(r,b)=>r.left>=b.left-.5&&r.top>=b.top-.5&&r.right<=b.right+.5&&r.bottom<=b.bottom+.5;
 if(Math.abs(a.width-a.height)>.5||a.width<39.5)issues.push('Account button squeezed');
 if(!p||Math.abs(p.width-p.height)>.5||p.width<a.width-3)issues.push('Avatar image squeezed or padded');
 if(!inside(a,h))issues.push('Avatar escapes header');
 if(rect(document.querySelector('.client-brand')).right>rect(document.querySelector('.client-header-tools')).left+.5)issues.push('Brand overlaps avatar tools');
 if(h.height>(s?56:52)+.5)issues.push('Header too tall');
 if(n){if(n.height>56.5)issues.push('Bottom navigation too tall');if(Math.abs(f.bottom-n.top)>.5)issues.push('Content and bottom navigation disagree');
  for(const tab of nav.querySelectorAll('a')){const t=rect(tab);if(t.height<43.5||!inside(t,n))issues.push('Navigation tap area clipped');for(const child of tab.children)if(!inside(rect(child),t))issues.push('Navigation item spills out');}
 }
 if(s&&o.left<s.right-.5)issues.push('Content extends behind sidebar');
 if(Math.abs(rect(doc.querySelector('.shell')).left)>1||doc.documentElement.scrollWidth>win.innerWidth+1)issues.push('Embedded view has a second sidebar offset or overflow');
 const dialog=doc.querySelector('.profile-dialog[open]');let modal=null;
 if(dialog){const r=rect(dialog),close=dialog.querySelector('.profile-close'),c=rect(close),local={left:0,top:0,right:win.innerWidth,bottom:win.innerHeight};
  modal={...r,scrollTop:dialog.scrollTop,scrollHeight:dialog.scrollHeight,clientHeight:dialog.clientHeight};
  if(!inside(r,local))issues.push('User dialog escapes view');
  if(dialog.scrollWidth>dialog.clientWidth+1)issues.push('User dialog overflows horizontally');
  if(s&&f.left+r.left<s.right+8)issues.push('User dialog hidden by sidebar');
  if(!inside(c,local)||!inside(c,r))issues.push('Close button clipped');
  const cx=c.left+c.width/2,cy=c.top+c.height/2;
  if(document.elementFromPoint(f.left+cx,f.top+cy)!==frame||!close.contains(doc.elementFromPoint(cx,cy)))issues.push('Close button covered');
  if(dialog.scrollTop>0){const filter=dialog.querySelector('.profile-filter'),b=rect(filter);if(!inside(b,r))issues.push('Bottom action cannot scroll into view');
   const x=b.left+b.width/2,y=b.top+b.height/2;if(!filter.contains(doc.elementFromPoint(x,y)))issues.push('Bottom action covered');}
 }
 return {width:innerWidth,height:innerHeight,header:h.height,bottom:n?.height||0,avatar:{button:a,image:p},frame:f,sidebar:s,modal,issues};
}

const report=[];let failures=0;
try{
 for(const [engine,type,options] of [['chrome',chromium,{channel:'chrome'}],['webkit',webkit,{}]]){
  const browser=await type.launch({headless:true,...options});
  try{
   for(const [width,height] of [[320,700],[390,844],[768,1024],[834,1194],[1024,768],[1194,834],[1440,1000]]){
    const context=await browser.newContext({viewport:{width,height},isMobile:engine==='webkit'||width<700,hasTouch:true,deviceScaleFactor:1});
    const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(String(error)));
    await page.goto(base+'client.html');
    await page.waitForFunction(()=>document.querySelector('.client-account img')?.complete&&document.querySelector('iframe:not([hidden])')?.contentDocument?.querySelector('.user-card'));
    await page.evaluate(()=>{document.querySelector('.client-brand strong').textContent='LoHo Node · 很长的服务器名称';document.querySelector('.client-brand small').textContent='very-long-server-name.example.invalid:5000';document.querySelector('.client-live').textContent='连接异常';});
    const normal=await page.evaluate(snapshot);
    await page.evaluate(()=>{const doc=document.querySelector('iframe:not([hidden])').contentDocument;doc.querySelector('.user-card').click();});
    const open=await page.evaluate(snapshot);
    if([390,1024].includes(width))await page.screenshot({path:path.join(output,engine+'-'+width+'-dialog.png')});
    await page.evaluate(()=>{const dialog=document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog');dialog.scrollTop=dialog.scrollHeight;});
    const bottom=await page.evaluate(snapshot);
    if([390,1024].includes(width))await page.screenshot({path:path.join(output,engine+'-'+width+'-dialog-bottom.png')});
    // Use actual hit testing/clicks for both the close and avatar buttons.
    if(baseline)await page.evaluate(()=>document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').close());
    else await page.locator('iframe:not([hidden])').contentFrame().locator('.profile-close').click();
    await page.locator('.client-account').click();await page.locator('.client-tools-dialog').waitFor({state:'visible'});
    await page.locator('.client-tools-heading button').click();
    const result={engine,width,height,normal,open,bottom,errors};report.push(result);
    if(!baseline){
     await page.evaluate(()=>NodeViewClientHost.navigate('/node-view/client.html?tab=connections'));
     const connections=page.locator('iframe:not([hidden])').contentFrame();await connections.locator('.record').first().waitFor();
     const typography=await page.evaluate(()=>{const doc=document.querySelector('iframe:not([hidden])').contentDocument,win=doc.defaultView;return ['search','user','source','window','from-date','to-date'].map(id=>{const s=win.getComputedStyle(doc.getElementById(id));return {id,font:s.fontFamily,size:s.fontSize,weight:s.fontWeight};});});
     if(typography.some(s=>s.size!=='14px'||s.weight!=='400')||new Set(typography.map(s=>s.font)).size!==1)throw Error('Filter typography differs: '+JSON.stringify(typography));
     await connections.locator('#user').selectOption('key:demo-0');await connections.locator('#source').selectOption('203.0.113.10');await connections.locator('#window').selectOption('1800');
     await connections.locator('#search').fill('media');
     if([390,1024].includes(width))await page.screenshot({path:path.join(output,engine+'-'+width+'-filters.png')});
     result.filters=typography;
    }
    const issues=[...normal.issues,...open.issues,...bottom.issues,...errors];
    if(issues.length){failures++;console.log(engine+' '+width+': '+JSON.stringify([...new Set(issues)]));}
    else console.log(engine+' '+width+': layout and modal hit tests passed');
    await context.close();
   }
  }finally{await browser.close();}
 }
}finally{
 fs.writeFileSync(path.join(output,'mobile-layout.json'),JSON.stringify(report,null,2));server.close();
}
if(failures&&!baseline)throw Error(failures+' mobile layout cases failed');
