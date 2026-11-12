"""Offline scope/handoff/runtime integration demo; never a model-quality score."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pico.providers.clients import FakeModelClient
from pico.providers.native_tools import NativeToolConversation
from pico.native_context import NativeContextBudget, encoded_size
from pico.web import Workbench
from pico.persistence import write_json_atomic


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    workspace=args.output/'workspace';workspace.mkdir()
    (workspace/'public.txt').write_text('Public fixture: inspect sources before proposing conclusions.\n',encoding='utf-8')
    requests=[]
    class ScriptedNative(FakeModelClient):
        supports_native_tools=True
        def __init__(self):super().__init__([]);self.calls=0
        def begin_native_run(self,tools,context,request,command):
            self.native_conversation=NativeToolConversation(tools,context,request,command,require_reasoning=False)
        def observe_native_tool(self,result):self.native_conversation.observe(result)
        def native_notice(self,text):self.native_conversation.notice(text)
        def complete(self,prompt,max_new_tokens,**kwargs):
            self.calls+=1;c=self.native_conversation
            wire=c.prepare_payload({'max_tokens':max_new_tokens,**c.payload()})
            context=c.context_state['goal_tree'];worker=context['mode']=='execute'
            requests.append({'role':'worker' if worker else 'coordinator','payload':wire,'audit':c.last_context_audit})
            if self.calls==1:
                name,args=('read_file',{'path':'public.txt'}) if worker else ('run_task',{'task_id':context['existing_tasks'][0]['id']})
                message={'tool_calls':[{'id':'fixture-'+str(len(requests)),'type':'function',
                                       'function':{'name':name,'arguments':json.dumps(args)}}]}
            else:message={'content':('Observed the actual public file. '+ 'Public fixture detail. '*200) if worker else 'Task output is ready for manual review.'}
            return c.accept({'choices':[{'message':message,'finish_reason':'tool_calls' if 'tool_calls' in message else 'stop'}]}, {})
    app=Workbench(workspace,model_factory=ScriptedNative,harness_profile='workbench',load_env=False,
                  native_input_budget=60000,native_context_window=98304)
    goal=app.goals.action({'action':'create_goal','title':'Public research','constraints':'PUBLIC_ONLY'})
    node=app.goals.action({'action':'create_task','goal_id':goal['id'],'title':'Inspect public evidence',
                           'acceptance':'Provide source-backed observations','verification_mode':'manual'})
    app.goals.action({'action':'create_goal','title':'FOREIGN_GOAL_SENTINEL_DO_NOT_COPY'})
    parent=app.task(app.submit({'goal_id':goal['id'],'mode':'assistant','prompt':'Execute the existing inspection task.',
                               'max_steps':8,'policy':'ask'})['id'])
    with parent.condition:
        if not parent.condition.wait_for(lambda:parent.finished is not None and app.active_id is None,timeout=40):
            app.cancel(parent.id);raise RuntimeError('Offline fixture did not finish; evidence retained')
    children=[t for t in app.tasks.values() if t.id!=parent.id]
    child=children[0] if children else None
    receipt=next((e for e in parent.events if e['event']=='handoff_received'),None)
    tool=next((e for e in parent.events if e['event']=='tool_executed' and e['name']=='run_task'),None)
    full=parent.agent.evidence.get(tool['evidence_id'],parent.session_id)['content'] if tool else '{}'
    returned=json.loads(full)
    write_json_atomic(args.output/'native-requests.json',requests)
    write_json_atomic(args.output/'parent.json',parent.view())
    if child:write_json_atomic(args.output/'child.json',child.view())
    # Force a separate tiny-window pressure probe; this is synthetic protocol
    # input, not a claimed live model decision or real source-read trajectory.
    c=NativeToolConversation({},'public fixture','Keep current acceptance',None,require_reasoning=False)
    c.context_state={'acceptance':'EXACT_ACCEPTANCE','ancestor_constraints':'PUBLIC_ONLY',
                     'session_retention':{'unresolved_error':{'head':'EXACT_ASSERTION_ERROR','evidence_id':'fixture-reference'}}}
    c.messages.extend({'role':'user','content':'Historical optional data '+str(i)+' '+ ' filler'*700} for i in range(8))
    size=encoded_size(c.payload());c.context_budget=NativeContextBudget(int(size*1.05),int(size*2))
    compacted=c.prepare_payload(c.payload());audit=c.last_context_audit
    write_json_atomic(args.output/'compacted-request.json',{'payload':compacted,'audit':audit})
    checks={'coordinator_and_worker_completed':bool(child and parent.status==child.status=='completed'),
            'independent_sessions':bool(child and child.session_id!=parent.session_id),
            'bounded_handoff':bool(receipt and len(full)<=3400 and returned.get('answer_truncated')),
            'manual_acceptance_pending':app.goals.read()['tasks'][node['id']]['status']=='review',
            'foreign_goal_excluded':'FOREIGN_GOAL_SENTINEL_DO_NOT_COPY' not in json.dumps(requests),
            'soft_compaction_triggered':bool(audit['compacted_groups'] and size<audit['input_budget']),
            'mandatory_frames_preserved':audit['protected_frames_preserved'] and 'EXACT_ASSERTION_ERROR' in json.dumps(compacted)}
    report={'schema':'context-layers-offline-demo-v1','external_model_calls':0,'fixture':'Explicit scripted native responses; no model understanding claim',
            'checks':checks,'passed':all(checks.values()),'native_fixture_requests':len(requests),'compaction_audit':audit}
    write_json_atomic(args.output/'report.json',report)
    write_json_atomic(args.output/'evidence-sha256.json',{p.relative_to(args.output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in args.output.rglob('*.json') if 'workspace' not in p.relative_to(args.output).parts})
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
