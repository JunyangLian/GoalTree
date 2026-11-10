const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness(responses) {
  const elements = new Map();
  const element = () => ({textContent: '', cloneNode: element, addEventListener() {}, classList: {add() {}, remove() {}, toggle() {}}});
  const timers = [], rendered = [], busy = [], notices = [];
  let calls = 0;
  const sandbox = {
    URL, location:{href:'http://127.0.0.1:8773/'},
    document: {getElementById(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);}, addEventListener() {}},
    setTimeout(fn, delay) {timers.push({fn, delay});}, clearTimeout() {},
    fetch: async () => {const response = responses[calls++]; if (response instanceof Error) throw response; return {ok: true, json: async () => response};},
    rendered, busy, notices,
  };
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '../pico/web_assets/app.js'), 'utf8');
  vm.runInContext(source.slice(0, source.lastIndexOf('init();')) + `
    renderJob = job => rendered.push(job);
    setBusy = value => busy.push(value);
    toast = message => notices.push(message);
    renderConnection = () => { $('connection').textContent = 'AppContainer'; };
    refreshSessions = async () => { if (globalThis.historyFailure) throw new Error('history unavailable'); };
    state.taskId = 'run-1';
    globalThis.subject = {poll, state, renderTree, selectNode, newSession, baseForRun};
  `, sandbox);
  return {sandbox, timers, rendered, busy, notices, elements, calls: () => calls};
}

test('initialization locks the composer until configuration and scope binding finish', async () => {
  const h = harness([]);
  vm.runInContext(`
    api = async () => await new Promise(resolve => { globalThis.resolveConfig = resolve; });
    installControls = () => {}; renderConnection = () => {};
    globalThis.startInit = init;
  `, h.sandbox);
  const pending = h.sandbox.startInit();
  assert.equal(h.sandbox.subject.state.initializing, true);
  assert.equal(h.elements.get('send').disabled, true);
  assert.equal(h.elements.get('prompt').disabled, true);
  assert.equal(h.elements.get('new-session').disabled, true);
  // Failure remains locked; a partially initialized page must not clear input
  // or submit against a stale CSRF token/session.
  h.sandbox.resolveConfig(null);
  await pending;
  assert.equal(h.sandbox.subject.state.initializing, true);
  assert.equal(h.elements.get('send').disabled, true);
});

test('a transient GET failure recovers without another task submission', async () => {
  const h = harness([new Error('connection reset'), {status: 'waiting_approval'}, {status: 'completed'}]);
  await h.sandbox.subject.poll('run-1');
  assert.match(h.elements.get('connection').textContent, /正在重试/);
  assert.equal(h.timers[0].delay, 1000);
  await h.timers.shift().fn();
  assert.equal(h.rendered[0].status, 'waiting_approval');
  assert.equal(h.elements.get('connection').textContent, 'AppContainer');
  await h.timers.shift().fn();
  assert.deepEqual(h.busy, [false]);
  assert.equal(h.calls(), 3);
});

test('five consecutive failures stop, with bounded exponential backoff', async () => {
  const h = harness(Array.from({length: 5}, () => new Error('offline')));
  await h.sandbox.subject.poll('run-1');
  const delays = [];
  while (h.timers.length) {const timer = h.timers.shift(); delays.push(timer.delay); await timer.fn();}
  assert.deepEqual(delays, [1000, 2000, 4000, 8000]);
  assert.equal(h.calls(), 5);
  assert.match(h.elements.get('connection').textContent, /自动重试已停止/);
  assert.deepEqual(h.busy, []); // An unseen active job must not allow duplicate submission.
});

test('a successful read resets the consecutive failure counter', async () => {
  const h = harness([new Error('offline'), {status: 'running'}, new Error('offline')]);
  await h.sandbox.subject.poll('run-1', 3);
  assert.equal(h.timers[0].delay, 8000);
  await h.timers.shift().fn();
  await h.timers.shift().fn();
  assert.equal(h.timers[0].delay, 1000);
});

test('an obsolete in-flight task cannot schedule retries or update connection state', async () => {
  const h = harness([new Error('offline')]);
  const request = h.sandbox.subject.poll('run-1');
  h.sandbox.subject.state.taskId = 'run-2';
  await request;
  assert.equal(h.timers.length, 0);
  assert.equal(h.elements.has('connection'), false);
});

test('a history refresh failure does not turn a completed job into a disconnected job', async () => {
  const h = harness([{status: 'completed'}]);
  h.sandbox.historyFailure = true;
  await h.sandbox.subject.poll('run-1');
  assert.equal(h.elements.get('connection').textContent, 'AppContainer');
  assert.deepEqual(h.busy, [false]);
  assert.equal(h.timers.length, 0);
  assert.deepEqual(h.notices, ['history unavailable']);
});

test('a run from another goal never overwrites the viewed conversation', async () => {
  const h = harness([{status: 'running', binding: {goal_id: 'goal-a', task_id: 'task-a', mode: 'execute'}}]);
  Object.assign(h.sandbox.subject.state, {goalId: 'goal-b', nodeId: '', mode: 'chat'});
  await h.sandbox.subject.poll('run-1');
  assert.equal(h.rendered.length, 0);
  assert.equal(h.sandbox.subject.state.latestJob.binding.goal_id, 'goal-a');
  assert.equal(h.timers.length, 1);
});

test('returning to the running scope allows the next poll to render it', async () => {
  const binding = {goal_id: 'goal-a', task_id: 'task-a', mode: 'execute'};
  const h = harness([{status: 'running', binding}, {status: 'completed', binding}]);
  Object.assign(h.sandbox.subject.state, {goalId: 'goal-b', nodeId: '', mode: 'chat'});
  await h.sandbox.subject.poll('run-1');
  Object.assign(h.sandbox.subject.state, {goalId: 'goal-a', nodeId: 'task-a', mode: 'execute'});
  await h.timers.shift().fn();
  assert.equal(h.rendered.length, 1);
  assert.equal(h.rendered[0].status, 'completed');
  assert.deepEqual(h.busy, [false]);
});

test('unchanged polling preserves tree nodes and input focus; revisions and scope changes redraw', () => {
  const h = harness([]);
  vm.runInContext('globalThis.treePaints = 0; paintTree = () => { treePaints++; };', h.sandbox);
  h.sandbox.subject.state.tree = {revision: 10};
  h.sandbox.subject.renderTree();
  h.sandbox.subject.renderTree();
  h.sandbox.subject.renderTree();
  assert.equal(h.sandbox.treePaints, 1);
  h.sandbox.subject.state.goalId = 'another-goal';
  h.sandbox.subject.renderTree();
  assert.equal(h.sandbox.treePaints, 2);
  h.sandbox.subject.state.tree.revision++;
  h.sandbox.subject.renderTree();
  assert.equal(h.sandbox.treePaints, 3);
  h.sandbox.subject.state.busy = true;
  h.sandbox.subject.renderTree();
  assert.equal(h.sandbox.treePaints, 4);
});

test('scope switching disables the composer until the new context is bound', async () => {
  const h = harness([]);
  vm.runInContext(`
    api = async path => path.startsWith('/api/tree/context') ?
      await new Promise(resolve => { globalThis.resolveScope = resolve; }) : {tree: {revision: 1}};
    showMessages = () => {}; renderTree = () => {};
  `, h.sandbox);
  h.sandbox.subject.state.goalId = 'old-goal';
  const pending = h.sandbox.subject.selectNode('new-goal');
  assert.equal(h.sandbox.subject.state.switching, true);
  assert.equal(h.elements.get('send').disabled, true);
  assert.equal(h.elements.get('prompt').disabled, true);
  assert.equal(h.sandbox.subject.state.goalId, 'old-goal');
  h.sandbox.resolveScope({session_id: 'new-context', messages: [], node: {title: 'New goal'}});
  await pending;
  assert.equal(h.sandbox.subject.state.goalId, 'new-goal');
  assert.equal(h.sandbox.subject.state.sessionId, 'new-context');
  assert.equal(h.sandbox.subject.state.switching, false);
  assert.equal(h.elements.get('send').disabled, false);
  assert.equal(h.elements.get('prompt').disabled, false);
});

test('new conversation locks input before its first request can resolve', async () => {
  const h = harness([]);
  vm.runInContext(`
    api = async () => await new Promise(resolve => { globalThis.resolveNew = resolve; });
    selectNode = async () => { state.scopeEpoch++; state.switching=false; $('send').disabled=false; $('prompt').disabled=false; }; $('prompt').focus = () => {};
  `, h.sandbox);
  const pending = h.sandbox.subject.newSession();
  assert.equal(h.sandbox.subject.state.switching, true);
  assert.equal(h.elements.get('prompt').disabled, true);
  assert.equal(h.elements.get('send').disabled, true);
  assert.equal(h.elements.get('new-session').disabled, true);
  h.sandbox.resolveNew({tree: {revision: 2}});
  await pending;
  assert.equal(h.sandbox.subject.state.switching, false);
  assert.equal(h.elements.get('prompt').disabled, false);
  assert.equal(h.elements.get('send').disabled, false);
});

test('returning during final persistence does not duplicate an already stored answer', () => {
  const h = harness([]);
  const messages=[{role:'user',content:'Earlier'},{role:'assistant',content:'Previous answer'},
                  {role:'user',content:'Current'},{role:'assistant',content:'Stored current answer'}];
  const base=h.sandbox.subject.baseForRun(messages,'Current');
  assert.equal(base.length,3);
  assert.equal(base[2].content,'Current');
  assert.equal(messages.length,4);
});

test('a late new-conversation response cannot unlock or overwrite a newer goal switch', async () => {
  const h = harness([]);
  vm.runInContext(`
    api = async (path, data) => data?.action === 'new_conversation' ?
      await new Promise(resolve => { globalThis.resolveOldNew = resolve; }) :
      path.startsWith('/api/tree/context') ? await new Promise(resolve => { globalThis.resolveNewGoal = resolve; }) : {tree: {revision: 9}};
    showMessages = () => {}; renderTree = () => {}; $('prompt').focus = () => {};
  `, h.sandbox);
  const first=h.sandbox.subject.newSession();
  const latest=h.sandbox.subject.selectNode('newer-goal');
  h.sandbox.resolveOldNew({tree: {revision: 8}});
  await first;
  assert.equal(h.sandbox.subject.state.switching,true);
  assert.equal(h.elements.get('send').disabled,true);
  assert.equal(h.elements.get('prompt').disabled,true);
  h.sandbox.resolveNewGoal({session_id:'newer-session',messages:[],node:{title:'Newer goal'}});
  await latest;
  assert.equal(h.sandbox.subject.state.goalId,'newer-goal');
  assert.equal(h.sandbox.subject.state.sessionId,'newer-session');
  assert.equal(h.sandbox.subject.state.switching,false);
  assert.equal(h.elements.get('send').disabled,false);
});
