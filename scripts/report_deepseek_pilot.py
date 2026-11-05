"""Independent revalidation and descriptive reporting; never changes run results."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pico.evaluation.cross_agent import snapshot,scope_valid,verify_candidate
from pico.evaluation.repair_suite import digest,load_json,write_json


def rows(output):
    return [load_json(p) for p in sorted((output/'runs').glob('*/result.json'))]


def audit(output):
    target=output/'independent-audit'
    if (target/'summary.json').exists():return load_json(target/'summary.json')
    target.mkdir(exist_ok=False)
    tasks={t['id']:t for t in load_json(output/'tasks.json')};checks=[]
    before={p.relative_to(output).as_posix():digest(p) for p in (output/'runs').glob('*/result.json')}
    for row in rows(output):
        task=tasks[row['task_id']];directory=output/'runs'/row['run_key'];source=directory/'final-workspace'
        actual=snapshot(source);same=actual==row['final_sha256']
        implementation={n:(source/n).read_bytes() for n in task['editable_files'] if actual.get(n) not in (None,'REPARSE_POINT')}
        verifier=verify_candidate(task,implementation,target/row['run_key'])
        passed=bool(verifier['accepted'] and scope_valid(task,actual))
        check={'run_key':row['run_key'],'source_snapshot_matches':same,'revalidated_functional_pass':passed,
               'agrees_with_original_result':passed==row['functional_passed'],'external':verifier}
        checks.append(check);print('AUDIT '+row['run_key']+' '+str(check['agrees_with_original_result']),flush=True)
    unchanged=all(digest(output/n)==sha for n,sha in before.items())
    result={'runs':len(checks),'all_source_snapshots_match':all(c['source_snapshot_matches'] for c in checks),
            'all_results_reproduced':all(c['agrees_with_original_result'] for c in checks),'original_results_unchanged':unchanged,
            'revalidated_functional_passes':sum(c['revalidated_functional_pass'] for c in checks),'checks':checks}
    write_json(target/'summary.json',result,exclusive=True)
    if not (result['all_source_snapshots_match'] and result['all_results_reproduced'] and unchanged):
        raise RuntimeError('Independent audit mismatch; original records retained')
    return result


def events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def details(output,row):
    directory=output/'runs'/row['run_key'];tools=events(directory/'tools.jsonl')
    reads=[];seen=set();repeats=0
    for tool in tools:
        if tool['name']=='read_file' and not tool.get('rejected'):
            args=dict(tool['args']);path=args.pop('path','').replace('\\','/')
            if Path(path).is_absolute():
                try:path=Path(path).relative_to(load_json(directory/'started.json')['workspace']).as_posix()
                except ValueError:pass
            sha=tool.get('before',{}).get(path)
            key=(path,sha,json.dumps(args,sort_keys=True))
            if sha is not None and key in seen:repeats+=1
            seen.add(key);reads.append(key)
    protocol=[]
    for event in events(directory/'model_io.jsonl'):
        if event.get('usage',{}).get('protocol_error'):protocol.append(event['usage']['protocol_error'])
    usage=[];models=set();fingerprints=set()
    for event in events(directory/'wire.jsonl'):
        if event.get('event')!='response':continue
        data=event['body'] if isinstance(event['body'],dict) else json.loads(event['body'])
        usage.append(data.get('usage') or {});models.add(data.get('model'));fingerprints.add(data.get('system_fingerprint'))
    return {'run_key':row['run_key'],'agent':row['agent'],'task_id':row['task_id'],'repeat':row['repeat'],
        'functional_passed':row['functional_passed'],'workflow_passed':row['workflow_passed'],
        'tool_steps':row.get('tool_steps'),'elapsed_ms':row.get('elapsed_ms'),'input_tokens':row.get('input_tokens'),
        'output_tokens':row.get('output_tokens'),'read_calls':len(reads),'exact_repeated_reads':repeats,
        'protocol_errors':protocol if row['agent']=='codetrail' else None,'explicit_denials':sum(t.get('rejected',False) for t in tools),
        'source_scope_valid':row['scope_valid'],'failure_category':row['failure_category'],'agent_error':row.get('agent_error'),
        'observed_model_ids':sorted(m for m in models if m),'fingerprints':sorted(m for m in fingerprints if m),
        'raw_response_usage':usage}


def percentile(values,q):
    values=sorted(values)
    if not values:return None
    position=(len(values)-1)*q;low=int(position);high=min(low+1,len(values)-1)
    return values[low]+(values[high]-values[low])*(position-low)


def render(output,public,do_audit):
    public.mkdir(parents=True,exist_ok=False)
    original=ROOT/'artifacts/cross-agent-deepseek-pilot-2026-11-05'
    audited=audit(output) if do_audit else load_json(output/'independent-audit/summary.json')
    old_audit=audit(original) if do_audit else load_json(original/'independent-audit/summary.json')
    current=rows(output);records=[details(output,r) for r in current];summary=load_json(output/'summary.json')
    write_json(public/'runs.json',records,exclusive=True)
    write_json(public/'summary.json',summary,exclusive=True)
    cost=load_json(output/'cost-report.json');groups={}
    for name in ('codetrail','apecode'):
        subset=[r for r in records if r['agent']==name]
        groups[name]={'runs':len(subset),'functional':sum(r['functional_passed'] for r in subset),
            'workflow':sum(r['workflow_passed'] for r in subset),
            'protocol_errors':sum(len(r['protocol_errors']) for r in subset) if name=='codetrail' else None,
            'read_calls':sum(r['read_calls'] for r in subset),'exact_repeated_reads':sum(r['exact_repeated_reads'] for r in subset),
            'mean_tools':sum(r['tool_steps'] for r in subset)/len(subset) if subset else None,
            'input_tokens':sum(r['input_tokens'] for r in subset) if subset and all(r['input_tokens'] is not None for r in subset) else None,
            'output_tokens':sum(r['output_tokens'] for r in subset) if subset and all(r['output_tokens'] is not None for r in subset) else None,
            'p50_seconds':percentile([r['elapsed_ms']/1000 for r in subset],.5),
            'p95_seconds':percentile([r['elapsed_ms']/1000 for r in subset],.95),
            'explicit_denials':sum(r['explicit_denials'] for r in subset)}
    write_json(public/'process-metrics.json',{'groups':groups,'percentiles':'linear interpolation at (n-1)*q',
        'repeat_definition':'Same read_file arguments, canonical file path and unchanged before-file SHA; overlapping ranges not assessed',
        'protocol_definition':'CodeTrail explicit native parser protocol_error; ApeCode has no equivalent parser diagnostic and is null; raw events retained. Neither is a broad semantic correctness score',
        'cost':cost,'independent_audit':audited,'initial_invalid_batch_audit':old_audit},exclusive=True)
    lines=['# CodeTrail / ApeCode 同模型小规模修复试验','',
        '日期：2026-11-05（Asia/Shanghai）。本轮只涉及4道已有、此前评测过的短任务，不是新留出集或完整仓库排行榜。', '',
        f"有效对照批次计划16次，实际{len(current)}次。功能结果与流程结果分开统计；首批接入错误批次另列，不删除、替换或混入能力排名。",'',
        '| Agent | 运行 | 功能验收 | 完整流程 | 平均工具调用 | 输入Token | 输出Token | p50秒 | p95秒 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name,g in groups.items():
        lines.append(f"| {name} | {g['runs']} | {g['functional']}/{g['runs']} | {g['workflow']}/{g['runs']} | {g['mean_tools']:.2f} | {g['input_tokens']} | {g['output_tokens']} | {g['p50_seconds']:.2f} | {g['p95_seconds']:.2f} |")
    lines+=['','## 逐题分布','','| 任务 | CodeTrail功能/流程 | ApeCode功能/流程 |','|---|---|---|']
    for task in load_json(output/'tasks.json'):
        cells=[]
        for name in ('codetrail','apecode'):
            subset=[r for r in current if r['agent']==name and r['task_id']==task['id']]
            cells.append(f"{sum(r['functional_passed'] for r in subset)}/{len(subset)}；{sum(r['workflow_passed'] for r in subset)}/{len(subset)}")
        lines.append('| '+task['id']+' | '+' | '.join(cells)+' |')
    lines+=['','## 费用与余额','',
        f"初始余额{cost['initial_balance_cny']}元，最后查询{cost['current_balance_cny']}元；账户可观察差额{cost['observed_account_delta_cny']}元，包含首批、协议预检及入口小样例。不能将其他可能的账户活动也归因为本次试验。",
        f"基于返回usage和运行时记录费率的已知用量估算合计{cost['tariff_estimate_known_cny']}元；不是独立账单。未知请求{len(cost['unknown_request_ids'])}个。账户余额结算/显示精度和费率时段可能使估算与余额差不同，两个数分别保留。",
        '总预算2.50元，逐请求余额与保守高峰费率预留，未知用量不按0计。两批沿用原始2.71元起点；导入账本为既有请求ID增加命名空间，原始账本与映射证据均保留。','',
        '## 首批接入故障与重做规则','',
        '首批已启动9次：3条CodeTrail修复通过，6条ApeCode在模型请求前因旧凭据名KeyError失败，7槽位未启动。根因为脚本模块名与包模块名导致加载两份适配模块。它是评测接入错误，不能据此判断ApeCode修复能力。',
        '发现后停止控制器，允许正在执行的工作器完成并落盘；9条结果和源码快照全部保留。修复模块别名，新增回归，并用实际模型经同一入口修复独立小样例后，另冻完整16次批次，双边全部重做，未只补失败槽位。',
        f"本轮两正式批次共启动{len(rows(original))+len(current)}次，仍只有4道不同任务；另有非计分入口小样例及协议预检。最初硅基流动批次0次启动也保留，不与本轮合并。",'',
        '## 过程、安全和结论边界','']
    for name,g in groups.items():
        protocol_text=str(g['protocol_errors']) if g['protocol_errors'] is not None else '未评估（无等价诊断器）'
        lines.append(f"- {name}：相同参数及文件SHA的重复读取{g['exact_repeated_reads']}/{g['read_calls']}；明确策略拒绝{g['explicit_denials']}；记录的原生协议诊断{protocol_text}。")
    lines+=['',
        'ApeCode没有与CodeTrail等价的协议诊断器，不能将缺少该诊断字段当作所有协议均正确。以原始API错误、工具记录和外部验收进一步核对。',
        f"独立第二次外部验收：有效批次{audited['runs']}条、首批{old_audit['runs']}条均与原始功能结果一致；原始结果JSON和最终源文件SHA未改变。AppContainer运行，未放宽ACL、未开放额外宿主Shell，验收测试按冻结原件恢复。",
        '文件越界和最终测试改动单独核对；没有开展新的对抗安全集，不能由本轮正常修复推导任意Prompt Injection防护能力。',
        '相同模型deepseek-flash、Thinking low、top_p=0.95、输出上限4096、20工具调用、30模型请求、300秒、并发1；temperature在Thinking模式不生效，因此未发送。reasoning_content按原生协议回传，不混用DSML文本工具。',
        'ApeCode是保留原始循环/默认工具并接入固定策略和AppContainer的受控适配版，不是原版CLI。两边工具schema、系统提示、上下文治理、流式方式和Python版本不同；即使结果有差异，也不能归因于某一个记忆/检索功能。',
        '每题仅2次，样本小、题目已知，无法证明泛化优越性；单会话短任务也未检验长期记忆和仓库演化收益。未变更历史Benchmark指标。','',
        '## 证据与复现','',
        '[完整证据](evidence.zip) · [SHA256](evidence.sha256) · [逐运行JSON](runs.json) · [过程指标](process-metrics.json)。原始CSV、两份冻结、源码/上游许可证、完整轨迹、差异、外部验收、账本和首批故障在证据包中。',
        '运行方法见仓库scripts/run_deepseek_pilot.py及原同模型试验说明。密钥只从DEEPSEEK_API_KEY环境变量读取，不写入命令参数或仓库。恢复只启动未开始槽位，已有目录不替换。',
        '官方依据：[模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[Thinking与reasoning_content回传](https://api-docs.deepseek.com/guides/thinking_mode/)。']
    (public/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(groups),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--public',type=Path,required=True);parser.add_argument('--audit',action='store_true')
    args=parser.parse_args();render(args.output.resolve(),args.public.resolve(),args.audit)
