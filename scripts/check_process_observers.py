"""Real tool-output observer integration; scripted decisions, not model evaluation."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.process_observer import ProcessObserver
from pico.evaluation.repair_suite import write_json


def check(output, source):
    output, source = Path(output).resolve(), Path(source).resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output/'workspace'
    workspace.mkdir()
    text = 'def answer():\n    return 42\n'
    (workspace/'sample.py').write_text(text, encoding='utf-8', newline='\n')
    sha = hashlib.sha256(text.encode()).hexdigest()
    gold = {'required_files': ['sample.py'], 'lines': [
        {'path': 'sample.py', 'line': i, 'text': line, 'source_sha256': sha}
        for i, line in enumerate(text.splitlines(), 1)]}
    # ApeCode's real default tool registry, no model and no shell execution.
    sys.path.insert(0, str(source/'src'))
    from apecode.tools import ToolContext, create_default_registry
    from scripts.run_cross_agent_pilot import source_info
    pinned_hashes = source_info(source)
    registry = create_default_registry(ToolContext(cwd=workspace, ask_approval=lambda *args: False))
    raw = registry.execute('read_file', json.dumps({'path': 'sample.py'}))
    observer = ProcessObserver('apecode', output/'apecode-observation', gold)
    observer.tool_result('actual-read', 'read_file', {'path': 'sample.py'}, raw, source_text=text)
    observer.outbound_request('actual-request', {'messages': [
        {'role': 'tool', 'tool_call_id': 'actual-read', 'content': raw}]})
    metrics = observer.finish()
    assert metrics['necessary_evidence_coverage']['value'] == 1
    from pico.tool_context import ToolContext as CodeTrailContext
    from pico.tools import tool_read_file
    context = CodeTrailContext(root=workspace, path_resolver=lambda p: workspace/p,
        shell_env_provider=lambda: {}, depth=0, max_depth=0, spawn_delegate=lambda args: '')
    native_raw = tool_read_file(context, {'path': 'sample.py'})
    native = ProcessObserver('codetrail', output/'codetrail-observation', gold)
    native.tool_result('native-read', 'read_file', {'path': 'sample.py'}, native_raw, source_text=text)
    native.outbound_request('native-request', {'messages': [
        {'role': 'tool', 'tool_call_id': 'native-read', 'content': native_raw}]})
    assert native.finish()['necessary_evidence_coverage']['value'] == 1
    # A compacted next request really omits a returned line. Its observer must
    # measure loss rather than assume the pre-compaction tool output was sent.
    compacted = ProcessObserver('apecode', output/'compacted-observation', gold)
    compacted.tool_result('actual-read', 'read_file', {'path': 'sample.py'}, raw, source_text=text)
    compacted.outbound_request('compacted-request', {'messages': [
        {'role': 'tool', 'tool_call_id': 'actual-read', 'content': raw.splitlines()[0]}]})
    loss = compacted.finish()
    assert loss['necessary_evidence_coverage']['value'] == .5
    assert loss['immediate_context_loss']['value'] == .5
    result = {'status': 'passed', 'actual_upstream_read_tool': True, 'actual_codetrail_read_tool': True, 'api_calls': 0,
        'agent_task_runs': 0, 'scripted': True, 'full_span_coverage': 1,
        'upstream_source_hashes': pinned_hashes,
        'deliberate_context_omission_detected': True,
        'scope': 'observer integration probe; not actual model compression performance'}
    write_json(output/'summary.json', result, exclusive=True)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--apecode-source', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(check(a.output, a.apecode_source)))
