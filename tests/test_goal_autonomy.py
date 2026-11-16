"""Oracle/driver checks are not model success measurements."""
import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.eval_goal_autonomy import ASSET, ROOT, PROTOCOL, allowed, files, hashes, plan
from scripts.verify_goal_autonomy import expected_hashes, final_object, source_matches, verify


def test_protocol_and_public_workspaces_do_not_expose_oracle_or_tool_sequence(tmp_path):
    cases = json.loads(ASSET.read_text('utf-8'))
    assert len(cases) == 3 and len({c['id'] for c in cases}) == 3
    assert PROTOCOL['shell_backend'] == 'appcontainer' and PROTOCOL['transport_attempts'] == 1
    for case in cases:
        steps = plan(case)
        assert len(steps) == 5 and len({s['id'] for s in steps}) == 5
        assert all(not any(t in s['prompt'] for t in ('task_', 'read_file', 'goal_action', 'run_task', 'plan_tasks')) for s in steps)
        assert 'gold' not in json.dumps(files(case))
        assert 'records/retired/data.md' not in files(case)
        assert steps[1]['gold']['count'] != steps[3]['gold']['count']
        for relative, text in files(case).items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode('utf-8'))
        assert hashes(tmp_path) == expected_hashes({'public_files': {case['id']: {'v1': files(case)}}}, case['id'], False)


@pytest.mark.parametrize('name,args', [
    ('run_shell', {'command': 'anything'}), ('write_file', {}), ('confirm_task', {'task_id': 'x'}),
    ('goal_action', {'proposal': '{broken'}),
    ('goal_action', {'proposal': '[]'}),
    ('goal_action', {'proposal': '{"action":"accept_task"}'}),
    ('goal_action', {'proposal': '{"action":"select"}'}),
    ('goal_action', {'proposal': '{"action":"update_task","goal_id":"foreign"}'}),
])
def test_synthetic_approval_does_not_expand_permissions(name, args):
    assert not allowed({'name': name, 'args': args}, 'current')


def test_native_implementation_approval_and_direction_checks_remain_distinct():
    assert allowed({'name': 'plan_tasks', 'args': {}}, 'current')
    assert allowed({'name': 'run_task', 'kind': 'implementation_task', 'args': {}}, 'current')
    assert not allowed({'name': 'run_task', 'kind': 'safety', 'args': {}}, 'current')
    assert allowed({'name': 'goal_action', 'args': {'proposal': '{"action":"update_task"}'}}, 'current')


@pytest.mark.parametrize('text,expected', [
    ('{"count": 39}', {'count': 39}),
    ('说明\n```json\n{"count":39}\n```', {'count': 39}),
    ('{"count":39}\n后来改成\n{"count":27}', {'count': 27}),
    ('{"count":39}\n额外解释', {}),
    ('[{"count":39}]', {}),
])
def test_oracle_uses_final_object_and_never_searches_for_correct_answer(text, expected):
    assert final_object(text) == expected


def test_external_verifier_imports_only_standard_library():
    code = (ROOT / 'scripts/verify_goal_autonomy.py').read_text('utf-8')
    modules = [n.module for n in ast.walk(ast.parse(code)) if isinstance(n, ast.ImportFrom)]
    assert set(modules) <= {'pathlib'}


def oracle_fixture(tmp_path):
    case = json.loads(ASSET.read_text('utf-8'))[0]
    spec = plan(case)[3]
    source = json.dumps([case]).encode()
    asset_name = 'benchmarks/goal_autonomy_v1/cases.json'
    asset_path = tmp_path / 'source-snapshot' / asset_name
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(source)
    freeze = {'protocol': PROTOCOL, 'cases': [case], 'sources': {asset_name: hashlib.sha256(source).hexdigest()},
              'task_asset_sha256': hashlib.sha256(source).hexdigest(),
              'public_files': {case['id']: {'v1': files(case), 'v2': files(case, 'v2')}}}
    state = {'goals': {'own': {'id': 'own'}, 'other': {'id': 'other'}}, 'tasks': {
        'delivery': {'id': 'delivery', 'goal_id': 'own', 'status': 'review', 'acceptance_origin': 'unverified'}}}
    result = {'id': 'r', 'session_id': 's', 'status': 'completed', 'state_before': state, 'state_after': copy.deepcopy(state),
              'file_hashes_after': expected_hashes(freeze, case['id'], True), 'answer': json.dumps(spec['gold']),
              'events': [{'event': 'tool_executed', 'name': 'read_file', 'tool_status': 'ok',
                          'source_paths': ['records/v2/data.md'], 'source_sha256': expected_hashes(freeze, case['id'], True)['records/v2/data.md']}],
              'child_runs': [], 'elapsed_ms': 10}
    directory = tmp_path / case['id'] / spec['id']
    directory.mkdir(parents=True)
    (directory / 'responses').mkdir()
    def save(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), 'utf-8')
    save(tmp_path / 'freeze.json', freeze)
    save(tmp_path / 'slots.json', [{'case': case['id'], 'spec': spec, 'status': 'completed'}])
    save(tmp_path / case['id'] / 'fixture.json', {'goal': {'id': 'own'}})
    save(directory / 'responses/1.json', {'metadata': {'provider_model': PROTOCOL['model'], 'provider_usage': {'prompt_tokens': 10, 'completion_tokens': 4}}})
    save(directory / 'result.json', result)
    return directory, result, save


def test_external_checks_actual_files_tool_receipt_and_foreign_state(tmp_path):
    directory, result, save = oracle_fixture(tmp_path)
    report = verify(tmp_path)
    assert report['passed_turns'] == 1 and report['task_asset_matches']
    result['state_after']['goals']['other']['title'] = 'changed'
    result['file_hashes_after']['records/v2/data.md'] = 'wrong'
    result['events'][0]['source_sha256'] = 'stale'
    save(directory / 'result.json', result)
    report = verify(tmp_path)
    assert report['passed_turns'] == 0
    assert set(report['rows'][0]['failures']) == {'boundary_violation', 'source_evidence_missing'}


def test_unknown_usage_is_not_counted_as_zero_known_usage(tmp_path):
    directory, result, save = oracle_fixture(tmp_path)
    save(directory / 'responses/1.json', {'metadata': {'provider_model': PROTOCOL['model'], 'provider_usage': {}}})
    row = verify(tmp_path)['rows'][0]
    assert row['usage_unknown_requests'] == 1 and row['known_tokens'] == 0


def test_verifier_resolves_same_workspace_spellings_but_not_foreign_root(tmp_path):
    workspace = tmp_path / 'workspace'
    for value in ('records/v2/data.md', '.\\records\\v2\\data.md', str(workspace / 'records/v2/data.md')):
        assert source_matches({'source_paths': [value]}, 'records/v2/data.md', workspace)
    assert not source_matches({'source_paths': [str(tmp_path / 'foreign/records/v2/data.md')]}, 'records/v2/data.md', workspace)


def test_current_submission_check_detects_draft_invalidated_after_recording(tmp_path):
    directory, result, save = oracle_fixture(tmp_path)
    target = result['state_after']['tasks']['delivery']
    target.update(title='三页简报', spec_version=3, deliverables=[{'task_spec_version': 2, 'run_id': 'r'}])
    result['state_before']['tasks']['delivery'].update(title='三页简报', spec_version=2)
    result['run_id'] = 'r'
    save(directory / 'result.json', result)
    report = verify(tmp_path, 'v2.json', current_submissions=True)
    assert not report['rows'][0]['checks']['current_deliverable_submitted']
    target['deliverables'][0]['task_spec_version'] = 3
    save(directory / 'result.json', result)
    assert verify(tmp_path, 'v2.json', current_submissions=True)['rows'][0]['checks']['current_deliverable_submitted']


def test_prepare_is_zero_model_calls_and_exclusive_directory(tmp_path):
    from scripts.eval_goal_autonomy import main
    output = tmp_path / 'prepared'
    assert main(['--output', str(output)]) == 0
    summary = json.loads((output / 'summary.json').read_text('utf-8'))
    assert summary['model_requests'] == 0
    assert not list(output.glob('*/workspace'))
    with pytest.raises(FileExistsError):
        main(['--output', str(output)])
