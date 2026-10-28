(function(root){
  'use strict';
  const finite=n=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
  function bytes(value){
    if(!finite(value))return '—';
    const units=['B','KB','MB','GB','TB'];let i=0;
    while(value>=1024&&i<units.length-1){value/=1024;i++;}
    return (i===0?Math.round(value):value>=100?value.toFixed(0):value.toFixed(1))+' '+units[i];
  }
  function activity(last,now,uncertain=false){
    if(uncertain||!finite(now)||(last!==null&&last!==undefined&&(!finite(last)||last>now+300)))return {tone:'unknown',text:'待确认'};
    if(!last)return {tone:'red',text:'暂无近期活动'};
    const age=Math.max(0,now-last);
    if(age<=900)return {tone:'green',text:'15 分钟内活跃'};
    if(age<3600)return {tone:'yellow',text:'1 小时内有活动'};
    return {tone:'red',text:'较久无活动'};
  }
  function currentUsers(roster,traffic){
    const byKey=new Map((traffic?.users||[]).filter(u=>typeof u.key==='string'&&u.key).map(u=>[u.key,u]));
    return (Array.isArray(roster)?roster:[]).filter(u=>u.enabled&&typeof u.label==='string'&&typeof u.key==='string').map(u=>({...u,usage:byKey.get(u.key)||null}));
  }
  function relative(last,now){
    if(!finite(last)||!last)return '7 天内暂无连接';
    const seconds=Math.max(0,now-last);
    if(seconds<60)return '最近连接：刚刚';
    if(seconds<3600)return '最近连接：'+Math.floor(seconds/60)+' 分钟前';
    if(seconds<86400)return '最近连接：'+Math.floor(seconds/3600)+' 小时前';
    return '最近连接：'+Math.floor(seconds/86400)+' 天前';
  }
  const api={bytes,activity,currentUsers,relative};root.NodeViewDashboard=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  if(typeof document==='undefined')return;
  const F=root.NodeViewFast;
  const el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
  const total=x=>x&&finite(x.up)&&finite(x.down)?x.up+x.down:null;
  const stamp=x=>finite(x)&&x>0?new Date(x*1000).toLocaleString():'—';
  const number=x=>finite(x)?Math.round(x).toLocaleString():'—';
  function start(){
    const intro=document.querySelector('.intro');if(!intro||document.getElementById('usage-dashboard'))return;
    document.body.classList.add('dashboard-view');
    const section=el('section','usage-dashboard');section.id='usage-dashboard';section.setAttribute('aria-label','用户流量与活动');
    const top=el('div','usage-top'),heading=el('div');heading.append(el('span','usage-kicker','LIVE SUMMARY'),el('h2','','运行概览'));
    const freshness=el('span','usage-freshness','正在连接');top.append(heading,freshness);
    const overview=el('div','usage-overview');
    const metrics=[['今日总流量','上传与下载合计'],['当前下载速率','5 秒采样平均值'],['近期活跃','最近 15 分钟有连接'],['当前账号','当前启用的账号']].map(([title,description])=>{
      const box=el('article','usage-metric'),value=el('strong','','—');box.append(el('span','',title),value,el('small','',description));overview.append(box);return value;
    });
    const warning=el('p','usage-warning');warning.setAttribute('role','status');warning.hidden=true;
    const list=el('div','user-cards');
    const workspace=el('div','overview-content'),trend=el('section','overview-trend'),trendHeading=el('div','overview-panel-heading');
    trendHeading.append(el('h2','','流量趋势'),el('span','','近 7 日 · 上传＋下载'));
    const overallChart=el('div','daily-chart overview-chart');overallChart.setAttribute('aria-label','全体当前用户近七日流量');
    const speedRow=el('div','overview-speeds'),upSpeed=el('strong','','—'),downSpeed=el('strong','','—');
    for(const [text,value] of [['当前上传',upSpeed],['当前下载',downSpeed]]){const box=el('div');box.append(el('span','',text),value);speedRow.append(box);}
    const trendFoot=el('p','overview-chart-note','流量从启用统计后开始累计。');trend.append(trendHeading,overallChart,speedRow,trendFoot);
    const accountsPanel=el('section','overview-accounts'),accountsHeading=el('div','overview-panel-heading'),manageLink=el('a','','管理账号 →');manageLink.href='manager.html';accountsHeading.append(el('h2','','用户活动'),manageLink);accountsPanel.append(accountsHeading,list);
    const legend=el('div','usage-legend');
    for(const [tone,text] of [['green','≤15 分钟'],['yellow','15 分钟–1 小时'],['red','≥1 小时或无记录']]){const item=el('span');item.append(el('i','user-dot '+tone),document.createTextNode(text));legend.append(item);}
    const foot=el('p','usage-foot','状态依据连接活动估计；后台请求也会计入。流量按北京时间归日，来源 IP 数不代表设备数。');
    accountsPanel.append(legend);workspace.append(trend,accountsPanel);section.append(top,overview,warning,workspace,foot);intro.after(section);

    const dialog=el('dialog','profile-dialog');dialog.setAttribute('aria-labelledby','profile-title');
    const dialogHeader=el('header','profile-header'),titleWrap=el('div'),profileTitle=el('h2');profileTitle.id='profile-title';titleWrap.append(el('span','usage-kicker','USER PROFILE'),profileTitle);
    const close=el('button','profile-close','×');close.type='button';close.setAttribute('aria-label','关闭用户详情');close.onclick=()=>dialog.close();dialogHeader.append(titleWrap,close);
    const body=el('div','profile-body'),profileStatus=el('p','profile-status'),detailMetrics=el('div','profile-metrics'),activitySummary=el('p','profile-note');
    const detailValues=['今日流量','近 7 日流量','本月流量','当前上传 / 下载'].map(title=>{const box=el('div'),value=el('strong','','—');box.append(el('span','',title),value);detailMetrics.append(box);return value;});
    const trafficPanel=el('section','profile-panel'),trafficHeading=el('div','profile-section-title');trafficHeading.append(el('h3','','近 7 日流量'),el('span','','上传＋下载 · 北京时间'));const trafficChart=el('div','daily-chart');trafficChart.setAttribute('aria-label','最近七日流量柱状图');trafficPanel.append(trafficHeading,trafficChart);
    const activityPanel=el('section','profile-panel'),activityHeading=el('div','profile-section-title');activityHeading.append(el('h3','','连接活跃时段'),el('span','','近 24 小时 · 按本机时间'));const activityChart=el('div','activity-chart');activityChart.setAttribute('aria-label','最近二十四小时连接记录分布');const activityLabels=el('div','activity-labels');activityPanel.append(activityHeading,activityChart,activityLabels,el('p','profile-note','连接数包含后台请求，不代表网页浏览次数或真实使用时长。'));
    const columns=el('div','profile-columns'),sourcesPanel=el('section','profile-panel'),domainsPanel=el('section','profile-panel');
    const sourcesTitle=el('div','profile-section-title'),sourceSummary=el('span');sourcesTitle.append(el('h3','','来源 IP'),sourceSummary);const sourcesList=el('div','source-list');sourcesPanel.append(sourcesTitle,sourcesList,el('p','profile-note','列出最近使用的 20 个公网 IP。切换网络会改变 IP，多台设备也可能共用 IP。'));
    const domainsTitle=el('div','profile-section-title');domainsTitle.append(el('h3','','常用域名'),el('span','','近 7 天连接记录'));const domainsList=el('div','domain-rank');domainsPanel.append(domainsTitle,domainsList);columns.append(sourcesPanel,domainsPanel);
    const detailsWarning=el('p','usage-warning');detailsWarning.hidden=true;
    const detailsFoot=el('p','profile-note');
    const filterButton=el('a','button profile-filter','前往连接记录页 →');
    body.append(profileStatus,detailsWarning,detailMetrics,activitySummary,trafficPanel,activityPanel,columns,detailsFoot,filterButton);dialog.append(dialogHeader,body);document.body.append(dialog);
    dialog.addEventListener('click',event=>{if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();}});
    let roster=null,usage=null,snapshot=null,received=0,serverTime=0,lastRoster=0,polling=false,selected=null,rosterError='',usageError='',snapshotError='',lastRosterAttempt=-Infinity,lastRosterUsageSignature='';
    const userNodes=new Map(),detailSignatures=new Map();
    function detailsChanged(key,value){const next=JSON.stringify(value);if(detailSignatures.get(key)===next)return false;detailSignatures.set(key,next);return true;}
    const now=()=>received?serverTime+(performance.now()/1000-received):Date.now()/1000;
    const lastSeen=user=>snapshot?.account_last_seen?.[user.key]??user.usage?.connections?.last??null;
    const logsUncertain=()=>!snapshot||!!snapshot.error||!!snapshot.importing||now()-snapshot.generated>45;
    const usageFresh=()=>usage&&!usage.error&&finite(usage.sampled)&&now()-usage.sampled<25;
    const connectionFresh=u=>u?.connections&&now()-u.connections.generated<100;
    function openProfile(key){selected=key;detailSignatures.clear();renderDetails();if(!dialog.open)dialog.showModal();}
    let chartSignature='',heavy=null,heavyError='',lastHeavyAttempt=-Infinity,failures=0,nextPoll=0;
    function render(){
      const users=currentUsers(roster,usage),fresh=usageFresh(),uncertain=logsUncertain();
      const complete=usage&&users.length>0&&users.every(u=>u.usage);
      metrics[0].textContent=complete?bytes(users.reduce((n,u)=>n+total(u.usage.today),0)):'—';
      const ratesComplete=fresh&&users.length>0&&users.every(u=>u.usage?.rate&&finite(u.usage.rate.down));
      metrics[1].textContent=ratesComplete?bytes(users.reduce((n,u)=>n+u.usage.rate.down,0))+'/s':'—';
      metrics[2].textContent=roster&&!uncertain?users.filter(u=>activity(lastSeen(u),now()).tone==='green').length+' / '+users.length:'—';
      metrics[3].textContent=roster?String(users.length):'—';
      upSpeed.textContent=ratesComplete&&users.every(u=>finite(u.usage.rate.up))?bytes(users.reduce((n,u)=>n+u.usage.rate.up,0))+'/s':'—';
      downSpeed.textContent=metrics[1].textContent;
      const pageStatus=document.getElementById('status');if(pageStatus){pageStatus.textContent=fresh?'统计运行中':'统计待确认';pageStatus.classList.toggle('error',!fresh);}
      const chartData=complete?users.map(u=>u.usage.days):null,chartKey=JSON.stringify(chartData);
      if(chartKey!==chartSignature){
        chartSignature=chartKey;const fragment=document.createDocumentFragment();
        if(chartData&&chartData.every(days=>Array.isArray(days)&&days.length===7)){
          const days=chartData[0].map(day=>({day:day.day,value:users.reduce((sum,u)=>sum+(total(u.usage.days.find(d=>d.day===day.day))||0),0)}));
          const maximum=Math.max(1,...days.map(d=>d.value));
          for(const day of days){const column=el('div','daily-column');column.title=day.day+'：'+bytes(day.value);column.append(el('span','daily-value',bytes(day.value)));const track=el('div','daily-track'),bar=el('div','daily-bar');bar.style.height=(day.value?Math.max(3,day.value/maximum*100):0)+'%';track.append(bar);column.append(track,el('small','',day.day.slice(5).replace('-','/')));fragment.append(column);}
        }else fragment.append(el('p','usage-empty','等待流量统计数据。'));
        overallChart.replaceChildren(fragment);
      }
      trendFoot.textContent='按北京时间归日，近 7 日包含今日。'+(usage?.started?'自 '+new Date(usage.started*1000).toLocaleDateString()+' 启用统计。':'启用后开始累计。');
      freshness.textContent=fresh?'● 每 5 秒更新':'○ '+(!usage?'等待统计数据':'采集待确认');freshness.classList.toggle('is-stale',!fresh);
      const warnings=[];
      if(!roster)warnings.push(rosterError||'正在读取当前账号列表。');
      else if(now()-lastRoster>60)warnings.push('账号名单同步暂时失败，正在显示上次成功读取的名单。');
      if(usageError||usage?.error)warnings.push(usageError||usage.error);
      else if(usage&&!fresh)warnings.push('流量采样超过 25 秒未更新，累计值暂未更新，实时速度待确认。');
      if(heavyError)warnings.push(heavyError);
      if(snapshotError||snapshot?.error||snapshot?.importing)warnings.push(snapshot?.importing?'连接历史导入中，活动状态暂不判断。':snapshotError||snapshot.error);
      if(usage?.logs_error)warnings.push(usage.logs_error);
      warning.textContent=[...new Set(warnings)].join(' ');warning.hidden=!warnings.length;
      const nodes=[],keep=new Set();
      for(const user of users){
        const state=activity(lastSeen(user),now(),uncertain);keep.add(user.key);
        let view=userNodes.get(user.key);
        if(!view){
          const card=el('button','user-card');card.type='button';card.dataset.userKey=user.key;card.onclick=()=>openProfile(user.key);
          const name=el('div','compact-user-name'),avatar=el('span','user-avatar'),identity=el('div'),labelNode=el('strong'),stateTag=el('span','user-state'),dot=el('i','user-dot'),stateText=document.createTextNode('');
          stateTag.append(dot,stateText);identity.append(labelNode,stateTag);name.append(avatar,identity);
          const primary=el('div','compact-user-traffic'),value=el('strong');primary.append(el('span','','今日流量'),value);card.append(name,primary,el('span','compact-user-arrow','↗'));
          view={card,avatar,labelNode,stateTag,dot,stateText,value};userNodes.set(user.key,view);
        }
        const signature=JSON.stringify([user.label,state.tone,state.text,bytes(total(user.usage?.today))]);
        if(view.signature!==signature){
          if(root.NodeViewProfiles)root.NodeViewProfiles.decorate(view.avatar,'user',user.key,user.label);else F.text(view.avatar,user.label.slice(0,1));F.text(view.labelNode,user.label);F.text(view.stateText,state.text);F.text(view.value,bytes(total(user.usage?.today)));
          view.stateTag.className='user-state '+state.tone;view.dot.className='user-dot '+state.tone;
          view.card.setAttribute('aria-label','查看 '+user.label+' 的用户详情，'+state.text);view.card.title='查看 '+user.label+' 的流量、来源 IP 与常用域名';view.signature=signature;
        }
        nodes.push(view.card);
      }
      if(!users.length){let empty=list.querySelector('.usage-empty');if(!empty)empty=el('p','usage-empty');F.text(empty,roster?'当前没有启用的账号。':'正在同步账号列表…');nodes.push(empty);}
      F.reconcile(list,nodes);for(const key of userNodes.keys())if(!keep.has(key))userNodes.delete(key);
      foot.textContent='状态根据近期连接估计，不代表设备实时在线。点击用户查看 IP、域名与详细流量；完整连接在“连接记录”页查询。';
      if(dialog.open)renderDetails();
    }
    function renderDetails(){
      const user=currentUsers(roster,usage).find(u=>u.key===selected);
      if(!user){profileTitle.textContent='账号已移除';body.hidden=true;return;}
      body.hidden=false;profileTitle.textContent=user.label;
      filterButton.href='connections.html?account='+encodeURIComponent(user.key);
      const u=user.usage,c=u?.connections,connectionOK=connectionFresh(u),fresh=usageFresh(),state=activity(lastSeen(user),now(),logsUncertain());
      profileStatus.textContent=state.text+' · '+relative(lastSeen(user),now());profileStatus.className='profile-status '+state.tone;
      detailValues[0].textContent=bytes(total(u?.today));detailValues[1].textContent=bytes(total(u?.week));detailValues[2].textContent=bytes(total(u?.month));
      detailValues[3].textContent=fresh&&u?.rate?bytes(u.rate.up)+'/s ↑  '+bytes(u.rate.down)+'/s ↓':'采样待确认';
      activitySummary.textContent=c?'近 7 天 '+number(c.records)+' 条连接 · '+number(c.hosts)+' 个目标地址。保留记录内首次：'+stamp(c.first)+'；最近：'+stamp(c.last)+'。':'等待连接历史汇总。';
      detailsWarning.textContent=!u?'该账号统计尚未就绪。':!fresh?'流量采样暂未更新，显示已累计值。':!connectionOK?'连接历史汇总暂未更新。':c.recent_ips>=3?'此账号 15 分钟内出现至少 3 个来源 IP，可能由网络切换或多设备使用造成，不能据此认定账号共享。':'';detailsWarning.hidden=!detailsWarning.textContent;
      if(detailsChanged('traffic',u?.days)){
        const days=u?.days||[],max=Math.max(1,...days.map(d=>total(d)||0)),chart=document.createDocumentFragment();
        for(const day of days){const column=el('div','daily-column'),value=total(day)||0;column.title=day.day+'：上传 '+bytes(day.up)+'，下载 '+bytes(day.down);column.append(el('span','daily-value',bytes(value)));const track=el('div','daily-track'),bar=el('div','daily-bar');bar.style.height=(value?Math.max(3,value/max*100):0)+'%';track.append(bar);column.append(track,el('small','',day.day.slice(5).replace('-','/')));chart.append(column);}
        if(!days.length)chart.append(el('p','usage-empty','等待首个流量采样。'));trafficChart.replaceChildren(chart);
      }
      if(detailsChanged('activity',[c?.activity,Math.floor(now()/3600)])){
        const hours=new Map((c?.activity||[]).map(h=>[h.hour,h.records])),end=Math.floor(now()/3600),values=Array.from({length:24},(_,i)=>hours.get(end-23+i)||0),peak=Math.max(1,...values),bars=document.createDocumentFragment();
        values.forEach((value,i)=>{const bar=el('div','activity-bar');bar.style.height=(value?Math.max(4,value/peak*100):2)+'%';bar.classList.toggle('empty',!value);bar.title=new Date((end-23+i)*3600000).toLocaleString()+'：'+number(value)+' 条连接记录';bars.append(bar);});activityChart.replaceChildren(bars);
        activityLabels.replaceChildren(el('span','',new Date((end-23)*3600000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})),el('span','','当前小时'));
      }
      if(detailsChanged('sources',[c?.ips,c?.sources])){
        sourceSummary.textContent=c?'近 7 天 '+number(c.ips)+' 个 IP':'待汇总';const sourceNodes=document.createDocumentFragment();
        for(const source of c?.sources||[]){const row=el('div','source-row'),text=el('div');text.append(el('code','',source.ip),el('small','',stamp(source.last)));row.append(text,el('span','',number(source.records)+' 条'));sourceNodes.append(row);}
        if(!c?.sources?.length)sourceNodes.append(el('p','usage-empty','暂无来源记录。'));sourcesList.replaceChildren(sourceNodes);
      }
      if(detailsChanged('domains',c?.domains)){
        const ranks=document.createDocumentFragment(),maximum=Math.max(1,...(c?.domains||[]).map(d=>d.records));
        for(const [i,domain] of (c?.domains||[]).entries()){
          const row=el('div','domain-row'),badge=el('span','rank-number',String(i+1)),text=el('div','domain-text'),item=root.NodeViewIdentity?.identify(domain.host),name=el('strong','',item?.name||domain.host);
          text.append(name);if(item?.name)text.append(el('small','',domain.host));const track=el('div','domain-track'),bar=el('i');bar.style.width=(domain.records/maximum*100)+'%';track.append(bar);text.append(track);row.append(badge,text,el('span','domain-records',number(domain.records)));ranks.append(row);
        }
        if(!c?.domains?.length)ranks.append(el('p','usage-empty','暂无域名记录，IP 连接与 DNS 查询不计入此榜单。'));domainsList.replaceChildren(ranks);
      }
      detailsFoot.textContent='流量为采样统计，日界线为北京时间 00:00；近 7 日含今日。服务重启的采样间隙可能遗漏少量流量，不作为计费账单。连接汇总每约 30 秒更新，升级后的连接按固定用户 ID 关联；升级前记录按原名称保留。'+(c?' 汇总时间：'+stamp(c.generated)+'。'+(c.legacy_records?' 另有 '+number(c.legacy_records)+' 条旧名称记录，请在历史页查看；身份归属未经确认。':''):'');
    }
    let renderFrame=null;
    function scheduleRender(){
      if(renderFrame!==null||document.hidden||window.NodeViewClient?.inactive)return;
      renderFrame=requestAnimationFrame(()=>{renderFrame=null;if(!(document.hidden||window.NodeViewClient?.inactive))render();});
    }
    async function json(path){
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),7000);
      try{const response=await fetch(path,{cache:'no-store',credentials:'same-origin',signal:controller.signal});if(response.status===401)throw new Error('网站登录已过期，请刷新后登录。');if(!response.ok)throw new Error('数据读取失败（HTTP '+response.status+'），稍后重试。');const data=await response.json();return {data,server:Date.parse(response.headers.get('Date')||'')/1000};}
      finally{clearTimeout(timeout);}
    }
    async function poll(){
      if(polling||(document.hidden||window.NodeViewClient?.inactive)||performance.now()<nextPoll)return;polling=true;
      let rosterAttempted=false,rosterSucceeded=false;
      const usageSignature=()=>JSON.stringify((usage?.users||[]).map(u=>[u.key,u.label]).sort((a,b)=>String(a[0]).localeCompare(String(b[0]))));
      const rosterMismatch=()=>{
        if(!roster||!usageFresh())return false;
        const select=u=>u.filter(v=>v.enabled!==false).map(v=>[v.key,v.label]).sort((a,b)=>String(a[0]).localeCompare(String(b[0])));
        return usageSignature()!==lastRosterUsageSignature&&JSON.stringify(select(roster))!==JSON.stringify(select(usage.users));
      };
      async function syncRoster(){rosterAttempted=true;lastRosterAttempt=performance.now();try{const r=await json('manager/api/users');if(!Array.isArray(r.data.users))throw new Error('用户名单格式异常。');roster=r.data.users;lastRoster=Number.isFinite(r.server)?r.server:Date.now()/1000;rosterError='';rosterSucceeded=true;}catch(e){rosterError=e.message;}finally{scheduleRender();}}
      await Promise.allSettled([
        (performance.now()-lastRosterAttempt>=30000||((!roster||rosterError)&&performance.now()-lastRosterAttempt>=5000)||rosterMismatch())?syncRoster():Promise.resolve(),
        (async()=>{try{
          if(performance.now()-lastHeavyAttempt>=30000){lastHeavyAttempt=performance.now();(async()=>{
            try{const value=(await json('api/users.json')).data;if(value.schema!==1||!Array.isArray(value.users))throw new Error('趋势数据格式不正确。');heavy=value;heavyError='';
              if(usage){const details=new Map(heavy.users.map(u=>[u.key,u]));usage={...usage,users:usage.users.map(u=>details.has(u.key)?{...u,days:details.get(u.key).days,connections:details.get(u.key).connections}:u)};}
            }catch(error){heavyError='趋势与连接汇总暂不可用，已保留上次数据；实时指标继续更新。';}
            if(usage)scheduleRender();
          })();}
          const r=await json('api/users-live.json');if(r.data.schema!==1||!Array.isArray(r.data.users))throw new Error('用户流量数据尚未就绪。');
          const details=new Map((heavy?.users||[]).map(u=>[u.key,u]));usage={...r.data,users:r.data.users.map(u=>({...details.get(u.key),...u}))};
          received=performance.now()/1000;serverTime=Number.isFinite(r.server)?r.server:Date.now()/1000;usageError='';
        }catch(e){usageError=e.message;}finally{scheduleRender();}})(),
        (async()=>{try{const r=await json('api/activity.json');if(r.data.schema!==1||!r.data.user_last_seen||typeof r.data.user_last_seen!=='object')throw new Error('连接状态数据尚未就绪。');snapshot=r.data;snapshotError='';}catch(e){snapshotError=e.message;}finally{scheduleRender();}})()
      ]);
      if(rosterSucceeded)lastRosterUsageSignature=usageSignature();
      if(!rosterAttempted&&rosterMismatch()&&performance.now()-lastRosterAttempt>=4000){await syncRoster();if(rosterSucceeded)lastRosterUsageSignature=usageSignature();}
      failures=usageError&&snapshotError?Math.min(failures+1,4):0;nextPoll=performance.now()+(failures?Math.min(60000,5000*2**failures):5000);polling=false;scheduleRender();
    }
    document.addEventListener('nodeview:changed',()=>{lastRosterAttempt=-Infinity;lastHeavyAttempt=-Infinity;nextPoll=0;if(!window.NodeViewClient?.isHidden())poll();});
    render();poll();setInterval(()=>{if(!(document.hidden||window.NodeViewClient?.inactive))poll();},5000);document.addEventListener('visibilitychange',()=>{if(!(document.hidden||window.NodeViewClient?.inactive)){nextPoll=Math.min(nextPoll,performance.now()+5000);poll();}});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})(typeof window!=='undefined'?window:globalThis);
