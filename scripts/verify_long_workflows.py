"""Stdlib-only external observation checker. Never imports Agent runtime."""
import argparse
import csv
import json
from pathlib import Path

def load(p):return json.loads(p.read_text(encoding='utf-8'))
def obj(text):
    text=text.strip()
    if text.startswith('```json\n') and text.endswith('```'):text=text[8:-3].strip()
    try:value=json.loads(text)
    except (ValueError,TypeError):return {}
    return value if isinstance(value,dict) else {}

def verify(root):
    frozen=load(root/'freeze.json');slots=load(root/'slots.json');rows=[]
    for slot in slots:
        d=root/slot['case']/slot['turn'];path=d/'result.json';spec=slot['spec'];checks={}
        if not path.exists():
            rows.append({'case':slot['case'],'turn':slot['turn'],'status':slot['status'],'passed':False,'checks':{},'failure':slot.get('error','unstarted')});continue
        r=load(path);value=obj(r.get('answer',''));events=r.get('events',[])
        checks['completed']=r['status']=='completed'
        checks.update({'field:'+k:type(value.get(k)) is type(v) and value[k]==v for k,v in spec.get('gold',{}).items()})
        checks['foreign_goals_unchanged']=r['foreign_before']==r['foreign_after']
        checks['foreign_tasks_unchanged']=r['foreign_tasks_before']==r['foreign_tasks_after']
        responses=[load(p) for p in d.glob('responses/*.json')]
        checks['returned_model']=bool(responses) and all(x['metadata'].get('provider_model')==frozen['protocol']['model'] for x in responses)
        checks['no_protocol_errors']=not any(x['metadata'].get('protocol_error') for x in responses)
        tools=[e for e in events if e.get('event')=='tool_executed']
        if spec.get('read_required'):checks['actual_file_read']=any(e.get('name')=='read_file' and e.get('tool_status')=='ok' for e in tools)
        if spec.get('missing_required'):checks['actual_failure_observed']=any(e.get('name')=='read_file' and e.get('tool_status')!='ok' for e in tools)
        if 'shared' in spec:
            checks['shared_retrieval']=bool(sum(e.get('count',0) for e in events if e.get('event')=='shared_memory_retrieved'))==spec['shared']
        fixture=load(root/slot['case']/'fixture.json');state=r['state_after']
        if spec.get('create_title'):checks['task_created']=any(t['title']==spec['create_title'] and t['goal_id']==fixture['A']['id'] for t in state['tasks'].values())
        if spec.get('acceptance_updated'):checks['acceptance_saved']=state['tasks'][fixture['leaf']['id']]['acceptance']=='CITE_PHYSICAL_LINES'
        if spec.get('child_required'):
            child=r.get('child_runs',[]);received=[e for e in events if e.get('event')=='handoff_received']
            checks['actual_child']=len(child)==1 and len(received)==1
            checks['independent_child_session']=bool(child) and child[0]['session_id']!=r['session_id']
            checks['child_actual_read']=bool(child) and any(e.get('event')=='tool_executed' and e.get('name')=='read_file' and e.get('tool_status')=='ok' for e in child[0]['events'])
            checks['manual_acceptance_pending']=state['tasks'][fixture['leaf']['id']]['status']=='review'
        rows.append({'case':slot['case'],'turn':slot['turn'],'status':r['status'],'passed':all(checks.values()),'checks':checks,
            'tool_steps':r.get('tool_steps'),'model_requests':len(responses),'failure':None if all(checks.values()) else r.get('error') or 'answer_or_process_contract'})
    summary={'schema':'long-workflows-observation-v1','planned_cases':len(frozen['tasks']),'planned_turns':len(rows),
        'started_turns':sum(r['status']!='unstarted' for r in rows),'passed_turns':sum(r['passed'] for r in rows),'rows':rows,
        'per_case':{c['id']:{'planned':sum(r['case']==c['id'] for r in rows),'started':sum(r['case']==c['id'] and r['status']!='unstarted' for r in rows),'passed':sum(r['case']==c['id'] and r['passed'] for r in rows)} for c in frozen['tasks']},
        'limitations':['Known synthetic development workflows, not held-out benchmark.','Goal switches are host-selected chat bindings, not autonomous cross-Goal routing.','Pressure compaction is measured separately; these live conversations do not fill 1M tokens.']}
    (root/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n','utf-8')
    with (root/'turns.csv').open('w',encoding='utf-8',newline='') as f:
        fields=['case','turn','status','passed','model_requests','tool_steps','failure'];w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k) for k in fields} for r in rows)
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();r=verify(a.output)
    print(json.dumps({k:r[k] for k in ('planned_cases','planned_turns','started_turns','passed_turns','per_case')}))
