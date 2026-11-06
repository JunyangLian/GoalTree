"""Audit and export a completed/stopped CTRepair30 batch; no model calls."""
import argparse
from collections import Counter
import csv
from decimal import Decimal
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.repair_suite import load_json, write_json, digest, utcnow
from pico.evaluation.cross_agent import snapshot
from pico.evaluation.process_trace import efficiency
from scripts.run_glm_live_pilot import check
from scripts.run_glm_benchmark import report
from scripts.glm_live_support import Budget


def jsonlines(p):
    return [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []


def resource_observations(directory, wire):
    usages = [(e['body'].get('usage') or {}) for e in wire if e['event']=='response']
    known = [u for u in usages if all(type(u.get(k)) is int and u[k]>=0 for k in ('prompt_tokens','completion_tokens'))]
    requests = sum(e['event']=='request' for e in wire)
    result = {'known_input_tokens':sum(u['prompt_tokens'] for u in known),
        'known_output_tokens':sum(u['completion_tokens'] for u in known),
        'requests_without_observed_usage':requests-len(known),
        'observed_tool_steps':len(jsonlines(directory/'session-b/tools.jsonl')),
        'timing_scope':'Agent execution through first external verification; worker initialization and second external audit excluded'}
    if (directory/'controller-terminal.json').exists():
        end=load_json(directory/'controller-terminal.json')['at']
        start=load_json(directory/'dispatched.json')['at']
        result['controller_terminal_wall_ms']=round((datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()*1000)
        result['timing_scope']='Dispatch through forced controller termination; separate clock, includes worker initialization'
        result['scope_observed']=False
        result['scope_valid']=None  # no final scope audit, never label this a proven scope violation.
    return result


def resource_totals(rows, previous):
    value=dict(previous)
    value['tokens_observed']=sum(r.get('known_input_tokens',0)+r.get('known_output_tokens',0) for r in rows)
    value['requests_without_observed_usage']=sum(r.get('requests_without_observed_usage',0) for r in rows)
    value['token_complete']=value['requests_without_observed_usage']==0 and bool(rows)
    value['tokens_per_success']=value['tokens_observed']/value['successes'] if value['token_complete'] and value['successes'] else None
    value['terminated_dispatch_wall_ms']=[r['controller_terminal_wall_ms'] for r in rows if 'controller_terminal_wall_ms' in r]
    value['elapsed_complete']=value['elapsed_observed_runs']==len(rows)
    value['timing_note']='Normal-record percentiles exclude missing timing; terminated dispatch wall times retained separately. Matched-success latency is separate, all failure tokens retained.'
    return value


def export(batch, output):
    check(batch)
    summary = report(batch)
    if not all(a['complete'] for a in summary['agents'].values()) and not (batch/'stopped.json').exists():
        raise RuntimeError('Batch still active or not dispatched; refuse a terminal report')
    output.mkdir(parents=True, exist_ok=False)
    rows = load_json(batch/'runs.json')
    tasks = {t['id']: t for t in load_json(batch/'tasks.json')}
    plan = load_json(batch/'plan.json')
    _, _, ledger = Budget(batch/'budget').state()
    owners = {e['id']: e.get('owner', '') for e in ledger if e['event']=='reserved'}
    detailed = []
    for row in rows:
        d = batch/'runs'/row['run_key']
        item = dict(row)
        item['status'] = 'finished' if (d/'result.json').exists() else 'interrupted'
        item.update(historical_cohort=tasks[row['task_id']].get('cohort'),
            development_tuned=tasks[row['task_id']].get('development_tuned'),
            current_exposure='Known regression; not a new holdout')
        if (d/'result.json').exists():
            original = load_json(d/'result.json')
            assert snapshot(d/'final-workspace') == original['final_sha256']
            item['source_result_sha256'] = digest(d/'result.json')
            item['second_verification'] = load_json(d/'second-verification-result.json')
        tools = jsonlines(d/'session-b/tools.jsonl')
        wire = jsonlines(d/'session-b/wire.jsonl')
        item.update(resource_observations(d,wire))
        protocol = jsonlines(d/'session-b/protocol.jsonl')
        session = load_json(d/'session-b/session-result.json') if (d/'session-b/session-result.json').exists() else {}
        observed = jsonlines(d/'session-b/process/events.jsonl')
        returned_lines = [
            (s['path'],s['source_sha256'],s['line'],s['text'])
            for e in observed if e.get('kind')=='read' and e.get('ok')
            for s in e.get('snippets',[])]
        repeated_lines = len(returned_lines)-len(set(returned_lines))
        requests = sum(e['event']=='request' for e in wire)
        missing = []
        for e in wire:
            if e['event']=='response':
                message = e['body']['choices'][0]['message']
                if not isinstance(message.get('reasoning_content'),str):
                    missing.append(((e['body'].get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens'))
        item.update(model_requests_observed=requests, protocol_rejections=sum(e['event']=='response_not_executable' for e in protocol),
            missing_reasoning_token_counts=missing,
            policy_denials=sum(t['metadata'].get('tool_error_code') in {'scenario_policy_denied','approval_denied'} for t in tools),
            tests_executed=sum(t['name'] in {'run_shell','exec_command'} and t.get('exit_code') is not None for t in tools),
            baseline_test_observed=session.get('baseline_test_observed'), final_test_observed=session.get('final_test_observed'),
            repeated_reads=(session.get('process_evidence') or {}).get('repeated_reads'),
            overlapping_returned_lines_posthoc={'duplicate_line_returns':repeated_lines,
                'all_attributed_line_returns':len(returned_lines),
                'rate':repeated_lines/len(returned_lines) if returned_lines else None,
                'method':'posthoc diagnostic v1, same path/file-version/line/text; not part of frozen primary score or Recall'},
            context_quality_score=None, semantic_faithfulness_score=None, adversarial_safety_score=None)
        if 'missing reasoning_content' in (item.get('error') or ''):
            item['detailed_failure_category'] = 'provider_positive_reasoning_without_text' if missing and type(missing[-1]) is int and missing[-1]>0 else 'provider_reasoning_field_missing'
        else: item['detailed_failure_category'] = item.get('failure_category')
        if 'Truncated response' in (item.get('error') or ''):
            item['detailed_failure_category']='model_output_budget_truncation'
        if 'Undelivered native tool results' in (item.get('error') or '') and any(
                t['metadata'].get('tool_error_code')=='verification_budget_reserved' for t in tools):
            item['detailed_failure_category']='verification_budget_boundary_with_undelivered_native_calls'
        item['tariff_upper_estimate_cny'] = str(sum((Decimal(e['tariff_upper_estimate_cny']) for e in ledger
            if e['event']=='settled' and str((d/'session-b').resolve())==owners[e['id']]),Decimal(0)))
        detailed.append(item)
    # Explicitly retain every unstarted slot with its frozen identity.
    recorded = {r['run_key'] for r in detailed}
    for slot in plan:
        if slot['run_key'] not in recorded:
            detailed.append({**slot, 'status':'unstarted', 'functional_passed':None, 'workflow_passed':None,
                'input_tokens':None, 'output_tokens':None, 'detailed_failure_category':'unstarted_after_stop'})
    matched = {t for t in tasks if all(any(r['task_id']==t and r['agent']==a and r.get('functional_passed') is True
        and r.get('workflow_passed') is True and r.get('audit_valid') is True for r in detailed)
        for a in ('codetrail','apecode'))}
    groups = {}
    for agent in ('codetrail','apecode'):
        actual = [r for r in detailed if r['agent']==agent and r.get('status')!='unstarted']
        prepared = [{**r,'total_tokens':r['input_tokens']+r['output_tokens'] if type(r.get('input_tokens')) is int
                    and type(r.get('output_tokens')) is int else None} for r in actual]
        groups[agent] = {'started_runs':len(actual),'functional_passes':sum(r.get('functional_passed') is True for r in actual),
            'verified_passes':sum(r.get('functional_passed') is True and r.get('workflow_passed') is True and r.get('audit_valid') is True for r in actual),
            'protocol_rejections':sum(r.get('protocol_rejections',0) for r in actual),
            'policy_denials':sum(r.get('policy_denials',0) for r in actual),
            'failure_categories':dict(Counter(r['detailed_failure_category'] for r in actual)),
            'efficiency':resource_totals(actual,efficiency(prepared)),
            'origins':{kind:{'runs':len(g),'functional':sum(r.get('functional_passed') is True for r in g),
                'workflow':sum(r.get('workflow_passed') is True and r.get('audit_valid') is True for r in g)}
                for kind in sorted({t['origin']['kind'] for t in tasks.values()})
                if (g:=[r for r in actual if tasks[r['task_id']]['origin']['kind']==kind])}}
        groups[agent]['matched_verified_tasks'] = sorted(matched)
        groups[agent]['matched_verified_efficiency'] = efficiency([r for r in prepared if r['task_id'] in matched])
        e=groups[agent]['efficiency']
        groups[agent]['tokens_per_verified_success'] = (e['tokens_observed']/groups[agent]['verified_passes']
            if e['token_complete'] and groups[agent]['verified_passes'] else None)
    for name in ('summary.json','protocol.json','plan.json','freeze.json','runs.csv'):
        shutil.copyfile(batch/name,output/name)
    write_json(output/'runs-detailed.json',detailed,exclusive=True)
    write_json(output/'diagnostics.json',groups,exclusive=True)
    with (output/'runs-detailed.csv').open('w',encoding='utf-8',newline='') as f:
        keys=['run_key','agent','task_id','category','repeat','status','functional_passed','workflow_passed','audit_valid',
            'tool_steps','model_requests_observed','input_tokens','output_tokens','elapsed_ms','protocol_rejections',
            'policy_denials','tests_executed','tariff_upper_estimate_cny','detailed_failure_category']
        w=csv.DictWriter(f,keys,extrasaction='ignore',lineterminator='\n');w.writeheader();w.writerows(detailed)
    test = ROOT/'artifacts/glm-benchmark-full-v3-2026-11-06.xml'
    tree=ET.parse(test).getroot()
    assert not tree.findall('.//failure') and not tree.findall('.//error') and not tree.findall('.//skipped')
    shutil.copyfile(test,output/test.name)
    scoring = batch/'scoring-method.md'
    assert digest(scoring)==load_json(batch/'scoring-freeze.json')['scoring_doc_sha256']
    shutil.copyfile(scoring,output/'scoring-method.md')
    archive=ROOT/'artifacts'/(batch.name+'-evidence.zip')
    originals={p.relative_to(batch).as_posix():digest(p) for p in batch.rglob('*') if p.is_file()}
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for n in sorted(originals):z.write(batch/n,batch.name+'/'+n)
        prior=ROOT/'artifacts/glm-repair30-v1-2026-11-06'
        if prior.exists():
            for p in sorted(prior.rglob('*')):
                if p.is_file():z.write(p,'pre-formal-history/'+p.relative_to(prior).as_posix())
        z.write(scoring,'scoring-method.md')
        for n in ('UPSTREAM_NOTICE.md','LICENSE'):
            if (ROOT/n).exists():z.write(ROOT/n,'legal/'+n)
    assert all(digest(batch/n)==sha for n,sha in originals.items())
    write_json(output/'audit.json',{'at':utcnow(),'frozen_source_and_assets_valid':True,'originals_sha256':originals,
        'tasks_source_unchanged':digest(ROOT/'benchmarks/final_agent_v1/tasks.json')==load_json(batch/'protocol.json')['task_source_sha256'],
        'raw_archive':archive.relative_to(ROOT).as_posix(),'raw_archive_sha256':digest(archive)},exclusive=True)
    def fmt(v):return '未完成' if v is None else f'{v:.2f}'
    text=['# CTRepair30 已知回归集：GLM 同模型评测','',
        '主分为四类别等权的可验证修复率（0–100）。初次正式运行前冻结规则、任务、源码与预算，失败不替换。',
        '30 道不同任务（18 道上游故障复现、12 道自设计），每题每 Agent 一次，计划 60 次；不称为新留出集或 SWE-bench。','',
        '| Agent | 完整核心12题主分 | 全30题主分 | 全30题上下界 | 已记录/计划 | 功能通过 | 可验证通过 |',
        '|---|---:|---:|---|---:|---:|---:|']
    for a in ('codetrail','apecode'):
        s=summary['agents'][a]; c=summary['core_12']['agents'][a];g=groups[a]
        text.append(f'| {a} | {fmt(c["verified_score"])} | {fmt(s["verified_score"])} | {s["verified_bounds"][0]:.2f}–{s["verified_bounds"][1]:.2f} | {s["recorded_runs"]}/{s["planned_runs"]} | {g["functional_passes"]} | {g["verified_passes"]} |')
    text += ['', '未启动槽位保留为未知；预算停止时不删除分母，不给全量发布虚高单点分。每题一次，稳定性未评估。','',
        '## 效率和过程','', '| Agent | 协议拒绝 | 策略拒绝 | 共同成功样本 p50 / p95 秒 | 全部已知Token（含失败和终止部分） |', '|---|---:|---:|---|---:|']
    for a,g in groups.items():
        e=g['matched_verified_efficiency']; times=' / '.join(f'{e[k]/1000:.2f}' if e[k] is not None else '未知' for k in ('p50_ms','p95_ms'))
        text.append(f'| {a} | {g["protocol_rejections"]} | {g["policy_denials"]} | {times} (n={len(matched)}) | {g["efficiency"]["tokens_observed"]} |')
    for a,g in groups.items():
        if g['efficiency']['terminated_dispatch_wall_ms']:
            text += ['',f'{a} 的终止运行调度耗时（秒）：'+', '.join(f'{t/1000:.3f}' for t in g['efficiency']['terminated_dispatch_wall_ms'])+
                '。这些时间单独保留，不混入另一计时口径的分位数；其已知Token计入总量，未知请求仍为未知。']
    b=summary['budget']
    text += ['',f'共享预算：已观测输入＋输出 {b["known_total_tokens"]} Token，未知请求保留 {b["unknown_reserved_tokens"]} Token 预留；标价上界估算 {b["known_tariff_upper_cny"]} 元，未知请求另预留 {b["unknown_reserved_cny"]} 元。上限 2,000,000 Token / 5 元，账户实际扣款未知。预检和失败调用均在共享账本中。',
        '', f'两个 Agent 都可验证成功的共同任务数：{len(matched)}。共同成功样本的效率另存 diagnostics.json；全部失败开销仍保留在总开销及单位成功成本中。失败早退更快、更省 Token 不构成效率优势。',
        '', '普通 API `glm-5.3-flash`，具体参数及限额见 [protocol.json](protocol.json)。两 Agent 循环、提示和原生工具不同；协议异常处理也保留各自策略，不能将差异归因于单一记忆/上下文模块。',
        '', '这是固定上游循环加标准 API/策略适配器的对照，不能当作上游默认 CLI 成绩。配置常量 verification_status 沿用最初离线合同文字；真实调用确认以本批 preflight、wire 响应、usage 与独立验收证据为准，未事后改写冻结常量。',
        '', 'Agent 循环使用 Python 3.13.11，独立测试统一由固定 Python 3.10.1 的 AppContainer 后端执行；宿主可信启动器只负责内核隔离，禁止宿主执行测试及缺少内核证明的结果。初始 Python3.13 参考资格失败、预检用量和新环境的30题红绿资格记录另保留，没有更改 ACL、金标或换题。',
        '', '## 证据与限制','',
        f'- 全量自动化 {len(tree.findall(".//testcase"))} 项通过：[JUnit]({test.name})。',
        '- [计分方法](scoring-method.md)、[逐运行CSV](runs-detailed.csv)、[逐运行JSON及二次验收](runs-detailed.json)、[来源分组及失败归因](diagnostics.json)、[冻结与原始哈希/证据包索引](audit.json)。',
        '- 完整本地证据包包含逐次请求、响应、工具、最终文件、diff、两次验收和隔离审计；索引给出本地位置及 SHA256，不假称远程附件。',
        '- 原30题测试保持原样，参考补丁不进入 Agent 工作区。API Key 仅存在临时进程环境，公开前另做凭据检查。',
        '- 旧上下文证据代理存在已知限制，本轮检索质量、语义记忆、广义忠实性和对抗安全不评分。可复核重复读取与实际请求，不能将其包装为 Recall 提升。',
        '- 额外事后诊断：按实际成功编号读取中的同路径/同文件版本/同行号/同文本，统计重复返回的源码行数 / 所有已归属源码行返回数。它可以发现区间不同但大量重叠的读取；不纳入冻结主分、不声称预注册，也不等于无效阅读或语义检索质量。',
        '- 未完成 Linux Docker 和官方 verifier 资格，SWE-bench 公开线未执行；旧模型和旧评测记录均未覆盖。',
        '- 固定开发/回归题分数仅衡量该任务集与资源条件，不代表未见任务泛化能力。总分不能脱离样本规模、任务来源和失败类别独立解释。','']
    (output/'report.md').write_text('\n'.join(text),encoding='utf-8',newline='\n')
    write_json(output/'manifest-sha256.json',{p.name:digest(p) for p in output.iterdir() if p.is_file()},exclusive=True)
    print('EXPORTED',output,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();export(a.batch.resolve(),a.output.resolve())
