const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  class Element {
    constructor() { this.children=[];this.value='';this.textContent='';this.disabled=false;this.classList={add(){},remove(){},toggle(){}}; }
    append(...elements) { this.children.push(...elements); }
    replaceChildren(...elements) { this.children=elements; }
    cloneNode() { return new Element(); }
    setAttribute(key,value) { this[key]=value; }
    querySelectorAll() { return []; }
    addEventListener(event, callback) { this[event]=callback; }
  }
  const elements=new Map(), calls=[], notices=[];
  const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
    createElement:()=>new Element(),createDocumentFragment:()=>new Element(),addEventListener(){},
    querySelector(query){return {value:query.includes('planning')?'step':'autonomous'};}};
  const sandbox={document,notices,calls,setTimeout(){},clearTimeout(){},
    FileReader:class {readAsDataURL(){this.result='data:text/plain;base64,Zm9v';this.onload();}}};
  vm.createContext(sandbox);
  const source=fs.readFileSync(path.join(__dirname,'../pico/web_assets/app.js'),'utf8');
  vm.runInContext(source.slice(0,source.lastIndexOf('init();'))+`
    state.config={configured:true,attachments:{max_files:4,max_bytes:5242880,image_input:true}};
    state.sessionId='session-a';state.goalId='goal-a';
    toast=message=>notices.push(message);poll=()=>{};renderJob=()=>{};
    api=async(path,body)=>{calls.push({path,body});return path==='/api/cooperation'?body.cooperation:{id:'run-a',session_id:'session-a'};};
    globalThis.controls={state,addAttachments,saveCooperation,renderComposerTools,send};
  `,sandbox);
  return {sandbox,elements,calls,notices,subject:sandbox.controls};
}

test('two independent preferences and attachment IDs enter the actual request', async()=>{
  const h=harness();h.subject.state.cooperation={planning:'step',implementation:'autonomous'};
  h.subject.state.drafts.set('session-a',[{id:'att_a',name:'scope.md',mime:'text/plain'}]);
  h.sandbox.document.getElementById('prompt').value='Plan using this file';
  await h.subject.send();
  const body=h.calls.find(c=>c.path==='/api/tasks').body;
  assert.equal(body.cooperation.planning,'step');assert.equal(body.cooperation.implementation,'autonomous');
  assert.deepEqual(Array.from(body.attachments),['att_a']);assert.equal(body.policy,'ask');
  assert.equal(h.subject.state.drafts.has('session-a'),false);
});

test('settings apply to one conversation and are not editable during a run',async()=>{
  const h=harness();await h.subject.saveCooperation();
  assert.equal(h.subject.state.cooperation.planning,'step');assert.equal(h.subject.state.cooperation.implementation,'autonomous');
  assert.equal(h.calls[0].body.session_id,'session-a');
  h.subject.state.busy=true;await h.subject.saveCooperation();assert.equal(h.calls.length,1);
});

test('upload completes in its original conversation after a scope switch',async()=>{
  const h=harness();
  vm.runInContext(`api=async(path,body)=>{calls.push({path,body});return await new Promise(resolve=>{globalThis.resolveUpload=resolve;});};`,h.sandbox);
  const pending=h.subject.addAttachments([{name:'scope.md',size:3}]);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.subject.state.uploading,1);
  h.subject.state.scopeEpoch++;h.subject.state.sessionId='session-b';h.subject.state.goalId='goal-b';
  h.sandbox.resolveUpload({id:'att_a',name:'scope.md',mime:'text/plain'});await pending;
  assert.equal(h.subject.state.drafts.get('session-a').length,1);
  assert.equal(h.subject.state.drafts.has('session-b'),false);
  assert.equal(h.calls[0].body.session_id,'session-a');
  assert.match(h.notices[0],/原对话/);assert.equal(h.subject.state.uploading,0);
});

test('non-vision model and oversized files are rejected before upload',async()=>{
  const h=harness();h.subject.state.config.attachments.image_input=false;
  await h.subject.addAttachments([{name:'photo.png',size:100}]);
  assert.equal(h.calls.length,0);assert.match(h.notices[0],/图片理解/);
  await h.subject.addAttachments([{name:'big.txt',size:6000000}]);
  assert.equal(h.calls.length,0);assert.match(h.notices[1],/5MB/);
});

test('removing a draft affects only that attachment; file-only messages have an explicit read-only request',async()=>{
  const h=harness();
  h.subject.state.drafts.set('session-a',[{id:'att_a',name:'scope.md',mime:'text/plain'},{id:'att_b',name:'notes.md',mime:'text/plain'}]);
  h.subject.renderComposerTools();
  const first=h.elements.get('attachment-drafts').children[0];
  first.children[1].click();
  assert.equal(h.subject.state.drafts.get('session-a')[0].id,'att_b');
  await h.subject.send();
  assert.match(h.calls[0].body.prompt,/不自动创建目标或执行任务/);
  assert.deepEqual(Array.from(h.calls[0].body.attachments),['att_b']);
});
