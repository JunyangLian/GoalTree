"""Two original Agent loops, one isolated policy and one wire/process observer."""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import patch

from .cross_agent import check_tool_permission, process_metrics, snapshot
from .process_observer import ProcessObserver
from .repair_suite import append_json, write_json, utcnow
from ..tool_executor import ToolExecutionResult
from ..windows_sandbox import VERIFY
from ..providers.glm_protocol import valid_optional_reasoning


def apecode_result_status(name, content):
    failed = content.startswith(('error:', 'Tool execution failed:', 'Invalid tool arguments:',
        'Unknown tool:', 'file not found:', 'path does not exist:'))
    if name == 'replace_in_file' and content == 'no replacements made':
        failed = True
    return 'error' if failed else 'ok'


class SessionAudit:
    def __init__(self, agent, task, workspace, directory, executor, phase, max_tools=20):
        self.agent, self.task, self.workspace = agent, task, Path(workspace)
        self.directory, self.executor, self.phase = Path(directory), executor, phase
        self.observer = ProcessObserver(agent, self.directory/'process', task['gold'])
        self.pending = deque()
        self.requests, self.tools = [], []
        self.max_tools = max_tools
        self.call_ids = set()

    def wire(self, event):
        body = event.get('payload', event.get('body'))
        body = json.loads(body) if isinstance(body, str) else body
        if event['event'] == 'request':
            if getattr(self.executor, 'poisoned', False):
                raise RuntimeError('Execution cleanup unresolved; model request refused')
            returned = {m.get('tool_call_id'): m.get('content') for m in body['messages'] if m.get('role') == 'tool'}
            while self.pending:
                call = self.pending.popleft()
                if call['id'] not in returned:
                    raise RuntimeError('Undelivered native tool results before next request')
                append_json(self.directory/'protocol.jsonl', {'event': 'native_call_not_dispatched',
                    'call_id': call['id'], 'result': returned[call['id']]})
            if len(self.requests) >= 30:
                raise RuntimeError('Model-request budget exhausted')
            self.observer.outbound_request(str(len(self.requests)+1), body)
            self.requests.append({'input_tokens': None, 'output_tokens': None, 'complete': False})
        elif event['event'] == 'response':
            usage = body.get('usage') or {}
            self.requests[-1].update(input_tokens=usage.get('prompt_tokens'),
                output_tokens=usage.get('completion_tokens'), complete=all(
                    type(usage.get(k)) is int for k in ('prompt_tokens', 'completion_tokens')))
            choice = body['choices'][0]
            message = choice['message']
            calls = message.get('tool_calls') or []
            optional_glm = body.get('model') == 'glm-5.3-flash' and valid_optional_reasoning(message)
            if choice.get('finish_reason') == 'length' or (not isinstance(message.get('reasoning_content'), str) and not optional_glm):
                append_json(self.directory/'protocol.jsonl', {'event': 'response_not_executable',
                    'reason': 'truncated' if choice.get('finish_reason') == 'length' else 'missing_reasoning'})
                calls = []
            for call in calls:
                if not call.get('id') or call['id'] in self.call_ids:
                    raise RuntimeError('Missing or reused native tool ID')
                self.call_ids.add(call['id'])
                self.pending.append(call)
        append_json(self.directory/'wire.jsonl', {'event': event['event'], 'at': utcnow(), 'body': body})

    def allowed(self, name, args):
        allowed = {'read_file', 'list_files', 'search', 'grep_files', 'patch_file',
                   'write_file', 'replace_in_file', 'apply_changes', 'run_shell', 'exec_command'}
        if self.phase == 'a':
            allowed = {'read_file', 'list_files', 'search', 'grep_files'}
        return name in allowed and check_tool_permission(self.workspace,
            self.task['editable_files'] if self.phase == 'b' else [], name, args)

    def invoke(self, name, args, execute):
        if getattr(self.executor, 'poisoned', False):
            raise RuntimeError('Execution cleanup unresolved; no further tools permitted')
        if len(self.tools) >= self.max_tools:
            raise RuntimeError('Tool-call budget exhausted before execution')
        if not self.pending:
            raise RuntimeError('Tool has no matching native call')
        call = self.pending.popleft()
        if call['function']['name'] != name or json.loads(call['function']['arguments']) != args:
            raise RuntimeError('Tool arguments differ from actual native response')
        before = snapshot(self.workspace)
        allowed = self.allowed(name, args)
        source = None
        if allowed and name == 'read_file':
            path = self.workspace/args['path']
            if path.is_file():
                source = path.read_bytes().decode('utf-8', errors='replace')
        if allowed:
            value = execute()
        else:
            value = ToolExecutionResult('error: frozen scenario policy denied; no effect',
                {'tool_status': 'rejected', 'tool_error_code': 'scenario_policy_denied'})
        after = snapshot(self.workspace)
        self.observer.tool_result(call['id'], name, args, value.content,
            source_text=source, changed=before != after)
        row = {'call_id': call['id'], 'name': name, 'args': args, 'output': value.content,
            'metadata': value.metadata, 'before': before, 'snapshot': after,
            'exit_code': value.metadata.get('exit_code'),
            'rejected': value.metadata.get('tool_status') == 'rejected'}
        self.tools.append(row)
        append_json(self.directory/'tools.jsonl', row)
        return value

    def finish(self, completed, error, answer, session_id):
        result = {**process_metrics(self.tools, self.requests, self.task, snapshot(self.workspace)),
            'process_evidence': self.observer.finish(), 'completed': bool(completed),
            'agent_error': error, 'answer': answer, 'session_id': session_id,
            'phase': self.phase, 'backend': self.executor.backend,
            'unexecuted_native_calls': len(self.pending)}
        write_json(self.directory/'session-result.json', result, exclusive=True)
        return result


def run_codetrail(task, workspace, directory, executor, phase, session_id, client_factory):
    from ..runtime import Pico, SessionStore
    from ..workspace import WorkspaceContext
    directory, workspace = Path(directory), Path(workspace)
    directory.mkdir(parents=True, exist_ok=False)
    audit = SessionAudit('codetrail', task, workspace, directory, executor, phase)
    client = client_factory(directory)
    if phase == 'a':
        begin = client.begin_native_run
        client.begin_native_run = lambda tools, context, request, verification_command: begin(
            tools, context, request, None)
    client.on_wire_event = audit.wire
    context = WorkspaceContext(str(workspace), str(workspace), '-', '-', 'fresh scenario phase', [], {})
    session = {'id': session_id, 'created_at': utcnow(), 'workspace_root': str(workspace),
               'history': [], 'memory': {}}
    # The registry captures ToolContext during construction. Overriding the
    # instance method afterwards leaves its original bound runner in partials.
    class EvaluatedPico(Pico):
        def _isolated_shell(self, command, timeout):
            return executor.execute(self.root, command, timeout)

    agent = EvaluatedPico(client, context, SessionStore(workspace/'.pico/sessions'), session=session,
        approval_policy='ask', max_steps=20, max_depth=0, max_new_tokens=4096,
        read_only=phase == 'a',
        allowed_tools=['read_file', 'list_files', 'search'] if phase == 'a' else None,
        verification_command=VERIFY, shell_backend='appcontainer',
        harness_profile='workbench', embedding_mode='off', secret_env_names={'BIGMODEL_API_KEY'})
    agent.approve = audit.allowed
    client.stream = False  # Pico workbench enables streaming; freeze both adapters to non-stream.
    original = agent.execute_tool
    def invoke(name, args):
        def execute():
            value = original(name, args)
            if name == 'run_shell':
                value = ToolExecutionResult(value.content, {**value.metadata, 'execution_backend': executor.backend})
            return value
        return audit.invoke(name, args, execute)
    agent.execute_tool = invoke
    prompt = task['phase_a']['prompt'] if phase == 'a' else task['prompt']
    answer = error = None
    try:
        answer = agent.ask(prompt)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    return audit.finish(agent.current_task_state and agent.current_task_state.status == 'completed',
                        error, answer, agent.session['id'])


def run_apecode(task, workspace, directory, executor, phase, session_id, model_factory, source, frozen_at):
    from scripts.run_cross_agent_pilot import source_info
    pinned = source_info(Path(source))
    sys.path.insert(0, str(Path(source)/'src'))
    from apecode.agent import NanoCodeAgent, AgentConfig
    from apecode.tools import ToolContext, create_default_registry
    from apecode import system_prompt
    directory, workspace = Path(directory), Path(workspace)
    directory.mkdir(parents=True, exist_ok=False)
    audit = SessionAudit('apecode', task, workspace, directory, executor, phase)
    registry = create_default_registry(ToolContext(cwd=workspace, ask_approval=lambda *a: True))
    original = registry.execute
    def execute(name, raw):
        args = json.loads(raw)
        if not isinstance(args, dict):
            raise ValueError('Tool arguments must be an object')
        def invoke():
            if name == 'exec_command':
                value = executor.execute(workspace, args['command'], 25)
                return ToolExecutionResult(f"exit_code: {value['returncode']}\n"+value['output'],
                    {'tool_status': 'ok' if value['returncode'] == 0 else 'error',
                     'exit_code': value['returncode'], 'execution_backend': executor.backend})
            content = original(name, raw)
            return ToolExecutionResult(content, {'tool_status': apecode_result_status(name, content)})
        return audit.invoke(name, args, invoke).content
    registry.execute = execute
    model = model_factory(directory, audit.wire)
    class Clock:
        @classmethod
        def now(cls, tz): return datetime.fromisoformat(frozen_at)
    with patch.object(system_prompt, 'find_agents_md', lambda cwd: []), patch.object(system_prompt, 'datetime', Clock):
        system = system_prompt.build_system_prompt(workspace)
    write_json(directory/'adapter.json', {'upstream_hashes': pinned, 'system_prompt': system,
        'adaptations': ['standard API transport', 'scoped rules and frozen clock',
            'exact-command isolation and file policy', 'per-tool budget and shared observer'],
        'native_persistent_memory_supported': False}, exclusive=True)
    agent = NanoCodeAgent(model=model, tools=registry, system_prompt=system, config=AgentConfig(max_steps=30))
    prompt = task['phase_a']['prompt'] if phase == 'a' else task['prompt']
    answer = error = None
    try:
        answer = agent.run(prompt)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    write_json(directory/'messages.json', agent.messages, exclusive=True)
    return audit.finish(error is None, error, answer, session_id)
