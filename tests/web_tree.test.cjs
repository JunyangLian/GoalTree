const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function harness(){
  class Element{
    constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.attrs={};this.className='';this.style={setProperty(k,v){this[k]=v;}};this.scrollLeft=this.scrollTop=0;this.clientWidth=1000;this.clientHeight=650;this.open=false;this.events={};
      this.classList={add:(...names)=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),...names])].join(' ');},remove:(...names)=>{this.className=this.className.split(' ').filter(n=>!names.includes(n)).join(' ');},toggle:(name,force)=>{const has=this.className.split(' ').includes(name);if(force??!has)this.classList.add(name);else this.classList.remove(name);}};
    }
    append(...items){for(const n of items){n.parent=this;this.children.push(n);}}
    prepend(...items){for(const n of items)n.parent=this;this.children.unshift(...items);}
    replaceChildren(...items){this.children=[];this.append(...items);}
    setAttribute(k,v){this.attrs[k]=String(v);}
    addEventListener(k,fn){this.events[k]=fn;}
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
    querySelectorAll(selector){const cls=selector.slice(1),out=[];for(const n of this.children){if(n.className.split(' ').includes(cls))out.push(n);out.push(...n.querySelectorAll(selector));}return out;}
    showModal(){this.open=true;}close(){this.open=false;this.events.close?.();}
    setPointerCapture(){}
  }
  const elements=new Map(),timers=[];const sandbox={document:{getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag)},setTimeout(fn){timers.push(fn);},fetch(){throw Error('Read-only visualization must not send requests');}};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../pico/web_assets/tree.js'),'utf8'),sandbox);
  return {view:sandbox.GoalTreeView,elements,timers,Element};
}
function node(id,parent,status='planned',extra={}){return {id,parent_id:parent,title:id,status,validity:status==='completed'?'fresh':'unknown',depends_on:[],...extra};}
function options(h,tasks,scope='folder-a|goal-a'){return {goal:{id:'goal-a',title:'报告目标'},tasks,scope,focusId:'',container:new h.Element(),onFocus(){}};}

test('completed stages collapse, simultaneous active stages expand and leaf counts stay accurate',()=>{
  const h=harness(),tasks=[node('done','','completed'),node('done-leaf','done','completed'),node('one','','active'),node('one-leaf','one','review'),node('two','','active'),node('two-leaf','two'),node('archived','one','completed',{context_status:'archived'})],before=JSON.stringify(tasks),o=options(h,tasks);
  h.view.render(o);
  const headers=o.container.querySelectorAll('.stage-toggle');
  assert.deepEqual(headers.map(n=>n.attrs['aria-expanded']),['false','true','true']);
  const tree=h.view.index(tasks);assert.equal(h.view.progress(tasks[0],tree).done,1);
  assert.equal(h.view.progress(tasks[2],tree).total,1);
  assert.equal(JSON.stringify(tasks),before);
});

test('manual expansion survives polling and does not cross workspace boundaries',()=>{
  const h=harness(),tasks=[node('stage','','completed'),node('child','stage','completed')],o=options(h,tasks);
  h.view.render(o);o.container.querySelectorAll('.stage-toggle')[0].events.click();
  assert.equal(o.container.querySelectorAll('.stage-toggle')[0].attrs['aria-expanded'],'true');
  h.view.render(o);assert.equal(o.container.querySelectorAll('.stage-toggle')[0].attrs['aria-expanded'],'true');
  const other=options(h,tasks,'folder-b|goal-a');h.view.render(other);
  assert.equal(other.container.querySelectorAll('.stage-toggle')[0].attrs['aria-expanded'],'false');
});

test('only actual task changes highlight; initial load and unchanged polling do not',()=>{
  const h=harness(),tasks=[node('stage',''),node('child','stage')],o=options(h,tasks);
  h.view.render(o);assert.equal(o.container.querySelectorAll('.tree-change').length,0);
  h.view.render(o);assert.equal(o.container.querySelectorAll('.tree-change').length,0);
  o.tasks=[...tasks,node('new-child','stage')];h.view.render(o);
  assert.equal(o.container.querySelectorAll('.tree-change-tag')[0].textContent,'新增');
  for(const timer of h.timers)timer();assert.equal(o.container.querySelectorAll('.tree-change').length,0);
});

test('graph layout preserves hierarchy, keeps same-depth nodes apart and ignores dependency order for placement',()=>{
  const h=harness(),tasks=[node('s1',''),node('a','s1'),node('b','s1'),node('s2',''),node('c','s2')];
  const a=h.view.computeLayout({title:'Goal'},tasks),b=h.view.computeLayout({title:'Goal'},tasks.map(t=>({...t,depends_on:t.id==='c'?['a']:[]})));
  assert.equal(a.positions.length,6);assert.equal(a.edges.length,5);
  assert.equal(JSON.stringify(a.positions.map(p=>[p.task.id,p.x,p.y])),JSON.stringify(b.positions.map(p=>[p.task.id,p.x,p.y])));
  for(const p of a.positions)for(const q of a.positions)if(p!==q&&p.x===q.x)assert.ok(Math.abs(p.y-q.y)>=84);
  assert.ok(a.edges.every(e=>e.to.x>e.from.x));
});

test('canvas controls and node inspection are read-only, scoped, and labels are literal text',()=>{
  const h=harness(),title='<img src=x onerror=alert(1)>',tasks=[node('s',''),node('leaf','s','review',{title,description:'<script>unsafe</script>'})],before=JSON.stringify(tasks),o=options(h,tasks);
  let focus;o.onFocus=id=>{focus=id;};h.view.render(o);
  h.elements.get('expand-task-map').events.click();assert.ok(h.elements.get('task-map-dialog').open);
  const buttons=h.elements.get('map-stage').querySelectorAll('.map-node');
  const leaf=buttons.find(n=>n.dataset.nodeId==='leaf');
  assert.equal(leaf.children[1].textContent,title);assert.equal(leaf.children[1].innerHTML,undefined);
  leaf.events.click();assert.equal(focus,'leaf');
  h.elements.get('map-zoom-in').events.click();assert.match(h.elements.get('map-zoom-level').textContent,/%$/);
  h.view.render(options(h,tasks,'folder-b|goal-a'));assert.equal(h.elements.get('task-map-dialog').open,false);
  assert.equal(JSON.stringify(tasks),before);
});

test('a 500-task wide plan has one position per node and no duplicate hierarchy edges',()=>{
  const h=harness(),tasks=Array.from({length:500},(_,i)=>node('task-'+i,''));
  const graph=h.view.computeLayout({title:'Goal'},tasks);
  assert.equal(graph.positions.length,501);assert.equal(graph.edges.length,500);
});

test('details keep historical outputs and only show the selected task change records as text',()=>{
  const h=harness(),tasks=[node('s',''),node('leaf','s','planned',{spec_version:4,deliverables:[{task_spec_version:2}]})],o=options(h,tasks);
  o.focusId='leaf';o.events=[{kind:'operator_update_task',at:'2026-11-10T08:00:00Z',payload:{changes:{tasks:{leaf:{revision:5}}}}},
    {kind:'operator_accept_task',payload:{changes:{tasks:{other:{revision:99}}}}}];
  h.view.render(o);const records=o.container.querySelectorAll('.task-records');
  assert.equal(records.length,2);
  assert.match(records[0].children.map(n=>n.textContent||'').join(' '),/历史要求.*版本 2/);
  const history=records[1].children.map(n=>n.textContent||'').join(' ');
  assert.match(history,/任务要求调整 · 版本 5/);assert.doesNotMatch(history,/99/);
});
