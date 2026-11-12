"""Known-task memory diagnostics after wire-order repair; not a new long score."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.eval_goal_stress import Observer,dump,sources
from scripts.eval_context_sharing_live import make_app,goal,consent
from scripts.eval_long_workflows_live import PROTOCOL,plan,turn
from scripts.glm_live_support import RATES
from scripts.run_goaltree_glm import load_key
from pico.context_tokens import GLMTokenCounter

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--live',action='store_true');a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False);out=a.output.resolve()
    tasks=json.loads((ROOT/'benchmarks/long_workflows_v1/tasks.json').read_text('utf-8'))
    protocol={**PROTOCOL,'schema':'memory-wire-followup-v1','max_total_tokens':100000,'max_spend_cny':'0.2','maximum_requests':15}
    slots=[{'case':c['id'],'turn':s['id'],'spec':s,'status':'unstarted'} for c in tasks for s in plan(c) if s['id'] in {'t04','t07','t10'}]
    frozen=sources();dump(out/'freeze.json',{'protocol':protocol,'tasks':tasks,'sources':frozen,'plans':slots,
        'scope':'Known-task targeted memory diagnosis only; does not rescore previous 36-turn batch','frozen_before_first_request':True})
    for r,h in frozen.items():
        data=(ROOT/r).read_bytes()
        if hashlib.sha256(data).hexdigest()!=h:raise ValueError('Source drift during freeze')
        target=out/'source-snapshot'/r;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    dump(out/'slots.json',slots);dump(out/'budget.json',{'max_total_tokens':100000,'max_spend_cny':'0.2','rates':{**RATES,'checked_date':'2026-11-12'}})
    if not a.live:return
    key=load_key(ROOT/'.env.glm.local')
    if not key:raise ValueError('Ordinary GLM key unavailable')
    os.environ['BIGMODEL_API_KEY']=key;o=Observer(out,protocol,frozen);counter=GLMTokenCounter();apps={};nodes={}
    for c in tasks:
        app=make_app(out/c['id']/'workspace',key,o,counter)
        donor=goal(app,c['title'],constraints=f'goal_code={c["code"]}; PUBLIC_ONLY; manual acceptance only.',
            context_notes=f'公开协作代号 shared_code={c["shared"]}。只适用于本批公开合成协作。')
        recipient=goal(app,'第二目标 '+c['title'],constraints=f'goal_code={c["secondary"]}; different Goal; PUBLIC_ONLY; manual acceptance only.')
        apps[c['id']]=app;nodes[c['id']]={'A':donor,'B':recipient,'leaf':{}};dump(out/c['id']/'fixture.json',nodes[c['id']])
    for slot in slots:
        if o.halted:break
        app=apps[slot['case']];n=nodes[slot['case']];o.turn_dir=out/slot['case']/slot['turn'];dump(o.turn_dir/'input.json',slot['spec'])
        slot['status']='started';dump(out/'slots.json',slots)
        try:
            if slot['turn']=='t07':consent(app,n['A'],True);consent(app,n['B'],True)
            if slot['turn']=='t10':consent(app,n['B'],False)
            r=turn(app,n['B'],slot['spec'],o);slot['status']=r['status']
        except Exception as exc:
            slot.update(status='runner_failed',error={'type':type(exc).__name__,'detail':str(exc)[:300]});o.halted='Runner failure'
            dump(o.turn_dir/'runner-error.json',slot['error'])
        dump(out/'slots.json',slots);print(json.dumps({'case':slot['case'],'turn':slot['turn'],'status':slot['status'],'tokens':o.budget.token_state()}),flush=True)
    dump(out/'summary.json',{'halted':o.halted,'sources_unchanged':sources()==frozen,'tokens':o.budget.token_state(),'model_requests':o.calls})
    subprocess.run([sys.executable,str(ROOT/'scripts/verify_long_workflows.py'),'--output',str(out)],check=True)
    dump(out/'evidence-sha256.json',{p.relative_to(out).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file() and p.name!='evidence-sha256.json'})

if __name__=='__main__':main()
