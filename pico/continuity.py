"""Small, typed continuity views derived from host state, never model thought."""
import hashlib
import json

from .retrieval import tokens


def goal_capsule(state, goal_id, task_id=''):
    """Current contracts plus an explicit frontier; no other Goal's prose."""
    goal=state['goals'][goal_id]
    tasks={key:t for key,t in state['tasks'].items() if t['goal_id']==goal_id}
    from .task_progress import task_progress
    progress=task_progress(state)
    def contract(item):
        return {k:item[k] for k in ('id','spec_version','constraints','acceptance','context_notes')}
    current=tasks.get(task_id)
    contracts=[contract(goal)]
    visible=set(tasks) if not current else {current['id']}
    if current:
        lineage=[];node=current
        while node:
            lineage.append(node);node=tasks.get(node['parent_id'])
        contracts.extend(contract(t) for t in reversed(lineage))
        visible.update(t['id'] for t in lineage)
        visible.update(current['depends_on'])
    frontier=[]
    for task in tasks.values():
        if task['id'] not in visible:continue
        if task['context_status']=='archived' or task['status']=='cancelled':continue
        blocked=[key for key in task['depends_on'] if tasks[key]['context_status']=='archived'
                 or not progress[key]['accepted']]
        if progress[task['id']]['accepted'] and not blocked:continue
        issues=[{'id':key,'title':tasks[key]['title'],'status':tasks[key]['status'],
                 'validity':tasks[key]['validity'],'archived':tasks[key]['context_status']=='archived'} for key in blocked]
        row={'id':task['id'],'title':task['title'],'spec_version':task['spec_version'],'status':task['status'],
                         'validity':task['validity'],'blocked_by':blocked,
                         'ready_for_requested_execution':not blocked and task['status'] in {'planned','blocked'}}
        if issues:row['dependency_issues']=issues
        for key in ('recovery_reason','invalidated_reason'):
            if task.get(key):row[key]=task[key]
        evidence=[e['evidence_id'] for e in task.get('deliverables',[])[-2:] if e.get('evidence_id')]
        if evidence:row['evidence_ids']=evidence
        frontier.append(row)
    frontier.sort(key=lambda row:(row['id']!=task_id,not bool(row['blocked_by']),row['id']))
    return {'schema':'goal-continuity-v1','goal_id':goal_id,'task_id':task_id,
            'contracts':contracts,'frontier':frontier[:30],'frontier_omitted':max(0,len(frontier)-30),
            'rule':'Current versions prevail; notes and references are not acceptance or permission. Resolve dependencies first; '
                   'archived prerequisites require reopening. Ready is not authorization. Inspect uncertain effects before resuming; do not replay completed effects.'}


def retention_key(event,query):
    effects={'write_file','patch_file','apply_changes','rewind_changes','goal_action','plan_tasks','run_task'}
    failure=any(word in str(event.get('result_preview','')).lower() for word in ('error','failed','rejected','traceback'))
    historical_request = str(event.get('notice', '')).startswith('Historical user request #')
    category=('historical_request' if historical_request else 'correction' if 'notice' in event else 'change' if event.get('tool') in effects
              else 'verification' if event.get('tool')=='run_shell' else 'failure' if failure else 'evidence')
    # Current user intent and contracts are pinned. Treating every old question
    # as a correction displaced the current turn's successfully read sources.
    priority={'correction':4,'change':3,'verification':3,'failure':3,'evidence':0,'historical_request':0}[category]
    searchable=' '.join(str(event.get(key,'') or '') for key in ('path','command','notice','result_preview'))
    overlap=len(set(tokens(query)) & set(tokens(searchable)))
    return priority,overlap,category,failure


def select_events(events, query, limit=8):
    """Retain user corrections, edits, failure leads and relevant read pointers.

    No event is elevated to a trusted fact. Current acceptance is host state.
    Older relevant evidence need not be displaced by recent unrelated reads.
    """
    ranked=[]
    for index,event in enumerate(events):
        priority,overlap,category,failure=retention_key(event,query)
        # Keep one latest version per action/location; original evidence is
        # available by ID. Failure is a separate lead until later verification.
        key=('notice',hashlib.sha256(event['notice'].encode()).hexdigest()) if 'notice' in event else (
            event.get('tool'),event.get('path'),event.get('command'),
            event.get('sha256') if category=='evidence' else None,failure)
        ranked.append((priority,overlap,index,key,category,event))
    latest={}
    for row in ranked:latest[row[3]]=row
    # Give relevant reads a slot before filling with recency. Preserve the
    # newest verification/change and corrections without inventing semantics.
    candidates=sorted(latest.values(),key=lambda r:(r[0],r[1],r[2]),reverse=True)
    selected=candidates[:limit]
    relevant=[r for r in candidates if r[1] and r[4]=='evidence']
    if relevant and selected and relevant[0] not in selected:selected[-1]=relevant[0]
    selected.sort(key=lambda r:r[2])
    return [dict(row[5],retention_reason=row[4]) for row in selected],{
        'method':'typed_relevance_and_recency_v1','input_events':len(events),'selected_events':len(selected),
        'omitted_events':len(events)-len(selected),'max_events':limit,
        'selected_call_ids':[r[5]['call_id'] for r in selected if r[5].get('call_id')]}
