"""Posthoc four-dimension system view; no model calls or changed frozen scores."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pico.evaluation.system_diagnostics import diagnostics
from scripts.report_glm_benchmark import jsonlines, native_continuity


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values)-1)*q
    lo = int(position)
    return values[lo]+(values[min(lo+1,len(values)-1)]-values[lo])*(position-lo)


def verified(row):
    return all(row.get(k) is True for k in ('functional_passed','workflow_passed','audit_valid','scope_valid'))


def metrics(rows):
    reads = [r.get('repeated_reads') or {} for r in rows]
    lines = [r.get('overlapping_returned_lines_posthoc') or {} for r in rows]
    rejected = [r for r in rows if r.get('protocol_rejections',0)>0]
    usage_complete = bool(rows) and all(r.get('requests_without_observed_usage')==0 for r in rows)
    elapsed = [r['elapsed_ms'] for r in rows if type(r.get('elapsed_ms')) is int]
    return {
        'attempts':len(rows),'functional_passes':sum(r.get('functional_passed') is True for r in rows),
        'verified_passes':sum(verified(r) for r in rows),
        'observed_tool_steps':sum(r.get('observed_tool_steps',0) for r in rows),
        'protocol_rejections':sum(r.get('protocol_rejections',0) for r in rows),
        'tasks_with_protocol_rejections':len(rejected),
        'functional_passes_in_tasks_with_protocol_rejections':sum(r.get('functional_passed') is True for r in rejected),
        'policy_denials':sum(r.get('policy_denials',0) for r in rows),
        'scope_valid_runs':sum(r.get('scope_valid') is True for r in rows),
        'scope_invalid_runs':sum(r.get('scope_valid') is False for r in rows),
        'scope_unknown_runs':sum(r.get('scope_valid') is None for r in rows),
        'exact_repeat_numerator':sum(v.get('numerator',0) for v in reads),
        'exact_repeat_denominator':sum(v.get('denominator',0) for v in reads),
        'line_overlap_numerator':sum(v.get('duplicate_line_returns',0) for v in lines),
        'line_overlap_denominator':sum(v.get('all_attributed_line_returns',0) for v in lines),
        'known_input_tokens':sum(r.get('known_input_tokens',0) for r in rows),
        'known_output_tokens':sum(r.get('known_output_tokens',0) for r in rows),
        'usage_complete':usage_complete,
        'requests_without_observed_usage':sum(r.get('requests_without_observed_usage',0) for r in rows),
        'elapsed_observed_runs':len(elapsed),'elapsed_p50_ms':percentile(elapsed,.5),'elapsed_p95_ms':percentile(elapsed,.95),
        'terminated_dispatch_wall_ms':[r['controller_terminal_wall_ms'] for r in rows if 'controller_terminal_wall_ms' in r],
        'recall_at_k':None,'semantic_context_loss_rate':None,'memory_benefit':None,'adversarial_safety_score':None}


def main(baseline, compatibility, output):
    result={'method':'posthoc-system-view-v1','model_calls':0,'new_benchmark_attempts':0,
        'scope':'Configured Agent + model + adapter, descriptive system comparison; not a single-factor causal experiment',
        'all_original_grades_unchanged':True,'new_composite_score':None,'cohorts':{}}
    trace_rows=[]
    for label, directory in [('baseline',baseline),('compatibility',compatibility)]:
        plan=load(directory/'plan.json');all_rows=load(directory/'runs-detailed.json')
        rows=[r for r in all_rows if r.get('status')!='unstarted']
        core=set(load(directory/'protocol.json')['core_task_ids'])
        rows=[r for r in rows if r['task_id'] in core]
        raw=ROOT/Path(load(directory/'audit.json')['raw_archive']).with_name(
            Path(load(directory/'audit.json')['raw_archive']).name.removesuffix('-evidence.zip'))
        raw_tasks={t['id']:t for t in load(raw/'tasks.json')} if (raw/'tasks.json').exists() else {}
        for row in rows:
            d=raw/'runs'/row['run_key'];session=d/'session-b'
            if not (session/'tools.jsonl').exists():
                trace_rows.append({'cohort':label,'agent':row['agent'],'run_key':row['run_key'],'observed':False})
                continue
            tools=jsonlines(session/'tools.jsonl');wire=jsonlines(session/'wire.jsonl')
            events=jsonlines(session/'process/events.jsonl')
            ext=d/'second-verification-result.json'
            detail=diagnostics(tools,wire,events,external=load(ext) if ext.exists() else None,
                expected_tests=raw_tasks.get(row['task_id'],{}).get('expected_tests'),
                session_complete=(d/'session-result.json').exists() or (session/'session-result.json').exists())
            detail['native_continuity']=native_continuity(wire,tools)
            trace_rows.append({'cohort':label,'agent':row['agent'],'run_key':row['run_key'],
                'observed':True,'evidence_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [session/'tools.jsonl',session/'wire.jsonl',session/'process/events.jsonl'] if p.exists()},
                'metrics':detail})
        agents=('codetrail','apecode')
        attempted={a:{r['task_id'] for r in rows if r['agent']==a} for a in agents}
        paired=set.intersection(*attempted.values())
        matched={t for t in paired if all(any(r['task_id']==t and r['agent']==a and verified(r) for r in rows) for a in agents)}
        group={}
        for a in agents:
            mine=[r for r in rows if r['agent']==a]
            group[a]={'all_started':metrics(mine),'paired_started':metrics([r for r in mine if r['task_id'] in paired]),
                'matched_verified':metrics([r for r in mine if r['task_id'] in matched]),
                'core_planned_slots':sum(s['agent']==a and s['task_id'] in core for s in plan),
                'unstarted_core_task_ids':sorted(core-attempted[a])}
        result['cohorts'][label]={'source':directory.as_posix(),'source_sha256':{
            name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in ['plan.json','runs-detailed.json','summary.json']},
            'core_task_ids':sorted(core),'paired_started_task_ids':sorted(paired),
            'matched_verified_task_ids':sorted(matched),'agents':group}
    for cohort in result['cohorts'].values():
        for group in cohort['agents'].values():
            for population in ('all_started','paired_started','matched_verified'):
                m=group[population]
                assert 0 <= m['verified_passes'] <= m['functional_passes'] <= m['attempts']
                assert m['scope_valid_runs']+m['scope_invalid_runs']+m['scope_unknown_runs']==m['attempts']
            m=group['matched_verified']
            assert m['verified_passes']==m['attempts']
    output.mkdir(parents=True,exist_ok=False)
    (output/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    (output/'trace-metrics.json').write_text(json.dumps(trace_rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    with (output/'metrics.csv').open('w',encoding='utf-8',newline='') as f:
        fields=['cohort','agent','population','attempts','functional_passes','verified_passes','observed_tool_steps',
            'known_input_tokens','known_output_tokens','usage_complete','elapsed_p50_ms','elapsed_p95_ms',
            'policy_denials','protocol_rejections','exact_repeat_numerator','exact_repeat_denominator',
            'line_overlap_numerator','line_overlap_denominator','scope_invalid_runs','scope_unknown_runs']
        writer=csv.DictWriter(f,fields,extrasaction='ignore');writer.writeheader()
        for label,c in result['cohorts'].items():
            for agent,g in c['agents'].items():
                for pop in ['all_started','paired_started','matched_verified']:
                    writer.writerow({'cohort':label,'agent':agent,'population':pop,**g[pop]})
    print('EXPORTED',output,'; no model calls or changes to source records')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--compatibility',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();main(args.baseline,args.compatibility,args.output)
