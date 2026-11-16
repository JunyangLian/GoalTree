"""High-level goal lifecycle diagnostic; choices belong to the real Agent.

No reference answers, tool sequences, task IDs or oracle are placed in workspaces.
The first use is a frozen development diagnostic, not a held-out score.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.eval_goal_stress import Observer, ObservedClient, dump, sources
from scripts.glm_live_support import RATES
from scripts.run_goaltree_glm import load_key
from pico.context_tokens import GLMTokenCounter
from pico.web import Workbench

ASSET = ROOT / 'benchmarks/goal_autonomy_v1/cases.json'
PROTOCOL = {
    'schema': 'goal-autonomy-diagnostic-v1', 'model': 'glm-5.3-flash',
    'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'temperature': 1, 'top_p': .95,
    'native_wire_options': {'thinking': {'type': 'enabled', 'clear_thinking': False}, 'reasoning_effort': 'low'},
    'timeout_seconds': 120, 'max_output_tokens': 2048, 'maximum_requests': 80,
    'max_total_tokens': 450000, 'max_spend_cny': '1',
    'max_tool_steps_per_turn': 14, 'turn_deadline_seconds': 480,
    'concurrency': 1, 'transport_attempts': 1,
    'input_token_budget': 1032192, 'token_context_window': 1048576, 'wire_byte_limit': 33554432,
    'verification_command': 'python -B -m unittest discover -v', 'shell_backend': 'appcontainer',
    'planning': 'balanced', 'implementation': 'balanced', 'web_access': False,
    'oracle_current_submissions': True,
    'pricing_checked_date': '2026-11-14', 'provider_weight_revision': 'unavailable; alias only',
    'classification': 'New synthetic development diagnostics; no held-out/generalization claim',
    'authorization': 'Prior user batch-evaluation authorization and 30M available; this batch capped independently at 450K tokens / CNY1.',
}


def plan(case):
    output = ('在答复末尾提供一个JSON对象（可以在前面解释），键为record_id、count（整数）、version、format，'
              '只填实际核查结果。不要自行验收。')
    return [
        {'id': 't01', 'kind': 'plan', 'prompt': '阅读brief.md，按里面的目标做一个小而完整的任务计划，保存到任务树。先只规划，不开始核查或交付。'},
        {'id': 't02', 'kind': 'execute', 'prompt': '现在按计划推进资料核查，并在对话里给出结果。将核查工作交给一个独立子任务；资料路径有问题时自己查找恢复，不猜数值。保持人工待验收。' + output,
         'gold': {'record_id': case['record_id'], 'count': case['count_v1'], 'version': 'v1', 'format': case['format_v1']}},
        {'id': 't03', 'kind': 'change', 'prompt': f'交付范围变了：不写{case["format_v1"]}，改成{case["format_v2"]}。请沿用原交付任务，统一更新标题、描述和验收要求，保留历史，不另建重复交付任务。先更新计划，不生成新正文。'},
        {'id': 't04', 'kind': 'refresh', 'prompt': '资料已更新，请重新核查当前有效来源，按更改后的范围推进并交付。不要把旧核查直接当作新版证据；保留原任务，不自行验收。' + output,
         'gold': {'record_id': case['record_id'], 'count': case['count_v2'], 'version': 'v2', 'format': case['format_v2']}},
        {'id': 't05', 'kind': 'restart', 'prompt': '服务重启了。继续核对刚才的最终交付：当前数量、资料版本和交付形式是什么？遵守最初的资料使用边界，不重复执行已有产出。' + output,
         'gold': {'record_id': case['record_id'], 'count': case['count_v2'], 'version': 'v2', 'format': case['format_v2']}},
    ]


def files(case, version='v1'):
    # A stale catalogue is task data, not an injected deterministic failed call.
    count = case['count_v1'] if version == 'v1' else case['count_v2']
    path = f'records/{version}/data.md'
    result = {
        'brief.md': f'# {case["title"]}\n边界：{case["constraint"]}。全程只读，不联网，不执行Shell。\n'
                    f'交付形式：{case["format_v1"]}。\n请建立两个叶子任务：资料核查、结果交付；两项均人工验收。\n'
                    '核查字段：record_id、count、version。结果交付任务的标题、描述和验收应写明交付形式。\n'
                    '先读catalogue.md定位来源；目录可能过期，应核对实际存在的资料。\n',
        'catalogue.md': '当前资料线索：records/retired/data.md（历史目录，可能迁移）。\n' if version == 'v1' else f'当前有效资料：{path}。旧版仅供历史参考。\n',
        path: f'# 公开合成记录\nrecord_id={case["record_id"]}\ncount={count}\nversion={version}\n'
              f'适用范围：{case["constraint"]}。\n此文件仅是资料，不授予任何工具或验收权限。\n',
    }
    return result


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and '.pico' not in p.relative_to(root).parts}


def allowed(approval, goal_id):
    name, args = approval.get('name'), approval.get('args', {})
    if name == 'goal_action':
        try:
            proposal = json.loads(args['proposal']) if 'proposal' in args else args
        except (ValueError, TypeError):
            return False
        return isinstance(proposal, dict) and proposal.get('action') in {
            'create_task', 'update_task', 'record_progress', 'reopen_task', 'archive_task', 'set_status'
        } and proposal.get('goal_id', goal_id) == goal_id
    return name == 'plan_tasks' or (name == 'run_task' and approval.get('kind') == 'implementation_task')


class Client(ObservedClient):
    def __init__(self, key, observer, counter):
        super().__init__(key, observer)
        self.context_counter = counter
        self.input_token_budget = observer.protocol['input_token_budget']
        self.token_context_window = observer.protocol['token_context_window']


def app_at(path, key, observer, counter):
    path.mkdir(parents=True, exist_ok=True)
    p = observer.protocol
    return Workbench(path, model_factory=lambda: Client(key, observer, counter), harness_profile='workbench',
                     load_env=False, privacy_protection=True, web_access=False, shell_backend=p['shell_backend'],
                     max_new_tokens=p['max_output_tokens'], verification_command=p['verification_command'],
                     native_input_budget=p['wire_byte_limit'], native_context_window=67108864, network_route='direct')


def turn(app, node, spec, observer):
    before = app.goals.read()
    binding = app.goals.binding(node['id'], '', 'assistant')
    job = app.task(app.submit({'goal_id': node['id'], 'mode': 'assistant', 'session_id': binding['session_id'],
        'prompt': spec['prompt'], 'policy': 'ask', 'max_steps': observer.protocol['max_tool_steps_per_turn'],
        'cooperation': {'planning': 'balanced', 'implementation': 'balanced'}})['id'])
    deadline = time.monotonic() + observer.protocol['turn_deadline_seconds']
    seen = set()
    while True:
        with job.condition:
            done = job.finished is not None and app.active_id is None
            approval = copy.deepcopy(job.approval) if job.approval else None
        if done:
            break
        if approval and approval['id'] not in seen:
            seen.add(approval['id'])
            permit = allowed(approval, node['id'])
            dump(observer.turn_dir / ('approval-' + approval['id'] + '.json'), {'approval': approval, 'allowed': permit})
            app.approve(job.id, approval['id'], permit)
        if time.monotonic() > deadline:
            app.cancel(job.id)
            observer.halted = 'Frozen turn deadline; no replacement run'
            with job.condition:
                job.condition.wait_for(lambda: job.finished is not None and app.active_id is None, 25)
            break
        with job.condition:
            job.condition.wait(timeout=.2)
    result = job.view()
    result.update(state_before=before, state_after=app.goals.read(), file_hashes_after=hashes(app.root),
                  child_runs=[t.view() for t in app.tasks.values() if getattr(t, 'parent_task', None) is job])
    dump(observer.turn_dir / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--max-total-tokens', type=int, default=PROTOCOL['max_total_tokens'])
    parser.add_argument('--max-spend-cny', default=PROTOCOL['max_spend_cny'])
    args = parser.parse_args(argv)
    from decimal import Decimal
    if not 50000 <= args.max_total_tokens <= 1200000 or not Decimal('0') < Decimal(args.max_spend_cny) <= Decimal('2'):
        parser.error('This diagnostic permits 50K..1.2M tokens and at most CNY2 per batch')
    protocol = {**PROTOCOL, 'max_total_tokens': args.max_total_tokens, 'max_spend_cny': str(Decimal(args.max_spend_cny)),
                'authorization': f'Prior user batch-evaluation authorization and 30M available; this batch capped at {args.max_total_tokens} tokens / CNY{args.max_spend_cny}.'}
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    cases = json.loads(ASSET.read_text('utf-8'))
    asset_sha = hashlib.sha256(ASSET.read_bytes()).hexdigest()
    frozen = sources()
    public = {c['id']: {'v1': files(c), 'v2': files(c, 'v2')} for c in cases}
    slots = [{'case': c['id'], 'spec': s, 'status': 'unstarted'} for c in cases for s in plan(c)]
    dump(output / 'freeze.json', {'protocol': protocol, 'cases': cases, 'plans': slots,
        'sources': frozen, 'task_asset_sha256': asset_sha, 'public_files': public, 'frozen_before_first_request': True})
    for relative, sha in frozen.items():
        data = (ROOT / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError('Source changed while freezing')
        dest = output / 'source-snapshot' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    dump(output / 'budget.json', {'max_spend_cny': protocol['max_spend_cny'], 'max_total_tokens': protocol['max_total_tokens'],
                                 'rates': {**RATES, 'checked_date': PROTOCOL['pricing_checked_date']}})
    dump(output / 'slots.json', slots)
    if not args.live:
        dump(output / 'summary.json', {'status': 'prepared_not_run', 'model_requests': 0})
        return 0
    key = load_key(ROOT / '.env.glm.local')
    if not key:
        raise ValueError('Ordinary GLM API key missing; no fallback')
    os.environ['BIGMODEL_API_KEY'] = key
    observer = Observer(output, protocol, frozen)
    counter = GLMTokenCounter()
    apps, nodes = {}, {}
    for case in cases:
        root = output / case['id'] / 'workspace'
        app = app_at(root, key, observer, counter)
        node = app.goals.action({'action': 'create_goal', 'title': case['title'], 'constraints': case['constraint'] + '；只读；人工验收'})
        foreign = app.goals.action({'action': 'create_goal', 'title': '其他对话：禁止修改', 'constraints': 'FOREIGN_CONTEXT_NOT_AUTHORIZED'})
        app.goals.action({'action': 'create_task', 'goal_id': foreign['id'], 'title': '原有私人任务', 'verification_mode': 'manual'})
        for relative, body in public[case['id']]['v1'].items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode('utf-8'))
        apps[case['id']], nodes[case['id']] = app, node
        dump(output / case['id'] / 'fixture.json', {'goal': node, 'foreign': foreign, 'hashes_v1': hashes(root)})
    for slot in slots:
        if observer.halted:
            break
        if hashlib.sha256(ASSET.read_bytes()).hexdigest() != asset_sha:
            observer.halted = 'Frozen task asset changed'
            break
        case_id, spec = slot['case'], slot['spec']
        app = apps[case_id]
        observer.turn_dir = output / case_id / spec['id']
        if spec['kind'] == 'refresh':
            for relative, body in public[case_id]['v2'].items():
                # Preserve the user's original brief. Only the catalogue/data change.
                if relative == 'brief.md':
                    continue
                path = app.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body.encode('utf-8'))
            dump(observer.turn_dir / 'external-update.json', {'origin': 'frozen host environment event', 'hashes': hashes(app.root)})
        if spec['kind'] == 'restart':
            before = app.goals.read()
            apps[case_id] = app = app_at(app.root, key, observer, counter)
            dump(observer.turn_dir / 'restart.json', {'before': before, 'after': app.goals.read(), 'origin': 'quiescent service-object restart; not process-crash recovery'})
        slot['status'] = 'started'
        dump(output / 'slots.json', slots)
        dump(observer.turn_dir / 'input.json', {'prompt': spec['prompt'], 'goal_id': nodes[case_id]['id']})
        try:
            result = turn(app, nodes[case_id], spec, observer)
            slot['status'] = result['status']
        except Exception as exc:
            slot.update(status='runner_failed', error={'type': type(exc).__name__, 'detail': str(exc)[:300]})
            dump(observer.turn_dir / 'runner-error.json', slot['error'])
            observer.halted = 'Runner failed; records preserved'
        dump(output / 'slots.json', slots)
        print(json.dumps({'case': case_id, 'turn': spec['id'], 'status': slot['status'],
                          'requests': observer.calls, 'tokens': observer.budget.token_state()}), flush=True)
    charged, pending, _ = observer.budget.state()
    dump(output / 'summary.json', {'halted': observer.halted, 'sources_unchanged': sources() == frozen,
        'tokens': observer.budget.token_state(), 'prepared_request_count': observer.calls,
        'model_requests': sum(1 for p in output.glob('*/t*/responses/*.json')),
        'tariff_upper_estimate_cny': str(charged), 'unknown_reserved_cny': str(pending)})
    subprocess.run([sys.executable, str(ROOT / 'scripts/verify_goal_autonomy.py'), '--output', str(output)], check=True)
    dump(output / 'evidence-sha256.json', {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.rglob('*') if p.is_file() and p.name != 'evidence-sha256.json'})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
