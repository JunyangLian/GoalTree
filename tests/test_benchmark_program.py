import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pico.evaluation.process_trace import (efficiency, evidence_metrics, normalize_context,
                                          normalize_read)
from pico.evaluation.public_benchmark import agent_input, select, export_predictions, check, docker_preflight, verifier_handoff, HARNESS_COMMIT
from pico.evaluation.repair_suite import digest, write_json
from pico.providers.clients import OpenAICompatibleModelClient
from scripts.bigmodel_benchmark_adapter import BigModelChatModel, CONFIG, make_codetrail_client
from scripts.build_processbench_assets import build, build_cases
from scripts.export_processbench_dataset import export
from pico.evaluation.process_observer import ProcessObserver
from pico.evaluation.process_scenario import start as start_scenario, advance as advance_scenario


def test_public_selection_is_order_invariant_stratified_and_outcome_blind():
    rows = [{'repo': f'org/repo{i}', 'instance_id': f'repo{i}-{j}', 'patch': str(j)}
            for i in range(6) for j in range(4)]
    picked = select(rows)
    assert picked == select(list(reversed(rows)))
    assert len({r['repo'] for r in picked}) == 5
    altered = [{**r, 'patch': 'totally different reference'} for r in rows]
    assert [r['instance_id'] for r in picked] == [r['instance_id'] for r in select(altered)]
    with pytest.raises(ValueError):
        select(rows+rows[:1])


def test_agent_input_cannot_leak_patch_tests_hints_or_future_metadata():
    row = {'instance_id': 'x', 'repo': 'a/b', 'base_commit': 'abc', 'problem_statement': 'repair',
           'patch': 'GOLD', 'test_patch': 'TEST GOLD', 'hints_text': 'hint', 'new_secret_field': 'secret'}
    assert set(agent_input(row)) == {'instance_id', 'repo', 'base_commit', 'problem_statement'}
    assert 'GOLD' not in json.dumps(agent_input(row))


def public_lock(tmp_path):
    write_json(tmp_path/'agent-inputs.json', [], exclusive=True)
    write_json(tmp_path/'selection-lock.json', {'selected': [{'instance_id': 'one'}, {'instance_id': 'two'}],
        'assets': {'agent-inputs.json': digest(tmp_path/'agent-inputs.json')}}, exclusive=True)


def test_prediction_export_keeps_empty_failure_and_rejects_replacement(tmp_path):
    public_lock(tmp_path)
    rows = [{'instance_id': 'one', 'model_patch': '', 'model_name_or_path': 'agent'},
            {'instance_id': 'two', 'model_patch': 'diff', 'model_name_or_path': 'agent'}]
    export_predictions(tmp_path, rows, tmp_path/'predictions.jsonl')
    assert json.loads((tmp_path/'predictions.jsonl').read_text().splitlines()[0])['model_patch'] == ''
    with pytest.raises(ValueError):
        export_predictions(tmp_path, rows[:1], tmp_path/'missing.jsonl')
    with pytest.raises(ValueError):
        export_predictions(tmp_path, rows[:1]*2, tmp_path/'duplicated.jsonl')
    with pytest.raises(FileExistsError):
        export_predictions(tmp_path, rows, tmp_path/'predictions.jsonl')
    (tmp_path/'agent-inputs.json').write_text('["tampered"]')
    with pytest.raises(ValueError):
        check(tmp_path)


def test_missing_docker_never_executes_host_tests():
    with patch('pico.evaluation.public_benchmark.shutil.which', return_value=None), \
         patch('pico.evaluation.public_benchmark.subprocess.run') as run:
        assert docker_preflight() == {'ready': False, 'reason': 'docker_cli_missing', 'host_fallback': False}
        run.assert_not_called()


def test_official_handoff_retains_blocked_record_and_does_not_fallback(tmp_path):
    public_lock(tmp_path)
    export_predictions(tmp_path, [{'instance_id': k, 'model_patch': '', 'model_name_or_path': 'offline'}
        for k in ('one', 'two')], tmp_path/'p.jsonl')
    write_json(tmp_path/'verifier-only.json', [], exclusive=True)
    class Result:
        def __init__(self, value): self.stdout = value
    destination = tmp_path/'blocked'
    with patch('pico.evaluation.public_benchmark.subprocess.run', side_effect=[Result(HARNESS_COMMIT), Result('')]) as run, \
         patch('pico.evaluation.public_benchmark.docker_preflight', return_value={'ready': False}):
        assert verifier_handoff(tmp_path, tmp_path/'p.jsonl', tmp_path, 'python', destination) is False
        assert run.call_count == 2  # git checks only; neither host tests nor grader invoked.
    assert json.loads((destination/'handoff.json').read_text())['official_grading_completed'] is False
    assert (destination/'predictions.jsonl').is_file()


@pytest.mark.parametrize('agent,args,output', [
    ('codetrail', {'path': './logic.py', 'start': 1, 'end': 2}, '1: value = 1\n2: other = 2'),
    ('apecode', {'path': 'logic.py', 'start_line': 1, 'num_lines': 2}, '     1\tvalue = 1\n     2\tother = 2')])
def test_both_actual_read_formats_have_equivalent_evidence(agent, args, output):
    source = 'value = 1\nother = 2\n'
    read = normalize_read(agent, 'call1', args, output, source)
    context = normalize_context('req2', [{'role': 'tool', 'tool_call_id': 'call1', 'content': output}], [read])
    gold = {'required_files': ['logic.py'], 'lines': [context['snippets'][0]]}
    result = evidence_metrics([read, context], gold)
    assert result['recall_at_5']['value'] == result['necessary_evidence_coverage']['value'] == 1
    assert result['immediate_context_loss']['value'] == 0
    assert result['repeated_reads']['value'] == 0


def test_context_measurement_uses_actual_wire_tool_id_not_final_prose_or_stale_version():
    read = normalize_read('codetrail', 'r1', {'path': 'a.py'}, '1: value=1', 'value=1\n')
    gold = {'required_files': ['a.py'], 'lines': [read['snippets'][0]]}
    prose = normalize_context('m2', [{'role': 'assistant', 'content': '1: value=1'},
        {'role': 'tool', 'tool_call_id': 'wrong-id', 'content': '1: value=1'}], [read])
    result = evidence_metrics([read, prose], gold)
    assert result['recall_at_5']['value'] == 1
    assert result['necessary_evidence_coverage']['value'] == 0
    assert result['immediate_context_loss']['value'] == 1
    assert evidence_metrics([read], gold)['necessary_evidence_coverage'] is None
    changed = normalize_read('codetrail', 'r1', {'path': 'a.py'}, '1: value=2', 'value=2\n')
    visible = normalize_context('m', [{'role': 'tool', 'tool_call_id': 'r1', 'content': '1: value=2'}], [changed])
    assert evidence_metrics([changed, visible], gold)['necessary_evidence_coverage']['value'] == 0


def test_truncated_read_does_not_count_unreturned_lines_and_edit_cuts_off_evidence():
    read = normalize_read('codetrail', 'c', {'path': 'a.py'}, '1: x=1', 'x=1\ny=2\n')
    sha = hashlib.sha256(b'x=1\ny=2\n').hexdigest()
    gold = {'required_files': ['a.py'], 'lines': [{'path': 'a.py', 'line': 2, 'text': 'y=2', 'source_sha256': sha}]}
    context = normalize_context('m', [{'role': 'tool', 'tool_call_id': 'c', 'content': '1: x=1'}], [read])
    result = evidence_metrics([read, read, context], gold)
    assert result['necessary_evidence_coverage']['value'] == 0
    assert result['immediate_context_loss']['value'] is None
    assert result['repeated_reads']['value'] == .5
    assert evidence_metrics([{'kind': 'edit', 'changed': True}, read, context], gold)['recall_at_5']['value'] == 0


def test_unknown_cost_does_not_become_zero_and_failures_cost_tokens():
    assert efficiency([{'functional_passed': True, 'total_tokens': None}])['tokens_per_success'] is None
    result = efficiency([{'functional_passed': True, 'total_tokens': 20, 'elapsed_ms': 10},
                         {'functional_passed': False, 'total_tokens': 40, 'elapsed_ms': 30}])
    assert result['tokens_per_success'] == 60 and result['p50_ms'] == 20
    assert result['currency_cost'] is None and result['overall_score'] is None
    assert efficiency([{'elapsed_ms': float('nan')}])['elapsed_observed_runs'] == 0


def test_process_assets_have_disjoint_faults_precise_versions_and_explicit_synthetic_origin():
    tasks, references = build_cases()
    assert len(tasks) == len({t['root_fault_id'] for t in tasks}) == 12
    for split in ('dev', 'test'):
        assert len([t for t in tasks if t['split'] == split]) == 6
    for task in tasks:
        assert task['origin']['kind'] == 'author_created_synthetic'
        assert task['expected_tests'] == 3
        assert set(references[task['id']]) <= set(task['editable_files'])
        assert task['sha256'] == {n: hashlib.sha256(t.encode()).hexdigest() for n, t in task['files'].items()}
        assert 'controller-only-reference' not in json.dumps(task['files'])
        for line in task['gold']['lines']:
            assert task['files'][line['path']].splitlines()[line['line']-1] == line['text']
        if task['phase_a']:
            for path in task['phase_a'].get('stale_paths', []):
                assert task['phase_a']['files'][path] != task['files'][path]


def test_hf_export_excludes_reference_repairs_marks_draft_and_rejects_changed_assets(tmp_path):
    assets, qualification, destination = [tmp_path/n for n in ('assets', 'checks', 'hf')]
    tasks = build(assets)
    write_json(qualification/'summary.json', {'asset_lock_sha256': digest(assets/'asset-lock.json'),
        'qualified': 12, 'scenarios': 12, 'agent_runs': 0}, exclusive=True)
    files = export(assets, qualification, destination)
    manifest = json.loads((destination/'manifest.json').read_text())
    assert manifest['publication_ready'] is False and manifest['model_runs'] == 0
    for split in ('dev', 'test'):
        rows = [json.loads(s) for s in (destination/'data'/f'{split}.jsonl').read_text().splitlines()]
        assert len(rows) == 6 and all('gold' not in r for r in rows)
    assert not any('reference' in name for name in files)
    assert all(digest(destination/n) == sha for n, sha in files.items())
    (assets/'tasks.json').write_text('[]')
    with pytest.raises(ValueError):
        export(assets, qualification, tmp_path/'changed')


class Response:
    headers = {'Content-Type': 'application/json'}
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.body).encode()


def test_bigmodel_apecode_adapter_preserves_native_reasoning_and_pins_official_standard_endpoint(tmp_path):
    history = [{'role': 'assistant', 'content': None, 'reasoning_content': 'previous reasoning',
        'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c', 'content': 'actual result'}]
    body = {'model': 'glm-5.3-flash', 'choices': [{'finish_reason': 'stop', 'message':
        {'role': 'assistant', 'content': 'done', 'reasoning_content': 'current reasoning'}}],
        'usage': {'prompt_tokens': 12, 'completion_tokens': 4}}
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'private-fixture-credential'}):
        model = BigModelChatModel(tmp_path)
        with patch('urllib.request.urlopen', return_value=Response(body)) as send:
            returned = model.complete(messages=history, tools=[])
    request = send.call_args[0][0]
    payload = json.loads(request.data)
    assert request.full_url == 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
    assert payload['messages'] == history and payload['thinking']['clear_thinking'] is False
    assert payload['reasoning_effort'] == 'max' and payload['model'] == 'glm-5.3-flash'
    assert returned['reasoning_content'] == 'current reasoning'
    assert model.requests[0]['complete']
    assert 'private-fixture-credential' not in (tmp_path/'wire.jsonl').read_text()


@pytest.mark.parametrize('model_name,reasoning,finish', [
    ('other-model', 'thinking', 'stop'), ('glm-5.3-flash', None, 'stop'),
    ('glm-5.3-flash', 'thinking', 'length')])
def test_bigmodel_mismatch_missing_reasoning_and_truncation_fail_closed(tmp_path, model_name, reasoning, finish):
    body = {'model': model_name, 'choices': [{'finish_reason': finish, 'message': {'reasoning_content': reasoning}}]}
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'private-fixture-credential'}):
        model = BigModelChatModel(tmp_path)
        with patch('urllib.request.urlopen', return_value=Response(body)), pytest.raises(RuntimeError):
            model.complete(messages=[], tools=[])


def test_bigmodel_codetrail_uses_existing_native_conversation_without_siliconflow_flags():
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'private-fixture-credential'}):
        client = make_codetrail_client()
    assert client.supports_native_tools and not client.siliconflow_deepseek
    assert client.native_wire_options == {'thinking': {'type': 'enabled', 'clear_thinking': False}, 'reasoning_effort': 'max'}
    assert client.max_transport_attempts == 1 and client.stream is False
    client.begin_native_run({}, 'context', 'repair', 'python -B -m unittest discover -v')
    metadata = {}
    reply = client.native_conversation.accept({'choices': [{'finish_reason': 'stop', 'message':
        {'role': 'assistant', 'content': 'done', 'reasoning_content': 'thinking'}}]}, metadata)
    assert reply.action_kind == 'final'
    assert not OpenAICompatibleModelClient('glm-unknown', CONFIG['base_url'], '', 1, 30, 'chat_completions').supports_native_tools


def test_bigmodel_codetrail_actual_complete_pins_model_and_replays_reasoning():
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'private-fixture-credential'}):
        client = make_codetrail_client()
    client.begin_native_run({'read_file': {'description': 'read', 'schema': {'path': 'str'}}},
        'context', 'repair', 'python -B -m unittest discover -v')
    body = {'model': 'glm-5.3-flash', 'choices': [{'finish_reason': 'tool_calls', 'message':
        {'role': 'assistant', 'content': None, 'reasoning_content': 'first thinking', 'tool_calls': [
            {'id': 'call1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"a.py"}'}}]}}]}
    with patch('urllib.request.urlopen', return_value=Response(body)) as first:
        client.complete('prompt', max_new_tokens=4096)
    assert first.call_args[0][0].full_url == 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
    assert json.loads(first.call_args[0][0].data)['thinking']['clear_thinking'] is False
    client.observe_native_tool('1: value=1')
    final = {'model': 'glm-5.3-flash', 'choices': [{'finish_reason': 'stop', 'message':
        {'role': 'assistant', 'content': 'done', 'reasoning_content': 'next thinking'}}]}
    with patch('urllib.request.urlopen', return_value=Response(final)) as second:
        client.complete('prompt', max_new_tokens=4096)
    wire = json.loads(second.call_args[0][0].data)
    assert any(m.get('reasoning_content') == 'first thinking' for m in wire['messages'])
    final['model'] = 'other-model'
    with patch('urllib.request.urlopen', return_value=Response(final)), pytest.raises(RuntimeError, match='model mismatch'):
        client.complete('prompt', max_new_tokens=4096)


def test_shared_observer_persists_only_attributed_spans_and_rejects_id_reuse(tmp_path):
    text = 'value=1\n'
    sha = hashlib.sha256(text.encode()).hexdigest()
    gold = {'required_files': ['a.py'], 'lines': [
        {'path': 'a.py', 'line': 1, 'text': 'value=1', 'source_sha256': sha}]}
    observer = ProcessObserver('codetrail', tmp_path/'trace', gold)
    observer.tool_result('c1', 'read_file', {'path': 'a.py'}, '1: value=1', source_text=text)
    observer.outbound_request('m1', {'messages': [{'role': 'tool', 'tool_call_id': 'c1', 'content': '1: value=1'}]})
    assert observer.finish()['necessary_evidence_coverage']['value'] == 1
    assert (tmp_path/'trace/events.jsonl').is_file()
    with pytest.raises(ValueError):
        observer.outbound_request('m1', {'messages': []})
    with pytest.raises(ValueError):
        observer.tool_result('c1', 'read_file', {'path': 'a.py'}, '1: value=1', source_text=text)


def test_cross_session_transition_keeps_workspace_identity_but_refreshes_source_and_session(tmp_path):
    tasks, _ = build_cases()
    task = next(t for t in tasks if t['id'] == 'dev_retry_policy_change')
    destination = tmp_path/'trial'
    first = start_scenario(task, destination, 'codetrail')
    memory = destination/'workspace/.pico/memory'
    memory.mkdir(parents=True)
    (memory/'fixture-native-memory').write_text('old evidence')
    second = advance_scenario(task, destination, first['session_id'])
    assert first['session_id'] != second['session_id']
    assert (memory/'fixture-native-memory').read_text() == 'old evidence'
    assert (destination/'workspace/policy.py').read_text() == 'MAX_TRIES = 4\n'
    assert second['model_runs'] == 0
    assert 'policy.py' in second['controller_interventions'][0]['changed_paths']
    with pytest.raises(ValueError):
        advance_scenario(task, destination, first['session_id'])
    with pytest.raises(FileExistsError):
        start_scenario(task, destination, 'codetrail')


def test_phase_a_tampering_is_preserved_and_blocks_source_transition(tmp_path):
    tasks, _ = build_cases()
    task = next(t for t in tasks if t['id'] == 'test_quota_policy_change')
    first = start_scenario(task, tmp_path/'trial', 'apecode')
    path = tmp_path/'trial/workspace/limits.py'
    path.write_text('tampered source')
    with pytest.raises(ValueError, match='tampering'):
        advance_scenario(task, tmp_path/'trial', first['session_id'])
    assert path.read_text() == 'tampered source'
    assert first['native_persistent_memory_supported'] is False
