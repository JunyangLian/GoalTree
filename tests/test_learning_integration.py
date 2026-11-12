import json
import threading
import urllib.error
import urllib.request

import pytest

from pico.evolution import EvolutionStore
from pico.providers.clients import FakeModelClient
from pico.web import Workbench, RequestError, make_server


BODY = {'text': '先给报告提纲', 'conditions': '报告任务'}
SKILL = {'description': '报告准备', 'when': '需要报告', 'steps': ['核对任务要求', '形成提纲'], 'acceptance': '用户验收提纲'}


def app(root, outputs=('<final>完成本轮回复，但未验收。</final>',)):
    return Workbench(root, harness_profile='workbench', model_factory=lambda: FakeModelClient(list(outputs)))


def fixture(app, kind='memory'):
    goal = app.goals.action({'action': 'create_goal', 'title': '报告目标'})
    binding = app.goals.binding(goal['id'], '', 'assistant')
    store = EvolutionStore(app.root)
    source = store.observe('feedback', category='feedback', session_id=binding['session_id'], goal_id=goal['id'],
                           task_id='source_task', observation='用户纠正：先给提纲')
    item = store.propose(kind, 'report', BODY if kind == 'memory' else SKILL, [source['id']],
                         goal_id=goal['id'], session_id=binding['session_id'])
    return store, goal, binding, item


def wait(app, submitted):
    task = app.task(submitted['id'])
    with task.condition:
        assert task.condition.wait_for(lambda: task.finished is not None and app.active_id is None, 15)
    return task.view()


def test_operator_review_is_scoped_cas_guarded_and_does_not_change_permissions(tmp_path):
    web = app(tmp_path)
    store, goal, binding, item = fixture(web)
    scope = {'goal_id': goal['id'], 'session_id': binding['session_id']}
    assert web.learning_view(scope)['items'][0]['status'] == 'candidate'
    web.learn({**scope, 'action': 'activate', 'id': item['id'], 'version': 1})
    assert store.get(item['id'])['status'] == 'active'
    with pytest.raises(ValueError, match='conflict'):
        web.learn({**scope, 'action': 'retract', 'id': item['id'], 'version': 1})
    other = web.goals.action({'action': 'create_goal', 'title': '另一个目标'})
    assert web.learning_view({'goal_id': other['id']})['items'] == []
    with pytest.raises(RequestError, match='其他目标'):
        web.learn({'goal_id': other['id'], 'action': 'activate', 'id': item['id'], 'version': 2})
    web.active_id = 'currently_running'
    with pytest.raises(RequestError, match='运行结束'):
        web.learn({**scope, 'action': 'retract', 'id': item['id'], 'version': 2})
    web.active_id = None
    assert not (tmp_path/'.pico/policy.json').exists()


def test_trial_is_real_context_exposure_and_final_answer_is_not_validation(tmp_path):
    web = app(tmp_path)
    store, goal, binding, item = fixture(web, 'skill')
    job = wait(web, web.submit({'prompt': '请给报告提纲', 'goal_id': goal['id'], 'mode': 'assistant',
        'learning_trial': {'id': item['id'], 'version': 1}}))
    assert job['status'] == 'completed'
    usage = store.usages(item['id'])
    assert usage[0]['mode'] == 'trial' and usage[0]['run_id'] == job['run_id']
    assert usage[0]['outcome'] == 'unverified'
    assert not store.transfer_passed(item)
    with pytest.raises(ValueError, match='different task'):
        web.learn({'goal_id': goal['id'], 'action': 'activate', 'id': item['id'], 'version': 1})
    assert store.get(item['id'])['status'] == 'candidate'


def test_operator_task_acceptance_can_validate_a_trial_on_another_task(tmp_path):
    web = app(tmp_path)
    store, goal, binding, item = fixture(web, 'skill')
    task = web.goals.action({'action': 'create_task', 'goal_id': goal['id'], 'title': '导师报告提纲',
                             'verification_mode': 'manual', 'acceptance': '形成四节提纲'})
    # Existing goal_action tool records a real deliverable, rather than setting completed.
    proposal = {'action': 'record_progress', 'task_id': task['id'], 'revision': task['revision'],
                'status': 'review', 'note': '提纲已形成', 'output': '背景、方法、结果、讨论。'}
    outputs = ['<tool>' + json.dumps({'name': 'goal_action', 'args': {'proposal': json.dumps(proposal)}}) + '</tool>',
               '<final>已提交提纲供验收。</final>']
    web.make_model = lambda: FakeModelClient(outputs.copy())
    job = wait(web, web.submit({'prompt': '给导师准备报告提纲', 'mode': 'assistant', 'goal_id': goal['id'],
                               'learning_trial': {'id': item['id'], 'version': 1}}))
    current = web.goals.read()['tasks'][task['id']]
    assert current['status'] == 'review'
    assert store.usages(item['id'])[0]['outcome'] == 'unverified'
    web.tree_action({'action': 'accept_task', 'goal_id': goal['id'], 'task_id': task['id'], 'revision': current['revision'],
                     'reason': '已人工核对背景、方法、结果、讨论四节提纲。'})
    assert store.transfer_passed(item)
    assert store.usages(item['id'])[0]['outcome'] == 'passed'
    # Existing task acceptance is enough; learning advances automatically.
    assert store.get(item['id'])['status'] == 'active'
    # Changing the verified contract invalidates experiences anchored to that acceptance.
    receipt = store.source(store.usages(item['id'])[0]['source_id'])
    derived = store.propose('memory', 'outline', BODY, [receipt['id']], goal_id=goal['id'])
    store.activate(derived['id'], 1, reviewer='operator')
    latest = web.goals.read()['tasks'][task['id']]
    web.goals.action({'action': 'update_task', 'goal_id': goal['id'], 'task_id': task['id'],
                      'revision': latest['revision'], 'acceptance': '新要求：八节提纲'})
    assert store.search('报告', goal['id'], binding['session_id'])
    assert store.get(derived['id'])['status'] == 'stale'


def test_http_review_requires_csrf_and_serves_real_frontend_module(tmp_path):
    web = app(tmp_path)
    store, goal, binding, item = fixture(web)
    server = make_server(web, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    body = json.dumps({'action': 'activate', 'id': item['id'], 'version': 1, 'goal_id': goal['id']}).encode()
    try:
        with urllib.request.urlopen(base + '/') as response:
            html = response.read()
            assert response.status == 200 and b'learning-panel' not in html and b'learning.js' not in html
        request = urllib.request.Request(base + '/api/learning', data=body, headers={'Content-Type': 'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
        request.add_header('X-Pico-Token', web.csrf)
        with urllib.request.urlopen(request) as response:
            data = json.load(response)
        assert data['items'][0]['status'] == 'active' and data['audit']['valid']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
