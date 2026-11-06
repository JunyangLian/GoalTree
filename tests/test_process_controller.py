import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from pico.evaluation.agent_session import SessionAudit, run_codetrail, apecode_result_status
from pico.evaluation.cross_agent import snapshot
from pico.evaluation.isolated_execution import IsolatedExecutor, ExecutionUnavailable, cleanup_abandoned
from pico.evaluation.process_scenario import start
from pico.providers.clients import OpenAICompatibleModelClient
from pico.windows_sandbox import VERIFY
from scripts.bigmodel_benchmark_adapter import CONFIG
from scripts.run_process_controller import fixtures, actions, ScriptedHTTP, freeze, check, report


IMAGE = 'python@sha256:'+'a'*64


def test_apecode_unapplied_patch_is_not_a_success_or_a_policy_denial():
    assert apecode_result_status('replace_in_file', 'no replacements made') == 'error'
    assert apecode_result_status('replace_in_file', 'applied replacements in compute.py') == 'ok'
    assert apecode_result_status('read_file', 'file not found: compute.py') == 'error'
    assert apecode_result_status('grep_files', '(no matches)') == 'ok'


def test_isolation_requires_digest_exact_command_and_records_unavailable_without_host_fallback(tmp_path):
    with pytest.raises(ValueError):
        IsolatedExecutor('host', tmp_path/'host')
    with pytest.raises(ValueError):
        IsolatedExecutor('docker', tmp_path/'mutable', 'python:latest')
    executor = IsolatedExecutor('docker', tmp_path/'docker', IMAGE)
    with pytest.raises(ValueError):
        executor.execute(tmp_path, VERIFY+'; echo bypass')
    with patch('pico.evaluation.isolated_execution.docker_preflight', return_value={'ready': False}), \
         patch('pico.evaluation.isolated_execution.subprocess.run') as run:
        with pytest.raises(ExecutionUnavailable):
            executor.execute(tmp_path, VERIFY)
        run.assert_not_called()
    audit = json.loads((tmp_path/'docker/0001/audit.json').read_text())
    assert audit['status'] == 'failed_closed' and audit['host_fallback'] is False


def test_docker_mount_contains_only_filtered_copy_and_cannot_inherit_host_credentials(tmp_path):
    executor = IsolatedExecutor('docker', tmp_path/'audit', IMAGE)
    command = executor.docker_command('docker', tmp_path/'copy', 'codetrail-verify-'+'b'*32)
    assert '--privileged' not in command and '--env' not in command
    for required in ['none', '--read-only', 'ALL', 'no-new-privileges:true', '65534:65534', 'never']:
        assert required in command
    assert command[command.index('--mount')+1].endswith('dst=/workspace,readonly')
    assert command[-6:] == [IMAGE, '-B', '-m', 'unittest', 'discover', '-v']
    with pytest.raises(ValueError):
        executor.docker_command('docker', tmp_path/'x,readonly=false', 'codetrail-verify-'+'b'*32)


def test_container_timeout_always_cleans_only_its_nonce_and_keeps_timeout_audit(tmp_path):
    workspace = tmp_path/'workspace'
    workspace.mkdir(); (workspace/'a.py').write_text('x=1\n')
    executor = IsolatedExecutor('docker', tmp_path/'audit', IMAGE)
    class Result:
        stdout = json.dumps([{'Os': 'linux', 'RepoDigests': [IMAGE], 'Id': 'pinned'}])
        returncode = 0
    calls = []
    def invoke(command, **kwargs):
        calls.append(command)
        assert 'BIGMODEL_API_KEY' not in kwargs['env']
        if command[1] == 'run':
            raise subprocess.TimeoutExpired(command, 1)
        return Result()
    with patch('pico.evaluation.isolated_execution.docker_preflight', return_value={'ready': True}), \
         patch('pico.evaluation.isolated_execution.shutil.which', return_value='docker'), \
         patch('pico.evaluation.isolated_execution.subprocess.run', side_effect=invoke):
        with pytest.raises(subprocess.TimeoutExpired):
            executor.execute(workspace, VERIFY, 1)
    run = next(c for c in calls if c[1] == 'run')
    assert calls[-1] == ['docker', 'rm', '-f', run[run.index('--name')+1]]
    assert json.loads((tmp_path/'audit/0001/audit.json').read_text())['status'] == 'failed_closed'


def test_code_loop_native_calls_observers_denials_and_failed_patch_recovery_are_connected(tmp_path):
    task = fixtures()[0]
    state = start(task, tmp_path/'scenario', 'codetrail')
    workspace = tmp_path/'scenario/workspace'
    class Executor:
        backend = 'scripted_unit_fixture_not_os_test'
        def execute(self, root, command, timeout):
            assert command == VERIFY
            green = 'return a + b' in (root/'compute.py').read_text()
            return {'returncode': 0 if green else 1,
                'output': 'Ran 1 test in 0.01s\n\n'+('OK\n' if green else 'FAILED (failures=1)\n')}
    def factory(directory):
        client = OpenAICompatibleModelClient(CONFIG['model'], CONFIG['base_url'], 'offline-fixture',
                                            1, 10, 'chat_completions')
        client.max_transport_attempts = 1
        return client
    scripted = ScriptedHTTP(actions(task, 'codetrail', 'b'), 'b')
    with patch('urllib.request.urlopen', side_effect=scripted):
        result = run_codetrail(task, workspace, tmp_path/'session', Executor(), 'b', state['session_id'], factory)
    assert result['completed'] and not result['agent_error']
    assert result['baseline_test_observed'] and result['final_test_observed']
    assert result['security_denials'] >= 2
    assert result['input_tokens'] is None and result['output_tokens'] is None
    assert result['process_evidence']['necessary_evidence_coverage']['value'] == 1
    assert (workspace/'test_acceptance.py').read_text() == task['files']['test_acceptance.py']
    tools = [json.loads(s) for s in (tmp_path/'session/tools.jsonl').read_text().splitlines()]
    patches = [r for r in tools if r['name'] == 'patch_file']
    assert patches[0]['before'] == patches[0]['snapshot']
    assert patches[0]['metadata']['tool_status'] != 'ok'
    assert patches[1]['metadata']['tool_status'] == 'ok'
    assert all(r['call_id'] for r in tools)


def test_audit_pairs_real_tool_ids_and_rejects_mismatched_arguments(tmp_path):
    task = fixtures()[0]
    workspace = tmp_path/'workspace'; workspace.mkdir()
    class Executor: backend = 'unit'
    audit = SessionAudit('apecode', task, workspace, tmp_path/'audit', Executor(), 'b')
    audit.wire({'event': 'request', 'payload': {'messages': []}})
    audit.wire({'event': 'response', 'body': {'choices': [{'message': {'reasoning_content': 'fixture', 'tool_calls': [
        {'id': 'read-1', 'function': {'name': 'read_file', 'arguments': '{"path":"compute.py"}'}}]}}]}})
    with pytest.raises(RuntimeError, match='arguments differ'):
        audit.invoke('read_file', {'path': 'test_acceptance.py'}, lambda: None)


def test_parser_rejected_call_is_recorded_as_not_dispatched_instead_of_false_missing_result(tmp_path):
    task = fixtures()[0]
    class Executor: backend = 'unit'
    audit = SessionAudit('codetrail', task, tmp_path, tmp_path/'audit', Executor(), 'b')
    audit.wire({'event': 'request', 'payload': {'messages': []}})
    audit.wire({'event': 'response', 'body': {'choices': [{'message': {'reasoning_content': 'fixture', 'tool_calls': [
        {'id': 'invalid', 'function': {'name': 'read_file', 'arguments': 'not json'}}]}}]}})
    audit.wire({'event': 'request', 'payload': {'messages': [
        {'role': 'tool', 'tool_call_id': 'invalid', 'content': 'Not executed: invalid JSON'}]}})
    assert not audit.pending
    assert json.loads((tmp_path/'audit/protocol.jsonl').read_text())['call_id'] == 'invalid'


def test_report_preserves_interrupted_denominator_and_does_not_produce_benchmark_scores(tmp_path):
    from pico.evaluation.repair_suite import write_json
    write_json(tmp_path/'plan.json', [{'run_key': 'started', 'agent': 'codetrail'},
                                    {'run_key': 'unstarted', 'agent': 'apecode'}])
    (tmp_path/'runs/started').mkdir(parents=True)
    result = report(tmp_path)
    assert result['started_scenarios'] == 1 and result['planned_scenarios'] == 2
    assert result['mechanism_functional_passes'] == 0 and not result['complete']
    assert result['new_model_benchmark_runs'] == 0 and result['overall_score'] is None
    assert result['efficiency']['tokens_observed'] is None
    write_json(tmp_path/'plan.json', [{'run_key': 'started', 'agent': 'codetrail'}])
    assert report(tmp_path)['complete'] is False


@pytest.mark.parametrize('message,finish', [({'reasoning_content': 'partial'}, 'length'), ({}, 'stop')])
def test_nonexecutable_provider_envelope_does_not_poison_native_recovery(tmp_path, message, finish):
    class Executor: backend = 'unit'
    audit = SessionAudit('codetrail', fixtures()[0], tmp_path, tmp_path/'audit', Executor(), 'b')
    audit.wire({'event': 'request', 'payload': {'messages': []}})
    audit.wire({'event': 'response', 'body': {'choices': [{'message': {**message, 'tool_calls': [
        {'id': 'c', 'function': {'name': 'read_file', 'arguments': '{}'}}]}, 'finish_reason': finish}]}})
    audit.wire({'event': 'request', 'payload': {'messages': []}})
    assert not audit.pending
    assert json.loads((tmp_path/'audit/protocol.jsonl').read_text())['event'] == 'response_not_executable'


def test_abandoned_container_recovery_accepts_only_own_lease_names(tmp_path):
    from pico.evaluation.repair_suite import write_json
    name = 'codetrail-verify-'+'d'*32
    write_json(tmp_path/'0001/lease.json', {'container': name, 'image': IMAGE})
    class Result:
        returncode = 0
    with patch('pico.evaluation.isolated_execution.shutil.which', return_value='docker'), \
         patch('pico.evaluation.isolated_execution.subprocess.run', return_value=Result()) as run:
        assert cleanup_abandoned(tmp_path) == [name]
        assert run.call_args[0][0] == ['docker', 'rm', '-f', name]
        assert cleanup_abandoned(tmp_path) == []
    write_json(tmp_path/'0002/lease.json', {'container': 'unrelated-user-container'})
    with pytest.raises(ExecutionUnavailable, match='Unowned'):
        cleanup_abandoned(tmp_path)


def test_unresolved_cleanup_blocks_every_later_execution(tmp_path):
    executor = IsolatedExecutor('docker', tmp_path/'audit', IMAGE)
    executor.poisoned = True
    with patch('pico.evaluation.isolated_execution.subprocess.run') as run:
        with pytest.raises(ExecutionUnavailable):
            executor.execute(tmp_path, VERIFY)
        run.assert_not_called()
    audit = SessionAudit('codetrail', fixtures()[0], tmp_path, tmp_path/'session', executor, 'b')
    with pytest.raises(RuntimeError, match='model request refused'):
        audit.wire({'event': 'request', 'payload': {'messages': []}})
    assert not audit.requests


def test_frozen_controller_rejects_changed_plan_and_has_single_hard_deadline(tmp_path):
    from pico.evaluation.repair_suite import write_json
    source = tmp_path/'upstream'; source.mkdir()
    (source/'agent.py').write_text('original source')
    import scripts.run_process_controller as controller
    from pico.evaluation.repair_suite import digest
    external = {'agent.py': digest(source/'agent.py')}
    class Result: stdout = 'Python 3.13.11\n'
    with patch.object(controller, 'source_info', return_value=external), \
         patch.object(controller.subprocess, 'run', return_value=Result()):
        batch = tmp_path/'batch'
        freeze(batch, source, Path(__import__('sys').executable))
        check(batch)
        protocol = json.loads((batch/'protocol.json').read_text())
        assert protocol['worker_deadline_seconds'] == 645
        assert 'deadline_per_session_seconds' not in protocol
        assert protocol['session_slots'] == 12
        write_json(batch/'plan.json', [])
        with pytest.raises(ValueError, match='asset changed'):
            check(batch)


def test_read_only_inspection_does_not_receive_a_conflicting_mandatory_test_instruction(tmp_path):
    task = fixtures()[1]
    state = start(task, tmp_path/'scenario', 'codetrail')
    clients = []
    class Executor:
        backend = 'unit_no_shell_allowed'
        def execute(self, *args): raise AssertionError('Read-only phase must not execute tests')
    def factory(directory):
        client = OpenAICompatibleModelClient(CONFIG['model'], CONFIG['base_url'], 'offline-fixture',
                                            1, 10, 'chat_completions')
        clients.append(client)
        return client
    with patch('urllib.request.urlopen', side_effect=ScriptedHTTP(actions(task, 'codetrail', 'a'), 'a')):
        result = run_codetrail(task, tmp_path/'scenario/workspace', tmp_path/'session',
                               Executor(), 'a', state['session_id'], factory)
    assert result['completed'] and result['agent_error'] is None
    assert clients[0].native_conversation.verification_command is None
    wire = [json.loads(s) for s in (tmp_path/'session/wire.jsonl').read_text().splitlines()]
    system = next(w['body']['messages'][0]['content'] for w in wire if w['event'] == 'request')
    assert 'Before finalizing obtain exit code 0' not in system


def test_independent_verifier_never_writes_a_controller_supplied_escape_overlay(tmp_path):
    from pico.evaluation.isolated_execution import verify
    class Executor:
        backend = 'unit'
        def execute(self, *args): raise AssertionError('Invalid overlay must not execute')
    task = {'files': {'a.py': 'x=1\n'}, 'editable_files': ['../outside.py'], 'expected_tests': 1}
    with pytest.raises(ValueError, match='escapes root'):
        verify(task, {'../outside.py': b'forged'}, tmp_path/'workspace', Executor())
    assert not (tmp_path/'outside.py').exists()
