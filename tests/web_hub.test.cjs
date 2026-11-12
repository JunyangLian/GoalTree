const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function harness(){
  class Element {
    constructor(){this.value='';this.children=[];this.attrs={};this.handlers={};this.open=false;this.classes=new Set();this.classList={add:c=>this.classes.add(c),remove:c=>this.classes.delete(c),toggle:(c,v)=>v?this.classes.add(c):this.classes.delete(c)};}
    append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}
    setAttribute(k,v){this.attrs[k]=v;}addEventListener(k,v){this.handlers[k]=v;}
    showModal(){this.open=true;}close(){this.open=false;this.handlers.close?.();}focus(){this.focused=true;}
  }
  const nodes=new Map(),tabs=['plugins','skills','mcp'].map(id=>Object.assign(new Element(),{dataset:{hubTab:id}})),calls=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const state={workspaceId:'A',goalId:'g-a',sessionId:'s-a',csrf:'token-a',busy:false};let responder=async()=>catalog();
  const sandbox={URL,URLSearchParams,document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>tabs}};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../pico/web_assets/hub.js'),'utf8'),sandbox);
  const subject=sandbox.GoalTreeExtensions.install({scope:()=>state,api:(path,body,pin)=>{calls.push({path,body,pin});return responder(path,body,pin);}});
  return {nodes,get,tabs,state,calls,subject,library:sandbox.GoalTreeExtensions,setResponder:f=>responder=f};
}
function catalog(){return {workspace_name:'test',revision:2,plugins:[{id:'goals',title:'目标与报告',description:'整理报告',skills:['builtin:goal-planning'],enabled:true}],
  skills:[{id:'builtin:goal-planning',setting_id:'goal-planning',title:'目标规划',description:'整理目标',origin:'bundled',enabled:true,parent_enabled:true}],
  mcp:[{id:'playwright',title:'Playwright',description:'模板',status:'template',status_label:'配置模板 · 未接通',supported:false,enabled:false,transport:'stdio',license:'Apache-2.0'}]};}
const flush=()=>new Promise(r=>setImmediate(r));
test('open reads metadata only; tabs, search and template do not probe or install',async()=>{
  const h=harness();h.get('open-extensions').onclick();await flush();
  assert.equal(h.calls.length,1);assert.match(h.calls[0].path,/^\/api\/extensions\?/);assert.equal(h.calls[0].body,undefined);
  assert.equal(h.get('hub-list').children.length,1);
  h.get('hub-search').value='missing';h.get('hub-search').oninput();assert.equal(h.get('hub-count').textContent,'0 项');
  h.get('hub-search').value='';h.tabs[2].onclick();
  const card=h.get('hub-list').children[0],actions=card.children.at(-1);
  assert.equal(actions.children.length,1);assert.equal(actions.children[0].textContent,'配置说明');assert.equal(h.calls.length,1);
});
test('switches send CAS revision and pin workspace while selection changes',async()=>{
  const h=harness();h.get('open-extensions').onclick();await flush();let resolve;
  h.setResponder(()=>new Promise(r=>resolve=r));
  const toggle=h.get('hub-list').children[0].children.at(-1).children[0];toggle.onclick();
  const call=h.calls.at(-1);assert.equal(call.body.revision,2);assert.equal(call.body.enabled,false);assert.equal(call.pin.workspaceId,'A');
  h.state.workspaceId='B';h.state.csrf='token-b';resolve({...catalog(),workspace_name:'old A'});await flush();
  assert.notEqual(h.get('hub-workspace').textContent,'old A');
});
test('reopen after detail restores visible list and ignores previous scope results',async()=>{
  const h=harness();h.get('open-extensions').onclick();await flush();h.tabs[1].onclick();
  h.setResponder(async()=>({title:'Planning',content:'# Safe text'}));
  await h.get('hub-list').children[0].children.at(-1).children.at(-1).onclick();
  assert(h.get('hub-list').classes.has('hidden'));
  h.get('close-extensions').onclick();h.setResponder(async()=>catalog());h.get('open-extensions').onclick();await flush();
  assert(!h.get('hub-list').classes.has('hidden'));assert.equal(h.get('open-extensions').attrs['aria-expanded'],'true');
  let resolve;h.setResponder(()=>new Promise(r=>resolve=r));h.state.workspaceId='B';h.subject.changed();
  h.get('close-extensions').onclick();resolve({...catalog(),workspace_name:'should ignore'});await flush();
  assert.notEqual(h.get('hub-workspace').textContent,'should ignore');assert(h.get('open-extensions').focused);
});
test('busy state blocks toggles and unsafe sources never become clickable',async()=>{
  const h=harness();h.setResponder(async()=>({...catalog(),skills:[{...catalog().skills[0],source:{url:'javascript:alert(1)'}}]}));
  h.get('open-extensions').onclick();await flush();h.state.busy=true;h.tabs[1].onclick();
  const card=h.get('hub-list').children[0],button=card.children.at(-1).children[0];assert(button.disabled);
  await button.onclick();assert.equal(h.calls.length,1);
  assert(card.children.every(n=>!n.href));
});

test('enabled-only filter works and images accept only packaged paths',async()=>{
  const h=harness(),data=catalog();
  data.plugins=[{...data.plugins[0],icon_url:'/icons/goaltree.svg'},
    {...data.plugins[0],id:'optional',title:'Optional',enabled:false,icon_url:'https://outside.invalid/icon.png'}];
  h.setResponder(async()=>data);h.get('open-extensions').onclick();await flush();
  const cards=h.get('hub-list').children;
  assert.equal(cards[0].children[0].children[0].children[0].src,'/icons/goaltree.svg');
  assert.equal(cards[1].children[0].children[0].children.length,0);
  h.get('hub-enabled-only').onclick();
  assert.equal(h.get('hub-list').children.length,1);
  assert.equal(h.get('hub-enabled-only').attrs['aria-pressed'],'true');
});

test('late skill preview cannot overwrite a different tab',async()=>{
  const h=harness();h.get('open-extensions').onclick();await flush();h.tabs[1].onclick();let resolve;
  h.setResponder(()=>new Promise(r=>resolve=r));
  const pending=h.get('hub-list').children[0].children.at(-1).children.at(-1).onclick();
  h.tabs[2].onclick();resolve({title:'late',content:'old text'});await pending;
  assert(!h.get('hub-list').classes.has('hidden'));
  assert.equal(h.get('hub-list').children[0].children[0].children[1].children[0].textContent,'Playwright');
});
