"""Explicit policy-adapted integration of pinned ApeCode; upstream sources stay unchanged."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.cross_agent import check_tool_permission, process_metrics, snapshot
from pico.evaluation.repair_suite import append_json, load_json, utcnow, write_json
from pico.windows_sandbox import VERIFY, run_verification
from scripts.run_cross_agent_pilot import MODEL, PROTOCOL, source_info


class SiliconFlowChatModel:
    """Implements ApeCode's public ChatModel interface without changing its loop."""
    def __init__(self, directory):
        self.directory = directory
        self.key = os.environ['SILICONFLOW_API_KEY']
        self.requests = []
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def complete(self, *, messages, tools):
        payload = {'model': MODEL, 'messages': messages, 'tools': tools, 'tool_choice': 'auto',
                   'temperature': .2, 'top_p': .95, 'max_tokens': 4096,
                   'enable_thinking': True, 'thinking_budget': 4096, 'stream': False}
        index = len(self.requests) + 1
        usage = {'input_tokens': None, 'output_tokens': None, 'complete': False}
        self.requests.append(usage)
        append_json(self.directory/'wire.jsonl', {'event': 'request', 'index': index, 'payload': payload, 'at': utcnow()})
        req = urllib.request.Request(PROTOCOL['base_url']+'/chat/completions',data=json.dumps(payload).encode(),
                    headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'})
        for attempt in range(1, 4):
            try:
                with self.opener.open(req, timeout=180) as response: body = response.read().decode()
                if self.key in body: raise RuntimeError('Credential returned by provider; refuse export')
                data = json.loads(body)
                append_json(self.directory/'wire.jsonl', {'event':'response','index':index,'attempt':attempt,'body':data,'at':utcnow()})
                if data.get('model') != MODEL: raise RuntimeError('Returned model mismatch; no substitution')
                returned = data.get('usage') or {}
                usage.update(input_tokens=returned.get('prompt_tokens'), output_tokens=returned.get('completion_tokens'), complete=attempt==1)
                return data['choices'][0]['message']
            except (urllib.error.URLError, TimeoutError) as exc:
                code = getattr(exc, 'code', None)
                body = exc.read().decode() if isinstance(exc, urllib.error.HTTPError) else str(exc)
                if self.key in body: raise RuntimeError('Credential in error; refuse export')
                append_json(self.directory/'transport.jsonl', {'index':index,'attempt':attempt,'status':code,'error':body,'at':utcnow()})
                if attempt == 3 or code not in (None, 429, 500, 502, 503, 504):
                    raise RuntimeError(f'Provider HTTP{code}: {type(exc).__name__}') from exc
                time.sleep(.25 * attempt)


def run_apecode(output, task, workspace, directory, model_factory=None):
    external = load_json(output/'external-agent.json')
    source = Path(external['source'])
    actual = source_info(source)
    if actual != external['source_hashes']: raise RuntimeError('Pinned ApeCode bytes changed')
    sys.path.insert(0, str(source/'src'))
    from apecode.agent import NanoCodeAgent, AgentConfig
    from apecode.tools import ToolContext, create_default_registry
    from apecode import system_prompt

    registry = create_default_registry(ToolContext(cwd=workspace, ask_approval=lambda *args: True))
    original = registry.execute
    observations = []
    def execute(name, arguments_json):
        if len(observations) >= PROTOCOL['max_tool_calls']:
            append_json(directory/'budget.jsonl', {'at':utcnow(),'reason':'tool_budget_exhausted','calls':len(observations)})
            raise RuntimeError('Tool call budget exhausted; no additional side effect')
        before = snapshot(workspace); code = None; rejected = False
        try:
            args = json.loads(arguments_json)
            if not isinstance(args, dict): raise ValueError('arguments must be an object')
            if not check_tool_permission(workspace, task['editable_files'], name, args):
                rejected = True; result = 'error: rejected by frozen pilot policy; no effect'
            elif name == 'exec_command':
                value = run_verification(workspace, VERIFY, timeout=25)
                code = value['returncode']; result = f'exit_code: {code}\n'+value['output']
            else:
                result = original(name, arguments_json)
        except (ValueError, TypeError, KeyError) as exc:
            args = {}; rejected = True; result = f'error: {type(exc).__name__}: {exc}'
        except Exception as exc:
            result = f'error: {type(exc).__name__}: {exc}'
        o = {'name':name,'args':args,'output':result,'before':before,'snapshot':snapshot(workspace),
             'rejected':rejected,'exit_code':code,'at':utcnow()}
        observations.append(o); append_json(directory/'tools.jsonl',o)
        return result
    registry.execute = execute

    frozen_time = datetime.fromisoformat(load_json(output/'freeze.json')['at'])
    class FrozenClock:
        @classmethod
        def now(cls, tz): return frozen_time
    # Host discovery/clock are environment adapters, not edits to the upstream prompt template.
    def scoped_rules(cwd):
        return [workspace/'AGENTS.md'] if (workspace/'AGENTS.md').is_file() else []
    with patch.object(system_prompt, 'find_agents_md', scoped_rules), patch.object(system_prompt, 'datetime', FrozenClock):
        system = system_prompt.build_system_prompt(workspace)
    write_json(directory/'adapter.json', {'upstream_commit':PROTOCOL['apecode']['commit'],
        'source_hashes':actual,'adaptations':PROTOCOL['apecode']['adaptations'],'system_prompt':system}, exclusive=True)
    model = model_factory(directory) if model_factory else SiliconFlowChatModel(directory)
    agent = NanoCodeAgent(model=model,tools=registry,system_prompt=system,config=AgentConfig(max_steps=60))
    error = None; answer = None
    try: answer = agent.run(load_json(output/'prompts.json')[task['id']])
    except Exception as exc: error = f'{type(exc).__name__}: {exc}'
    write_json(directory/'messages.json', agent.messages, exclusive=True)
    write_json(directory/'answer.json', {'answer':answer,'error':error}, exclusive=True)
    return {**process_metrics(observations,model.requests,task,snapshot(workspace)),
            'completed':error is None,'agent_error':error}
