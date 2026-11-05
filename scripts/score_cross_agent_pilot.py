"""Post-hoc descriptive scorecard; does not rewrite or rescore original task results."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def lower_is_better(values):
    """Return a relative index only with complete, positive observations."""
    if not values or any(not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0 for v in values.values()):
        return {name: None for name in values}
    best = min(values.values())
    return {name: 100 * best / value for name, value in values.items()}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source, output):
    source = source.resolve()
    paths = {name: source/name for name in ('process-metrics.json', 'summary.json')}
    inputs = {name: json.loads(path.read_text(encoding='utf-8')) for name, path in paths.items()}
    groups = inputs['process-metrics.json']['groups']
    agents = inputs['summary.json']['agents']
    if not inputs['summary.json']['complete']:
        raise ValueError('Incomplete planned cohort; no comparative scorecard')
    if set(groups) != set(agents) or {v['runs'] for v in groups.values()} != {8}:
        raise ValueError('Expected the original complete 8+8 corrected cohort')
    values = {
        'latency': {a: v['p50_seconds'] for a, v in groups.items()},
        'tokens': {a: v['input_tokens']+v['output_tokens']
                   if isinstance(v.get('input_tokens'), int) and isinstance(v.get('output_tokens'), int) else None
                   for a, v in groups.items()},
        'tools': {a: v['mean_tools'] for a, v in groups.items()},
    }
    relative = {name: lower_is_better(v) for name, v in values.items()}
    scores = {}
    for agent, g in groups.items():
        n = agents[agent]['attempted_runs']
        assert n == g['runs']
        assert agents[agent]['functional_passes'] == g['functional']
        assert agents[agent]['workflow_passes'] == g['workflow']
        scores[agent] = {
            'functional_score': 100*g['functional']/n if n else None,
            'workflow_score': 100*g['workflow']/n if n else None,
            'latency_efficiency_score': relative['latency'][agent],
            'token_efficiency_score': relative['tokens'][agent],
            'tool_efficiency_score': relative['tools'][agent],
            'overall_score': None,
        }
    output.mkdir(parents=True, exist_ok=False)
    protocol = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'Post-hoc descriptive scorecard, not preregistered and not an overall benchmark ranking',
        'cohort': 'Corrected official DeepSeek pilot only, 4 known tasks x 2 repeats per agent, 16 runs',
        'initial_failed_cohort': 'All original 9 records, including 6 integration failures, remain in all-executions.json/CSV and original report',
        'absolute_formulas': {'functional_score': '100 * external functional passes / attempted runs',
                              'workflow_score': '100 * complete verified workflow passes / attempted runs'},
        'relative_formula': '100 * min(value over both agents) / current agent value',
        'efficiency_values': {'latency': 'p50 agent execution seconds; does not measure tail reliability',
                              'tokens': 'Total known input + output tokens over the same eight runs; not currency cost',
                              'tools': 'Mean individual tool calls, including rejected calls, over the same eight runs'},
        'missing_policy': 'If any comparison value is missing or nonpositive, relative score remains null; unknown is never zero',
        'overall_score_policy': 'Not computed: no weights frozen before the experiment; overlapping metrics could be double-counted',
        'unassessed': ['Common safety attack score', 'Long-term memory benefit', 'Retrieval Recall with common gold',
                       'Broad semantic faithfulness', 'Agent recovery under controlled injected faults', 'Generalization'],
        'comparison_limits': 'Adapted ApeCode, different prompts/tools/Python/streaming; known short tasks; best=100 is relative to this pair only',
        'input_sha256': {name: sha(p) for name, p in paths.items()},
        'scoring_script_sha256': sha(Path(__file__).resolve()),
    }
    for name, data in [('protocol.json', protocol), ('scores.json', {'scores': scores, 'raw_efficiency_values': values})]:
        (output/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    names = sorted(groups)
    lines = ['# 同模型横评分项得分（事后展示）', '',
             '**评分规则在实验结束后制定，属于描述性转换，不是预注册综合排行榜。原始验收和冻结证据未改。**', '',
             '| 分项（0–100） | CodeTrail | ApeCode受控适配版 | 计算依据 |',
             '|---|---:|---:|---|']
    labels = [('功能验收', 'functional_score', '外部验收通过数 / 已启动数 × 100，均8/8'),
              ('完整流程', 'workflow_score', '流程成功数 / 已启动数 × 100，均8/8'),
              ('耗时效率', 'latency_efficiency_score', '17.478 / 当前p50秒数 × 100'),
              ('Token效率', 'token_efficiency_score', '208071 / 当前输入输出总Token × 100'),
              ('工具调用效率', 'tool_efficiency_score', '6.875 / 当前平均工具调用 × 100')]
    def display(value): return f'{value:.2f}' if value is not None else '未评估'
    for title, key, formula in labels:
        lines.append(f"| {title} | {display(scores['codetrail'][key])} | {display(scores['apecode'][key])} | {formula} |")
    lines += ['', '功能与流程为绝对通过率转换；效率是本组相对指数，最优值为100。59分不代表只能修好59%的代码。增加或更换对照会改变相对效率分，因此不能直接跨批次比较。', '',
              '原始效率数据：CodeTrail的p50为29.477秒、总Token为230265、平均工具6.875；ApeCode分别为17.478秒、208071、7.125。Token没有按货币单价或缓存命中加权。', '',
              '**没有综合总分。** 事后选择权重会影响名次，功能/流程存在重叠，Token/工具/耗时也相关。安全、记忆、检索等缺乏本轮可靠共同观测，不补成100分、不计入总分。', '',
              '当前小题中两边正确性打平；CodeTrail工具调用略少，ApeCode耗时和Token更低。四道已知题、每题两次不足以证明整体能力优劣。', '',
              '[评分协议与输入SHA](protocol.json) · [机器可读分数](scores.json) · [原始报告](../report.md) · [全部历史执行](../all-executions.csv)', '',
              '复算：`python scripts/score_cross_agent_pilot.py --source validation/cross-agent-deepseek-pilot-2026-11-05 --output artifacts/your-new-scorecard`。输出目录必须全新，原数据只读。']
    (output/'scorecard.md').write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')
    assert all(sha(p) == protocol['input_sha256'][name] for name, p in paths.items())
    print(json.dumps(scores, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output)
