"""Autonomous evolution uses host evidence; no learning panel or approval."""
import json
from pathlib import Path

import pytest

from pico.evolution import EvolutionStore
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico, SessionStore
from pico.workspace import WorkspaceContext


MEMORY = {'text': '报告先给提纲', 'conditions': '报告准备'}
SKILL = {'description': '报告准备流程', 'when': '报告准备任务', 'steps': ['核对来源', '先写提纲'], 'acceptance': '满足当前任务验收'}


def feedback(s, key='user', text='报告先给提纲'):
    return s.observe(key, category='feedback', session_id='session', goal_id='goal', observation=text)


def candidate(s, source, kind='memory', body=None, key='report'):
    return s.propose(kind, key, body or (MEMORY if kind == 'memory' else SKILL), [source['id']],
                     goal_id='goal', session_id='session')


def run(s, name, task, outcome, verification, **kw):
    return s.observe(name, category='run', session_id='session', goal_id='goal', task_id=task, run_id=name,
                     outcome=outcome, observation='Host execution observation', detail={'verification': {'status': verification}}, **kw)


def test_actual_user_feedback_promotes_and_revises_without_confirmation(tmp_path):
    s = EvolutionStore(tmp_path)
    first = candidate(s, feedback(s))
    assert s.automatic('goal', 'session') == [{'id': first['id'], 'action': 'activated'}]
    revised = candidate(s, feedback(s, 'correction', '报告要先核对听众'), body={**MEMORY, 'text': '报告先核对听众，再给提纲'})
    s.automatic('goal', 'session')
    assert s.get(first['id'])['status'] == 'superseded'
    assert s.get(revised['id'])['reviewer'] == 'automatic_evidence_gate'
    assert s.audit()['valid']


def test_unverified_run_prose_cannot_approve_itself_and_scope_cannot_expand(tmp_path):
    s = EvolutionStore(tmp_path)
    source = run(s, 'seed', 'a', 'unverified', 'not_run')
    item = candidate(s, source)
    shared = s.propose('memory', 'shared', MEMORY, [feedback(s)['id']], scope='workspace')
    assert s.automatic('goal', 'session') == []
    assert s.get(item['id'])['status'] == s.get(shared['id'])['status'] == 'candidate'
    assert s.choose_trial('报告', 'other', 'other_session') is None


def test_skill_is_automatically_selected_then_promoted_only_after_independent_verification(tmp_path):
    s = EvolutionStore(tmp_path)
    seed = run(s, 'seed', 'a', 'passed', 'passed')
    item = candidate(s, seed, 'skill')
    assert s.choose_trial('报告准备', 'goal', 'session', task_id='a') is None
    selected = s.choose_trial('报告准备', 'goal', 'session', task_id='b')
    assert selected['id'] == item['id']
    s.expose(item['id'], 1, run_id='second', task_id='b', session_id='session', goal_id='goal', trial=True)
    s.settle(run(s, 'second', 'b', 'unverified', 'not_run'))
    assert s.automatic('goal', 'session') == []
    accepted = s.observe('accepted', category='acceptance', session_id='session', goal_id='goal', task_id='b',
                         run_id='second', outcome='passed', observation='Existing task acceptance')
    s.settle(accepted)
    s.automatic('goal', 'session')
    assert s.get(item['id'])['status'] == 'active'


def test_api_failures_do_not_demote_but_distinct_verified_regressions_restore_predecessor(tmp_path):
    s = EvolutionStore(tmp_path)
    first = candidate(s, run(s, 'seed0', 'seed0', 'passed', 'passed'), 'skill')
    s.expose(first['id'], 1, run_id='trial0', task_id='trial0', session_id='session', goal_id='goal', trial=True)
    s.settle(run(s, 'trial0', 'trial0', 'passed', 'passed'))
    s.automatic('goal', 'session')
    revised = candidate(s, run(s, 'seed1', 'seed1', 'passed', 'passed'), 'skill', body={**SKILL, 'steps': ['先核对所有结果', '再准备提纲']})
    s.expose(revised['id'], 1, run_id='trial1', task_id='trial1', session_id='session', goal_id='goal', trial=True)
    s.settle(run(s, 'trial1', 'trial1', 'passed', 'passed'))
    s.automatic('goal', 'session')
    active = s.get(revised['id'])
    for name, task in [('api1', 'x'), ('api2', 'y')]:
        s.expose(active['id'], active['version'], run_id=name, task_id=task, session_id='session', goal_id='goal')
        s.settle(run(s, name, task, 'failed', 'not_run'))
    assert s.automatic('goal', 'session') == []
    for name, task in [('fail1', 'x'), ('fail2', 'y')]:
        s.expose(active['id'], active['version'], run_id=name, task_id=task, session_id='session', goal_id='goal')
        s.settle(run(s, name, task, 'failed', 'failed'))
    changes = s.automatic('goal', 'session')
    assert s.get(revised['id'])['status'] == 'retracted'
    assert s.get(first['id'])['status'] == 'active'
    assert any(c['action'] == 'restored_predecessor' for c in changes)
    assert s.audit()['valid']


def test_repeated_same_task_failure_is_not_two_independent_failures(tmp_path):
    s = EvolutionStore(tmp_path)
    item = candidate(s, feedback(s))
    s.automatic('goal', 'session')
    item = s.get(item['id'])
    for name in ['repeat1', 'repeat2', 'repeat3']:
        s.expose(item['id'], item['version'], run_id=name, task_id='same', session_id='session', goal_id='goal')
        s.settle(run(s, name, 'same', 'failed', 'failed'))
    assert s.automatic('goal', 'session') == []
    assert s.get(item['id'])['status'] == 'active'


def test_candidate_with_bad_trials_stops_being_retried(tmp_path):
    s = EvolutionStore(tmp_path)
    item = candidate(s, run(s, 'seed', 'seed', 'passed', 'passed'), 'skill')
    for name in ['failure_a', 'failure_b']:
        s.expose(item['id'], 1, run_id=name, task_id=name, session_id='session', goal_id='goal', trial=True)
        s.settle(run(s, name, name, 'failed', 'failed'))
    s.automatic('goal', 'session')
    assert s.get(item['id'])['status'] == 'retracted'
    assert s.choose_trial('报告准备', 'goal', 'session', task_id='next') is None


def test_runtime_remembers_explicit_correction_even_when_model_omits_learning_tool(tmp_path):
    model = FakeModelClient(['<final>收到。</final>', '<final>先给报告提纲。</final>'])
    agent = Pico(model, WorkspaceContext.build(tmp_path), SessionStore(tmp_path/'.pico/sessions'),
                 harness_profile='workbench', max_depth=0)
    agent.ask('请记住，报告准备先给提纲。')
    store = agent.capabilities.learning.store
    items = store.list(session_id=agent.session['id'])
    assert len(items) == 1 and items[0]['status'] == 'active'
    assert items[0]['body']['text'] == '请记住，报告准备先给提纲。'
    agent.ask('准备报告')
    assert agent.session['working_state']['learning']['memories'][0]['id'] == items[0]['id']
    assert agent.current_task_state.attempts == 1
    assert not {'activate_learning', 'approve_skill', 'learning_feedback'} & agent.tools.keys()


def test_ui_has_no_learning_management_or_manual_trial_inputs():
    root = Path(__file__).resolve().parents[1]
    html = (root/'pico/web_assets/index.html').read_text(encoding='utf-8')
    app = (root/'pico/web_assets/app.js').read_text(encoding='utf-8')
    assert 'learning-panel' not in html and 'learning.js' not in html
    assert '保存反馈' not in html and '经验与技能' not in html
    assert 'learning_trial' not in app and 'GoalTreeLearning' not in app


@pytest.mark.parametrize('message', ['不要记住这条消息。', '请记住这个凭据：sk-' + 'x'*30])
def test_opt_out_and_sensitive_input_do_not_become_automatic_memory(tmp_path, message):
    agent = Pico(FakeModelClient(['<final>收到。</final>']), WorkspaceContext.build(tmp_path),
                 SessionStore(tmp_path/'.pico/sessions'), harness_profile='workbench', max_depth=0)
    assert agent.ask(message) == '收到。'
    assert agent.capabilities.learning.store.list(session_id=agent.session['id']) == []


def test_model_can_extract_natural_feedback_without_requiring_a_remember_keyword(tmp_path):
    class FeedbackClient(FakeModelClient):
        def __init__(self):
            super().__init__([])
        def complete(self, prompt, *args, **kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) > 1:
                return '<final>后续会先给精简提纲。</final>'
            source = next(s for s in agent.capabilities.learning.store.sources(session_id=agent.session['id'])
                          if s['category'] == 'feedback')
            proposal = {'name': 'learning_propose', 'args': {'kind': 'memory', 'key': 'report-length',
                'body': json.dumps({'text': '报告先给精简提纲。', 'conditions': '此对话中的报告准备'}),
                'source_ids': json.dumps([source['id']])}}
            return '<tool>' + json.dumps(proposal) + '</tool>'
    agent = Pico(FeedbackClient(), WorkspaceContext.build(tmp_path), SessionStore(tmp_path/'.pico/sessions'),
                 harness_profile='workbench', max_depth=0)
    agent.ask('这份报告太长了，先给个精简提纲就行。')
    items = agent.capabilities.learning.store.list(session_id=agent.session['id'])
    assert len(items) == 1 and items[0]['status'] == 'active'
    assert items[0]['body']['text'] == '报告先给精简提纲。'


def test_explicit_preference_reference_retrieves_without_word_overlap_but_unrelated_chat_does_not(tmp_path):
    agent = Pico(FakeModelClient(['<final>收到。</final>', '<final>先给提纲。</final>', '<final>普通问答。</final>']),
                 WorkspaceContext.build(tmp_path), SessionStore(tmp_path/'.pico/sessions'), harness_profile='workbench', max_depth=0)
    agent.ask('请记住，报告先给简短提纲。')
    agent.ask('按已有偏好，准备项目汇报。')
    context = agent.session['working_state']['learning']
    assert context['memories'] and context['recall_reason'] == 'explicit_scoped_reference'
    agent.ask('今天天气怎么样？')
    assert agent.session['working_state']['learning']['memories'] == []


def test_unrelated_test_failures_cannot_undo_explicit_user_preferences(tmp_path):
    s = EvolutionStore(tmp_path)
    item = candidate(s, feedback(s))
    s.automatic('goal', 'session')
    item = s.get(item['id'])
    for name in ['bad_code_a', 'bad_code_b']:
        s.expose(item['id'], item['version'], run_id=name, task_id=name, session_id='session', goal_id='goal')
        s.settle(run(s, name, name, 'failed', 'failed'))
    assert s.automatic('goal', 'session') == []
    assert s.get(item['id'])['status'] == 'active'
