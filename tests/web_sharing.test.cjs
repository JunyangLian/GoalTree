const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function harness(){
  class Element{
    constructor(){this.children=[];this.value='';this.disabled=false;this.classList={add(){},remove(){},toggle(){}};}
    cloneNode(){return new Element();}
    append(...x){this.children.push(...x);}replaceChildren(...x){this.children=x;}setAttribute(k,v){this[k]=v;}querySelectorAll(){return[];}addEventListener(){}
  }
  const elements=new Map(),calls=[],notices=[];
  const sandbox={calls,notices,setTimeout(){},clearTimeout(){},document:{getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},createDocumentFragment(){return new Element();}}};
  vm.createContext(sandbox);
  const source=fs.readFileSync(path.join(__dirname,'../pico/web_assets/app.js'),'utf8');
  vm.runInContext(source.slice(0,source.lastIndexOf('init();'))+`
    state.config={configured:true,attachments:{},workspace_switching:true};state.workspaceId='ws-a';state.sessionId='session-a';state.goalId='goal-a';
    toast=message=>notices.push(message);api=async(path,body)=>{calls.push({path,body});return {enabled:body.enabled,revision:body.revision+1};};
    globalThis.subject={state,toggleMemorySharing,renderComposerTools,send,newSession};
  `,sandbox);
  return{subject:sandbox.subject,sandbox,elements,calls,notices};
}
test('sharing is off by default and toggles using an explicit owner and revision',async()=>{
  const h=harness();h.subject.renderComposerTools();assert.equal(h.elements.get('memory-sharing-toggle')['aria-pressed'],'false');
  await h.subject.toggleMemorySharing();
  assert.equal(h.calls[0].path,'/api/memory-sharing');assert.equal(h.calls[0].body.goal_id,'goal-a');
  assert.equal(h.calls[0].body.session_id,'session-a');assert.equal(h.calls[0].body.revision,0);assert.equal(h.calls[0].body.enabled,true);
  assert.equal(h.elements.get('memory-sharing-toggle')['aria-pressed'],'true');
  await h.subject.toggleMemorySharing();assert.equal(h.calls[1].body.revision,1);assert.equal(h.subject.state.memorySharing.enabled,false);
});
test('a failed save never shows sharing as enabled',async()=>{
  const h=harness();vm.runInContext(`api=async()=>{throw new Error('revision conflict');};`,h.sandbox);
  await h.subject.toggleMemorySharing();assert.equal(h.subject.state.memorySharing.enabled,false);assert.match(h.notices[0],/conflict/);
});
test('pending saves lock sending and cannot overwrite another conversation selection',async()=>{
  const h=harness();vm.runInContext(`api=async(path,body)=>{calls.push({path,body});return new Promise(resolve=>globalThis.resolveSharing=resolve);};`,h.sandbox);
  const pending=h.subject.toggleMemorySharing();
  assert.equal(h.subject.state.savingSharing,true);assert.equal(h.elements.get('send').disabled,true);
  await h.subject.send();assert.equal(h.calls.length,1);
  h.subject.state.scopeEpoch++;h.subject.state.sessionId='session-b';h.subject.state.memorySharing={enabled:false,revision:0};
  h.sandbox.resolveSharing({enabled:true,revision:1});await pending;
  assert.equal(h.subject.state.memorySharing.enabled,false);assert.equal(h.subject.state.savingSharing,false);
});
test('running tasks cannot change the sharing consent',async()=>{
  const h=harness();h.subject.state.busy=true;await h.subject.toggleMemorySharing();assert.equal(h.calls.length,0);
});
test('creating a conversation locks navigation immediately and unlocks on failure',async()=>{
  const h=harness();vm.runInContext(`api=async()=>new Promise((resolve,reject)=>globalThis.failCreate=reject);`,h.sandbox);
  const pending=h.subject.newSession();
  assert.equal(h.elements.get('goals').inert,true);assert.equal(h.elements.get('memory-sharing-toggle').disabled,true);
  h.sandbox.failCreate(new Error('fixture create failed'));await pending;
  assert.equal(h.elements.get('goals').inert,false);assert.equal(h.subject.state.switching,false);
});
test('the visible control explains both opt-in scope and non-retroactive withdrawal',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../pico/web_assets/index.html'),'utf8');
  assert.match(html,/id="memory-sharing-toggle"[^>]*aria-pressed="false"/);
  assert.match(html,/其他已开启的对话/);assert.match(html,/既有产出保留/);
});
