"""Small cross-agent evaluation primitives; no agent self-report is a grader."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .repair_suite import digest, install, utcnow, write_json
from ..windows_sandbox import VERIFY, run_verification


TASK_IDS = ('pagination_boundaries', 'user_lookup_contract', 'boltons_isoparse', 'humanize_fractional')
IGNORED = {'.pico', '.git', '__pycache__', '.pytest_cache'}
APECODE_COMMIT = '8d85a3929cac72665e96f10d1dcbcce2962ca49b'


def snapshot(root):
    """Inventory payloads without following reparse points or trusting agent metadata."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            path = base / name
            if name in IGNORED:
                dirs.remove(name)
            elif path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                result[path.relative_to(root).as_posix()] = 'REPARSE_POINT'
                dirs.remove(name)
        for name in files:
            path = base / name
            result[path.relative_to(root).as_posix()] = (
                'REPARSE_POINT' if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
                else digest(path))
    return result


def scope_valid(task, final):
    before = task['sha256']
    changed = {n for n in before.keys() | final.keys() if before.get(n) != final.get(n)}
    return changed <= set(task['editable_files']) and all(
        final.get(n) not in (None, 'REPARSE_POINT') for n in task['editable_files'])


def unittest_result(text, code, expected):
    counts = re.findall(r'^Ran (\d+) tests? in ', text, re.M)
    count = int(counts[-1]) if counts else None
    accepted = code == 0 and count == expected and bool(re.search(r'^OK\s*$', text, re.M))
    return {'accepted': accepted, 'tests_run': count, 'exit_code': code, 'output': text}


def verify_candidate(task, implementation, destination):
    """Fresh frozen tests + eligible final bytes; OS-isolated execution, fail closed."""
    install(destination, task['files'])
    for name, data in implementation.items():
        if name not in task['editable_files']:
            raise ValueError('noneditable external overlay')
        (destination / name).write_bytes(data)
    before = snapshot(destination)
    try:
        raw = run_verification(destination, VERIFY, timeout=25)
        result = unittest_result(raw['output'], raw['returncode'], task['expected_tests'])
    except Exception as exc:
        result = {'accepted': False, 'tests_run': None, 'error': f'{type(exc).__name__}: {exc}'}
    result['workspace_unchanged_during_test'] = before == snapshot(destination)
    result['accepted'] = bool(result['accepted'] and result['workspace_unchanged_during_test'])
    result['backend'] = 'windows_appcontainer'
    result['host_fallback'] = False
    write_json(destination.parent / (destination.name + '-result.json'), result, exclusive=True)
    return result


def clean_environment():
    names = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'HOMEDRIVE',
             'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
             'PROGRAMDATA', 'COMSPEC', 'USERNAME', 'USERDOMAIN', 'PATHEXT', 'OS',
             'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', GIT_TERMINAL_PROMPT='0')
    return env


def kill_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
    else:
        process.terminate()
    process.wait(timeout=15)


def check_tool_permission(workspace, editable, name, args):
    """Controller policy, never execute the upstream unrestricted host shell."""
    if name in {'run_shell', 'exec_command'}:
        return args.get('command') == VERIFY
    mutations = {'write_file', 'patch_file', 'replace_in_file'}
    paths = [args.get('path', '.')] if name in mutations | {'read_file', 'list_files', 'grep_files', 'search'} else []
    if name == 'apply_changes':
        try:
            values = json.loads(args['changes'])
            if not isinstance(values, list) or not values: return False
            paths = [v['path'] for v in values]
        except (KeyError, ValueError, TypeError): return False
    if name in {'rewind_changes', 'delegate', 'mcp_docs'}:
        return False
    root = Path(workspace).resolve()
    for raw in paths:
        try:
            candidate = Path(str(raw))
            if candidate.is_absolute(): return False
            path = (root / candidate).resolve()
            rel = path.relative_to(root).as_posix()
            if any(p in IGNORED or p == '.codetrail' for p in Path(rel).parts): return False
            for part in (root/candidate, *(root/candidate).parents):
                if part == root: break
                if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400): return False
            if path.is_file() and path.stat().st_nlink > 1: return False
            if (name in mutations or name == 'apply_changes') and rel not in editable: return False
        except (ValueError, OSError, TypeError): return False
    return True


def process_metrics(observations, requests, task, final):
    checks = []
    for o in observations:
        if o['name'] in {'run_shell', 'exec_command'} and o['args'].get('command') == VERIFY:
            checks.append({**unittest_result(o['output'], o.get('exit_code'), task['expected_tests']),
                           'before': o.get('before'), 'snapshot': o['snapshot']})
    def tokens(key):
        values = [r.get(key) for r in requests]
        return sum(values) if values and all(isinstance(v, int) for v in values) else None
    return {'tool_steps': len(observations), 'tool_step_unit': 'individual native tool calls, including rejected calls',
            'input_tokens': tokens('input_tokens'), 'output_tokens': tokens('output_tokens'),
            'usage_complete': bool(requests) and all(r.get('complete') for r in requests),
            'model_requests': len(requests), 'agent_tests': checks,
            'baseline_test_observed': any(t['before'] == task['sha256'] == t['snapshot'] and t['tests_run'] == task['expected_tests'] and not t['accepted'] for t in checks),
            'final_test_observed': bool(checks and checks[-1]['accepted'] and checks[-1]['before'] == checks[-1]['snapshot'] == final),
            'security_denials': sum(o.get('rejected', False) for o in observations)}


def summarize(plan, rows):
    """Keep unstarted slots out of attempted denominators, expose both counts."""
    planned_keys = {p['run_key'] for p in plan}
    keys = [r['run_key'] for r in rows]
    if len(planned_keys) != len(plan) or len(set(keys)) != len(keys) or not set(keys) <= planned_keys:
        raise ValueError('Duplicate or unplanned run records')
    grouped = {}
    for agent in sorted({p['agent'] for p in plan}):
        planned = [p for p in plan if p['agent'] == agent]
        actual = [r for r in rows if r['agent'] == agent]
        n = len(actual)
        def stats(name):
            values = sorted(r[name] for r in actual if isinstance(r.get(name), (int, float)))
            return {'observed_runs': len(values), 'total': sum(values) if values else None,
                    'mean': sum(values) / len(values) if values else None}
        grouped[agent] = {'planned_runs': len(planned), 'attempted_runs': n, 'not_started': len(planned)-n,
                          'functional_passes': sum(r['functional_passed'] for r in actual),
                          'functional_rate': sum(r['functional_passed'] for r in actual)/n if n else None,
                          'workflow_passes': sum(r['workflow_passed'] for r in actual),
                          'workflow_rate': sum(r['workflow_passed'] for r in actual)/n if n else None,
                          'failure_categories': dict(Counter(r['failure_category'] for r in actual if r['failure_category'] != 'success')),
                          'elapsed_ms': stats('elapsed_ms'), 'input_tokens': stats('input_tokens'),
                          'output_tokens': stats('output_tokens'), 'tool_steps': stats('tool_steps'),
                          'per_task': {t: {'attempted': len(s), 'functional': sum(r['functional_passed'] for r in s),
                                           'workflow': sum(r['workflow_passed'] for r in s)}
                                       for t in TASK_IDS if (s := [r for r in actual if r['task_id'] == t])}}
    return {'agents': grouped, 'planned_slots': len(plan), 'attempted_runs': len(rows),
            'complete': len(rows) == len(plan), 'cross_agent_conclusion_available': len(rows) == len(plan),
            'comparison_type': 'same-model, policy-adapted harness comparison; tool sets and system prompts differ; no single-feature causal claim'}
