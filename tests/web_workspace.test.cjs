const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function harness(){
  class Element {
    constructor(){this.value='';this.children=[];this.classList={add(){},remove(){},toggle(){}};}
    cloneNode(){return new Element();}append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}
    addEventListener(){}setAttribute(key,value){this[key]=value;}querySelectorAll(){return[];}
  }
  const elements=new Map(),calls=[],notices=[],historyCalls=[];
  const sandbox={URL,location:{href:'http://127.0.0.1:8773/'},history:{replaceState(...args){historyCalls.push(args);}},
    document:{getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:()=>new Element(),createDocumentFragment:()=>new Element()},
    setTimeout(){},clearTimeout(){},calls,notices,fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({ok:true})};}};
  vm.createContext(sandbox);const source=fs.readFileSync(path.join(__dirname,'../pico/web_assets/app.js'),'utf8');
  vm.runInContext(source.slice(0,source.lastIndexOf('init();'))+`
    state.workspaceId='ws-a';state.sessionId='session-a';state.goalId='goal-a';
    state.config={workspace_id:'ws-a',workspace:'A',workspace_name:'A',csrf:'token-a',workspace_switching:true,attachments:{}};
    toast=message=>notices.push(message);showMessages=()=>{};renderTree=()=>{};
    globalThis.subject={state,api,switchWorkspace,draftKey};
  `,sandbox);
  return {sandbox,subject:sandbox.subject,elements,calls,notices,historyCalls};
}
test('requests pin the selected workspace and CSRF token even if selection changes in flight',async()=>{
  const h=harness(),pending=h.subject.api('/api/tree',{action:'select'});
  h.subject.state.workspaceId='ws-b';h.subject.state.config.csrf='token-b';await pending;
  assert.equal(h.calls[0].options.headers['X-GoalTree-Workspace'],'ws-a');
  assert.equal(h.calls[0].options.headers['X-Pico-Token'],'token-a');
  await h.subject.api('/api/tree',undefined,{workspaceId:'ws-a'});
  assert.equal(h.calls[1].options.headers['X-GoalTree-Workspace'],'ws-a');
});
test('workspace switching binds fresh context and updates reload location without copying old chat',async()=>{
  const h=harness();
  h.subject.state.messages=[{role:'assistant',content:'BIOLOGY_ONLY'}];
  vm.runInContext(`api=async(path,data)=>{
    if(path==='/api/config')return {workspace_id:'ws-b',workspace:'B',workspace_name:'B',csrf:'token-b',workspace_switching:true,attachments:{},active_id:null};
    const tree={revision:1,selection:{goal_id:''},tasks:{},goals:{}};
    if(path.startsWith('/api/tree/context'))return {session_id:'session-b',messages:[],node:{title:'新对话'},cooperation:{planning:'balanced',implementation:'balanced'}};
    return data?{tree}:tree;
  };`,h.sandbox);
  await h.subject.switchWorkspace('ws-b');
  assert.equal(h.subject.state.workspaceId,'ws-b');assert.equal(h.subject.state.sessionId,'session-b');
  assert.equal(h.subject.state.messages.length,0);assert.equal(h.subject.state.config.csrf,'token-b');
  assert.match(String(h.historyCalls[0][2]),/workspace=ws-b/);
});
test('cannot retarget a live job and failed selection restores the prior context',async()=>{
  const h=harness();h.subject.state.busy=true;await h.subject.switchWorkspace('ws-b');
  assert.equal(h.subject.state.workspaceId,'ws-a');assert.equal(h.calls.length,0);
  h.subject.state.busy=false;
  vm.runInContext(`api=async()=>{throw new Error('Folder unavailable');};`,h.sandbox);
  await h.subject.switchWorkspace('ws-b');
  assert.equal(h.subject.state.workspaceId,'ws-a');assert.equal(h.subject.state.sessionId,'session-a');
  assert.equal(h.subject.state.switchingWorkspace,false);assert.equal(h.subject.state.switching,false);
});
test('attachment drafts are namespaced even when two folders contain copied session IDs',()=>{
  const h=harness();
  assert.notEqual(h.subject.draftKey('same-session','ws-a'),h.subject.draftKey('same-session','ws-b'));
});

test('initial loading prevents folder retargeting before the first context has arrived',async()=>{
  const h=harness();h.subject.state.initializing=true;
  await h.subject.switchWorkspace('ws-b');
  assert.equal(h.calls.length,0);assert.equal(h.subject.state.workspaceId,'ws-a');
  assert.equal(h.subject.state.sessionId,'session-a');
  const html=fs.readFileSync(path.join(__dirname,'../pico/web_assets/index.html'),'utf8');
  assert.match(html,/<select[^>]*id="workspace-select"[^>]*disabled/);
});
