"""Independent post-run oracle: only frozen assets and exported observations."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def parse(text):
    text=text.strip()
    if text.startswith('```json\n') and text.endswith('```'):text=text[8:-3].strip()
    try:value=json.loads(text)
    except (ValueError,TypeError):return None
    return value if isinstance(value,dict) else None


def verify(out):
    frozen=load(out/'freeze.json');fixtures=load(out/'fixtures.json');rows=[]
    slots={r['id']:r for r in load(out/'slots.json')} if (out/'slots.json').exists() else {}
    for spec in frozen['plan']:
        case=out/spec['id'];p=case/'result.json'
        if not p.exists():
            status=slots.get(spec['id'],{}).get('status','unstarted')
            rows.append({'id':spec['id'],'kind':spec['kind'],'status':status,'passed':False,'checks':{},
                         'failure':'unstarted' if status=='unstarted' else 'runner_or_setup_failure'});continue
        result=load(p);obj=parse(result.get('answer',''));checks={}
        for key,expected in spec['gold'].items():checks['field:'+key]=type((obj or {}).get(key)) is type(expected) and obj[key]==expected
        events=result.get('events',[])
        requests=[load(p) for p in sorted((case/'requests').glob('*.json'))]
        responses=[load(p) for p in sorted((case/'responses').glob('*.json'))]
        checks['run_completed']=result['status']=='completed'
        checks['all_provider_models_correct']=bool(responses) and all(r.get('metadata',{}).get('provider_model')==frozen['protocol']['model'] for r in responses)
        checks['no_observed_protocol_errors']=not any(e.get('event')=='protocol_error' for e in events) and not any(r.get('metadata',{}).get('protocol_error') for r in responses)
        if spec['kind']=='sharing':
            consent=load(case/'consent.json')
            if spec['expected_shared']:
                checks['both_consents_enabled']=consent['donor']['enabled'] and consent['recipient']['enabled']
            if spec['id'] in {'sharing-default-off','sharing-close-existing'}:
                checks['recipient_is_off']=consent['recipient']['enabled'] is False
            if spec['id']=='sharing-source-revoked':checks['source_is_off']=consent['donor']['enabled'] is False
            if spec['id']=='sharing-core-source':checks['source_is_core']=consent['source_level']=='core'
            seen=sum(e['count'] for e in events if e['event']=='shared_memory_retrieved')
            advisory=[]
            for r in requests:
                for message in r['payload_without_reasoning']['messages']:
                    text=message.get('content') or ''
                    if isinstance(text,str) and text.startswith('Current retrieved memory (advisory data'):
                        try:advisory+=json.loads(text.split('\n',1)[1])
                        except ValueError:pass
            shared=[r for r in advisory if r.get('kind')=='shared_advisory']
            checks['shared_retrieval_matches_setting']=(seen>0)==spec['expected_shared']
            checks['actual_shared_frame_matches_setting']=bool(shared)==spec['expected_shared']
            checks['no_tool_execution']=not any(e.get('event')=='tool_executed' for e in events)
            checks['source_and_conditions_retained']=all(r.get('source_goal_id')==fixtures['donor']['id'] and r.get('conditions') for r in shared)
            checks['no_foreign_goal_mutation']=result['state_after']['goals'][fixtures['donor']['id']]['context_notes']==fixtures['donor']['context_notes']
            if not spec.get('existing_history_may_contain_code') and not spec['expected_shared']:
                checks['source_value_not_in_request']='CORAL-R82' not in json.dumps([r['payload_without_reasoning'] for r in requests])
        elif spec['kind']=='handoff':
            children=result.get('child_runs',[]);received=[e for e in events if e.get('event')=='handoff_received']
            checks['one_actual_child']=len(children)==1
            checks['one_handoff_received']=len(received)==1
            checks['separate_sessions']=bool(children) and children[0]['session_id']!=result['session_id']
            checks['child_actual_read']=bool(children) and any(e.get('event')=='tool_executed' and e.get('name')=='read_file' and e.get('tool_status')=='ok' for e in children[0]['events'])
            checks['manual_acceptance_pending']=result['state_after']['tasks'][fixtures['handoff_task']['id']]['status']=='review'
            checks['no_file_changes']=not result.get('diffs') and all(not c.get('diffs') for c in children)
            checks['verification_not_self_certified']=bool(received) and received[0]['acceptance']['status']=='review'
            response=load(case/'handoff-response.json')['content'] if (case/'handoff-response.json').exists() else ''
            envelope=parse(response) or {}
            checks['bounded_structured_return']=bool(envelope) and len(response)<=3400 and bool(envelope.get('detail_evidence_id'))
        else:
            audit=result.get('context_audit',{});checks['actual_compaction']=audit.get('compacted_groups',0)>0
            checks['mandatory_preserved']=audit.get('protected_frames_preserved') is True
            checks['under_original_pressure_budget']=audit.get('after_estimate',10**9)<=frozen['protocol']['pressure_byte_limit']
            checks['no_executable_model_tool']=all(r.get('parsed_action_kind')=='final' for r in responses)
            history=load(case/'controlled-history.json')
            checks['real_host_reads']=sum(r['metadata'].get('tool_status')=='ok' for r in history['receipts'])==8
        rows.append({'id':spec['id'],'kind':spec['kind'],'status':result['status'],'passed':all(checks.values()),'checks':checks,
                     'model_requests':len(requests),'tool_steps':result.get('tool_steps'),
                     'failure':None if all(checks.values()) else 'runtime_or_provider' if result.get('error') else 'model_decision_or_contract_check'})
    ledger=[json.loads(line) for line in (out/'ledger.jsonl').read_text().splitlines()] if (out/'ledger.jsonl').exists() else []
    settled=[e for e in ledger if e['event']=='settled'];done={e['id'] for e in settled}
    unknown=[e for e in ledger if e['event']=='reserved' and e['id'] not in done]
    summary={'schema':'context-sharing-live-verification-v1','planned_cases':len(rows),'started_cases':sum(r['status']!='unstarted' for r in rows),
        'passed_cases':sum(r['passed'] for r in rows),'rows':rows,'known_requests':len(settled),'unknown_requests':len(unknown),
        'known_input_tokens':sum(e['usage']['prompt_tokens'] for e in settled),'known_output_tokens':sum(e['usage']['completion_tokens'] for e in settled),
        'unknown_reserved_tokens':sum(e['reserved_tokens'] for e in unknown),'actual_billed_cost':None,
        'gold_and_plan_sha256':hashlib.sha256(json.dumps(frozen['plan'],sort_keys=True).encode()).hexdigest(),
        'limitations':['Known synthetic feature diagnostics, not held-out benchmark.','Pressure history is controlled host-generated tool execution, not an autonomous long-model trajectory.',
                       'Closed existing conversation may recall previously generated content; revocation checks fresh retrieval, not semantic erasure.',
                       'No causal comparison or repeated-run stability claim.']}
    (out/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (out/'cases.csv').open('w',encoding='utf-8',newline='') as f:
        fields=['id','kind','status','passed','model_requests','tool_steps','failure'];writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        writer.writerows({k:r.get(k) for k in fields} for r in rows)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();r=verify(args.output)
    print(json.dumps({k:r[k] for k in ('planned_cases','started_cases','passed_cases','known_requests','unknown_requests','known_input_tokens','known_output_tokens')}))
