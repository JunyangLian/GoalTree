const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
function harness(){
  class Element{
    constructor(tag){this.tagName=tag;this.children=[];this.classList={add(){},remove(){},toggle(){}};}
    append(...items){this.children.push(...items);}
    cloneNode(){return new Element(this.tagName);}
    addEventListener(name,fn){this[name]=fn;}
    replaceChildren(...items){this.children=items;}
  }
  const elements=new Map(),sandbox={document:{getElementById(id){if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);},createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment')},setTimeout(){},clearTimeout(){}};
  vm.createContext(sandbox);
  const assets=path.join(__dirname,'../pico/web_assets');
  for(const file of ['markdown-it.min.js','markdown.js'])vm.runInContext(fs.readFileSync(path.join(assets,file),'utf8'),sandbox);
  const source=fs.readFileSync(path.join(assets,'app.js'),'utf8');
  vm.runInContext(source.slice(0,source.lastIndexOf('init();'))+'\nglobalThis.subject={describe:GoalTreeProgress.describe,renderApproval,activityCard,showMessages,renderJob,pendingAnswer,modelErrorText,taskStatusLabel,state};',sandbox);
  return {subject:sandbox.subject,elements};
}

test('read-only source check shows visible progress without displaying the model review packet',()=>{
  const {subject}=harness();
  const status=subject.describe({status:'running',events:[{event:'model_requested',purpose:'shared_reference_read'},{event:'model_progress',phase:'reference_check'}]});
  assert.equal(status.label,'正在核对记忆来源');
  assert.equal(status.done,false);
});

test('privacy denial with answer-only backend error is localized and not rendered as success',()=>{
  const {subject}=harness();
  const job={status:'failed',stop_reason:'privacy_denied',error:'',answer:'Model request failed: Privacy policy blocked operation: core_external_forbidden',events:[{event:'model_requested'}]};
  assert.match(subject.modelErrorText(job),/隐私策略已阻止外发/);
  assert.doesNotMatch(subject.modelErrorText(job),/Model request failed|core_external_forbidden/);
  const progress=subject.describe(job);
  assert.equal(progress.label,'隐私策略已阻止外发');
  assert.doesNotMatch(JSON.stringify(progress),/已发送模型请求|已完成|验收通过/);
});

test('server redacted prompt replaces optimistic local echo after privacy denial',()=>{
  const {subject}=harness();
  subject.state.base=[{role:'user',content:'secret GT_CORE_20261111BROWSERABCDEF'}];
  const job={id:'privacy',status:'failed',prompt:'secret <redacted>',stop_reason:'privacy_denied',error:'',answer:'Model request failed: Privacy policy blocked operation: core_external_forbidden',events:[]};
  subject.renderJob(job);
  assert.equal(subject.state.messages[0].content,'secret <redacted>');
  assert.equal(subject.state.messages[1].error,true);
  assert.match(subject.state.messages[1].content,/隐私策略已阻止外发/);
  assert.doesNotMatch(JSON.stringify(subject.state.messages),/GT_CORE_/);
});
test('long first response has elapsed time and no fabricated intermediate accomplishments',()=>{
  const {subject}=harness();const result=subject.describe({status:'running',elapsed_ms:53000,events:[{event:'model_requested'}]});
  assert.equal(result.elapsedSeconds,53);assert.equal(result.label,'等待模型响应');
  assert.doesNotMatch(JSON.stringify(result),/已保存|已完成|正在读取/);
});

test('stream recovery replaces interrupted draft without duplicating text or mixing child runs',()=>{
  const {subject}=harness();
  const events=[{event:'model_requested'},{event:'model_delta',text:'Interrupted draft'},
    {event:'model_transport',ok:false,will_retry:true,stream_reset:true},
    {event:'model_delta',text:'Child private draft',delegated_run_id:'child'},
    {event:'model_delta',text:'Recovered '},{event:'model_delta',text:'answer'}];
  assert.equal(subject.pendingAnswer(events),'Recovered answer');
  assert.match(subject.describe({status:'running',events:events.slice(0,3)}).label,/正在重试/);
  assert.match(subject.modelErrorText({error:'Incomplete SSE response; no tool call accepted'}),/残缺工具调用未执行/);
});

test('stale requirement does not hide actual not-started or child-derived progress',()=>{
  const {subject}=harness();
  assert.equal(subject.taskStatusLabel({status:'planned',validity:'stale'}),'待进行');
  assert.equal(subject.taskStatusLabel({status:'planned',display_status:'active',validity:'stale'}),'进行中');
  assert.equal(subject.taskStatusLabel({status:'review',validity:'unknown'}),'待验收');
  assert.equal(subject.taskStatusLabel({status:'completed',validity:'stale'}),'待复核');
  assert.equal(subject.taskStatusLabel({status:'completed',validity:'fresh'}),'已完成');
});

test('stage approval shows actual host supplied child titles and acceptance criteria',()=>{
  const {subject,elements}=harness();
  subject.renderApproval({name:'confirm_task',args:{task_id:'p',include_children:1,reason:'核对产出',
    tasks:[{id:'a',title:'需求说明',revision:3,status:'review',acceptance:'范围准确',progress_note:'有依据'},
           {id:'p',title:'阶段一',revision:2,status:'planned',acceptance:'子项验收'}]}});
  const html=elements.get('approval-summary').innerHTML;
  assert.match(html,/需求说明/);assert.match(html,/阶段一/);assert.match(html,/范围准确/);
  assert.match(html,/版本：3/);assert.match(html,/待验收/);
});
test('analysis heartbeat shows status only; unfinished arguments are never marked executed',()=>{
  const {subject}=harness();
  const result=subject.describe({status:'running',events:[{event:'model_progress',phase:'analysis',reasoning_content:'PRIVATE_REASON'},{event:'model_progress',phase:'tool_arguments',arguments:'PRIVATE_PARAMETERS'}]});
  assert.equal(result.phase,'tool_arguments');assert.match(result.detail,/尚未执行/);
  assert.doesNotMatch(JSON.stringify(result),/PRIVATE/);
});
test('failed child operations, approvals and terminal state remain distinct',()=>{
  const {subject}=harness();
  const events=[{event:'tool_executed',name:'read_file',tool_status:'error',delegated_run_id:'child'}];
  assert.match(subject.describe({status:'running',events}).label,/子任务.*未完成/);
  assert.equal(subject.describe({status:'waiting_approval',events}).phase,'approval');
  const finished=subject.describe({status:'completed',events});
  assert.equal(finished.label,'本轮回复已结束');assert.match(finished.detail,/未完成操作/);
  assert.doesNotMatch(finished.label,/验收通过|目标完成/);
});

test('multi-source search displays the actual providers including partial failures',()=>{
  const {subject}=harness();const result=subject.describe({status:'running',events:[{
    event:'tool_executed',name:'web_search',tool_status:'partial_success',search_status:'partial',
    search_sources:[{source:'bing',status:'error',count:0},{source:'baidu',status:'ok',count:3}]
  }]});
  assert.equal(result.label,'搜索来源：Bing不可用 · 百度 3条');
  assert.match(result.detail,/部分来源/);
});

test('snapshot reuse and web budget stops are shown only from actual tool metadata',()=>{
  const {subject}=harness();
  const hit=subject.describe({status:'running',events:[{event:'tool_executed',name:'web_find',tool_status:'ok',web_cache:'snapshot',web_citation:'page:L2-L3'}]});
  assert.match(JSON.stringify(hit.entries),/未重新联网/);
  const stopped=subject.describe({status:'running',events:[{event:'tool_executed',name:'web_search',tool_status:'error',web_budget_stop:true}]});
  assert.match(JSON.stringify(stopped.entries),/预算已用尽/);
});
test('activity folds after completion and user fold choice survives rerender',()=>{
  const {subject}=harness(),job={id:'a',status:'running',elapsed_ms:1000,events:[]};
  let card=subject.activityCard(job);assert.equal(card.open,true);
  card.children[0].click();
  assert.equal(subject.activityCard(job).open,false);
  assert.equal(subject.activityCard({...job,id:'b',status:'completed'}).open,false);
});
test('confirmation prose renders safe headings, bold text and lists; raw JSON is literal',()=>{
  const {subject,elements}=harness();
  const approval={name:'goal_action',kind:'planning_step',args:{proposal:JSON.stringify({action:'create_goal',title:'研究计划',description:'## 研究范围\n\n**只做鱼类组学**\n\n- 需求\n- 工具\n\n<script>alert(1)</script>'})}};
  subject.renderApproval(approval);
  const html=elements.get('approval-summary').innerHTML;
  assert.match(html,/<h3>研究计划<\/h3>/);assert.match(html,/<h2>研究范围<\/h2>/);
  assert.match(html,/<strong>只做鱼类组学<\/strong>/);assert.match(html,/<ul>/);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
  assert.equal(elements.get('approval-args').textContent,JSON.stringify(approval.args,null,2));
  assert.equal(elements.get('approval-args').innerHTML,undefined);
  assert.equal(elements.get('approve').textContent,'确认规划');
});
test('step and stage approval variants render descriptions rather than encoded JSON',()=>{
  const {subject,elements}=harness();
  subject.renderApproval({name:'plan_tasks',kind:'planning_step',args:{step:2,total:3,task:{title:'第二项',description:'**需要评审**',acceptance:'- 检查来源'}}});
  assert.match(elements.get('approval-summary').innerHTML,/<strong>需要评审<\/strong>/);
  assert.match(elements.get('approval-summary').innerHTML,/步骤 2 \/ 3/);
  subject.renderApproval({name:'plan_tasks',kind:'planning_direction',args:{stages:['阶段一'],proposal:[{title:'阶段一',description:'**研究计划**'}]}});
  assert.match(elements.get('approval-summary').innerHTML,/<strong>研究计划<\/strong>/);
});

test('writing Markdown previews its actual content while source code and patches stay literal',()=>{
  const {subject,elements}=harness();
  subject.renderApproval({name:'write_file',kind:'safety',args:{path:'docs/plan.md',content:'# 项目方案\n\n**关键决定**\n\n- 研究范围'}});
  assert.match(elements.get('approval-summary').innerHTML,/<h1>项目方案<\/h1>/);
  assert.match(elements.get('approval-summary').innerHTML,/<strong>关键决定<\/strong>/);
  assert.equal(elements.get('approve').textContent,'允许执行');
  subject.renderApproval({name:'write_file',args:{path:'code.py',content:'print("<script>")'}});
  assert.match(elements.get('approval-summary').innerHTML,/<pre><code>print\(&quot;&lt;script&gt;/);
  subject.renderApproval({name:'patch_file',args:{path:'file.md',old_text:'## Old',new_text:'## New'}});
  assert.match(elements.get('approval-summary').innerHTML,/<code>## Old/);
  assert.match(elements.get('approval-summary').innerHTML,/<code>## New/);
});

test('long approval previews disclose truncation and preserve full raw parameters',()=>{
  const {subject,elements}=harness(),text='x'.repeat(13000);
  subject.renderApproval({name:'write_file',args:{path:'large.md',content:text}});
  assert.match(elements.get('approval-summary').innerHTML,/预览已截断/);
  assert.equal(JSON.parse(elements.get('approval-args').textContent).content.length,13000);
});


test('search keyword warning is evidence-derived and visible',()=>{
  const {subject}=harness();
  const result=subject.describe({status:'running',events:[{event:'tool_executed',name:'web_search',tool_status:'ok',search_status:'ok',search_sources:[{source:'bing',status:'ok',count:3,low_query_overlap:true},{source:'baidu',status:'ok',count:3,low_query_overlap:false}]}]});
  assert.match(result.label,/Bing 3条（关键词匹配不足）/);
  assert.match(result.label,/百度 3条/);
});


test('Baidu verification and actual WeChat index are named separately',()=>{
 const {subject}=harness();
 const result=subject.describe({status:'running',events:[{event:'tool_executed',name:'web_search',tool_status:'partial_success',search_status:'partial',search_sources:[{source:'baidu',status:'error',failure_type:'verification_required',count:0},{source:'wechat',status:'ok',count:2}]}]});
 assert.equal(result.label,'搜索来源：百度需验证 · 微信·搜狗索引 2条');
});

test('Search MCP shows its real provider instead of a legacy engine label',()=>{
 const {subject}=harness();
 const result=subject.describe({status:'running',events:[{event:'tool_executed',name:'web_search',tool_status:'ok',search_status:'ok',search_sources:[{source:'exa_mcp',status:'ok',count:3}]}]});
 assert.equal(result.label,'搜索来源：Exa Search MCP 3条');
});
