"""Run two frozen, multi-turn real GoalTree stress pilots without changing policy.

Public synthetic fixtures only. Expected answers are kept outside workspaces.
This is a development diagnostic, never a held-out/general Agent score.
"""
import argparse
import copy
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.persistence import write_json_atomic
from pico.providers.clients import OpenAICompatibleModelClient
from pico.security import redact_artifact
from pico.web import Workbench
from scripts.glm_live_support import Budget, BudgetExceeded, RATES
from scripts.run_goaltree_glm import load_key

ASSETS = ROOT / 'benchmarks/goal_stress_v1'


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, value)


def sources():
    files = []
    for folder in ('pico', 'goaltree', 'scripts', 'tests', 'benchmarks/goal_stress_v1', 'benchmarks/goal_autonomy_v1'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix in {'.py', '.json', '.txt', '.js', '.cjs', '.html', '.css', '.svg'})
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def without_reasoning(value):
    if isinstance(value, dict):
        return {k: ('<reasoning omitted; payload hash retained>' if k == 'reasoning_content' else without_reasoning(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [without_reasoning(v) for v in value]
    return value


class Observer:
    def __init__(self, output, protocol, frozen):
        self.output, self.protocol, self.frozen = output, protocol, frozen
        self.budget = Budget(output)
        self.turn_dir = None
        self.calls = 0
        self.halted = None
        self.lock = threading.Lock()

    def before(self, payload, audit):
        with self.lock:
            if self.halted:
                raise BudgetExceeded(self.halted)
            if sources() != self.frozen:
                self.halted = 'Frozen source changed'
                raise BudgetExceeded(self.halted)
            if self.calls >= self.protocol['maximum_requests']:
                self.halted = 'Frozen request limit'
                raise BudgetExceeded(self.halted)
            self.calls += 1
            request_dir = self.turn_dir / 'requests'
            identifier = f'{self.calls:04d}'
            # Payload is public synthetic task data, never auth headers. Do not
            # enable the protected transport's disabled raw-wire callbacks.
            dump(request_dir / f'{identifier}.json', {
                'stage': 'prepared_before_privacy_gate', 'request_number': self.calls,
                'payload_sha256': hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
                'payload_without_reasoning': redact_artifact(without_reasoning(payload)),
                'context_audit': copy.deepcopy(audit),
            })
            try:
                reservation = self.budget.before(payload, str(self.turn_dir.relative_to(self.output)))
            except BudgetExceeded:
                self.halted = 'Frozen money/token budget exhausted'
                raise
            return identifier, reservation


class ObservedClient(OpenAICompatibleModelClient):
    def __init__(self, key, observer):
        p = observer.protocol
        super().__init__(p['model'], p['base_url'], key, p['temperature'], p['timeout_seconds'], 'chat_completions')
        self.observer = observer
        self.top_p, self.max_transport_attempts, self.stream = p['top_p'], 1, True
        self.network_route = 'direct'
        self.native_wire_options = copy.deepcopy(p['native_wire_options'])

    def begin_native_run(self, *args, **kwargs):
        super().begin_native_run(*args, **kwargs)
        conversation = self.native_conversation
        prepare = conversation.prepare_payload

        def observed(payload):
            prepared = prepare(payload)
            self.observation = self.observer.before(prepared, conversation.last_context_audit)
            return prepared  # No edits, projections, extra context, or permissions.
        conversation.prepare_payload = observed

    def complete(self, *args, **kwargs):
        self.observation = None
        answer, error = None, None
        started = time.monotonic()
        try:
            answer = super().complete(*args, **kwargs)
            return answer
        except Exception as exc:
            error = {'type': type(exc).__name__, 'detail': str(exc)[:400]}
            raise
        finally:
            if self.observation:
                number, reservation = self.observation
                meta = copy.deepcopy(self.last_completion_metadata)
                usage = meta.get('provider_usage') or {}
                self.observer.budget.after(reservation, {'usage': usage, 'model': meta.get('provider_model')})
                if not all(type(usage.get(k)) is int for k in ('prompt_tokens', 'completion_tokens')):
                    self.observer.halted = 'Unknown provider usage; dispatch stopped'
                if meta.get('provider_model') and meta['provider_model'] != self.model:
                    self.observer.halted = 'Unexpected returned model; dispatch stopped'
                dump(self.observer.turn_dir / 'responses' / f'{number}.json', {
                    'metadata': redact_artifact(meta), 'error': redact_artifact(error),
                    'parsed_action_kind': getattr(answer, 'action_kind', None),
                    'answer_or_action': str(answer) if answer is not None else None,
                    'elapsed_seconds': round(time.monotonic() - started, 3),
                })


def authorized_approval(approval, bound_goal):
    if not approval:
        return False
    name, args = approval.get('name'), approval.get('args', {})
    if name == 'goal_action':
        try:
            proposal = json.loads(args.get('proposal', '{}'))
        except (ValueError, TypeError):
            return False
        return proposal.get('action') in {'update_goal', 'update_task', 'record_progress', 'create_task'} and proposal.get('goal_id', bound_goal) == bound_goal
    return name == 'plan_tasks'  # Existing host scope checks still enforce goal ownership.


def make_app(workspace, key, observer):
    p = observer.protocol
    return Workbench(workspace, model_factory=lambda: ObservedClient(key, observer),
                     harness_profile='workbench', load_env=False, privacy_protection=True,
                     native_input_budget=p['input_json_byte_budget'], native_context_window=p['context_byte_ceiling'],
                     max_new_tokens=p['max_output_tokens'], shell_backend='appcontainer',
                     verification_command='python -B -m unittest discover -v',
                     network_route='direct', web_access=False)


def create_fixture(app, case):
    fixture = {'goals': {}, 'tasks': {}}
    for alias, data in case['goals'].items():
        goal = app.goals.action({'action': 'create_goal', **data['goal']})
        fixture['goals'][alias] = goal
        fixture['tasks'][alias] = []
        for task in data['tasks']:
            node = app.goals.action({'action': 'create_task', 'goal_id': goal['id'],
                                     'verification_mode': 'manual', **task})
            fixture['tasks'][alias].append(node)
    binding = app.goals.binding(fixture['goals']['A']['id'], '', 'assistant')
    fixture['conversation_session_id'] = binding['session_id']
    return fixture


def run_turn(app, case, turn, fixture, observer):
    bound = fixture['goals']['A']['id']
    before = app.goals.read()
    started = time.monotonic()
    submitted = app.submit({'prompt': turn['prompt'], 'mode': 'assistant', 'goal_id': bound,
                            'session_id': fixture['conversation_session_id'], 'policy': 'ask',
                            'max_steps': observer.protocol['max_tool_steps_per_turn']})
    task = app.task(submitted['id'])
    approvals = set()
    deadline = started + observer.protocol['turn_deadline_seconds']
    while True:
        with task.condition:
            snapshot = task.view()
            done = task.finished is not None and app.active_id is None
        if done:
            break
        approval = snapshot.get('approval')
        if approval and approval['id'] not in approvals:
            approvals.add(approval['id'])
            allowed = authorized_approval(approval, bound)
            dump(observer.turn_dir / f'approval-{approval["id"]}.json',
                 {'approval': approval, 'allowed': allowed, 'authority': 'frozen synthetic fixture scope; not human acceptance'})
            app.approve(task.id, approval['id'], allowed)
        if time.monotonic() > deadline:
            observer.halted = 'Turn deadline reached'
            app.cancel(task.id)
            with task.condition:
                task.condition.wait_for(lambda: task.finished is not None and app.active_id is None, 25)
            break
        with task.condition:
            task.condition.wait(timeout=.2)
    snapshot = task.view()
    snapshot['state_after'] = app.goals.read()
    snapshot['bound_goal_before'] = before['goals'][bound]
    snapshot['tasks_before'] = before['tasks']
    snapshot['foreign_before'] = {key: value for key, value in before['goals'].items() if key != bound}
    snapshot['expected_target_alias'] = turn.get('target', 'A')
    dump(observer.turn_dir / 'result.json', snapshot)
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--carry-budget', type=Path, help='Retain every prior reservation and settlement in this experiment')
    parser.add_argument('--case', choices=('long-research', 'switch-three-goals'), help='Freeze and run only the selected original sequence')
    parser.add_argument('--token-limit', type=int, help='Lower the frozen batch ceiling to fit the remaining global budget')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Output must be new; failures and prior freezes must not be overwritten')
    output = args.output.resolve()
    output.mkdir(parents=True)
    protocol = json.loads((ASSETS / 'protocol.json').read_text(encoding='utf-8'))
    if args.token_limit is not None:
        if not 1 <= args.token_limit <= protocol['known_token_limit']:
            parser.error('token-limit may only lower the original frozen budget')
        protocol['known_token_limit'] = args.token_limit
    tasks = json.loads((ASSETS / 'tasks.json').read_text(encoding='utf-8'))
    if args.case:
        tasks = [case for case in tasks if case['id'] == args.case]
    gold = json.loads((ASSETS / 'gold.json').read_text(encoding='utf-8'))
    frozen = sources()
    dump(output / 'freeze.json', {'protocol': protocol, 'tasks': tasks, 'gold': gold, 'sources': frozen,
                                 'live': args.live, 'frozen_before_first_request': True})
    for relative, digest in frozen.items():
        data = (ROOT / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('Source changed during snapshot; no call dispatched')
        target = output / 'source-snapshot' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    dump(output / 'environment.json', {'python': sys.version, 'os': platform.system(), 'os_release': platform.release(),
                                      'model_identifier_is_alias': True, 'immutable_provider_weight_version_available': False})
    dump(output / 'budget.json', {'max_spend_cny': protocol['estimated_cny_limit'], 'max_total_tokens': protocol['known_token_limit'],
                                 'rates': RATES, 'actual_billed_cost': None, 'concurrency': 1})
    if args.carry_budget:
        origin = args.carry_budget.resolve()
        old_config = json.loads((origin / 'budget.json').read_text(encoding='utf-8'))
        old_freeze = json.loads((origin / 'freeze.json').read_text(encoding='utf-8'))
        if old_config['rates'] != RATES or old_freeze['protocol']['model'] != protocol['model']:
            parser.error('Cannot carry an incompatible tariff or model ledger')
        ledger = (origin / 'ledger.jsonl').read_bytes()
        entries = [json.loads(line) for line in ledger.decode('utf-8').splitlines() if line.strip()]
        dump(output / 'budget-origin.json', {'path': str(origin), 'ledger_sha256': hashlib.sha256(ledger).hexdigest(),
              'freeze_sha256': hashlib.sha256((origin / 'freeze.json').read_bytes()).hexdigest(),
              'imported_reservation_ids': [e['id'] for e in entries if e['event'] == 'reserved'],
              'policy': 'All unknown reservations remain charged against the cumulative ceiling; old failures not resumed or replaced'})
        (output / 'ledger.jsonl').write_bytes(ledger)
    slots = [{'case': case['id'], 'turn': turn['id'], 'status': 'unstarted'} for case in tasks for turn in case['turns']]
    dump(output / 'slots.json', slots)
    key = load_key(ROOT / '.env.glm.local') if args.live else ''
    if args.live and not key:
        parser.error('Local ordinary BIGMODEL_API_KEY missing; no fallback')
    if key:
        os.environ['BIGMODEL_API_KEY'] = key
    observer = Observer(output, protocol, frozen)
    for case in tasks:
        if observer.halted:
            break
        case_dir = output / case['id']
        workspace = case_dir / 'workspace'
        workspace.mkdir(parents=True)
        for path in (ASSETS / 'public').rglob('*'):
            if path.is_file():
                target = workspace / path.relative_to(ASSETS / 'public')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        app = make_app(workspace, key, observer)
        fixture = create_fixture(app, case)
        dump(case_dir / 'fixture.json', fixture)
        for turn in case['turns']:
            if observer.halted:
                break
            slot = next(s for s in slots if s['case'] == case['id'] and s['turn'] == turn['id'])
            observer.turn_dir = case_dir / turn['id']
            dump(observer.turn_dir / 'input.json', turn)
            if not args.live:
                slot['status'] = 'prepared_not_run'
                continue
            if turn.get('restart'):
                app = make_app(workspace, key, observer)  # Same persisted conversation, no injected history.
            slot['status'] = 'started'
            dump(output / 'slots.json', slots)
            try:
                result = run_turn(app, case, turn, fixture, observer)
                slot.update(status=result['status'], run_id=result.get('run_id'), stop_reason=result.get('stop_reason'))
            except Exception as exc:
                slot.update(status='runner_failed', error={'type': type(exc).__name__, 'detail': str(exc)[:400]})
                dump(observer.turn_dir / 'runner-error.json', slot['error'])
                observer.halted = 'Runner failure; no replacement attempt'
            dump(output / 'slots.json', slots)
            print(json.dumps({'case': case['id'], 'turn': turn['id'], 'status': slot['status'],
                              'requests': observer.calls, 'known_tokens': observer.budget.token_state()[0]}, ensure_ascii=False), flush=True)
    dump(output / 'slots.json', slots)
    dump(output / 'run-summary.json', {'live': args.live, 'halted': observer.halted, 'sources_unchanged': sources() == frozen,
                                      'planned_cases': len(tasks), 'planned_turns': len(slots), 'budget_tokens': observer.budget.token_state()})
    subprocess.run([sys.executable, str(ROOT / 'scripts/verify_goal_stress.py'), '--output', str(output)], check=True)
    paths = [p for p in output.rglob('*') if p.is_file() and p.name != 'evidence-sha256.json']
    dump(output / 'evidence-sha256.json', {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


if __name__ == '__main__':
    main()
