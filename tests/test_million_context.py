import copy
import json
import pytest
from pico.native_context import NativeContextBudget, ContextBudgetExceeded
from pico.native_history import goal_state_view, historical_requests


class Counter:
    counting = 'fixture_estimated_tokens'
    identity = {'fixture': True}
    def count(self, payload):
        return sum(len(m.get('content', '')) for m in payload['messages']) // 4 + 16


def test_token_budget_and_wire_bytes_are_independent_and_output_is_reserved():
    policy = NativeContextBudget(8_000_000, 16_000_000, token_counter=Counter(),
                                 input_token_budget=1_032_192, token_context_window=1_048_576)
    payload = {'messages': [{'role': 'user', 'content': 'x' * 4_000_000}], 'max_tokens': 8192}
    result, audit = policy.prepare(payload, 1)
    assert len(result['messages'][0]['content']) == 4_000_000
    assert audit['after_estimate'] > 1_000_000
    assert audit['wire_bytes_after'] > 4_000_000
    assert audit['budget_unit'] == 'estimated_tokens'
    with pytest.raises(ContextBudgetExceeded):
        policy.prepare({**payload, 'max_tokens': 100000}, 1)


def test_byte_ceiling_still_rejects_payload_with_tiny_token_count():
    p = NativeContextBudget(2048, 4096, token_counter=Counter(), input_token_budget=1_032_192,
                            token_context_window=1_048_576)
    with pytest.raises(ContextBudgetExceeded):
        p.prepare({'messages': [{'role': 'user', 'content': 'x'*3000}]}, 1)


def test_goal_projection_keeps_contract_and_cooperation_once_not_old_previews():
    contract = {'id': 'g', 'spec_version': 1, 'constraints': 'UNIQUE_CURRENT_CONSTRAINT', 'acceptance': 'verify', 'context_notes': 'facts'}
    context = {'goal': contract, 'continuity': {'contracts': [contract]}, 'rules': 'duplicate policy',
               'cooperation': {'planning': 'step'}, 'recent_deliverables': [{'evidence_id': 'ev_actual', 'preview': 'old prose'*1000}]}
    original = copy.deepcopy(context)
    view = goal_state_view(context)
    wire = json.dumps(view)
    assert wire.count('UNIQUE_CURRENT_CONSTRAINT') == 1
    assert 'old prose' not in wire and 'ev_actual' in wire
    assert view['cooperation'] == context['cooperation'] and context == original


def test_historical_request_keeps_full_recoverable_evidence_in_own_session(tmp_path):
    from types import SimpleNamespace
    from pico.evidence import EvidenceStore
    evidence = EvidenceStore(tmp_path)
    content = 'early text '*500 + 'CRITICAL_AT_END'
    agent = SimpleNamespace(session={'id': 'own', 'history': [{'role': 'user', 'content': content}, {'role':'user','content':'current'}]}, evidence=evidence)
    rows = historical_requests(agent)
    assert content in rows[0]['content']
    ref = next(iter(agent.session['native_user_history_refs'].values()))
    assert evidence.get(ref, 'own')['content'] == content
    with pytest.raises(ValueError): evidence.get(ref, 'other')
    from pico.native_context import group_fact
    assert group_fact(rows)[0]['evidence_id'] == ref


def test_large_privacy_scan_still_detects_tail_credential_and_email():
    from pico.privacy import inspect_text, PrivacyDenied
    text = 'a' * 1_000_000
    assert inspect_text(text)[0] == 'public'
    assert inspect_text(text + ' synthetic@example.com')[0] == 'sensitive'
    assert inspect_text(text + ' GT_CORE_MILLIONCANARY12345')[0] == 'core'
    with pytest.raises(PrivacyDenied): inspect_text('x' * 16_777_217)


def test_workspace_inherits_token_window_counter_without_weakening_tools(tmp_path):
    from pico.web import Workbench
    from pico.providers.clients import FakeModelClient
    def factory():
        model = FakeModelClient([])
        model.context_counter = Counter()
        model.input_token_budget = 1_032_192
        model.token_context_window = 1_048_576
        return model
    app = Workbench(tmp_path, model_factory=factory, load_env=False, harness_profile='workbench',
                    native_input_budget=8_000_000, native_context_window=16_000_000)
    agent = app._build_agent(None, 'ask', 3)
    assert agent.native_context_policy.token_context_window == 1_048_576
    assert agent.approval_policy == 'ask'
    assert app.config()['native_counting'] == Counter.counting


def test_dynamic_mock_is_not_an_explicit_token_configuration():
    from unittest.mock import MagicMock
    from pico.context_tokens import policy_for_client
    assert policy_for_client(MagicMock(), 32768, 65536).token_counter is None


def test_scoped_assistant_output_is_optional_evidence_not_user_correction(tmp_path):
    from types import SimpleNamespace
    from pico.evidence import EvidenceStore
    from pico.native_history import historical_deliverables
    from pico.native_context import group_fact
    store = EvidenceStore(tmp_path)
    ref = store.put('own', 'run1', 'conversation_deliverable', 'Domain workflow: inspect batch effects before differential analysis.', {})
    agent = SimpleNamespace(session={'id':'own'}, evidence=store,
        goal_context={'recent_deliverables':[{'evidence_id':ref,'preview':'not trusted cached preview'}]})
    rows = historical_deliverables(agent)
    assert 'batch effects' in rows[0]['content'] and 'not trusted cached preview' not in rows[0]['content']
    facts = group_fact(rows)
    assert facts[0]['evidence_id'] == ref and 'notice' not in facts[0]
    agent.session['id']='foreign'
    with pytest.raises(ValueError): historical_deliverables(agent)


def test_read_coverage_uses_returned_lines_not_requested_range_or_preview():
    from pico.native_context import group_fact, read_coverage
    source_hash = 'b' * 64
    text = '# report.txt (3 total lines) sha256=' + source_hash + '\n   1: first\n   2: second\n   3: third'
    def fact(end):
        return group_fact([{'role':'assistant','tool_calls':[{'id':'read','function':{
            'name':'read_file','arguments':json.dumps({'path':'report.txt','start':1,'end':end})}}]},
            {'role':'tool','tool_call_id':'read','content':text}])[0]
    assert fact(200)['read_coverage'] == fact(3)['read_coverage']
    assert fact(3)['read_coverage']['full_file_returned']
    assert fact(3)['read_coverage']['source_sha256'] == source_hash
    partial = read_coverage(text.rsplit('\n', 1)[0] + '\n[Unread lines 3..3; use read_file]')
    assert partial['returned_ranges'] == [[1,2]] and not partial['full_file_returned']
    clipped = read_coverage('# x (1 total lines)\n   1: prefix [line truncated; narrow the source with search]')
    assert clipped['returned_ranges'] == [] and not clipped['full_file_returned']
    assert read_coverage('Error: access denied') is None


def test_historical_questions_do_not_displace_current_source_coverage():
    from pico.continuity import select_events
    history = [{'notice':f'Historical user request #{i} (may be superseded): old topic {i}'} for i in range(10)]
    reads = [{'tool':'read_file','path':f'sources/note_{i:02d}.txt','call_id':str(i),
              'read_coverage':{'full_file_returned':True},'result_preview':'public source'} for i in range(5,9)]
    selected, _ = select_events(history + reads, 'Read sources/note_05.txt sources/note_06.txt sources/note_07.txt sources/note_08.txt')
    assert {e.get('call_id') for e in selected if e.get('tool') == 'read_file'} == {'5','6','7','8'}
    # Real runtime corrections retain their high priority, including old ones.
    correction = {'notice':'Runtime notice: previous write was rejected; no change occurred'}
    selected, _ = select_events([correction] + history + reads, 'read sources')
    assert any(e.get('notice') == correction['notice'] for e in selected)


def test_scrub_snapshots_environment_once_per_artifact_and_observes_rotation(monkeypatch):
    from pico import privacy
    class Environment(dict):
        snapshots = 0
        def items(self):
            self.snapshots += 1
            return super().items()
    environment = Environment(TEST_API_KEY='synthetic_first_credential')
    monkeypatch.setattr(privacy.os, 'environ', environment)
    value = {'rows':[{'text':'synthetic_first_credential', 'secret':'anything',
                      'tuple':('GT_CORE_ABCDEFGHIJKLMN', 'safe')} for _ in range(100)]}
    result = privacy.scrub(value, force=True)
    assert environment.snapshots == 1
    assert result['rows'][0] == {'text':'<redacted>','secret':'<redacted>','tuple':('<redacted>','safe')}
    assert value['rows'][0]['text'] == 'synthetic_first_credential'
    environment['TEST_API_KEY'] = 'synthetic_rotated_credential'
    assert privacy.scrub('synthetic_rotated_credential', force=True) == '<redacted>'
    assert environment.snapshots == 2


def test_web_progress_is_durable_without_rewriting_task_snapshot(tmp_path, monkeypatch):
    from pico.providers.clients import FakeModelClient
    from pico.web import Workbench
    class ProgressModel(FakeModelClient):
        on_progress = None
        def complete(self, *args, **kwargs):
            for _ in range(12):
                self.on_progress({'phase':'analysis'})
            return super().complete(*args, **kwargs)
    app = Workbench(tmp_path, model_factory=lambda:ProgressModel(['<final>Completed offline integration check.</final>']),
                    load_env=False, harness_profile='workbench', privacy_protection=True)
    writes = []
    persist = app._persist_task
    def observed(task):
        writes.append(task.events[-1]['event'] if task.events else None)
        return persist(task)
    monkeypatch.setattr(app, '_persist_task', observed)
    task = app.task(app.submit({'prompt':'Only answer, no tools.'})['id'])
    with task.condition:
        assert task.condition.wait_for(lambda:task.finished is not None and app.active_id is None, timeout=20)
    view = task.view()
    assert view['status'] == 'completed'
    assert 'model_progress' not in writes
    assert sum(e['event']=='model_progress' for e in view['events']) == 12
    trace = app.root/'.pico/runs'/view['run_id']/'trace.jsonl'
    assert sum(json.loads(line)['event']=='model_progress' for line in trace.read_text().splitlines()) == 12
    stored = json.loads((app.root/'.pico/web/tasks'/f'{task.id}.json').read_text())
    assert sum(e['event']=='model_progress' for e in stored['events']) == 12
