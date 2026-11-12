"""Learning lifecycle and transfer gates, with actual host-owned receipts."""
import copy
import json

import pytest

from pico.evolution import EvolutionStore
from pico.knowledge import KnowledgeStore


MEMORY = {'text': '报告先确认听众和时长，再编写提纲。', 'conditions': '汇报准备任务；已有信息不重复询问。'}
SKILL = {'description': '准备汇报提纲', 'when': '已有明确报告主题',
         'steps': ['检查听众和时长是否已提供', '给出章节提纲并核对来源'], 'acceptance': '满足本次报告要求并获得验收'}


def observation(store, name='seed', task='task_a', outcome='passed', **kwargs):
    return store.observe(name, category='run', session_id='session', goal_id='goal', task_id=task,
                         run_id=name, outcome=outcome, observation='Actual host test outcome', **kwargs)


def propose(store, receipt, kind='memory', key='report', **kwargs):
    return store.propose(kind, key, MEMORY if kind == 'memory' else SKILL, [receipt['id']],
                         goal_id='goal', session_id='session', **kwargs)


def test_receipts_are_immutable_and_candidates_require_real_provenance(tmp_path):
    s = EvolutionStore(tmp_path)
    source = observation(s)
    assert observation(s) == source
    with pytest.raises(ValueError, match='identity conflict'):
        observation(s, outcome='failed')
    with pytest.raises(ValueError, match='Unknown host'):
        s.propose('memory', 'x', MEMORY, ['fabricated'], goal_id='goal')
    candidate = propose(s, source)
    assert candidate['status'] == 'candidate'
    assert propose(s, source)['id'] == candidate['id']
    assert s.search('报告', 'goal', 'session') == []
    assert s.audit()['valid']


def test_scopes_do_not_mix_goal_or_workspace_state(tmp_path):
    s = EvolutionStore(tmp_path)
    source = observation(s)
    item = propose(s, source)
    s.activate(item['id'], 1, reviewer='operator')
    assert s.search('报告', 'goal', 'another_session')
    assert s.search('报告', 'another_goal', 'another_session') == []
    with pytest.raises(ValueError, match='different context'):
        s.propose('memory', 'x', MEMORY, [source['id']], goal_id='foreign', session_id='foreign')
    with pytest.raises(ValueError, match='another workspace'):
        KnowledgeStore(tmp_path/'other', database=s.knowledge.path)


def test_correction_merge_and_explicit_rollback_keep_history(tmp_path):
    s = EvolutionStore(tmp_path)
    source = observation(s)
    first = propose(s, source)
    first = s.activate(first['id'], 1, reviewer='operator')
    other = propose(s, source, key='style')
    other = s.activate(other['id'], 1, reviewer='operator')
    body = {**MEMORY, 'text': '导师汇报先给提纲，再写正文。'}
    revised = s.propose('memory', 'report', body, [source['id']], goal_id='goal', replaces=[other['id']])
    s.activate(revised['id'], 1, reviewer='operator')
    assert s.get(first['id'])['status'] == s.get(other['id'])['status'] == 'superseded'
    restored = s.activate(first['id'], 3, reviewer='operator', rollback=True)
    assert restored['body'] == MEMORY and restored['status'] == 'active'
    assert s.get(revised['id'])['status'] == 'superseded'
    assert s.audit()['valid']


def test_concurrent_candidate_cannot_silently_replace_new_head(tmp_path):
    s = EvolutionStore(tmp_path)
    source = observation(s)
    one = propose(s, source)
    two = s.propose('memory', 'report', {**MEMORY, 'text': '不同的新内容'}, [source['id']], goal_id='goal')
    s.activate(one['id'], 1, reviewer='operator')
    with pytest.raises(ValueError, match='predecessor changed'):
        s.activate(two['id'], 1, reviewer='operator')
    assert s.get(two['id'])['status'] == 'candidate'
    assert s.get(one['id'])['status'] == 'active'


def test_file_change_expiry_and_policy_revocation_stop_recall(tmp_path):
    (tmp_path/'rules.txt').write_text('version 1', encoding='utf-8')
    time = [100.0]
    s = EvolutionStore(tmp_path, KnowledgeStore(tmp_path, clock=lambda: time[0]))
    source = observation(s, evidence_paths=['rules.txt'])
    item = propose(s, source, ttl_seconds=10)
    s.activate(item['id'], 1, reviewer='operator')
    assert s.search('报告', 'goal', 'session')
    assert s.search('报告', 'goal', 'session', path_filter=lambda p: False) == []
    (tmp_path/'rules.txt').write_text('version 2', encoding='utf-8')
    assert s.search('报告', 'goal', 'session') == []
    assert s.get(item['id'])['status'] == 'stale'
    fresh = observation(s, name='later')
    expiry = propose(s, fresh, key='expiry', ttl_seconds=10)
    s.activate(expiry['id'], 1, reviewer='operator')
    time[0] = 111
    assert s.search('报告', 'goal', 'session') == []
    assert s.get(expiry['id'])['status'] == 'expired'


def test_skill_transfer_requires_another_task_and_real_outcome(tmp_path):
    s = EvolutionStore(tmp_path)
    source = observation(s)
    item = propose(s, source, kind='skill')
    with pytest.raises(ValueError, match='different task'):
        s.activate(item['id'], 1, reviewer='operator')
    s.expose(item['id'], 1, run_id='same', session_id='session', goal_id='goal', task_id='task_a', trial=True)
    s.settle(observation(s, name='same', task='task_a'))
    assert not s.transfer_passed(item)
    s.expose(item['id'], 1, run_id='new', session_id='session', goal_id='goal', task_id='task_b', trial=True)
    unverified = observation(s, name='new', task='task_b', outcome='unverified')
    s.settle(unverified)
    assert not s.transfer_passed(item)
    accepted = s.observe('accept:new', category='acceptance', session_id='session', goal_id='goal',
                        task_id='task_b', run_id='new', outcome='passed', observation='Operator verified deliverable')
    s.settle(accepted)
    assert s.transfer_passed(item)
    active = s.activate(item['id'], 1, reviewer='operator')
    assert active['status'] == 'active'
    s.settle(unverified)
    assert s.usages(item['id'])[-1]['outcome'] == 'passed'


def test_invalid_payload_secrets_and_internal_evidence_are_rejected(tmp_path):
    s = EvolutionStore(tmp_path)
    source = observation(s)
    with pytest.raises(ValueError, match='executable'):
        s.propose('skill', 'evil', {**SKILL, 'script': 'echo x'}, [source['id']], goal_id='goal')
    with pytest.raises(ValueError, match='credential'):
        s.propose('memory', 'secret', {**MEMORY, 'text': 'sk-' + 'a'*30}, [source['id']], goal_id='goal')
    with pytest.raises(ValueError):
        observation(s, name='escape', evidence_paths=['../outside'])
    with pytest.raises(ValueError, match='Sensitive'):
        observation(s, name='internal', evidence_paths=['.pico/memory/knowledge.sqlite3'])


def test_audit_detects_corruption_and_usage_is_idempotent(tmp_path):
    s = EvolutionStore(tmp_path)
    item = propose(s, observation(s))
    active = s.activate(item['id'], 1, reviewer='operator')
    for _ in range(3):
        s.expose(item['id'], active['version'], run_id='actual', session_id='session', goal_id='goal')
    assert len(s.usages(item['id'])) == 1
    assert s.usages(item['id'])[0]['mode'] == 'presented'
    assert s.usages(item['id'])[0]['outcome'] == 'unverified'
    assert s.audit()['valid']
    with s.knowledge.connect() as db:
        db.execute("UPDATE learning_events SET body='{}' WHERE seq=1")
    assert not s.audit()['valid']


def test_runtime_recalls_scoped_memory_without_extra_model_calls(tmp_path):
    from pico.providers.clients import FakeModelClient
    from pico.runtime import Pico, SessionStore
    from pico.workspace import WorkspaceContext
    s = EvolutionStore(tmp_path)
    item = propose(s, observation(s))
    s.activate(item['id'], 1, reviewer='operator')
    model = FakeModelClient(['<final>已提供汇报提纲。</final>'])
    agent = Pico(model, WorkspaceContext.build(tmp_path), SessionStore(tmp_path/'.pico/sessions'),
                 harness_profile='workbench', max_depth=0)
    agent.goal_binding = {'goal_id': 'goal', 'task_id': 'task_c'}
    assert agent.ask('准备报告提纲') == '已提供汇报提纲。'
    context = agent.session['working_state']['learning']
    assert context['memories'][0]['id'] == item['id']
    assert agent.current_task_state.attempts == 1
    usage = s.usages(item['id'])
    assert len(usage) == 1 and usage[0]['outcome'] == 'unverified'
    assert not {'activate_learning', 'approve_skill', 'learning_feedback'} & agent.tools.keys()
    assert s.audit()['valid']


def test_real_glm_argument_failures_have_actionable_repair_and_no_side_effect(tmp_path):
    from pico.providers.clients import FakeModelClient
    from pico.providers.native_tools import NativeToolConversation
    from pico.runtime import Pico, SessionStore
    from pico.workspace import WorkspaceContext
    agent = Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path), SessionStore(tmp_path/'.pico/sessions'),
                 harness_profile='workbench', approval_policy='auto', max_depth=0)
    store = agent.capabilities.learning.store
    source = store.observe('actual-user', category='feedback', session_id=agent.session['id'],
                           observation='报告准备先给提纲')
    valid = {'kind': 'memory', 'key': 'report', 'body': json.dumps(MEMORY), 'source_ids': json.dumps([source['id']])}
    malformed = [
        {**valid, 'body': json.dumps({'memory': MEMORY}), 'source_ids': source['id']},
        {**valid, 'source_ids': source['id']},
        {**valid, 'body': json.dumps({'memory': MEMORY})},
    ]
    for args in malformed:
        result = agent.execute_tool('learning_propose', args)
        assert result.metadata['tool_status'] == 'rejected'
        assert 'source_ids' in result.content
        assert store.list(session_id=agent.session['id']) == []
    result = agent.execute_tool('learning_propose', valid)
    assert result.metadata['tool_status'] == 'ok'
    assert store.list(session_id=agent.session['id'])[0]['status'] == 'candidate'
    wire = NativeToolConversation(agent.tools, '', '记住报告偏好', None, require_reasoning=False)
    function = next(t['function'] for t in wire.tools if t['function']['name'] == 'learning_propose')
    assert function['parameters']['properties']['kind']['enum'] == ['memory', 'skill']
    assert 'bare' in function['parameters']['properties']['source_ids']['description']


def test_read_only_sources_anchor_learning_and_revocation_blocks_future_context(tmp_path):
    from pico.providers.clients import FakeModelClient
    from pico.runtime import Pico, SessionStore
    from pico.workspace import WorkspaceContext
    (tmp_path/'notes.txt').write_text('REPORT_UNIQUE_PRIVATE_FACT', encoding='utf-8')
    agent = Pico(FakeModelClient(['<tool>{"name":"read_file","args":{"path":"notes.txt"}}</tool>', '<final>已读取。</final>']),
                 WorkspaceContext.build(tmp_path), SessionStore(tmp_path/'.pico/sessions'), harness_profile='workbench',
                 approval_policy='auto', max_depth=0)
    agent.ask('查看报告资料')
    store = agent.capabilities.learning.store
    source = next(r for r in store.sources(session_id=agent.session['id']) if r['category'] == 'run')
    assert [e['path'] for e in source['evidence']] == ['notes.txt']
    item = store.propose('memory', 'report', {'text': 'REPORT_UNIQUE_PRIVATE_FACT', 'conditions': '报告准备'},
                         [source['id']], scope='session', session_id=agent.session['id'])
    store.activate(item['id'], 1, reviewer='operator')
    (tmp_path/'.pico/policy.json').write_text(json.dumps({'protected_paths': ['notes.txt']}), encoding='utf-8')
    agent.model_client = FakeModelClient(['<final>当前资料访问受限。</final>'])
    agent.ask('报告准备')
    assert agent.session['working_state']['learning']['memories'] == []
    assert all('REPORT_UNIQUE_PRIVATE_FACT' not in p for p in agent.model_client.prompts)


def test_changed_trial_evidence_does_not_qualify_skill_activation(tmp_path):
    (tmp_path/'result.txt').write_text('verified', encoding='utf-8')
    s = EvolutionStore(tmp_path)
    seed = observation(s)
    item = propose(s, seed, kind='skill')
    s.expose(item['id'], 1, run_id='trial', session_id='session', goal_id='goal', task_id='task_b', trial=True)
    receipt = observation(s, name='trial', task='task_b', evidence_paths=['result.txt'])
    s.settle(receipt)
    assert s.transfer_passed(item)
    assert not s.transfer_passed(item, path_filter=lambda p: False)
    (tmp_path/'result.txt').write_text('unverified change', encoding='utf-8')
    assert not s.transfer_passed(item)
    with pytest.raises(ValueError, match='different task'):
        s.activate(item['id'], 1, reviewer='operator')
