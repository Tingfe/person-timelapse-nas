'use strict';

let overview={dates:[],tasks:[],plans:[],timelapses:[],diagnostics:{}},selected='',detailSignature='',detailOffset=0,detailRequest=0;
let rangeTarget='start',viewYear='',viewMonth='',planPage=0,taskPage=0,libraryPage=0,overviewLoading=false,statusLoading=false;
const $=s=>document.querySelector(s),rendered=new WeakMap();
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=d=>`${d.slice(0,4)}.${d.slice(4,6)}.${d.slice(6)}`,inputDate=d=>`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`;
const cameraLabel=c=>c==='legacy'?'历史录像':`${c} 路`,pad=n=>String(n).padStart(2,'0');
const time=s=>s==null?'计算中':s<60?`${Math.ceil(s)} 秒`:s<3600?`${Math.floor(s/60)} 分 ${Math.ceil(s%60)} 秒`:`${Math.floor(s/3600)} 小时 ${Math.floor(s%3600/60)} 分`;
const elapsed=(task,p)=>Math.max(p?.elapsed_seconds||0,task.started_at?(Date.now()-new Date(task.started_at).getTime())/1000:0,0);
const statusText={queued:'等待中',running:'运行中',completed:'已完成',partial:'部分完成',failed:'失败',cancelled:'已取消',interrupted:'已中断',paused:'已暂停',blocked:'需处理前置任务'};
const scanText={pending:'待扫描',completed:'已扫描',partial:'部分完成',scanning:'扫描中',queued:'已排队'};
const rangeLabel=item=>item.start_date===item.end_date?fmt(item.start_date):`${fmt(item.start_date)} — ${fmt(item.end_date)}`;
const fileSize=n=>n>=1073741824?`${(n/1073741824).toFixed(1)} GB`:`${(n/1048576).toFixed(1)} MB`;
const mediaURL=name=>'/media/'+((overview.timelapses||[]).find(item=>item.name===name)?.media_path||name).split('/').map(encodeURIComponent).join('/');
const active=()=>overview.tasks.find(t=>t.status==='running');
const signature=day=>day?`${day.events}:${day.ready}:${day.scan_status}:${day.timelapse_count}`:'';

function setContent(selector,html){
  const node=$(selector);if(rendered.get(node)===html)return;
  const scroll=node.scrollTop;node.innerHTML=html;node.scrollTop=scroll;rendered.set(node,html);
}
function options(selector,items,value){
  setContent(selector,items.map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join(''));
  $(selector).value=items.some(([key])=>key===value)?value:items[0]?.[0]??'';
}
function pager(selector,page,total,size,list){
  const count=Math.ceil(total/size);
  setContent(selector,count>1?`<button class="quiet" data-list="${list}" data-page="${page-1}" ${page===0?'disabled':''}>上一页</button><span>${page+1} / ${count} · 共 ${total} 项</span><button class="quiet" data-list="${list}" data-page="${page+1}" ${page+1===count?'disabled':''}>下一页</button>`:'');
}
async function api(url,opts={}){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
  try{const response=await fetch(url,{...opts,signal:controller.signal,cache:'no-store'});
    if(response.status===401){location.href='/login';throw Error('登录已过期')}
    const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data;
  }finally{clearTimeout(timer)}
}
function notify(error){$('#notice').textContent=error.name==='AbortError'?'服务暂未响应，保留当前内容，稍后自动重试。':error.message||error}

function renderMission(){
  const run=active(),p=run?.progress||{},d=overview.index_progress||overview.diagnostics||{};
  $('#mission').classList.toggle('empty',!run);
  $('#missionTitle').textContent=run?`${run.kind==='export'?(p.phase==='encoding'?'正在拼接已保存片段':'正在合成延时视频'):'正在扫描人物'} · ${fmt(run.date)}${run.dates?.length>1?' — '+fmt(run.dates.at(-1)):''}${run.camera?' · '+cameraLabel(run.camera):''}`:overview.queue_paused?'队列已暂停':overview.indexing?'正在建立录像索引':'当前没有运行中的任务';
  $('#missionBar').style.width=`${run?Math.max(0,Math.min(100,p.percent||0)):0}%`;
  $('#missionPercent').textContent=run?`${p.percent??0}%`:'—';
  $('#missionText').textContent=run?`${p.current_file||'正在准备文件…'}\n文件 ${p.file_index||0}/${p.total_files||'?'} · ${run.kind==='export'?'已输出 '+(p.frames||0)+' 帧':'抽帧 '+(p.samples||0)+'/'+(p.estimated_samples||'?')+' · YOLO '+(p.yolo_inferences||0)+' 次'} · 预计剩余 ${time(p.eta_seconds)}`:overview.indexing?(d.message||'后台正在检查录像日期，已有结果仍可查看。'):d.available?`已发现 ${d.mp4_files||0} 个 MP4，${d.recognized_files||0} 个文件名可识别。\n在下方选择日期范围；运行任务与成片会分别显示。`:(d.message||'等待录像索引；请确认源目录已挂载。');
  if(run?.kind==='export')$('#missionText').textContent+=`\n复用 ${p.reused_files||0} 个片段 · 解码 ${p.frames_per_second??'—'} 帧/秒 · 预计成片 ${time(p.estimated_output_seconds)}\n读取异常 ${p.failed_files||0} 个 · 时长校正 ${p.duration_adjusted_files||0} 个 · ${p.heartbeat_at?'最近更新 '+time(Math.max(0,Date.now()/1000-p.heartbeat_at))+'前':'正在准备检查点'}`;
  if(run?.kind==='scan'&&p.step){
    const step={model:'加载识别模型',preparing:'准备文件',metadata:'读取真实时长',decoding:'抽取画面',motion:'检查画面变化',inference:'识别人像',saving:'保存缩略图',checkpoint:'保存文件检查点'}[p.step]||p.step;
    $('#missionText').textContent+=`\n${step} · 本步 ${time(p.step_elapsed_seconds)} · ${p.decode_mode==='keyframes'?'关键帧快速抽样':'精确抽帧'}\n累计解码 ${time(p.decode_seconds)} · 识别 ${time(p.inference_seconds)} · 时长校正 ${p.duration_adjusted_files||0} 个 · 失败 ${p.failed_files||0} 个`;
  }
  $('#elapsed').textContent=run?`${time(elapsed(run,p))} 已运行`:overview.queue_paused?'PAUSED':overview.indexing?'INDEXING':'READY';
}
function renderPlans(){
  const plans=overview.plans||[];planPage=Math.min(planPage,Math.max(0,Math.ceil(plans.length/3)-1));
  $('#planCount').textContent=plans.length?`· ${plans.length} 项`:'';
  setContent('#plans',plans.slice(planPage*3,planPage*3+3).map(plan=>{
    const stage=plan.phase==='exporting'?'阶段 2 / 2 · 正在合成':plan.phase==='export_failed'?'合成失败 · 成功素材已保留':plan.phase==='waiting_export'?'扫描已结束 · 等待合成':plan.phase==='completed'?'处理已结束':`人物扫描${plan.current_date?' · '+fmt(plan.current_date):''}`;
    const scan=plan.scan_percent??Math.round(plan.completed_days/Math.max(plan.total_days,1)*100),film=plan.export_percent??0;
    const bars=`<div class="stage-row"><span>人物扫描 · ${plan.completed_days}/${plan.total_days} 天</span><b>${scan}%</b></div><div class="progress scan-progress"><i style="width:${scan}%"></i></div>${plan.kind==='scan'?'':`<div class="stage-row"><span>视频合成</span><b>${film}%</b></div><div class="progress"><i style="width:${film}%"></i></div>`}`;
    return `<article class="plan"><b class="plan-title">${plan.kind==='scan'?'范围扫描':'范围延时'} · ${esc(rangeLabel(plan))}</b><div class="plan-meta"><span class="tag ${esc(plan.status)}">${statusText[plan.status]||esc(plan.status)}</span><span class="plan-stage">${stage}</span></div>${bars}<small class="subtle">${plan.kind==='scan'?'全部摄像头':esc(cameraLabel(plan.camera))}${plan.partial_days?' · 部分完成 '+plan.partial_days+' 天':''}${plan.failed_days?' · 失败/取消 '+plan.failed_days+' 天':''}</small>${plan.outputs.map(name=>`<button class="watch" data-watch="${esc(name)}">观看成片</button>`).join('')}${plan.retry_ids.length?`<button class="watch" data-retry-plan="${esc(plan.id)}">重试未完成项</button>`:''}${['failed','blocked'].includes(plan.status)?`<small class="subtle">${esc(plan.detail||'请检查下方队列，已成功完成的文件会保留。')}</small>`:''}</article>`;
  }).join('')||'<div class="empty">创建一键或范围任务后，这里会显示总进度与最终成片入口。</div>');
  pager('#planPager',planPage,plans.length,3,'plans');
}
function calendarDays(){
  const days=new Map(overview.dates.map(day=>[day.date,{...day}]));
  for(const task of overview.tasks)if(task.kind==='scan'&&['running','queued'].includes(task.status)&&days.has(task.date))days.get(task.date).scan_status=task.status==='running'?'scanning':'queued';
  return days;
}
function renderCalendar(){
  const days=calendarDays(),years=[...new Set(overview.dates.map(day=>day.date.slice(0,4)))].sort().reverse();
  if(!viewYear)viewYear=years[0]||String(new Date().getFullYear());
  if(!years.includes(viewYear))years.push(viewYear);
  options('#yearSelect',years.map(year=>[year,year+' 年']),viewYear);
  options('#monthSelect',[['','全年总览'],...Array.from({length:12},(_,i)=>[pad(i+1),(i+1)+' 月'])],viewMonth);
  const visible=[...days.values()].filter(day=>day.date.startsWith(viewYear+viewMonth)),complete=visible.filter(day=>day.scan_status==='completed').length,exported=visible.filter(day=>day.timelapse_count).length;
  $('#dateCount').textContent=`· 共 ${overview.dates.length} 个录像日`;
  $('#archiveSummary').textContent=`${viewYear} 年${viewMonth?' '+Number(viewMonth)+' 月':''} · 有录像 ${visible.length} 天 · 已扫描 ${complete} 天 · 有成片 ${exported} 天${overview.indexing?' · 索引仍在更新':''}`;
  $('#selectMonth').disabled=!viewMonth||!visible.length;$('#selectYear').disabled=!overview.dates.some(day=>day.date.startsWith(viewYear));
  if(!viewMonth){
    setContent('#dates',`<div class="months">${Array.from({length:12},(_,i)=>{
      const month=pad(i+1),found=[...days.values()].filter(day=>day.date.startsWith(viewYear+month)),done=found.filter(day=>day.scan_status==='completed').length,clips=found.filter(day=>day.timelapse_count).length;
      return `<button class="month" data-month="${month}" aria-label="${viewYear} 年 ${i+1} 月，${found.length} 个录像日"><strong>${pad(i+1)} <span class="subtle">月</span></strong><small>${found.length?'有录像 '+found.length+' 天':'暂无录像'}</small><small>已扫描 ${done} · 有成片 ${clips}</small><div class="progress"><i style="width:${found.length?done/found.length*100:0}%"></i></div></button>`;
    }).join('')}</div>`);return;
  }
  const count=new Date(Number(viewYear),Number(viewMonth),0).getDate(),offset=(new Date(Number(viewYear),Number(viewMonth)-1,1).getDay()+6)%7;
  const start=$('#dateSelect').value.replaceAll('-',''),end=$('#endDate').value.replaceAll('-','')||start;
  const cells=Array.from({length:42},(_,i)=>{
    const n=i-offset+1;if(n<1||n>count)return '<div class="blank" aria-hidden="true"></div>';
    const date=viewYear+viewMonth+pad(n),day=days.get(date),state=day?.timelapse_count?'exported':day?.scan_status||'pending';
    const label=day?(day.timelapse_count?'有成片':scanText[day.scan_status]||'待扫描'):'无录像';
    return `<button class="day ${date===selected?'active':''} ${start&&date>=start&&date<=end?'in-range':''}" data-date="${date}" ${day?'':'disabled'} aria-label="${fmt(date)}，${label}" aria-pressed="${date===selected}" title="${fmt(date)} · ${label}${day?' · '+(day.total_files||0)+' 个文件':''}"><strong>${n}</strong>${day?`<i class="dot ${state}"></i>`:''}<span class="day-status">${label}</span></button>`;
  });
  setContent('#dates',`<div class="calendar">${['一','二','三','四','五','六','日'].map(n=>`<div class="weekday">${n}</div>`).join('')}${cells.join('')}</div>`);
}
function renderRange(){
  if(!$('#dateSelect').value&&overview.dates[0])$('#dateSelect').value=inputDate(overview.dates[0].date);
  const start=$('#dateSelect').value.replaceAll('-',''),end=$('#endDate').value.replaceAll('-','')||start;
  const days=overview.dates.filter(day=>day.date>=start&&day.date<=end),cameras=[...new Set(days.flatMap(day=>day.cameras?.length?day.cameras:day.source_cameras||[]))];
  const old=$('#cameraSelect').value,choices=[...new Set(['0','10','legacy',...cameras])];
  options('#cameraSelect',choices.map(c=>[c,cameraLabel(c)]),cameras.length===1?cameras[0]:old);
  $('#cameraHint').textContent=cameras.length===1?'已按范围选择摄像头。':cameras.length?'范围内有多个摄像头，请选择。':'';
  $('#rangeSummary').textContent=end<start?'结束日期不能早于开始日期。':`所选范围有录像 ${days.length} 天，已扫描 ${days.filter(day=>day.scan_status==='completed').length} 天。空白日期会自动跳过。`;
  for(const id of ['scan','timelapse','export'])$('#'+id).disabled=!start||end<start;
}
function renderQueue(){
  const tasks=overview.tasks;taskPage=Math.min(taskPage,Math.max(0,Math.ceil(tasks.length/8)-1));
  $('#queueCount').textContent=`· 待执行 ${overview.queued_count??tasks.filter(t=>t.status==='queued').length}`;
  $('#queueToggle').textContent=overview.queue_paused?'继续队列':'暂停队列';
  setContent('#tasks',tasks.slice(taskPage*8,taskPage*8+8).map(task=>{
    const retry=['failed','partial','interrupted','blocked','paused'].includes(task.status),p=task.progress||{};
    return `<article class="task"><b>${task.kind==='scan'?'人物扫描':'延时导出'} · ${fmt(task.date)}${task.dates?.length>1?' — '+fmt(task.dates.at(-1)):''}</b><span class="tag ${esc(task.status)}">${statusText[task.status]||esc(task.status)}</span>${task.status==='running'?`<div class="mini"><span>${esc(p.current_file||'准备中')}</span><span>${p.percent??0}%</span></div>`:''}${task.detail?`<small class="task-detail">${esc(task.detail)}</small>`:''}<div class="task-actions">${retry?`<button class="watch" data-retry="${esc(task.id)}">重试 / 继续</button>`:''}${['running','queued','paused','blocked'].includes(task.status)?`<button class="watch cancel" data-cancel="${esc(task.id)}">取消</button>`:''}</div></article>`;
  }).join('')||'<div class="empty">尚无任务</div>');
  pager('#taskPager',taskPage,tasks.length,8,'tasks');
}
function renderLibrary(){
  const items=overview.timelapses||[],years=new Set();
  for(const item of items)for(let y=Number(item.start_date.slice(0,4));y<=Number(item.end_date.slice(0,4));y++)years.add(String(y));
  options('#libraryYear',[['','全部年份'],...[...years].sort().reverse().map(y=>[y,y+' 年'])],$('#libraryYear').value);
  options('#libraryMonth',[['','全部月份'],...Array.from({length:12},(_,i)=>[pad(i+1),(i+1)+' 月'])],$('#libraryMonth').value);
  options('#libraryCamera',[['','全部摄像头'],...[...new Set(items.map(i=>i.camera))].sort().map(c=>[c,cameraLabel(c)])],$('#libraryCamera').value);
  const year=$('#libraryYear').value,month=year?$('#libraryMonth').value:'',camera=$('#libraryCamera').value,search=$('#librarySearch').value.trim().toLowerCase();
  $('#libraryMonth').disabled=!year;if(!year)$('#libraryMonth').value='';
  const start=year?year+(month||'01')+'01':'',end=year?year+(month||'12')+pad(month?new Date(Number(year),Number(month),0).getDate():31):'';
  const found=items.filter(item=>(!year||(item.start_date<=end&&item.end_date>=start))&&(!camera||item.camera===camera)&&(!search||`${item.name} ${rangeLabel(item)} ${cameraLabel(item.camera)}`.toLowerCase().includes(search)));
  libraryPage=Math.min(libraryPage,Math.max(0,Math.ceil(found.length/9)-1));
  $('#timelapseCount').textContent=`· ${found.length} / ${items.length} 条`;
  setContent('#timelapses',found.slice(libraryPage*9,libraryPage*9+9).map(item=>`<article class="timelapse"><div class="film-mark">${item.start_date.slice(0,4)}<span class="tag ${item.status==='partial'?'partial':'completed'}">${item.status==='partial'?'部分完成':'已成片'}</span></div><b>${rangeLabel(item)}</b><small>${esc(cameraLabel(item.camera))} · ${item.duration_seconds==null?'时长未记录':time(item.duration_seconds)} · ${fileSize(item.size_bytes)}</small><small>${esc(new Date(item.created_at).toLocaleString('zh-CN'))}</small>${item.status==='partial'?`<small>扫描失败 ${item.scan_failed_files||0} · 读取失败 ${item.failed_files||0}</small>`:''}${item.duration_adjusted_files?`<small>已按实际录像时长校正 ${item.duration_adjusted_files} 个素材</small>`:''}<small class="filename" title="${esc(item.name)}">${esc(item.name)}</small><button class="watch" data-watch="${esc(item.name)}">观看成片</button></article>`).join('')||`<div class="empty">${items.length?'没有符合筛选条件的成片。':'完成延时制作后，视频会归档到这里。'}</div>`);
  pager('#libraryPager',libraryPage,found.length,9,'library');
}
function render(){renderMission();renderPlans();renderRange();renderCalendar();renderQueue();renderLibrary()}
function setDateTarget(target){rangeTarget=target;$('#rangeHint').textContent=`点击日历：设置${target==='start'?'开始':'结束'}日期`;document.querySelectorAll('[data-range-target]').forEach(button=>{button.classList.toggle('active',button.dataset.rangeTarget===target);button.setAttribute('aria-pressed',String(button.dataset.rangeTarget===target))})}
function setRange(month){
  $('#dateSelect').value=`${viewYear}-${month||'01'}-01`;
  $('#endDate').value=`${viewYear}-${month||'12'}-${month?new Date(Number(viewYear),Number(month),0).getDate():31}`;
  notify(`已选择 ${viewYear} 年${month?' '+Number(month)+' 月':'全年'}，请确认摄像头与档位后创建任务。`);renderRange();renderCalendar();
}
async function showDate(date,offset=0){
  const changed=date!==selected,request=++detailRequest;selected=date;detailOffset=offset;renderCalendar();
  if(changed)setContent('#details','<div class="empty">正在读取当天事件…</div>');
  const data=await api('/api/date/'+date);if(date!==selected||request!==detailRequest)return;
  const events=Object.entries(data.events.events||{}).flatMap(([camera,list])=>list.map(event=>({...event,camera}))).sort((a,b)=>b.start.localeCompare(a.start));
  detailOffset=Math.min(offset,Math.max(0,(Math.ceil(events.length/50)-1)*50));
  const clips=(data.exports||[]).map(item=>`<button class="watch" data-watch="${esc(item.name)}">观看 ${rangeLabel(item)}</button>`).join('');
  const rows=events.slice(detailOffset,detailOffset+50).map(event=>`<div class="event">${event.thumbnail?`<img loading="lazy" src="${mediaURL(event.thumbnail)}" alt="人物事件缩略图">`:'<div></div>'}<div><b>${esc(cameraLabel(event.camera))} · ${esc(event.start.slice(11))} — ${esc(event.end.slice(11))}</b><br><small>检测命中 ${event.hits||0} 次</small></div></div>`).join('');
  setContent('#details',`<h3>${fmt(date)} · 人物事件 ${events.length} 段</h3>${clips?`<div class="date-exports">${clips}</div>`:''}${rows||'<div class="empty">尚未扫描或没有检测到人物。</div>'}<div id="eventPager" class="pager"></div>`);
  pager('#eventPager',detailOffset/50,events.length,50,'events');detailSignature=signature(overview.dates.find(day=>day.date===date));
}
function watch(name){
  const item=(overview.timelapses||[]).find(item=>item.name===name);if(!item)return notify('成片索引尚未更新，请稍后重试。');
  $('#playerTitle').textContent=rangeLabel(item)+' · '+cameraLabel(item.camera);$('#playerInfo').textContent=`${item.name} · ${fileSize(item.size_bytes)}${item.status==='partial'?' · 部分完成，请检查失败项':''}`;
  $('#playerVideo').src=mediaURL(item.name);$('#downloadVideo').href=mediaURL(item.name);$('#downloadVideo').download=item.name;
  $('#player').showModal();
}
async function load(statusOnly=false){
  if(statusOnly?statusLoading:overviewLoading)return;if(statusOnly)statusLoading=true;else overviewLoading=true;
  try{overview={...overview,...await api(statusOnly?'/api/status':'/api/overview')};render();const current=signature(overview.dates.find(day=>day.date===selected));if(!statusOnly&&selected&&current!==detailSignature)await showDate(selected,detailOffset)}catch(error){notify(error)}finally{if(statusOnly)statusLoading=false;else overviewLoading=false}
}
async function createTask(kind){
  try{const date=$('#dateSelect').value.replaceAll('-',''),end_date=$('#endDate').value.replaceAll('-','');if(!date)throw Error('请选择开始日期');notify('正在创建范围任务…');
    const result=await api('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,date,end_date,camera:$('#cameraSelect').value,profile:$('#profile').value,force:$('#forceScan').checked})});
    planPage=0;notify(kind==='timelapse'?`计划已创建：补扫 ${result.scan_created} 个日期，再合成 1 条范围视频。总进度会显示在上方。`:kind==='scan'?`已创建范围扫描：${result.created} 个日期任务。`:`已创建范围合成：包含 ${result.dates.length} 个录像日。`);await load();
  }catch(error){notify(error)}
}
async function retry(id){await api('/api/tasks/'+id+'/retry',{method:'POST'});await load(true)}
async function retryPlan(id){const plan=overview.plans.find(plan=>plan.id===id);if(!plan)return;for(const taskId of plan.retry_ids)await api('/api/tasks/'+taskId+'/retry',{method:'POST'});notify('未完成项已重新排队，成功处理的文件会自动跳过。');await load(true)}
async function cancel(id){if(!confirm('确定取消该任务吗？已完成的结果会保留。'))return;await api('/api/tasks/'+id,{method:'DELETE'});await load(true)}

function directoryMessage(value){$('#directoryNotice').textContent=value.message||value}
async function openDirectories(){
  $('#directoryDialog').showModal();directoryMessage('正在读取目录设置…');
  try{const data=await api('/api/directories');
    $('#scanDirectory').value=data.input_root;$('#exportDirectory').value=data.export_root;
    for(const [kind,roots] of [['scan',data.scan_roots],['export',data.export_roots]])setContent('#'+kind+'Mounts',roots.map(root=>`<button type="button" data-directory-root="${esc(root.path)}" data-directory-kind="${kind}" ${root.available?'':'disabled'}>${esc(root.path)}${root.available?'':' · 未挂载'}</button>`).join(''));
    $('#stateDirectory').textContent=data.state_root+' · 队列 / 人物索引 / 缩略图 / 片段缓存';
    const d=data.diagnostics||{};$('#directoryDiagnostics').textContent=`${data.indexing?'正在建立索引。':d.available?`当前索引：${d.mp4_files||0} 个 MP4，${d.recognized_files||0} 个文件名可识别。`:'尚无可用录像索引。'} ${d.scanned_at?'最近完成：'+new Date(d.scanned_at).toLocaleString('zh-CN',{hour12:false})+'。 ':''}${data.settings_error||d.message||''}`;
    $('#directoryBrowser').hidden=true;directoryMessage('修改目录需等待索引与队列空闲；已保存的文件不会迁移。');
  }catch(error){directoryMessage(error)}
}
async function browseDirectory(kind,path){
  try{const input=kind==='scan'?'#scanDirectory':'#exportDirectory',data=await api('/api/directories/browse?kind='+kind+'&path='+encodeURIComponent(path||$(input).value));
    $(input).value=data.path;$('#directoryBrowser').hidden=false;
    setContent('#directoryBrowser',`<b>${esc(data.path)}</b><button type="button" data-directory-up="${esc(data.path)}" data-directory-kind="${kind}">↑ 上一级（仅限挂载范围）</button>${data.children.map(child=>`<button type="button" data-directory-root="${esc(child.path)}" data-directory-kind="${kind}">▸ ${esc(child.name)}</button>`).join('')||'<p class="subtle">没有子目录，可使用当前目录。</p>'}${data.truncated?'<p class="subtle">仅列出前 200 个子目录，可直接输入路径。</p>':''}`);
    directoryMessage('当前路径已填入，点击“保存目录设置”后生效。');
  }catch(error){directoryMessage(error)}
}

document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button||button.disabled)return;const d=button.dataset;
  if(d.browseKind)browseDirectory(d.browseKind);
  else if(d.directoryRoot)browseDirectory(d.directoryKind,d.directoryRoot);
  else if(d.directoryUp)browseDirectory(d.directoryKind,d.directoryUp.slice(0,d.directoryUp.lastIndexOf('/'))||'/');
  else if(d.rangeTarget)setDateTarget(d.rangeTarget);
  else if(d.month){viewMonth=d.month;renderCalendar()}
  else if(d.date){$('#'+(rangeTarget==='start'?'dateSelect':'endDate')).value=inputDate(d.date);renderRange();showDate(d.date).catch(notify)}
  else if(d.watch)watch(d.watch);
  else if(d.retry)retry(d.retry).catch(notify);
  else if(d.retryPlan)retryPlan(d.retryPlan).catch(notify);
  else if(d.cancel)cancel(d.cancel).catch(notify);
  else if(d.list){const page=Number(d.page);if(d.list==='plans'){planPage=page;renderPlans()}else if(d.list==='tasks'){taskPage=page;renderQueue()}else if(d.list==='library'){libraryPage=page;renderLibrary()}else showDate(selected,page*50).catch(notify)}
});
$('#yearSelect').onchange=()=>{viewYear=$('#yearSelect').value;renderCalendar()};$('#monthSelect').onchange=()=>{viewMonth=$('#monthSelect').value;renderCalendar()};
$('#selectMonth').onclick=()=>setRange(viewMonth);$('#selectYear').onclick=()=>setRange('');
for(const id of ['dateSelect','endDate']){$('#'+id).onfocus=()=>setDateTarget(id==='dateSelect'?'start':'end');$('#'+id).onchange=()=>{renderRange();renderCalendar()}}
for(const id of ['libraryYear','libraryMonth','libraryCamera'])$('#'+id).onchange=()=>{libraryPage=0;renderLibrary()};$('#librarySearch').oninput=()=>{libraryPage=0;renderLibrary()};
for(const [id,kind] of [['timelapse','timelapse'],['scan','scan'],['export','export']])$('#'+id).onclick=()=>createTask(kind);
$('#queueToggle').onclick=async()=>{try{await api('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:overview.queue_paused?'resume':'pause'})});await load(true)}catch(error){notify(error)}};
$('#refreshIndex').onclick=async()=>{try{await api('/api/index',{method:'POST'});notify('已请求在后台检查新增录像。');await load()}catch(error){notify(error)}};
$('#openDirectories').onclick=openDirectories;$('#closeDirectories').onclick=()=>$('#directoryDialog').close();
$('#directoryRefresh').onclick=async()=>{try{await api('/api/index',{method:'POST'});directoryMessage('已请求刷新当前扫描目录；输入框内尚未保存的路径不会生效。');await load()}catch(error){directoryMessage(error)}};
$('#directoryForm').onsubmit=async event=>{event.preventDefault();$('#saveDirectories').disabled=true;directoryMessage('正在检查目录与权限…');try{await api('/api/directories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_root:$('#scanDirectory').value.trim(),export_root:$('#exportDirectory').value.trim()})});directoryMessage('已保存。后台正在更新索引，旧成片与任务记录已保留。');await load()}catch(error){directoryMessage(error)}finally{$('#saveDirectories').disabled=false}};
$('#closePlayer').onclick=()=>$('#player').close();$('#player').addEventListener('close',()=>{$('#playerVideo').pause();$('#playerVideo').removeAttribute('src');$('#playerVideo').load()});
setDateTarget('start');load();setInterval(()=>load(true),2000);setInterval(()=>load(),20000);
setInterval(()=>{const run=active();if(run)$('#elapsed').textContent=`${time(elapsed(run,run.progress))} 已运行`;$('#clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false})},1000);
