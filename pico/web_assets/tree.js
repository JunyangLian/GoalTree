/* Read-only stage cards and a local relationship canvas. No network or tree writes. */
globalThis.GoalTreeView=(()=>{
  const labels={planned:'待进行',active:'进行中',review:'待验收',completed:'已完成',blocked:'已阻塞',failed:'失败',cancelled:'已取消'};
  const scopes=new Map();let current=null,installed=false,graphKey='',graphScope='',scale=1,layout=null,selected='',showDependencies=false;
  const $=id=>document.getElementById(id);
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const live=t=>t.context_status!=='archived'&&t.status!=='cancelled';
  const status=t=>t.validity==='stale'&&t.status==='completed'?'review':(t.display_status||t.status);
  const label=t=>t.context_status==='archived'?'已归档':t.validity==='stale'&&t.status==='completed'?'待复核':labels[status(t)]||status(t);
  const accepted=t=>t.status==='completed'&&t.validity==='fresh'&&status(t)==='completed';
  function index(tasks){
    const byId=new Map(tasks.map(t=>[t.id,t])),children=new Map(tasks.map(t=>[t.id,[]]));
    const roots=[];
    for(const t of tasks){if(t.parent_id&&byId.has(t.parent_id))children.get(t.parent_id).push(t);else roots.push(t);}
    return {byId,children,roots};
  }
  function leaves(task,tree,seen=new Set()){
    if(seen.has(task.id)||!live(task))return[];seen.add(task.id);
    const kids=(tree.children.get(task.id)||[]).filter(live);
    return kids.length?kids.flatMap(t=>leaves(t,tree,seen)):[task];
  }
  function progress(task,tree){const rows=leaves(task,tree);return {done:rows.filter(accepted).length,total:rows.length};}
  function signature(t){return JSON.stringify([t.title,t.description,t.acceptance,t.constraints,t.parent_id,t.depends_on,t.status,t.display_status,t.validity,t.progress_note,t.context_status,t.revision,t.spec_version,t.deliverables]);}
  function scopeState(key,tasks){
    let view=scopes.get(key);
    if(!view){view={folds:new Map(),snapshot:new Map(),changes:new Map(),initialized:false};scopes.set(key,view);if(scopes.size>32)scopes.delete(scopes.keys().next().value);}
    const now=Date.now(),snapshot=new Map(tasks.map(t=>[t.id,signature(t)]));
    if(view.initialized)for(const t of tasks)if(view.snapshot.get(t.id)!==snapshot.get(t.id))view.changes.set(t.id,{at:now,kind:view.snapshot.has(t.id)?'已调整':'新增'});
    for(const [id,change] of view.changes)if(!snapshot.has(id)||now-change.at>6000)view.changes.delete(id);
    view.snapshot=snapshot;view.initialized=true;return view;
  }
  function pill(task){return el('span','task-status '+status(task),label(task));}
  function mark(task){return el('span','task-mark '+status(task),accepted(task)?'✓':status(task)==='review'?'·':'');}
  function details(task,tree){
    const box=el('section','task-inline-detail');box.setAttribute('aria-label',task.title+'的详情');
    for(const [name,value] of [['说明',task.description],['验收条件',task.acceptance],['边界',task.constraints]])if(value){box.append(el('small','detail-label',name),el('p','',value));}
    if(task.progress_note&&!(task.status==='planned'&&task.validity==='stale'))box.append(el('small','detail-label','最近进展'),el('p','',task.progress_note));
    if(task.validity==='stale')box.append(el('p','detail-note','要求已更新，历史产出保留，需按当前条件核对。'));
    if(task.summary)box.append(el('small','detail-label','产出摘要'),el('p','',task.summary));
    if(task.deliverables?.length){
      const records=el('details','task-records'),summary=el('summary','',task.deliverables.length+' 份产出记录');records.append(summary);
      task.deliverables.slice(-4).reverse().forEach((record,i)=>records.append(el('p','detail-note',
        '产出 '+(task.deliverables.length-i)+' · '+(record.task_spec_version===task.spec_version?'当前要求':'历史要求')+
        (record.task_spec_version?' · 版本 '+record.task_spec_version:''))));
      records.append(el('p','detail-note','完整内容保留在对应对话与审计记录中。'));box.append(records);
    }
    const changes=(current?.events||[]).filter(event=>event.payload?.changes?.tasks?.[task.id]).slice(-4).reverse();
    if(changes.length){
      const records=el('details','task-records');records.append(el('summary','','最近变更'));
      const names={operator_update_task:'任务要求调整',operator_accept_tree:'阶段验收',operator_accept_task:'任务验收',agent_task_progress:'进展更新',agent_plan:'任务规划',run_finished:'运行结果',workspace_validity_check:'有效性变化',operator_reopen_task:'重新打开',operator_archive_task:'任务归档'};
      for(const event of changes){const version=event.payload.changes.tasks[task.id].revision;records.append(el('p','detail-note',
        (names[event.kind]||'任务更新')+' · 版本 '+version+(event.at?' · '+event.at.slice(0,16).replace('T',' ')+' UTC':'')));}
      box.append(records);
    }
    const dependencies=(task.depends_on||[]).map(id=>tree.byId.get(id)?.title).filter(Boolean);
    if(dependencies.length)box.append(el('p','detail-note','依赖：'+dependencies.join('、')));
    return box;
  }
  function changed(element,task,view){
    const change=view.changes.get(task.id);if(!change)return;
    element.classList.add('tree-change');const tag=el('span','tree-change-tag',change.kind);element.append(tag);
    setTimeout(()=>{element.classList.remove('tree-change');tag.remove();},Math.max(0,6000-(Date.now()-change.at)));
  }
  function render(options){
    current=options;install();const {goal,tasks,scope,focusId,onFocus,container}=options;
    const view=scopeState(scope,tasks),tree=index(tasks);container.replaceChildren();
    $('expand-task-map').disabled=!goal||tasks.length===0;
    if(!goal||!tasks.length){container.append(el('div','tree-empty',goal?'在对话中规划任务后，会在这里展开。':'选择一个目标，查看它的任务。'));if($('task-map-dialog').open)$('task-map-dialog').close();return;}
    const allLeaves=tree.roots.flatMap(t=>leaves(t,tree)),done=allLeaves.filter(accepted).length;
    const overview=el('div','tree-overview');overview.append(el('h2','',goal.title));
    const meta=el('div','tree-overview-meta');meta.append(el('span','',done+' / '+allLeaves.length+' 项任务已验收'),el('span','',tree.roots.filter(live).length+' 个阶段'));
    const track=el('div','overview-track'),fill=el('span');fill.style.width=(allLeaves.length?100*done/allLeaves.length:0)+'%';track.append(fill);overview.append(meta,track);container.append(overview);
    const firstUnfinished=tree.roots.find(t=>live(t)&&!accepted(t));
    const cards=el('div','stage-list');
    tree.roots.forEach((stage,i)=>{
      const kids=tree.children.get(stage.id)||[],s=status(stage),p=progress(stage,tree);
      const expanded=view.folds.has(stage.id)?view.folds.get(stage.id):(live(stage)&&(s==='active'||s==='review'||s==='blocked'||stage.id===firstUnfinished?.id));
      const card=el('section','stage-card '+s+(expanded?' expanded':''));if(!live(stage))card.classList.add('inactive');
      const header=el('button','stage-toggle');header.type='button';header.setAttribute('aria-expanded',String(expanded));header.setAttribute('aria-controls','stage-content-'+stage.id);header.setAttribute('aria-label',stage.title+'，'+label(stage));
      const top=el('div','stage-meta');top.append(el('span','stage-number',String(i+1).padStart(2,'0')),pill(stage),el('span','stage-chevron',expanded?'⌃':'⌄'));
      const title=el('strong','stage-title',stage.title.replace(/^阶段[一二三四五六七八九十\d]+\s*[：:、.．]\s*/,''));
      header.append(top,title);
      if(kids.length){const counts=el('div','stage-progress');counts.append(el('span','',p.done+' / '+p.total+' 项已验收'));if(s==='active')counts.append(el('span','stage-current','正在推进'));header.append(counts);}
      changed(card,stage,view);header.addEventListener('click',()=>{view.folds.set(stage.id,!expanded);render(current);});card.append(header);
      if(expanded){
        const body=el('div','stage-content');body.id='stage-content-'+stage.id;
        function taskRow(task,depth){
          const row=el('div','task-row-group');row.style.setProperty('--task-depth',Math.min(depth,3));
          const button=el('button','stage-task'+(task.id===focusId?' selected':''));button.type='button';button.setAttribute('aria-expanded',String(task.id===focusId));button.dataset.nodeId=task.id;
          const text=el('span','stage-task-text');text.append(el('strong','',task.title),el('small','',label(task)));
          button.append(mark(task),text,el('span','task-detail-arrow','›'));changed(row,task,view);
          button.addEventListener('click',()=>onFocus(task.id===focusId?'':task.id));row.append(button);
          if(task.id===focusId)row.append(details(task,tree));body.append(row);
          for(const child of tree.children.get(task.id)||[])taskRow(child,depth+1);
        }
        if(kids.length)kids.forEach(t=>taskRow(t,0));
        else {const inspect=el('button','stage-inspect','查看任务详情');inspect.type='button';inspect.addEventListener('click',()=>onFocus(stage.id===focusId?'':stage.id));body.append(inspect);}
        if(stage.id===focusId)body.prepend(details(stage,tree));
        card.append(body);
      }
      cards.append(card);
    });container.append(cards);
    if($('task-map-dialog').open){
      if(graphScope!==scope){$('task-map-dialog').close();graphKey='';}
      else renderGraph(false);
    }
  }
  // A deterministic parent/child layout. Dependencies never change placement.
  function computeLayout(goal,tasks){
    const tree=index(tasks),positions=[],edges=[],width=218,height=84,gap=28,column=282;let cursor=24;
    const visiting=new Set();
    function place(task,depth){
      if(visiting.has(task.id))return null;visiting.add(task.id);
      const kids=(tree.children.get(task.id)||[]).map(child=>place(child,depth+1)).filter(Boolean);
      let y;if(kids.length)y=(kids[0].y+kids.at(-1).y)/2;else{y=cursor;cursor+=height+gap;}
      const pos={task,x:28+depth*column,y,width,height};positions.push(pos);
      for(const child of kids)edges.push({from:pos,to:child});return pos;
    }
    const roots=tree.roots.map(t=>place(t,1)).filter(Boolean),root={task:{...goal,id:'__goal__',status:'goal'},x:28,y:roots.length?(roots[0].y+roots.at(-1).y)/2:24,width,height};
    positions.push(root);roots.forEach(to=>edges.push({from:root,to}));
    return {positions,edges,width:Math.max(...positions.map(p=>p.x+p.width))+76,height:Math.max(cursor,...positions.map(p=>p.y+p.height))+24,tree};
  }
  function svgPath(from,to,dependency=false){
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');const x1=from.x+from.width,y1=from.y+from.height/2,x2=dependency?to.x+to.width:to.x,y2=to.y+to.height/2;
    const bend=dependency?Math.max(x1,x2)+45:(x1+x2)/2;
    path.setAttribute('d',`M ${x1} ${y1} C ${bend} ${y1}, ${bend} ${y2}, ${x2} ${y2}`);path.setAttribute('class',dependency?'map-dependency':'map-edge');
    if(dependency)path.setAttribute('marker-end','url(#map-dependency-arrow)');return path;
  }
  function graphDetails(){
    const task=layout?.tree.byId.get(selected),panel=$('map-detail');panel.replaceChildren();panel.classList.toggle('hidden',!task);
    for(const button of $('map-stage').querySelectorAll('.map-node'))button.classList.toggle('selected',button.dataset.nodeId===selected);
    if(task)panel.append(el('h3','',task.title),pill(task),details(task,layout.tree));
    if(layout)setScale(scale);
  }
  function setScale(next,fit=false){
    if(!layout)return;const viewport=$('map-viewport'),old=scale;scale=Math.max(.15,Math.min(1.6,next));
    const stage=$('map-stage'),oldLeft=parseFloat(stage.style.left)||16,oldTop=parseFloat(stage.style.top)||16;
    const cx=(viewport.scrollLeft+viewport.clientWidth/2-oldLeft)/old,cy=(viewport.scrollTop+viewport.clientHeight/2-oldTop)/old;
    const left=Math.max(16,(viewport.clientWidth-layout.width*scale)/2),top=Math.max(16,(viewport.clientHeight-layout.height*scale)/2);
    $('map-space').style.width=(layout.width*scale+32)+'px';$('map-space').style.height=(layout.height*scale+32)+'px';stage.style.transform=`scale(${scale})`;stage.style.left=left+'px';stage.style.top=top+'px';
    $('map-zoom-level').textContent=Math.round(scale*100)+'%';
    viewport.scrollLeft=fit?0:cx*scale+left-viewport.clientWidth/2;viewport.scrollTop=fit?0:cy*scale+top-viewport.clientHeight/2;
  }
  function fitGraph(){if(layout){const viewport=$('map-viewport');setScale(Math.min(1,(viewport.clientWidth-32)/layout.width,(viewport.clientHeight-32)/layout.height),true);}}
  function renderGraph(force){
    if(!current?.goal)return;
    const key=JSON.stringify([current.scope,current.goal.title,current.tasks.map(t=>[t.id,signature(t)])]);
    if(force||key!==graphKey){
      const viewport=$('map-viewport'),left=viewport.scrollLeft,top=viewport.scrollTop;graphKey=key;
      layout=computeLayout(current.goal,current.tasks);const stage=$('map-stage');stage.replaceChildren();stage.style.width=layout.width+'px';stage.style.height=layout.height+'px';
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('width',layout.width);svg.setAttribute('height',layout.height);svg.setAttribute('aria-hidden','true');svg.classList.add('map-lines');
      const defs=document.createElementNS('http://www.w3.org/2000/svg','defs'),marker=document.createElementNS('http://www.w3.org/2000/svg','marker'),arrow=document.createElementNS('http://www.w3.org/2000/svg','path');
      for(const [name,value] of Object.entries({id:'map-dependency-arrow',viewBox:'0 0 7 7',refX:'6',refY:'3.5',markerWidth:'6',markerHeight:'6',orient:'auto'}))marker.setAttribute(name,value);
      arrow.setAttribute('d','M 0 0 L 7 3.5 L 0 7 Z');arrow.setAttribute('fill','#b59663');marker.append(arrow);defs.append(marker);svg.append(defs);
      layout.edges.forEach(e=>svg.append(svgPath(e.from,e.to)));
      const byId=new Map(layout.positions.map(p=>[p.task.id,p]));
      for(const task of current.tasks)for(const id of task.depends_on||[])if(byId.has(id)&&byId.has(task.id))svg.append(svgPath(byId.get(id),byId.get(task.id),true));
      stage.append(svg);
      for(const pos of layout.positions){
        const task=pos.task,isGoal=task.id==='__goal__',card=el('button','map-node '+(isGoal?'goal-root':status(task)));
        card.type='button';card.dataset.nodeId=task.id;card.style.left=pos.x+'px';card.style.top=pos.y+'px';card.style.width=pos.width+'px';card.style.height=pos.height+'px';
        card.append(el('span','map-node-label',isGoal?'目标':label(task)),el('strong','',task.title));card.title=task.title;card.setAttribute('aria-label',task.title+(isGoal?'，目标':'，'+label(task)));
        card.addEventListener('click',()=>{selected=isGoal?'':task.id;if(!isGoal)current.onFocus(task.id);graphDetails();});stage.append(card);
      }
      stage.classList.toggle('show-dependencies',showDependencies);setScale(scale);viewport.scrollLeft=left;viewport.scrollTop=top;
    }
    graphDetails();
  }
  function install(){
    if(installed)return;installed=true;
    $('expand-task-map').addEventListener('click',()=>{if(!current?.goal)return;graphScope=current.scope;selected=current.focusId;graphKey='';$('task-map-dialog').showModal();renderGraph(true);fitGraph();});
    $('close-task-map').addEventListener('click',()=>$('task-map-dialog').close());
    $('task-map-dialog').addEventListener('close',()=>{graphKey='';layout=null;$('map-stage').replaceChildren();});
    $('map-zoom-in').addEventListener('click',()=>setScale(scale*1.2));$('map-zoom-out').addEventListener('click',()=>setScale(scale/1.2));$('map-fit').addEventListener('click',fitGraph);
    $('map-dependencies').addEventListener('change',event=>{showDependencies=event.target.checked;$('map-stage').classList.toggle('show-dependencies',showDependencies);});
    const viewport=$('map-viewport');let drag=null;
    viewport.addEventListener('pointerdown',event=>{if(event.button!==0||event.target.closest('button,input'))return;drag={x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(event.pointerId);viewport.classList.add('dragging');});
    viewport.addEventListener('pointermove',event=>{if(drag){viewport.scrollLeft=drag.left-event.clientX+drag.x;viewport.scrollTop=drag.top-event.clientY+drag.y;}});
    const end=()=>{drag=null;viewport.classList.remove('dragging');};viewport.addEventListener('pointerup',end);viewport.addEventListener('pointercancel',end);
  }
  return {render,install,index,leaves,progress,computeLayout,status,label};
})();
