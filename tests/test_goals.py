"""GoalTree invariants and real harness integration, without model API calls."""
import copy
import json
import threading
import urllib.request
import urllib.error

import pytest

from pico.goals import GoalError, GoalStore
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico, SessionStore
from pico.web import RequestError, Workbench
from pico.workspace import WorkspaceContext


def goal(store, title='Goal A', **kwargs):
    return store.action({'action': 'create_goal', 'title': title, **kwargs})


def task(store, g, title='Task A', **kwargs):
    return store.action({'action': 'create_task', 'goal_id': g['id'], 'title': title, **kwargs})


def edit(store, item, **kwargs):
    return store.action({'action': 'update_task', 'goal_id': item['goal_id'], 'task_id': item['id'], 'revision': item['revision'], **kwargs})


def finish(app, submitted):
    job = app.task(submitted['id'])
    with job.condition:
        assert job.condition.wait_for(lambda: job.finished is not None and app.active_id is None, 15)
    return job.view()


def app_with(root, outputs):
    return Workbench(root, harness_profile='workbench', model_factory=lambda: FakeModelClient(list(outputs)))


def test_multiple_goals_have_disjoint_task_and_interaction_sessions(tmp_path):
    s = GoalStore(tmp_path)
    a, b = goal(s), goal(s, 'Goal B')
    x, y = task(s, a), task(s, b)
    ids = [item[k] for item in (a, b, x, y) for k in ('session_id', 'chat_session_id', 'planner_session_id')]
    assert len(set(ids)) == 12
    assert s.binding(a['id'], x['id'], 'execute')['session_id'] == x['session_id']
    assert s.binding(a['id'], x['id'])['session_id'] == x['chat_session_id']
    with pytest.raises(GoalError, match='不匹配'):
        s.binding(b['id'], x['id'])
    assert GoalStore(tmp_path).read() == s.read()


def test_tree_and_dependencies_are_distinct_and_cross_goal_edges_rejected(tmp_path):
    s = GoalStore(tmp_path)
    a, b = goal(s), goal(s, 'B')
    x = task(s, a)
    y = task(s, a, parent_id=x['id'])
    z = task(s, a, depends_on=[y['id']])
    assert z['parent_id'] == ''
    assert y['depends_on'] == []
    before, events = s.read(), s.events()
    with pytest.raises(GoalError, match='同一个目标'):
        task(s, b, depends_on=[x['id']])
    assert s.read() == before and s.events() == events


@pytest.mark.parametrize('field', ['parent_id', 'depends_on'])
def test_cycle_rejected_atomically(tmp_path, field):
    s = GoalStore(tmp_path)
    g = goal(s)
    a = task(s, g)
    b = task(s, g, **{field: a['id'] if field == 'parent_id' else [a['id']]})
    before = s.read()
    with pytest.raises(GoalError, match='循环'):
        edit(s, a, **{field: b['id'] if field == 'parent_id' else [b['id']]})
    assert s.read() == before


def test_spec_edit_archives_context_and_invalidates_only_related_tasks(tmp_path):
    s = GoalStore(tmp_path)
    g, other = goal(s), goal(s, 'Other')
    a = task(s, g, constraints='No network', context_notes='Temporary decision')
    b = task(s, g, depends_on=[a['id']])
    untouched = task(s, g, 'Unrelated')
    foreign = task(s, other)
    updated = edit(s, a, description='Changed contract')
    state = s.read()
    assert updated['session_id'] != a['session_id']
    assert updated['context_history'][0]['session_id'] == a['session_id']
    assert state['tasks'][b['id']]['validity'] == 'stale'
    assert state['tasks'][untouched['id']] == untouched
    assert state['tasks'][foreign['id']] == foreign
    assert updated['constraints'] == 'No network'
    assert updated['context_notes'] == 'Temporary decision'
    with pytest.raises(GoalError, match='已更新'):
        edit(s, a, title='Lost update')


def test_goal_constraints_invalidate_own_tasks_only(tmp_path):
    s = GoalStore(tmp_path)
    a, b = goal(s), goal(s, 'B')
    x, y = task(s, a), task(s, b)
    current = s.read()['goals'][a['id']]
    s.action({'action': 'update_goal', 'goal_id': a['id'], 'revision': current['revision'], 'constraints': 'Offline only'})
    assert s.read()['tasks'][x['id']]['validity'] == 'stale'
    assert s.read()['tasks'][y['id']] == y


def test_model_plan_resolves_aliases_and_keeps_old_tasks(tmp_path):
    s = GoalStore(tmp_path)
    g = goal(s)
    old = task(s, g, 'Existing')
    current = s.read()['goals'][g['id']]
    result = s.plan(g['id'], [
        {'id': 'root', 'title': 'Inspect'},
        {'id': 'child', 'title': 'Implement', 'parent_id': 'root'},
        {'id': 'verify', 'title': 'Verify', 'depends_on': ['child']},
    ], current['revision'])
    assert result[1]['parent_id'] == result[0]['id']
    assert result[2]['depends_on'] == [result[1]['id']]
    assert s.read()['tasks'][old['id']] == old
    before = s.read()
    with pytest.raises(GoalError):
        s.plan(g['id'], [{'id': 'a', 'title': 'A', 'depends_on': ['b']}, {'id': 'b', 'title': 'B', 'depends_on': ['a']}], before['goals'][g['id']]['revision'])
    assert s.read() == before


def test_model_cannot_declare_completed_or_edit_executed_task(tmp_path):
    s = GoalStore(tmp_path)
    g = goal(s)
    with pytest.raises(GoalError):
        s.plan(g['id'], [{'title': 'A', 'status': 'completed'}], g['revision'])
    t = task(s, g)
    bound = s.binding(g['id'], t['id'], 'execute')
    s.start(bound, 'run1')
    s.finish(bound, {'id': 'run1', 'status': 'completed', 'answer': 'All passed'})
    t = s.read()['tasks'][t['id']]
    assert t['status'] == 'review' and t['validity'] == 'unknown'
    with pytest.raises(GoalError):
        s.plan(g['id'], [{'id': t['id'], 'title': 'Rewrite history'}], s.read()['goals'][g['id']]['revision'])


def test_late_result_cannot_complete_new_task_version(tmp_path):
    s = GoalStore(tmp_path)
    g = goal(s)
    t = task(s, g)
    bound = s.binding(g['id'], t['id'], 'execute')
    s.start(bound, 'old-run')
    t = edit(s, s.read()['tasks'][t['id']], acceptance='New contract')
    assert not s.finish(bound, {'id': 'old-run', 'status': 'completed', 'verification': {'status': 'passed'}})
    current = s.read()['tasks'][t['id']]
    assert current['validity'] == 'stale'
    assert current['runs'][0]['id'] == 'old-run'
    assert current['status'] != 'completed'


def test_dependency_requires_current_acceptance_and_reopen_invalidates(tmp_path):
    s = GoalStore(tmp_path)
    g = goal(s)
    a = task(s, g)
    b = task(s, g, depends_on=[a['id']])
    with pytest.raises(GoalError, match='依赖'):
        s.binding(g['id'], b['id'], 'execute')
    s.action({'action': 'accept_task', 'goal_id': g['id'], 'task_id': a['id'], 'revision': a['revision'], 'reason': 'Operator inspected current output'})
    assert s.binding(g['id'], b['id'], 'execute')
    current = s.read()['tasks'][a['id']]
    s.action({'action': 'reopen_task', 'goal_id': g['id'], 'task_id': a['id'], 'revision': current['revision']})
    with pytest.raises(GoalError):
        s.binding(g['id'], b['id'], 'execute')


def test_context_contains_ancestors_constraints_and_explicit_dependencies_only(tmp_path):
    s = GoalStore(tmp_path)
    g = goal(s, constraints='Never upload data')
    other = goal(s, 'Other', description='FOREIGN SECRET CONTEXT')
    a = task(s, g, 'Parent', context_notes='Parent decision')
    sibling = task(s, g, 'Sibling', description='UNRELATED TASK DETAIL')
    b = task(s, g, 'Child', parent_id=a['id'])
    context = json.dumps(s.context(s.binding(g['id'], b['id'])))
    assert 'Never upload data' in context and 'Parent decision' in context
    assert 'FOREIGN SECRET CONTEXT' not in context and 'UNRELATED TASK DETAIL' not in context
    assert sibling['id'] not in context and other['id'] not in context


def test_restart_preserves_history_and_requires_recovery(tmp_path):
    s = GoalStore(tmp_path)
    g = goal(s)
    t = task(s, g)
    bound = s.binding(g['id'], t['id'], 'execute')
    s.start(bound, 'interrupted')
    restored = GoalStore(tmp_path)
    assert restored.recover() == [t['id']]
    assert restored.read()['tasks'][t['id']]['status'] == 'blocked'
    assert restored.read()['tasks'][t['id']]['session_id'] == t['session_id']
    assert restored.recover() == []


def test_real_harness_plans_tree_then_chat_cannot_mutate_it(tmp_path):
    steps = [{'id': 'inspect', 'title': 'Inspect'}, {'title': 'Verify', 'depends_on': ['inspect']}]
    call = '<tool>' + json.dumps({'name': 'plan_tasks', 'args': {'steps': json.dumps(steps)}}) + '</tool>'
    app = app_with(tmp_path, [call, '<final>Plan saved.</final>'])
    g = goal(app.goals)
    run = finish(app, app.submit({'goal_id': g['id'], 'mode': 'plan', 'prompt': 'Plan this goal'}))
    assert run['status'] == 'completed', run['error']
    assert len(app.goals.read()['tasks']) == 2
    assert any(e.get('name') == 'plan_tasks' and e.get('tool_status') == 'ok' for e in run['events'])
    before = app.goals.read()
    chat = finish(app, app.submit({'goal_id': g['id'], 'mode': 'chat', 'prompt': 'Why this plan?', 'policy': 'auto'}))
    assert app.task(chat['id']).agent.read_only
    assert 'plan_tasks' not in app.task(chat['id']).agent.tools
    assert 'write_file' not in app.task(chat['id']).agent.tools
    assert app.goals.read()['tasks'] == before['tasks']
    assert app.goals.read()['goals'] == before['goals']


def test_goal_switch_does_not_mix_real_harness_history_or_memory(tmp_path):
    app = app_with(tmp_path, ['<final>Observed.</final>'])
    a, b = goal(app.goals), goal(app.goals, 'B')
    first = finish(app, app.submit({'goal_id': a['id'], 'prompt': 'ALPHA_ONLY_MARKER'}))
    second = finish(app, app.submit({'goal_id': b['id'], 'prompt': 'BETA_ONLY_MARKER'}))
    third = finish(app, app.submit({'goal_id': a['id'], 'prompt': 'Continue alpha'}))
    assert first['session_id'] == third['session_id'] != second['session_id']
    beta = app.task(second['id']).agent
    assert 'ALPHA_ONLY_MARKER' not in beta.prompt('Inspect scoped context')
    assert 'BETA_ONLY_MARKER' not in app.task(third['id']).agent.prompt('Inspect scoped context')
    knowledge = beta.memory.knowledge
    c = knowledge.propose('ALPHA_ONLY_MARKER project preference', 'alpha', scope='session', session_id=first['session_id'])
    knowledge.activate(c['id'], c['version'], reviewer='test')
    c = knowledge.propose('GLOBAL_OTHER_GOAL preference', 'global')
    knowledge.activate(c['id'], c['version'], reviewer='test')
    assert beta.memory.retrieval_candidates('ALPHA_ONLY_MARKER GLOBAL_OTHER_GOAL') == []
    assert beta.memory.durable_store is None


def test_task_context_is_separate_from_goal_chat_and_other_task(tmp_path):
    app = app_with(tmp_path, ['<final>Done, needs external acceptance.</final>'])
    g = goal(app.goals)
    a, b = task(app.goals, g), task(app.goals, g, 'B')
    first = finish(app, app.submit({'goal_id': g['id'], 'node_id': a['id'], 'mode': 'execute', 'prompt': 'TASK_ALPHA_MARKER'}))
    second = finish(app, app.submit({'goal_id': g['id'], 'node_id': b['id'], 'mode': 'execute', 'prompt': 'TASK_BETA_MARKER'}))
    assert first['session_id'] != second['session_id']
    assert 'TASK_ALPHA_MARKER' not in app.task(second['id']).agent.prompt('Inspect')
    assert app.goals.read()['tasks'][a['id']]['status'] == 'review'
    assert app.tree_context(g['id'])['messages'] == []
    with pytest.raises(RequestError, match='不匹配'):
        app.submit({'goal_id': g['id'], 'node_id': b['id'], 'mode': 'execute', 'session_id': first['session_id'], 'prompt': 'Leak'})
    with pytest.raises(RequestError, match='显式绑定'):
        app.submit({'session_id': first['session_id'], 'prompt': 'Bypass scope'})


def test_running_task_can_be_browsed_but_not_retargeted_or_edited(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Slow(FakeModelClient):
        def complete(self, *args, **kwargs):
            entered.set()
            assert release.wait(10)
            return super().complete(*args, **kwargs)
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: Slow(['<final>Done</final>']))
    a, b = goal(app.goals), goal(app.goals, 'B')
    t = task(app.goals, a)
    submitted = app.submit({'goal_id': a['id'], 'node_id': t['id'], 'mode': 'execute', 'prompt': 'Run alpha'})
    try:
        assert entered.wait(10)
        app.tree_action({'action': 'select', 'goal_id': b['id']})
        assert app.goals.read()['selection']['goal_id'] == b['id']
        with pytest.raises(RequestError, match='运行中'):
            app.tree_action({'action': 'update_task', 'goal_id': a['id'], 'task_id': t['id'], 'revision': t['revision'], 'title': 'Wrong'})
        with pytest.raises(RequestError, match='已有任务'):
            app.submit({'goal_id': b['id'], 'prompt': 'Concurrent'})
    finally:
        release.set()
    result = finish(app, submitted)
    assert result['binding']['goal_id'] == a['id']
    assert app.goals.read()['goals'][b['id']]['runs'] == []


def test_configuration_redacts_secrets_and_tree_events_survive(tmp_path):
    app = app_with(tmp_path, [])
    g = app.tree_action({'action': 'create_goal', 'title': 'A'})['result']
    restored = app_with(tmp_path, [])
    assert restored.config()['product'] == 'GoalTree'
    assert restored.tree()['goals'][g['id']]['title'] == 'A'
    assert restored.tree()['events'][-1]['kind'] == 'operator_create_goal'


def test_external_file_change_invalidates_acceptance_without_erasing_completion(tmp_path):
    app = app_with(tmp_path, [])
    g = goal(app.goals)
    t = task(app.goals, g)
    (tmp_path/'output.txt').write_text('v1')
    app.tree_action({'action': 'accept_task', 'goal_id': g['id'], 'task_id': t['id'], 'revision': t['revision'], 'reason': 'Inspected output v1'})
    assert app.tree()['tasks'][t['id']]['validity'] == 'fresh'
    (tmp_path/'output.txt').write_text('v2')
    current = app.tree()['tasks'][t['id']]
    assert current['validity'] == 'stale' and current['status'] == 'completed'
    assert current['acceptance_note'] == 'Inspected output v1'


def test_manual_task_does_not_inherit_code_verification_or_self_complete(tmp_path):
    app = app_with(tmp_path, ['<final>Observed local facts.</final>'])
    app.verification_command = 'python -B -m unittest discover -v'
    g = goal(app.goals)
    t = task(app.goals, g, verification_mode='manual', acceptance='Operator reviews local facts')
    result = finish(app, app.submit({'goal_id': g['id'], 'node_id': t['id'], 'mode': 'execute', 'prompt': 'Inspect'}))
    assert result['status'] == 'completed'
    assert result['verification']['status'] == 'not_configured'
    current = app.goals.read()['tasks'][t['id']]
    assert current['status'] == 'review' and current['validity'] == 'unknown'
    assert app.task(result['id']).agent.verification_command is None


def test_demo_fixture_routes_by_selected_task_not_dependency_title():
    from scripts.run_goaltree_demo import DemoClient
    client = DemoClient()
    scope = {'mode': 'execute', 'task': {'title': '生成并验证交付物'}, 'dependencies': [{'title': '读取工作区说明'}]}
    prompt = 'GoalTree current scoped task state:\n' + json.dumps(scope, ensure_ascii=False)
    assert 'read_file' in client.complete(prompt, 1024)
    assert 'write_file' in client.complete(prompt, 1024)
    assert 'run_shell' in client.complete(prompt, 1024)


def test_tree_http_api_preserves_csrf_and_optimistic_revision_checks(tmp_path):
    from pico.web import make_server
    app = app_with(tmp_path, [])
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f'http://127.0.0.1:{server.server_port}'
    def post(data, token=None):
        request = urllib.request.Request(url+'/api/tree', data=json.dumps(data).encode(), headers={
            'Content-Type': 'application/json', 'X-Pico-Token': token or ''})
        return json.loads(opener.open(request, timeout=10).read())
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            post({'action': 'create_goal', 'title': 'No token'})
        assert denied.value.code == 403 and app.goals.read()['goals'] == {}
        g = post({'action': 'create_goal', 'title': 'HTTP goal'}, app.csrf)['result']
        t = post({'action': 'create_task', 'goal_id': g['id'], 'title': 'HTTP task'}, app.csrf)['result']
        before = app.goals.read()
        with pytest.raises(urllib.error.HTTPError) as conflict:
            post({'action': 'update_task', 'goal_id': g['id'], 'task_id': t['id'], 'revision': 0, 'title': 'Old edit'}, app.csrf)
        assert conflict.value.code == 409 and app.goals.read() == before
        context = json.loads(opener.open(url+'/api/tree/context?goal_id='+g['id']+'&node_id='+t['id']+'&mode=chat', timeout=10).read())
        assert context['session_id'] == t['chat_session_id']
        assert context['messages'] == []
        assert app.goals.read() == before
    finally:
        server.shutdown()
        server.server_close()


def test_native_protocol_receives_scoped_context_and_plan_tool(tmp_path):
    from pico.providers.native_tools import NativeToolConversation
    class NativeFixture(FakeModelClient):
        supports_native_tools = True
        def begin_native_run(self, tools, context, message, command):
            self.native_conversation = NativeToolConversation(tools, context, message, command, require_reasoning=False)
            self.calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            message = {'content': 'Planned'} if self.calls > 1 else {'tool_calls': [{
                'id': 'native-plan-1', 'type': 'function', 'function': {'name': 'plan_tasks', 'arguments': json.dumps({'steps': json.dumps([{'title': 'Native task'}])})}}]}
            return self.native_conversation.accept({'choices': [{'message': message}]}, {})
        def observe_native_tool(self, result):
            self.native_conversation.observe(result)
        def native_notice(self, message):
            self.native_conversation.notice(message)
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: NativeFixture([]))
    g = goal(app.goals, 'Native goal', constraints='PINNED_GOAL_CONSTRAINT')
    unrelated = goal(app.goals, 'Foreign', constraints='FOREIGN_CONSTRAINT')
    result = finish(app, app.submit({'goal_id': g['id'], 'mode': 'plan', 'prompt': 'Plan'}))
    assert result['status'] == 'completed', result['error']
    agent = app.task(result['id']).agent
    conversation = agent.model_client.native_conversation
    content = json.dumps(conversation.payload()['messages'], ensure_ascii=False)
    assert 'PINNED_GOAL_CONSTRAINT' in content and 'FOREIGN_CONSTRAINT' not in content
    assert content.count('PINNED_GOAL_CONSTRAINT') == 1
    assert conversation.pending is None
    assert any(m.get('tool_call_id') == 'native-plan-1' for m in conversation.messages)
    assert len(app.goals.read()['tasks']) == 1


@pytest.mark.parametrize('data', [
    {'action': []}, {'action': 'create_goal', 'title': []},
    {'action': 'create_task', 'title': 'Task', 'parent_id': []},
    {'action': 'create_task', 'title': 'Task', 'depends_on': [False]},
])
def test_malformed_tree_requests_reject_without_partial_commit(tmp_path, data):
    s = GoalStore(tmp_path)
    g = goal(s)
    before = s.read()
    with pytest.raises(GoalError):
        s.action({**data, 'goal_id': g['id']})
    assert s.read() == before
