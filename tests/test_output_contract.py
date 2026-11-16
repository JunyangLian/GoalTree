"""Requirement provenance and atomic consistency, independent of benchmark gold."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from pico.evidence import EvidenceStore
from pico.goals import GoalStore
from pico.handoff import contracts
from pico.native_history import goal_state_view
from pico.output_contract import normalize, prepare, prepare_step
from pico.providers.clients import FakeModelClient
from pico.web import Workbench
from test_dynamic_goal_tree import submit, tool


FORM = '双语决策备忘录'


def proposal(form=FORM, **kwargs):
    return dict(kind='artifact', format=form, required_in=['title', 'description', 'acceptance'],
                source='request', quote=form, **kwargs)


def agent(root):
    return SimpleNamespace(current_task_state=SimpleNamespace(user_request='请写' + FORM, run_id='r1'),
        evidence=EvidenceStore(root), session={'id': 's1'}, path=lambda p: root / p)


def row(contract):
    return dict(title='编写' + FORM, description='交付' + FORM, acceptance='核对' + FORM,
                verification_mode='manual', output_contract=contract)


def source(a, root, content=FORM, session='s1', name='read_file', status='ok'):
    p = root / 'brief.txt'
    p.write_text(content, encoding='utf-8')
    return a.evidence.put(session, 'r1', name, content, {'tool_status': status,
        'args': {'path': 'brief.txt'}, 'source_paths': ['brief.txt'],
        'source_sha256': hashlib.sha256(p.read_bytes()).hexdigest()})


def test_current_request_quote_is_host_hashed_not_a_fabricated_model_provenance(tmp_path):
    a = agent(tmp_path)
    result = prepare(a, proposal())
    assert result['provenance'] == dict(origin='user_request', source_id='r1', quote=FORM,
        sha256=hashlib.sha256(a.current_task_state.user_request.encode()).hexdigest())
    with pytest.raises(ValueError, match='cannot be forged'):
        prepare(a, result)


@pytest.mark.parametrize('change', [dict(quote='并不存在的要求'), dict(format='报告'), dict(source='unknown')])
def test_unobserved_requirement_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        prepare(agent(tmp_path), {**proposal(), **change})


def test_read_evidence_requires_current_hash_and_session_and_path_authorization(tmp_path):
    a = agent(tmp_path)
    ref = source(a, tmp_path)
    raw = {**proposal(), 'source': ref}
    assert prepare(a, raw)['provenance']['origin'] == 'read_evidence'
    a.evidence.path_filter = lambda p: False
    with pytest.raises(ValueError, match='authorized'):
        prepare(a, raw)
    a.evidence.path_filter = None
    a.session['id'] = 'another_session'
    with pytest.raises(ValueError, match='this session'):
        prepare(a, raw)
    a.session['id'] = 's1'
    (tmp_path / 'brief.txt').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='source changed'):
        prepare(a, raw)


@pytest.mark.parametrize('name,status', [('conversation_deliverable','ok'), ('read_file','failed')])
def test_agent_summary_or_failed_read_cannot_attest_a_source(tmp_path, name, status):
    a = agent(tmp_path)
    ref = source(a, tmp_path, name=name, status=status)
    with pytest.raises(ValueError, match='actual read_file'):
        prepare(a, {**proposal(), 'source': ref})


@pytest.mark.parametrize('value', [None, {'kind': 'evidence'}, {'kind': 'artifact'},
    {**proposal(), 'required_in': []}])
def test_model_cannot_erase_or_weaken_a_committed_contract(tmp_path, value):
    a = agent(tmp_path)
    previous = prepare(a, proposal())
    with pytest.raises(ValueError):
        prepare(a, value, previous)


@pytest.mark.parametrize('field', ['title', 'description', 'acceptance'])
def test_missing_literal_field_causes_atomic_plan_rejection_without_auto_repair(tmp_path, field):
    store = GoalStore(tmp_path)
    g = store.action(dict(action='create_goal', title='采购决策'))
    item = row(normalize(dict(kind='artifact', format=FORM, required_in=['title','description','acceptance'])))
    item[field] = '通用措辞'
    before = store.read()
    with pytest.raises(ValueError, match=field):
        store.plan(g['id'], [dict(title='other'), item], g['revision'])
    assert store.read() == before


def test_real_store_contract_changes_invalidate_drafts_and_survive_reload_in_scoped_context(tmp_path):
    store = GoalStore(tmp_path)
    a = agent(tmp_path)
    g = store.action(dict(action='create_goal', title='采购决策'))
    n = store.action(dict(action='create_task', goal_id=g['id'], **prepare_step(a, row(proposal()))))
    n = store.record_progress(g['id'], n['id'], n['revision'], 'review', 'draft', {'evidence_id': 'ev_actual', 'task_spec_version': 1})
    old_spec = n['spec_version']
    a.current_task_state.user_request = '改为单页决策表'
    update = dict(action='update_task', goal_id=g['id'], task_id=n['id'], revision=n['revision'],
        **{k:v.replace(FORM,'单页决策表') for k,v in row(None).items() if k in {'title','description','acceptance'}},
        output_contract=proposal('单页决策表'))
    n = store.action(prepare_step(a, update, n))
    assert n['spec_version'] == old_spec + 1 and n['status'] == 'planned'
    assert n['deliverables'][0]['task_spec_version'] == 1
    store = GoalStore(tmp_path)
    ctx = store.context(store.binding(g['id'], '', 'assistant'))
    assert goal_state_view(ctx)['artifact_contracts'][0]['format'] == '单页决策表'
    child = store.context(store.binding(g['id'], n['id'], 'execute'))
    assert contracts(child)['task']['output_contract']['format'] == '单页决策表'
    foreign = store.action(dict(action='create_goal', title='另外的目标'))
    assert not store.context(store.binding(foreign['id'], '', 'assistant'))['artifact_contracts']


def test_update_keeps_contract_and_rejects_inconsistent_title_without_changing_version(tmp_path):
    store = GoalStore(tmp_path)
    g = store.action(dict(action='create_goal', title='决策'))
    n = store.action(dict(action='create_task', goal_id=g['id'], **row(normalize(dict(
        kind='artifact', format=FORM, required_in=['title'])))))
    before = store.read()
    with pytest.raises(ValueError, match='title'):
        store.action(dict(action='update_task', goal_id=g['id'], task_id=n['id'], revision=n['revision'], title='other'))
    assert store.read() == before


def test_native_state_serialization_keeps_contract_separate_from_factual_sources(tmp_path):
    a = agent(tmp_path)
    context = dict(goal=dict(id='g'), task=dict(id='t', spec_version=1, output_contract=prepare(a, proposal())),
        artifact_contracts=[dict(task_id='t', **prepare(a, proposal()))])
    view = goal_state_view(context)
    assert view['task']['output_contract']['format'] == FORM
    assert view['artifact_contracts'][0]['provenance']['origin'] == 'user_request'
    assert 'readonly' not in view['task']['output_contract']


def test_preflight_rejects_bad_contract_then_model_can_correct_without_approval_or_gold_lookup(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False, model_factory=lambda: FakeModelClient([]))
    g = app.goals.action(dict(action='create_goal', title='采购决策'))
    invalid = row(proposal())
    invalid['acceptance'] = '未保留要求'
    valid = row(proposal())
    outputs = [tool('plan_tasks', {'steps': json.dumps([invalid], ensure_ascii=False)}),
        tool('plan_tasks', {'steps': json.dumps([valid], ensure_ascii=False)}), '<final>方案已保存，等待复核。</final>']
    job, approvals = submit(app, g, outputs, prompt='请规划' + FORM)
    statuses = [e['tool_status'] for e in job.events if e.get('event')=='tool_executed' and e.get('name')=='plan_tasks']
    assert statuses == ['rejected','ok'] and not approvals
    n = next(iter(app.goals.read()['tasks'].values()))
    assert n['output_contract']['format'] == FORM and n['status'] == 'planned'
    assert n['verification_mode'] == 'manual' and not n.get('acceptance_origin')


@pytest.mark.parametrize('value', [dict(kind=[]), dict(kind='artifact',format='x\ny'),
    dict(kind='execution',format='pdf'), dict(kind='artifact',required_in=['title']),
    dict(kind='artifact',format='pdf',required_in=['title','title']), dict(kind='artifact',permissions='all')])
def test_invalid_contract_schema_has_no_authority_or_permission_fields(value):
    with pytest.raises(ValueError):
        normalize(value)


def test_legacy_nodes_and_provisional_artifacts_remain_compatible(tmp_path):
    store = GoalStore(tmp_path)
    g = store.action(dict(action='create_goal', title='旧会话'))
    n = store.action(dict(action='create_task', goal_id=g['id'], title='旧任务'))
    assert n['output_contract'] is None and n['verification_mode'] == 'configured'
    assert prepare(agent(tmp_path), dict(kind='artifact'))['format'] == ''
