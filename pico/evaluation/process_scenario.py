"""Fresh scenario workspaces and audited cross-session source transitions.

Only a controller calls these functions after stopping the previous session.
The same workspace identity preserves native memory ownership; it never copies
knowledge or transcripts from another run. This is not a model execution loop.
"""
from pathlib import Path
import hashlib
import json
import os
import uuid

from .cross_agent import snapshot
from .repair_suite import install, load_json, safe_path, write_json


def task_digest(task):
    return hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def start(task, destination, agent):
    if agent not in {'codetrail', 'apecode'}:
        raise ValueError('Unknown Agent')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    files = task['phase_a']['files'] if task.get('phase_a') else task['files']
    install(destination/'workspace', files)
    phase = 'a' if task.get('phase_a') else 'b'
    state = {'schema_version': 1, 'task_id': task['id'], 'agent': agent, 'phase': phase,
        'task_sha256': task_digest(task),
        'session_id': uuid.uuid4().hex, 'initial_files': snapshot(destination/'workspace'),
        'controller_interventions': [], 'model_runs': 0,
        'memory_policy': 'same scenario only; new session/client; no imported transcripts',
        'native_persistent_memory_supported': agent == 'codetrail'}
    write_json(destination/'scenario-state.json', state, exclusive=True)
    return state


def advance(task, destination, completed_session_id):
    destination = Path(destination).resolve()
    state = load_json(destination/'scenario-state.json')
    if state['task_id'] != task['id'] or state['task_sha256'] != task_digest(task) or state['phase'] != 'a' or state['session_id'] != completed_session_id:
        raise ValueError('Wrong scenario/session transition')
    workspace = destination/'workspace'
    if snapshot(workspace) != state['initial_files']:
        raise ValueError('Phase A source tampering; preserve workspace and refuse transition')
    # Reject any reparse component before controller file operations.
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        for name in dirs+files:
            path = Path(directory)/name
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse point in scenario workspace')
    # Phase A files must be a subset of the next snapshot. Unexpected deletion
    # would require an explicit future protocol; never recursively delete a run.
    if not set(state['initial_files']) <= set(task['files']):
        raise ValueError('Unspecified phase A file removal')
    before = snapshot(workspace)
    for name, text in task['files'].items():
        path = safe_path(workspace, name)
        if path.exists() and path.stat().st_nlink > 1:
            raise ValueError('Hardlink in controller transition')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
    after = snapshot(workspace)
    if after != task['sha256']:
        raise ValueError('Phase B snapshot mismatch')
    # Native memory stays at this identity; previous session state is not loaded
    # into the new model client. No confirmations or approvals are transferred.
    next_state = {**state, 'phase': 'b', 'session_id': uuid.uuid4().hex,
        'initial_files': after, 'previous_session_id': completed_session_id,
        'controller_interventions': [{'before': before, 'after': after,
            'changed_paths': sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))}]}
    write_json(destination/'phase-b-transition.json', next_state, exclusive=True)
    write_json(destination/'scenario-state.json', next_state)
    return next_state
