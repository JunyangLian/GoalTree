"""Derived tree progress; never writes acceptance, revisions or audit history."""
import copy


def task_progress(state):
    tasks=state['tasks']
    children={key:[] for key in tasks}
    for item in tasks.values():
        if item.get('parent_id') in children and tasks[item['parent_id']]['goal_id']==item['goal_id']:
            children[item['parent_id']].append(item['id'])
    result={}
    def visit(key):
        if key in result:return result[key]
        item=tasks[key]
        relevant=[c for c in children[key] if tasks[c].get('context_status')!='archived' and tasks[c]['status']!='cancelled']
        sub=[visit(c) for c in relevant]
        own=item['status']
        own_accepted=own=='completed' and item['validity']=='fresh'
        accepted=own_accepted and all(c['accepted'] for c in sub)
        counts={name:sum(c['display_status']==name for c in sub) for name in ['planned','active','review','blocked','failed','completed']}
        status='review' if own=='completed' and not own_accepted else own
        reason='own_status'
        if relevant and own not in {'failed','blocked','cancelled'}:
            if own=='active':status,reason='active','own_run_active'
            elif accepted:status,reason='completed','self_and_children_accepted'
            elif counts['active']:status,reason='active','child_active'
            elif counts['failed'] or counts['blocked']:status,reason='blocked','child_blocked_or_failed'
            elif all(c['display_status'] in {'completed','review'} for c in sub):
                status,reason='review','children_need_review_or_stage_acceptance'
            elif any(c['display_status']!='planned' for c in sub) or own!='planned':
                status,reason='active','child_work_started'
            else:status,reason='planned','children_not_started'
        result[key]={'display_status':status,'accepted':accepted,'total':len(relevant),
                     'excluded':len(children[key])-len(relevant),'reason':reason,**counts}
        return result[key]
    for key in tasks:visit(key)
    return result


def tree_view(state):
    view=copy.deepcopy(state)
    parents={t.get('parent_id') for t in state['tasks'].values()}
    for key,progress in task_progress(state).items():
        view['tasks'][key]['display_status']=progress['display_status']
        if key in parents:view['tasks'][key]['rollup']=progress
    return view
