import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createFixtureServer} from './server.mjs';

const root=path.resolve(process.argv[2]),output=path.resolve(process.argv[3]),baseline=process.argv.includes('--baseline');
const {chromium}=await import(pathToFileURL(path.resolve(process.env.LOHO_PLAYWRIGHT,'index.mjs')).href);
const {server,qaUsers,bumpRoster}=createFixtureServer(root);
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base='http://127.0.0.1:'+server.address().port+'/node-view/';
const browser=await chromium.launch({executablePath:process.env.CHROME_BIN||'/usr/bin/google-chrome',args:['--no-sandbox']});
const report={};fs.mkdirSync(output,{recursive:true});
async function scenario(name,work){
 const context=await browser.newContext({viewport:{width:1280,height:900},locale:'zh-CN'}),page=await context.newPage();
 try{report[name]=await work(page);console.log(name+': '+JSON.stringify(report[name]));}
 catch(error){report[name]={passed:false,error:String(error)};console.log(name+': '+error);}
 finally{await context.close();}
}
try{
 await scenario('progressiveOverview',async page=>{
  await page.addInitScript(()=>window.__qaDelays={'/node-view/api/activity.json':2000});
  const started=Date.now();await page.goto(base+'index.html',{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>document.querySelector('.usage-metric strong')?.textContent!=='—'&&!!document.querySelector('.user-card'));
  const metricPaintMs=Date.now()-started,activityAlreadyResolved=await page.evaluate(()=>!!window.__qaActivityResolved);
  await page.waitForFunction(()=>window.__qaActivityResolved);
  return {passed:!activityAlreadyResolved,activityDelayMs:2000,metricPaintMs,activityAlreadyResolved};
 });
 await scenario('historyEnterAndIME',async page=>{
  await page.addInitScript(()=>window.__qaDelays={'/node-view/history/index.json':250,'/node-view/history/h-000001-aaaaaaaaaa-0.json':250});
  await page.goto(base+'connections.html');await page.locator('.record').first().waitFor();
  await page.locator('#mode-history').click();await page.waitForFunction(()=>document.getElementById('history-page').textContent.includes('第 1 页'));
  await page.locator('#search').fill('media1');await page.locator('#search').press('Enter');await page.waitForTimeout(850);
  const enter=await page.evaluate(()=>({status:document.getElementById('status').textContent,rows:document.querySelectorAll('.record').length,message:document.getElementById('message').textContent}));
  // A composition update must not clear an already rendered historical result.
  await page.evaluate(()=>{const input=document.getElementById('search');input.dispatchEvent(new CompositionEvent('compositionstart'));input.value='媒';input.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}));});
  await page.waitForTimeout(230);const rowsDuringComposition=await page.locator('.record').count();
  await page.evaluate(()=>document.getElementById('search').dispatchEvent(new CompositionEvent('compositionend')));
  return {passed:enter.status==='历史查询'&&enter.rows>0&&rowsDuringComposition===enter.rows,enter,rowsDuringComposition};
 });
 await scenario('renamedConnectionLabels',async page=>{
  const original=qaUsers[0].label;
  try{
   await page.goto(base+'connections.html');await page.locator('.record').first().waitFor();
   await page.evaluate(()=>window.__qaFirstRow=document.querySelector('.record'));
   qaUsers[0].label='RENAMED';bumpRoster();await page.evaluate(()=>document.dispatchEvent(new Event('nodeview:changed')));
   await page.waitForTimeout(350);const immediate=await page.locator('.record .user-badge').first().textContent();
   await page.waitForTimeout(3200);const afterSnapshot=await page.locator('.record .user-badge').first().textContent();
   const rowRetained=await page.evaluate(()=>window.__qaFirstRow===document.querySelector('.record'));
   return {passed:immediate==='RENAMED'&&afterSnapshot==='RENAMED'&&rowRetained,immediate,afterSnapshot,rowRetained};
  }finally{qaUsers[0].label=original;bumpRoster();}
 });
 await scenario('incrementalIcons',async page=>{
  await page.addInitScript(()=>{const original=Element.prototype.querySelectorAll;window.__qaFullScans=0;Element.prototype.querySelectorAll=function(selector){if(this.id==='records'&&selector==='.record .target')window.__qaFullScans++;return original.call(this,selector);};});
  await page.goto(base+'connections.html');await page.locator('.identity-decorated').first().waitFor();
  const result=await page.evaluate(async()=>{
   window.__qaFullScans=0;const records=document.getElementById('records'),count=records.querySelectorAll('.identity-icon').length;
   for(let i=0;i<16;i++){records.querySelector('.time').textContent='QA '+i;await new Promise(resolve=>requestAnimationFrame(resolve));}
   const fullScans=window.__qaFullScans,iconsRetained=records.querySelectorAll('.identity-icon').length===count;
   const record=document.createElement('article'),target=document.createElement('div'),host=document.createElement('div');record.className='record';target.className='target';host.className='host';host.textContent='youtube.com';target.append(host);record.append(target);records.append(record);
   await new Promise(resolve=>requestAnimationFrame(resolve));return {fullScans,iconsRetained,newRowDecorated:!!target.querySelector('.identity-icon')};
  });
  return {passed:result.fullScans===0&&result.iconsRetained&&result.newRowDecorated,...result};
 });
 await scenario('retryKeepsAccount',async page=>{
  let rejected=false;
  await page.route('**/node-view/connections.html?*',async route=>{if(!rejected){rejected=true;await route.fulfill({contentType:'text/html',body:'<!doctype html><title>Unavailable test page</title>'});}else await route.continue();});
  await page.goto(base+'client.html?tab=connections&account=demo-1');
  await page.getByRole('button',{name:'重新加载',exact:true}).click();
  const account=await page.evaluate(()=>new URL(document.querySelector('iframe:not([hidden])').src).searchParams.get('account'));
  return {passed:account==='demo-1',account};
 });
 await scenario('saveFeedbackAndDialog',async page=>{
  await page.addInitScript(()=>window.__qaDelays={'/node-view/notify/api/change':750});
  await page.goto(base+'notifications.html');await page.waitForFunction(()=>!document.getElementById('test').disabled&&!!document.getElementById('topic').value);
  await page.locator('#topic').fill('saved-topic');const draftVisible=(await page.locator('#channel-state').textContent()).includes('未保存修改');
  await page.locator('#channel-form button[type=submit]').click();const inputsLocked=await page.locator('#topic').isDisabled();
  if(!inputsLocked)await page.locator('#topic').fill('typed-during-save');
  await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('推送设置已保存'));
  const savedTopic=await page.locator('#topic').inputValue();
  await page.locator('#add-rule').click();await page.locator('#rule-users input').first().check();await page.locator('#rule-keyword').fill('example');
  await page.locator('#rule-form button').filter({hasText:'保存规则'}).click();await page.keyboard.press('Escape');
  const dialogProtected=await page.locator('#rule-dialog').evaluate(node=>node.open);
  await page.waitForTimeout(1000);
  return {passed:draftVisible&&inputsLocked&&savedTopic==='saved-topic'&&dialogProtected,draftVisible,inputsLocked,savedTopic,dialogProtected};
 });
}finally{await browser.close();server.close();fs.writeFileSync(path.join(output,'audit.json'),JSON.stringify(report,null,2));}
const failures=Object.values(report).filter(result=>!result.passed).length;
if(failures&&!baseline)throw Error(failures+' experience regression checks failed');
