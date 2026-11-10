"use strict";
/* Evidence-derived activity, not generated or private model reasoning. */
(function(root){
  'use strict';
  const tools={goal_action:'更新目标',plan_tasks:'保存任务树',run_task:'执行子任务',read_file:'读取文件',list_files:'查看工作区',code_search:'检索代码',symbol_info:'查找符号',write_file:'写入文件',patch_file:'修改文件',apply_changes:'应用修改',run_shell:'运行命令',confirm_task:'申请人工验收',recall_evidence:'查回证据',find_evidence:'查找证据',load_skill:'读取技能说明'};
  const stages={connected:'已连接模型服务',analysis:'模型正在分析',answer:'正在生成回复',tool_arguments:'正在准备工具操作'};
  function target(e){
    if(e.target)return String(e.target).slice(0,180);
    const args=e.args||{};
    if(typeof args.path==='string')return args.path.slice(0,180);
    if(e.name==='web_search')return '['+String(args.source||'auto')+'] '+String(args.query||'').slice(0,160);
    if(e.name==='web_read')return String(args.url||'').slice(0,180);
    if(e.name==='web_find')return String(args.pattern||'').slice(0,120);
    try {if(e.name==='goal_action')return String(JSON.parse(args.proposal||'{}').title||'').slice(0,160);}catch{}
    return '';
  }
  function toolLabel(e){
    if(e.name==='plan_tasks') {
      let count=e.task_count;
      try {count??=JSON.parse(e.args?.steps||'[]').length;}catch{}
      return '任务树'+(count?'（'+count+'项）':'');
    }
    return (tools[e.name]||({web_search:'搜索网页',web_read:'读取网页',web_find:'页内查找'}[e.name])||'工具操作')+(target(e)?'：'+target(e):'');
  }
  function describe(job){
    const done=['completed','failed','stopped'].includes(job.status),entries=[];
    let phase='waiting',label=job.status==='queued'?'请求已提交，正在准备':'等待模型响应',detail='请求准备中，收到响应后将更新进度。';
    let errors=0;
    const push=(text,kind='info')=>{if(entries.at(-1)?.text!==text)entries.push({text,kind});};
    for(const e of job.events||[]) {
      const child=e.delegated_run_id?'子任务 · ':'';
      if(e.event==='model_requested') {phase='waiting';label=child+'等待模型响应';detail='模型请求已发出，等待服务返回。';push(child+'已发送模型请求');}
      if(e.event==='model_progress'&&stages[e.phase]) {phase=e.phase;label=child+stages[e.phase];detail=e.phase==='tool_arguments'?'正在接收操作参数，尚未执行。':e.phase==='analysis'?'模型正在处理请求，尚未返回完整结果。':e.phase==='answer'?'正文会逐步显示，最终结果以本轮结束后的记录为准。':'服务已开始返回数据。';push(child+stages[e.phase]);}
      if(e.event==='tool_started') {phase='tool';label=child+'准备'+toolLabel(e);detail='通过校验与所需确认后才会执行。';}
      if(e.event==='tool_executed') {
        const ok=e.tool_status==='ok';if(!ok)errors++;
        phase='tool';label=child+(ok?'已完成操作：':'操作未完成：')+toolLabel(e);
        detail=ok?'工具已返回，正在处理结果。':'已保留失败或拒绝记录，后续动作仍需遵守原约束。';push(label,ok?'success':'warning');
        if(e.name==='web_search'&&e.search_sources){
          const names={exa_mcp:'Exa Search MCP',bing:'Bing',baidu:'百度',wechat:'微信·搜狗索引',crossref:'Crossref',pubmed:'PubMed'};
          label='搜索来源：'+e.search_sources.map(s=>(names[s.source]||s.source)+(s.status==='error'?(s.failure_type==='verification_required'?'需验证':'不可用'):s.status==='empty'?'无结果':' '+s.count+'条')+(s.low_query_overlap?'（关键词匹配不足）':'')).join(' · ');
          detail=e.search_status==='partial'?'部分来源未返回，已保留错误和可用结果。':'各来源结果已记录；检索摘要不等于正文核验。';
          push(label,e.search_status==='ok'||e.search_status==='empty'?'success':'warning');
        }
        if(e.web_cache==='hit'||e.web_cache==='snapshot')push(child+'复用本会话网页快照（未重新联网）','success');
        if(e.web_citation)push(child+'已保留原文片段位置','success');
        if(e.document_type==='pdf'){
          push(child+'PDF · 共'+e.pdf_page_count+'页'+(e.pdf_start_page?' · 本次第'+e.pdf_start_page+(e.pdf_end_page!==e.pdf_start_page?'–'+e.pdf_end_page:'')+'页':''),'info');
          if(e.pdf_text_status!=='text_extracted')push(child+'PDF存在无可提取文字的页面，尚未进行OCR','warning');
        }
        if(e.web_budget_stop)push(child+'检索预算已用尽，使用已有证据回答','warning');
      }
      if(e.event==='approval_requested')push(child+'等待你的确认');
      if(e.event==='approval_resolved')push(child+(e.allowed?'已收到确认':'本项操作已被拒绝'),e.allowed?'success':'warning');
      if(e.event==='model_transport'&&e.ok===false) {phase='network';label=e.will_retry?'连接异常，正在重试':'模型连接未成功';detail='可以查看最近记录，或停止后重试。';push(label,'warning');}
    }
    if(job.status==='waiting_approval'){phase='approval';label='等待你确认下一步';detail='确认前不会继续该项操作。';}
    if(job.status==='cancelling'){phase='cancelling';label='正在停止';detail='已完成的操作会保留，尚未执行的操作不会自动重放。';}
    if(done){phase=job.status;label=job.status==='failed'?'本轮发生异常':job.status==='stopped'?'本轮已停止':'本轮回复已结束';detail=errors?'包含'+errors+'项未完成操作，可展开查看；回复结束不等于任务验收通过。':'执行摘要来自真实记录；任务验收状态以右侧任务树为准。';}
    return {phase,label,detail,done,elapsedSeconds:Math.max(0,Math.floor((Number(job.elapsed_ms)||0)/1000)),entries:entries.slice(-8)};
  }
  root.GoalTreeProgress=Object.freeze({describe});
})(globalThis);

const $ = id => document.getElementById(id);
const welcome = $('welcome').cloneNode(true);
const state = {config:null, tree:null, goalId:'', nodeId:'', focusId:'', mode:'assistant', sessionId:'', taskId:'', messages:[], base:[], busy:false, switching:false, initializing:false, scopeEpoch:0, latestJob:null, job:null};
state.cooperation={planning:'balanced',implementation:'balanced'}; state.drafts=new Map(); state.uploading=0; state.savingCooperation=false;
state.activityOpen=new Map();
state.workspaceId='';state.switchingWorkspace=false;
function draftKey(session=state.sessionId,workspace=state.workspaceId){return workspace?workspace+'|'+session:session;}
let toastTimer;
function node(tag, className='', text) { const e=document.createElement(tag); if(className)e.className=className; if(text!==undefined)e.textContent=text; return e; }
function toast(text) { $('toast').textContent=text; $('toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),5000); }
async function api(path,data,scope) {
  const headers={};const workspace=scope?.workspaceId??state.workspaceId;
  if(workspace)headers['X-GoalTree-Workspace']=workspace;
  if(data!==undefined){headers['Content-Type']='application/json';headers['X-Pico-Token']=scope?.csrf??state.config.csrf;}
  const response=await fetch(path,data===undefined?{headers}:{method:'POST',headers,body:JSON.stringify(data)});
  const value=await response.json(); if(!response.ok)throw new Error(value.error||'请求失败'); return value;
}
function setBusy(value) {
  state.busy=value; $('send').disabled=value||state.switching||state.initializing; $('new-session').disabled=value||state.switching||state.initializing;
  $('cancel-task').classList.toggle('hidden',!value); $('cancel-task').disabled=!value;
  $('session-badge').textContent=value?(state.latestJob&&globalThis.GoalTreeProgress?GoalTreeProgress.describe(state.latestJob).label:'请求已提交'):'';
  $('composer-state').textContent=value?'进度会随真实记录更新':'Enter 发送 · Shift + Enter 换行';
  renderTree();
  renderComposerTools();
}
function showMessages(messages) {
  const container=$('conversation'); const follow=container.scrollHeight-container.scrollTop-container.clientHeight<100;
  const fragment=document.createDocumentFragment(); if(!messages.length)fragment.append(welcome.cloneNode(true));
  for(const m of messages) {
    const article=node('article',`message ${m.role}${m.pending?' pending':''}${m.error?' error':''}`);
    const body=node('div','message-body');
    if(m.role==='assistant'&&!m.error&&globalThis.GoalTreeMarkdown) {
      body.classList.add('markdown-body');
      // Only this local renderer's HTML is accepted, never raw model HTML.
      body.innerHTML=GoalTreeMarkdown.render(m.content);
    } else body.textContent=m.content;
    if(m.attachments?.length) {
      const attachments=node('div','message-attachments');
      for(const file of m.attachments)attachments.append(node('span','attachment-chip',`${file.mime?.startsWith('image/')?'图片':'文件'} · ${file.name}`));
      body.append(attachments);
    }
    article.append(node('div','message-label',m.role==='user'?'你':'GoalTree'));
    if(m.activity&&globalThis.GoalTreeProgress)article.append(activityCard(m.activity));
    if(m.content||m.attachments?.length)article.append(body);
    fragment.append(article);
  }
  container.replaceChildren(fragment); if(follow)container.scrollTop=container.scrollHeight;
}
function renderConnection() {
  const offline=/offline|scripted/i.test(state.config.model);
  $('model-name').textContent=offline?'离线脚本 · 不支持自由规划':state.config.model;
  $('model-name').title=state.config.model;
  if(offline) {
    welcome.textContent='当前为固定脚本演示；自由对话与目标规划需要接入真实模型。';
    $('prompt').placeholder='离线模式仅支持预设的资料整理演示…';
  }
  $('connection').textContent=(state.config.shell_backend==='appcontainer'?'本地 · 隔离执行':'本地 · 宿主执行')+(state.config.web_access?' · 可联网':'')+(state.config.search_backend==='exa-mcp'?(state.config.search_mcp?.ready?' · Exa MCP':' · MCP待连接'):'');
}
async function refreshSessions() { /* Goal conversations replace the old session list. */ }
async function refreshTree() { state.tree=await api('/api/tree'); renderTree(); }
function isViewedJob(job) { const b=job.binding; return b?b.goal_id===state.goalId&&b.task_id===state.nodeId&&b.mode===state.mode:!state.goalId; }
function baseForRun(messages,prompt) {
  for(let i=messages.length-1;i>=0;i--)if(messages[i].role==='user'&&messages[i].content===prompt)return messages.slice(0,i+1);
  return [...messages,{role:'user',content:prompt}];
}
function adoptGoal(job) {
  if(job.binding?.mode==='assistant'&&job.binding.goal_id&&!state.goalId&&job.session_id===state.sessionId) {
    state.goalId=job.binding.goal_id; state.nodeId=''; state.mode='assistant';
  }
}
async function send() {
  if(state.busy||state.switching||state.switchingWorkspace||state.initializing||state.uploading||state.savingCooperation)return;
  const attachments=state.drafts.get(draftKey())||[];
  const prompt=$('prompt').value.trim()||(attachments.length?'请查看这些附件，先概括内容，不自动创建目标或执行任务。':''); if(!prompt)return;
  if(!state.config.configured)return toast('请在本地配置模型接口后重启服务。');
  state.base=[...state.messages,{role:'user',content:prompt,attachments}]; setBusy(true);
  showMessages([...state.base,{role:'assistant',content:'请求正在提交…',pending:true}]); $('run-error').classList.add('hidden');
  try {
    const job=await api('/api/tasks',{prompt,goal_id:state.goalId,node_id:'',mode:'assistant',session_id:state.sessionId,policy:'ask',cooperation:state.cooperation,attachments:attachments.map(f=>f.id),max_steps:state.config.recommended_steps||20});
    state.drafts.delete(draftKey()); renderComposerTools();
    state.taskId=job.id; state.latestJob=job; $('prompt').value=''; adoptGoal(job); if(isViewedJob(job))renderJob(job); poll(job.id);
  } catch(error) { setBusy(false); showMessages(state.messages); toast(error.message); }
}
async function poll(id,failures=0) {
  if(state.taskId!==id)return;
  try {
    const job=await api(`/api/tasks/${id}`); if(state.taskId!==id)return;
    renderConnection(); state.latestJob=job; adoptGoal(job); if(isViewedJob(job))renderJob(job);
    if(state.tree)await refreshTree().catch(error=>toast(error.message));
    if(['completed','failed','stopped'].includes(job.status)) { setBusy(false); await refreshSessions().catch(error=>toast(error.message)); }
    else setTimeout(()=>poll(id),1000);
  } catch(error) {
    if(state.taskId!==id)return; const attempts=failures+1;
    if(attempts<5) { $('connection').textContent=`连接中断 · 正在重试 ${attempts}/4`; setTimeout(()=>poll(id,attempts),Math.min(1000*2**failures,8000)); }
    else { $('connection').textContent='连接中断 · 自动重试已停止，请刷新页面'; toast(error.message); }
  }
}
function pendingAnswer(events) {
  events=(events||[]).filter(e=>!e.delegated_run_id);
  const boundary=events.map(e=>e.event==='model_requested'||(e.event==='model_transport'&&e.stream_reset===true)).lastIndexOf(true);
  return events.slice(boundary+1).filter(e=>e.event==='model_delta').map(e=>e.text||'').join('');
}
function modelErrorText(job) {
  if(!job.error)return '';
  if(job.outcome?.code==='model_stream_interrupted'||/Incomplete SSE response/.test(job.error))
    return '模型回复流中断，未收到完整结果。残缺工具调用未执行，已完成操作已保留，可以继续当前会话。';
  return job.error;
}
function renderJob(job) {
  state.job=job; if(job.session_id)state.sessionId=job.session_id;
  const done=['completed','failed','stopped'].includes(job.status), messages=[...state.base];
  if(job.answer)messages.push({role:'assistant',content:job.answer,activity:job});
  const errorText=modelErrorText(job);
  if(errorText)messages.push({role:'assistant',content:errorText,error:true,activity:job.answer?null:job});
  if(!done) {
    const partial=pendingAnswer(job.events);
    messages.push({role:'assistant',content:partial,pending:true,activity:job});
  }
  state.messages=messages.filter(m=>!m.pending); showMessages(messages);
  $('session-badge').textContent=globalThis.GoalTreeProgress?GoalTreeProgress.describe(job).label:(done?'已结束':'等待响应');
  $('run-error').classList.toggle('hidden',!errorText); $('run-error').textContent=errorText;
  renderApproval(job.approval);
}
function activityCard(job) {
  const progress=GoalTreeProgress.describe(job),card=node('details','run-progress'+(progress.done?' finished':''));
  card.open=state.activityOpen.get(job.id)??!progress.done;
  const summary=node('summary');summary.append(node('span','progress-label',progress.label),node('small','progress-elapsed',`总耗时 ${progress.elapsedSeconds}秒`));
  summary.addEventListener('click',()=>state.activityOpen.set(job.id,!card.open));
  card.append(summary,node('p','progress-detail',progress.detail));
  if(progress.entries.length) {
    const list=node('ol','progress-steps');
    for(const entry of progress.entries)list.append(node('li',entry.kind,entry.text));
    card.append(list);
  }
  return card;
}
const toolNames={write_file:'写入文件',patch_file:'修改文件',apply_changes:'应用修改',run_shell:'运行命令',confirm_task:'确认任务验收',rewind_changes:'撤销修改'};
function approvalContent(approval) {
  let args=approval.args||{}, task=args.task||args;
  if(typeof args.proposal==='string'){try{task=JSON.parse(args.proposal);}catch{task={description:'提议格式尚未通过校验，请核对原始参数。'};}}
  if(!task||typeof task!=='object'||Array.isArray(task))task={description:'请核对原始参数。'};
  const blocks=[];
  if(args.step)blocks.push(`步骤 ${args.step} / ${args.total}`);
  if(task.title)blocks.push('### '+task.title);
  for(const [key,label] of [['description','说明'],['constraints','边界'],['acceptance','验收条件'],['instruction','执行要求']])if(typeof task[key]==='string'&&task[key])blocks.push('**'+label+'**\n\n'+task[key]);
  if(Array.isArray(args.stages))blocks.push('**阶段安排**\n\n'+args.stages.map(s=>'- '+s).join('\n'));
  if(Array.isArray(args.proposal))for(const step of args.proposal)blocks.push((step.parent_id?'#### ':'### ')+step.title+'\n\n'+(step.description||''));
  if(args.path)blocks.push('**文件**\n\n`'+String(args.path).replaceAll('`','')+'`');
  if(args.command)blocks.push('**将执行的命令**\n\n'+String(args.command).split('\n').map(s=>'    '+s).join('\n'));
  if(typeof args.content==='string') {
    const content=args.content.slice(0,12000);
    blocks.push('**拟写入内容**\n\n'+(/\.(md|markdown)$/i.test(args.path||'')?content:content.split('\n').map(s=>'    '+s).join('\n')));
    if(args.content.length>12000)blocks.push('预览已截断，完整内容请展开原始参数核对。');
  }
  for(const [key,label] of [['old_text','待替换原文'],['new_text','替换后内容']])if(typeof args[key]==='string') {
    blocks.push('**'+label+'**\n\n'+args[key].slice(0,12000).split('\n').map(s=>'    '+s).join('\n'));
    if(args[key].length>12000)blocks.push('预览已截断，完整内容请展开原始参数核对。');
  }
  if(typeof args.reason==='string')blocks.push('**验收依据**\n\n'+args.reason);
  if(approval.name==='confirm_task'&&Array.isArray(args.tasks)) {
    blocks.push('**本次验收范围（系统读取的实际节点）**');
    for(const item of args.tasks)blocks.push('### '+item.title+'\n\n'+
      (item.acceptance?'验收条件：'+item.acceptance+'\n\n':'')+
      (item.progress_note?'产出说明：'+item.progress_note+'\n\n':'')+
      (item.output_preview?'实际产出：\n\n'+item.output_preview+'\n\n'+(item.output_preview_truncated?'预览已截断，请核对完整产出。\n\n':''):'')+
      '版本：'+item.revision+'；当前状态：'+(statuses[item.status]||item.status));
  }
  if(!blocks.length)blocks.push('请确认是否允许此项操作。具体参数可在下方展开核对。');
  return blocks.join('\n\n');
}
function renderApproval(approval) {
  $('approval').classList.toggle('hidden',!approval); if(!approval)return;
  $('approval-name').textContent=approval.kind&&approval.kind!=='safety'?({'planning_step':'确认规划步骤','planning_direction':'确认规划方向','implementation_task':'确认启动任务','implementation_step':'确认实现步骤'}[approval.kind]||'确认推进'):(toolNames[approval.name]||approval.name);
  const summary=$('approval-summary'),text=approvalContent(approval);
  if(globalThis.GoalTreeMarkdown)summary.innerHTML=GoalTreeMarkdown.render(text);else summary.textContent=text;
  $('approve').textContent=approval.kind?.startsWith('planning_')?'确认规划':approval.kind==='implementation_task'?'启动此任务':'允许执行';
  $('approval-args').textContent=JSON.stringify(approval.args,null,2); $('approve').disabled=false; $('deny').disabled=false;
}
async function decide(allowed) {
  const a=state.latestJob?.approval; if(!a)return; $('approve').disabled=true; $('deny').disabled=true;
  try { await api(`/api/tasks/${state.taskId}/approvals/${a.id}`,{allowed}); $('approval').classList.add('hidden'); } catch(error) { toast(error.message); }
}
const statuses={planned:'待进行',active:'进行中',blocked:'已阻塞',completed:'已完成',review:'待验收',failed:'失败',cancelled:'已取消'};
function taskStatusLabel(task) {
  if(task.context_status==='archived')return '已归档';
  if(task.validity==='stale'&&task.status==='completed')return '待复核';
  const status=task.display_status||task.status;
  return statuses[status]||status;
}
function renderTree() {
  if(!state.tree)return;
  const key=JSON.stringify([state.tree.revision,state.goalId,state.nodeId,state.focusId,state.mode,state.busy,state.latestJob?.id,state.latestJob?isViewedJob(state.latestJob):null]);
  if(key===state.treeRenderKey)return; state.treeRenderKey=key; paintTree();
}
function paintTree() {
  const list=$('goals'); list.replaceChildren();
  for(const g of Object.values(state.tree.goals).sort((a,b)=>a.created_at.localeCompare(b.created_at))) {
    const b=node('button',`goal-item${g.id===state.goalId?' selected':''}`,g.title); b.dataset.goalId=g.id;
    b.addEventListener('click',()=>selectNode(g.id).catch(e=>toast(e.message))); list.append(b);
  }
  if(!Object.keys(state.tree.goals).length)list.append(node('p','muted small','暂无目标'));
  $('return-active').classList.toggle('hidden',!state.busy||!state.latestJob||isViewedJob(state.latestJob));
  const goal=state.tree.goals[state.goalId], tree=$('task-tree'); tree.replaceChildren();
  $('conversation-title').textContent=goal?.title||'新对话';
  const tasks=Object.values(state.tree.tasks).filter(t=>t.goal_id===state.goalId).sort((a,b)=>a.created_at.localeCompare(b.created_at));
  $('tree-count').textContent=tasks.length?String(tasks.length):'';
  if(globalThis.GoalTreeView){
    $('node-detail').classList.add('hidden');
    GoalTreeView.render({goal,tasks,events:state.tree.events||[],container:tree,scope:state.workspaceId+'|'+state.goalId,focusId:state.focusId,
      onFocus:id=>{state.focusId=id;state.treeRenderKey=null;renderTree();}});
    return;
  }
  if(!goal||!tasks.length)tree.append(node('p','muted','暂无任务'));
  else {
    const heading=node('div','tree-goal',goal.title); tree.append(heading);
    function branch(parent) {
      const ul=node('ul','tree-branches');
      for(const t of tasks.filter(t=>t.parent_id===parent)) {
        const li=node('li'), button=node('button',`tree-node${state.focusId===t.id?' selected':''}`); button.dataset.nodeId=t.id;
        const status=t.display_status||t.status, progress=t.rollup;
        const label=taskStatusLabel(t);
        const badge=node('small','',label+(progress?.total?' · '+progress.completed+'/'+progress.total:''));
        if(progress)badge.title=`${progress.completed}/${progress.total} 个子任务已有效验收；${progress.review} 个待验收；${progress.excluded} 个已归档或取消`;
        button.append(node('span',`tree-dot ${status}`),node('strong','',t.title),badge);
        button.addEventListener('click',()=>{state.focusId=state.focusId===t.id?'':t.id;renderTree();});
        li.append(button); if(tasks.some(x=>x.parent_id===t.id))li.append(branch(t.id)); ul.append(li);
      }
      return ul;
    }
    tree.append(branch(''));
  }
  const detail=$('node-detail'), item=state.tree.tasks[state.focusId]; detail.replaceChildren(); detail.classList.toggle('hidden',!item||item.goal_id!==state.goalId);
  if(item&&item.goal_id===state.goalId) {
    detail.append(node('strong','',item.title));
    if(item.rollup) {
      const p=item.rollup;
      detail.append(node('p','',`子任务：${p.completed}/${p.total} 已有效验收，${p.review} 待验收，${p.active} 进行中，${p.blocked+p.failed} 阻塞或失败。`));
      if(p.excluded)detail.append(node('p','muted',`${p.excluded} 个已归档或取消的子任务不计入当前范围。`));
      if(p.display_status==='review')detail.append(node('p','muted','子任务和阶段自身仍需完成相应验收；状态汇总不会自动批准验收。'));
    }
    for(const value of [item.description,item.acceptance&&'验收：'+item.acceptance,item.constraints&&'约束：'+item.constraints])if(value)detail.append(node('p','',value));
    if(item.progress_note&&!(item.status==='planned'&&item.validity==='stale'))detail.append(node('p','', '进展：'+item.progress_note));
    if(item.validity==='stale')detail.append(node('p','muted','任务要求已更新；历史结论需要按当前要求核对。'));
    if(item.depends_on.length)detail.append(node('p','muted','依赖：'+item.depends_on.map(id=>state.tree.tasks[id]?.title||id).join('、')));
  }
}
async function selectNode(goalId,nodeId='',mode='assistant') {
  const epoch=++state.scopeEpoch; state.switching=true; $('send').disabled=true; $('prompt').disabled=true;
  $('new-session').disabled=true;
  try {
    const context=await api(`/api/tree/context?goal_id=${encodeURIComponent(goalId)}&node_id=${encodeURIComponent(nodeId)}&mode=${mode}`);
    if(epoch!==state.scopeEpoch)return;
    const result=await api('/api/tree',{action:'select',goal_id:goalId,task_id:nodeId}); if(epoch!==state.scopeEpoch)return;
    state.tree=result.tree; state.goalId=goalId; state.nodeId=nodeId; state.focusId=''; state.mode=mode; state.sessionId=context.session_id;
    state.messages=context.messages; state.base=[...context.messages]; state.job=null;
    state.cooperation=context.cooperation||{planning:'balanced',implementation:'balanced'};
    if(state.config?.attachments) { $('cooperation-panel').classList.add('hidden');$('cooperation-toggle').setAttribute('aria-expanded','false'); }
    $('conversation-title').textContent=context.node.title; $('prompt').value=''; $('session-badge').textContent='';
    $('approval').classList.add('hidden'); $('run-error').classList.add('hidden'); showMessages(state.messages); renderTree();
    if(state.busy&&state.latestJob&&isViewedJob(state.latestJob)) {
      state.base=baseForRun(context.messages,state.latestJob.prompt); renderJob(state.latestJob);
    }
  } finally { if(epoch===state.scopeEpoch) {state.switching=false;$('prompt').disabled=state.initializing;$('send').disabled=state.busy||state.initializing;$('new-session').disabled=state.busy||state.initializing;renderComposerTools();} }
}

function renderComposerTools() {
  if(!state.config?.attachments)return;
  const locked=state.busy||state.switching||state.switchingWorkspace||state.initializing||!!state.uploading||state.savingCooperation;
  $('workspace-select').disabled=locked||!state.config.workspace_switching;
  $('goals').inert=state.switchingWorkspace||state.initializing;
  for(const id of ['add-attachment','cooperation-toggle','save-cooperation','attachment-input'])$(id).disabled=locked;
  for(const input of $('cooperation-panel').querySelectorAll('input'))input.disabled=locked;
  $('send').disabled=locked; $('new-session').disabled=locked;
  const labels={step:'逐步',balanced:'适中',autonomous:'自主'};
  $('cooperation-toggle').textContent=`规划 ${labels[state.cooperation.planning]} · 实现 ${labels[state.cooperation.implementation]}`;
  const list=$('attachment-drafts');list.replaceChildren();
  for(const file of state.drafts.get(draftKey())||[]) {
    const chip=node('div','attachment-chip');
    if(file.mime.startsWith('image/')) {
      const img=node('img','attachment-thumbnail');img.src=`/api/attachments/${encodeURIComponent(file.id)}?goal_id=${encodeURIComponent(state.goalId)}&session_id=${encodeURIComponent(state.sessionId)}&workspace_id=${encodeURIComponent(state.workspaceId)}`;img.alt=file.name;chip.append(img);
    }
    chip.append(node('span','',file.name));
    const remove=node('button','attachment-remove','×');remove.type='button';remove.setAttribute('aria-label','移除附件 '+file.name);remove.disabled=locked;
    remove.addEventListener('click',()=>{state.drafts.set(draftKey(),(state.drafts.get(draftKey())||[]).filter(f=>f.id!==file.id));renderComposerTools();});chip.append(remove);list.append(chip);
  }
  if(state.uploading)list.append(node('span','muted small','正在读取附件…'));
}

async function addAttachments(files) {
  if(state.busy||state.switching||state.switchingWorkspace||state.initializing||state.uploading)return;
  const owner=state.sessionId, goal=state.goalId, epoch=state.scopeEpoch, workspace=state.workspaceId, csrf=state.config.csrf;
  const key=draftKey(owner,workspace);
  const selected=Array.from(files);
  if((state.drafts.get(key)||[]).length+selected.length>4)return toast('每条消息最多4个附件。');
  state.uploading++;renderComposerTools();
  try {
    for(const file of selected) {
      if(file.size>state.config.attachments.max_bytes)throw new Error('单个附件不能超过5MB。');
      if(/\.(png|jpe?g|webp)$/i.test(file.name)&&!state.config.attachments.image_input)throw new Error('当前模型未启用图片理解，不能发送图片。');
      const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=()=>reject(new Error('文件读取失败'));r.readAsDataURL(file);});
      const result=await api('/api/attachments',{name:file.name,data,goal_id:goal,session_id:owner},{workspaceId:workspace,csrf});
      state.drafts.set(key,[...(state.drafts.get(key)||[]),result]);
    }
    if(epoch!==state.scopeEpoch)toast('附件已保留在原对话，未带入当前目标。');
  } catch(error) {toast(error.message);}
  finally {state.uploading--;$('attachment-input').value='';renderComposerTools();}
}

async function saveCooperation() {
  if(state.busy||state.switching||state.switchingWorkspace||state.initializing||state.savingCooperation)return;
  const settings={planning:document.querySelector('input[name="planning"]:checked').value,implementation:document.querySelector('input[name="implementation"]:checked').value};
  const epoch=state.scopeEpoch;
  state.savingCooperation=true;renderComposerTools();
  try {
    const result=await api('/api/cooperation',{goal_id:state.goalId,session_id:state.sessionId,cooperation:settings});
    if(epoch!==state.scopeEpoch)return;
    state.cooperation=result;$('cooperation-panel').classList.add('hidden');$('cooperation-toggle').setAttribute('aria-expanded','false');renderComposerTools();
  } catch(error) {toast(error.message);}
  finally {state.savingCooperation=false;renderComposerTools();}
}
async function newSession() {
  if(state.busy||state.switching||state.switchingWorkspace||state.initializing)return;
  const epoch=++state.scopeEpoch;
  let handedOff=false;
  state.switching=true; $('send').disabled=true; $('prompt').disabled=true; $('new-session').disabled=true;
  try {
    const data=await api('/api/tree',{action:'new_conversation'});
    if(epoch!==state.scopeEpoch)return;
    state.tree=data.tree;state.taskId='';state.latestJob=null;handedOff=true;await selectNode('');$('prompt').focus();
  }
  catch(e) { toast(e.message); }
  finally {if(!handedOff&&epoch===state.scopeEpoch){state.switching=false;$('send').disabled=state.busy;$('prompt').disabled=false;$('new-session').disabled=state.busy;}}
}
function renderWorkspaces() {
  const select=$('workspace-select');select.replaceChildren();
  for(const item of state.config.workspaces||[{id:'',name:state.config.workspace_name,path:state.config.workspace,available:true}]) {
    const option=node('option','',item.name);option.value=item.id;option.title=item.path;option.disabled=!item.available;select.append(option);
  }
  if(state.config.workspace_switching){const option=node('option','','＋ 打开文件夹…');option.value='__open__';select.append(option);}
  select.value=state.workspaceId;select.title=state.config.workspace;
}
async function switchWorkspace(id) {
  if(state.busy||state.initializing||state.switching||state.switchingWorkspace||state.uploading||state.savingCooperation) {
    $('workspace-select').value=state.workspaceId;return toast('请先等待当前操作结束，或停止后切换工作区。');
  }
  const fields=['workspaceId','config','tree','goalId','nodeId','focusId','sessionId','messages','base','latestJob','job','taskId','busy','cooperation','treeRenderKey'];
  const old=Object.fromEntries(fields.map(k=>[k,state[k]]));
  state.switchingWorkspace=true;state.switching=true;state.scopeEpoch++;renderComposerTools();
  try {
    const config=await api('/api/config',undefined,{workspaceId:id});
    state.workspaceId=config.workspace_id||'';state.config=config;
    Object.assign(state,{tree:null,goalId:'',nodeId:'',focusId:'',sessionId:'',messages:[],base:[],latestJob:null,job:null,taskId:''});
    renderWorkspaces();renderConnection();showMessages([]);
    await refreshTree();await selectNode(state.tree.selection.goal_id||'');
    if(config.active_id){const job=await api(`/api/tasks/${config.active_id}`);state.taskId=job.id;state.latestJob=job;setBusy(true);if(isViewedJob(job)){state.base=baseForRun(state.messages,job.prompt);renderJob(job);}poll(job.id);}
    const url=new URL(location.href);url.searchParams.set('workspace',state.workspaceId);history.replaceState(null,'',url);
  } catch(error){Object.assign(state,old);renderWorkspaces();renderConnection();showMessages(state.messages);renderTree();toast(error.message);}
  finally {state.switchingWorkspace=false;state.switching=false;renderComposerTools();}
}
async function openWorkspace(native=false) {
  if(state.switchingWorkspace)return;
  const pick=$('pick-workspace'),open=$('open-workspace-path');pick.disabled=true;open.disabled=true;
  try {
    const result=await api(native?'/api/workspaces/pick':'/api/workspaces/open',native?{}:{path:$('workspace-path').value.trim()});
    if(result.cancelled||!$('workspace-dialog').open)return;
    $('workspace-dialog').close();await switchWorkspace(result.workspace_id);
  } catch(error){toast(error.message);}
  finally {pick.disabled=false;open.disabled=false;}
}
function installControls() {
  $('workspace-select').addEventListener('change',event=>{
    if(event.target.value==='__open__'){$('workspace-select').value=state.workspaceId;$('workspace-path').value='';$('workspace-dialog').showModal();}
    else switchWorkspace(event.target.value);
  });
  $('pick-workspace').addEventListener('click',()=>openWorkspace(true));
  $('open-workspace-path').addEventListener('click',()=>openWorkspace(false));
  $('close-workspace-dialog').addEventListener('click',()=>$('workspace-dialog').close());
  $('add-attachment').addEventListener('click',()=>$('attachment-input').click());
  $('attachment-input').addEventListener('change',event=>addAttachments(event.target.files));
  $('cooperation-toggle').addEventListener('click',()=>{
    const panel=$('cooperation-panel'), opening=panel.classList.contains('hidden');
    for(const [key,value] of Object.entries(state.cooperation))document.querySelector(`input[name="${key}"][value="${value}"]`).checked=true;
    panel.classList.toggle('hidden',!opening);$('cooperation-toggle').setAttribute('aria-expanded',String(opening));
  });
  $('save-cooperation').addEventListener('click',saveCooperation);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('cooperation-panel').classList.add('hidden');$('cooperation-toggle').setAttribute('aria-expanded','false');}});
  $('send').addEventListener('click',send); $('new-session').addEventListener('click',newSession);
  $('approve').addEventListener('click',()=>decide(true)); $('deny').addEventListener('click',()=>decide(false));
  $('cancel-task').addEventListener('click',async()=>{try {await api(`/api/tasks/${state.taskId}/cancel`,{});$('cancel-task').disabled=true;}catch(e){toast(e.message);}});
  $('return-active').addEventListener('click',async()=>{const b=state.latestJob?.binding;if(b)await selectNode(b.goal_id,b.task_id,b.mode).catch(e=>toast(e.message));});
  $('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}});
}
async function init() {
  state.initializing=true; $('workspace-select').disabled=true; $('goals').inert=true; $('prompt').disabled=true; $('send').disabled=true; $('new-session').disabled=true;
  try {
    state.workspaceId=new URL(location.href).searchParams.get('workspace')||'';
    try {state.config=await api('/api/config');}
    catch(error){if(!state.workspaceId)throw error;state.workspaceId='';state.config=await api('/api/config');toast('原工作区不可访问，已恢复初始工作区。');}
    state.workspaceId=state.config.workspace_id||'';installControls(); renderConnection();renderWorkspaces();
    await refreshTree(); await selectNode(state.tree.selection.goal_id||'');
    if(state.config.active_id) { const job=await api(`/api/tasks/${state.config.active_id}`);state.taskId=job.id;state.latestJob=job;setBusy(true);if(isViewedJob(job)){state.base=[{role:'user',content:job.prompt}];renderJob(job);}poll(job.id); }
    state.initializing=false; $('prompt').disabled=state.switching; setBusy(state.busy);
  } catch(e) {$('connection').textContent='无法连接本地服务';$('send').disabled=true;toast(e.message);}
}
init();
