'use strict';
const now=()=>Date.now()/1000;
const users=['ADMIN','LoHo','Phone','Tablet','Guest','Work'].map((label,i)=>({key:'demo-'+i,label,enabled:i!==4,masked:'••••••••-example'}));
const heavy=()=>({schema:1,generated:now(),sampled:now(),started:now()-604800,error:null,users:users.map((user,i)=>({...user,today:{up:12000000*(i+1),down:220000000*(i+1)},week:{up:33000000*(i+1),down:670000000*(i+1)},month:{up:66000000*(i+1),down:990000000*(i+1)},rate:{up:12000*(i+1),down:60000*(i+1)},days:Array.from({length:7},(_,n)=>({day:new Date(Date.now()-(6-n)*86400000).toISOString().slice(0,10),up:1000000*(n+1),down:10000000*(n+i+1)})),connections:{generated:now(),first:now()-86400,last:now()-i*300,records:200+i*17,ips:2,hosts:8,legacy_records:0,activity:[],sources:[],domains:[]}}))});
const records=Array.from({length:20},(_,i)=>({first:now()-60-i,last:now()-i,user:users[i%6].label,identity:'key:'+users[i%6].key,account:users[i%6].key,legacy:false,source:'203.0.113.10',host:'media'+i+'.example.net',port:'443',protocol:'tcp',domain:true,dns:false,count:i+1}));
const originalFetch=window.fetch.bind(window);window.fetch=async(input,options={})=>{
 if(!window.__qaFetches)window.__qaFetches=[];window.__qaFetches.push({time:Date.now(),path:String(input),method:options.method||'GET'});
 const delay=window.parent!==window?window.parent.__qaDelay||0:window.__qaDelay||0;if(delay)await new Promise((resolve,reject)=>{const t=setTimeout(resolve,delay);if(options.signal){const abort=()=>{clearTimeout(t);reject(new DOMException('Aborted','AbortError'));};if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});}});
 if(window.__qaUnauthorized||(window.parent!==window&&window.parent.__qaUnauthorized)||sessionStorage.getItem('qa:unauthorized')==='1')return new Response('{}',{status:401});
 const path=new URL(typeof input==='string'?input:input.url,location.href).pathname;let value;
 if(path.endsWith('/auth/api/me'))value={user:'demo-admin',csrf:'fixture',expires:now()+2592000};

 else if(path.endsWith('/api/users-live.json'))value={...heavy(),users:heavy().users.map(({days,connections,...user})=>user)};
 else if(path.endsWith('/api/users.json'))value=heavy();
 else if(path.endsWith('/api/activity.json'))value={schema:1,generated:now(),account_last_seen:Object.fromEntries(users.map((u,i)=>[u.key,now()-i*330])),user_last_seen:{},error:null,importing:false};
 else if(path.endsWith('/api/snapshot.json'))value={schema:2,generated:now(),version:1,rows:records,users:users.map(u=>'key:'+u.key),account_labels:Object.fromEntries(users.map(u=>[u.key,u.label])),sources:['203.0.113.10'],total_groups:records.length,error:null};
 else if(path.endsWith('/auth/api/system'))value={generated:now(),version:'10.1.0',disk:{total:40*1073741824,free:28*1073741824},memory:{total:1073741824,available:600*1048576},database_bytes:1024000,log_backlog_bytes:0,certificate:{expires:now()+60*86400},services:['代理服务','网站服务','连接采集','流量采集','用户管理','网站登录','通知服务'].map(name=>({name,state:'active',loaded:true,memory:18*1048576})),samples:[]};
 else if(path.endsWith('/notify/api/state'))value={settings:{enabled:true,server:'https://ntfy.sh',topic:'demonstration-only',token_set:false,details:true},users:users.filter(u=>u.enabled),rules:[{id:'rule-demo',accounts:['demo-0','demo-1'],account:'demo-0',keyword:'example',kind:'match',cooldown:600,enabled:true}],events:[],health:{reader:null,updated:now()},csrf:'fixture',revision:'demo'};
 else if(path.endsWith('/auth/api/devices'))value={devices:[]};
 else return originalFetch(input,options);
 return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json','Date':new Date().toUTCString()}});
};
