import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));

export function createFixtureServer(root){
const fixture=fs.readFileSync(path.join(here,'fixture.js'));
const mime={'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.webmanifest':'application/manifest+json'};
const qaUsers=['ADMIN','LoHo','Phone','Tablet','Guest','Work'].map((label,i)=>({key:'demo-'+i,label,enabled:i!==4,masked:'••••••••-example'}));
const photos=new Map();let rosterRevision=0;
const profileInfo=(kind,key='')=>{const raw=photos.get(kind+':'+key),version=raw?createHash('sha256').update(raw).digest('hex').slice(0,24):'';return {version,url:version?'/node-view/manager/api/avatar?'+new URLSearchParams({kind,key,v:version}):''};};
const json=(res,status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
const server=http.createServer(async(req,res)=>{try{
 const url=new URL(req.url,'http://localhost'),pathname=decodeURIComponent(url.pathname);
 if(pathname==='/node-view/fixture.js'){res.setHeader('Content-Type','application/javascript');res.end(fixture);return;}
 if(pathname==='/node-view/manager/api/profiles'){json(res,200,{admin:{...profileInfo('admin'),label:'demo-admin'},users:Object.fromEntries(qaUsers.map(u=>[u.key,profileInfo('user',u.key)])),csrf:'fixture'});return;}
 if(pathname==='/node-view/manager/api/avatar'){const raw=photos.get(url.searchParams.get('kind')+':'+url.searchParams.get('key'));if(!raw){res.writeHead(404);res.end();return;}res.writeHead(200,{'Content-Type':'image/png','Cache-Control':'no-store'});res.end(raw);return;}
 if(pathname==='/node-view/manager/api/users'){json(res,200,{users:qaUsers,csrf:'fixture',revision:'qa-'+rosterRevision,busy:false});return;}
 if(pathname==='/node-view/manager/api/job'){json(res,200,{status:'done',message:'已应用。'});return;}
 if(req.method==='POST'&&['/node-view/manager/api/profile','/node-view/manager/api/change'].includes(pathname)){
  let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
  if(req.headers['x-csrf-token']!=='fixture'){json(res,403,{error:'CSRF'});return;}
  if(pathname.endsWith('/profile')){if(body.version!==profileInfo(body.kind,body.key).version){json(res,409,{error:'头像已更新'});return;}const key=body.kind+':'+body.key;if(body.image===null)photos.delete(key);else photos.set(key,Buffer.from(body.image,'base64'));json(res,200,profileInfo(body.kind,body.key));return;}
  const user=qaUsers.find(u=>u.key===body.key);if(!user||body.revision!=='qa-'+rosterRevision||!['enable','disable'].includes(body.action)){json(res,409,{error:'change'});return;}user.enabled=body.action==='enable';rosterRevision++;json(res,202,{job:'qa-job'});return;
 }
 if(!pathname.startsWith('/node-view/'))throw Error('route');const file=path.resolve(root,pathname.slice('/node-view/'.length)||'index.html');if(!file.startsWith(root+path.sep))throw Error('path');let body=fs.readFileSync(file);
 if(file.endsWith('.html'))body=Buffer.from(body.toString().replace('<head>','<head><script src="fixture.js"></script>'));
 res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');
 if(file.endsWith('.html')&&!file.endsWith('login.html')){res.setHeader('X-Frame-Options',file.endsWith('client.html')?'DENY':'SAMEORIGIN');res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; base-uri 'self'; form-action 'self'; frame-ancestors "+(file.endsWith('client.html')?"'none'":"'self'"));}
 res.end(body);
 }catch(error){res.writeHead(404);res.end('Not found');}});
return {server,qaUsers,bumpRoster:()=>{rosterRevision++;}};
}
