"""Parent progress is derived, while completion remains evidence-gated."""
import copy

import pytest

from pico.goals import GoalStore,GoalError
from pico.task_progress import tree_view,task_progress


def setup(store):
    goal=store.action({'action':'create_goal','title':'Objective'})
    stage=store.action({'action':'create_task','goal_id':goal['id'],'title':'Stage','verification_mode':'manual'})
    children=[store.action({'action':'create_task','goal_id':goal['id'],'parent_id':stage['id'],'title':name,'verification_mode':'manual'}) for name in ['A','B']]
    return goal,stage,children


def run(store,goal,item,id='run-a',status='completed'):
    binding=store.binding(goal['id'],item['id'],'execute');store.start(binding,id)
    store.finish(binding,{'id':id,'status':status,'answer':'Actual work requires review','verification':{'status':'not_configured'}})


def accept(store,goal,id):
    item=store.read()['tasks'][id]
    return store.action({'action':'accept_task','goal_id':goal['id'],'task_id':id,'revision':item['revision'],'reason':'Operator inspected evidence'})


def test_partial_child_review_promotes_parent_view_without_rewriting_history(tmp_path):
    store=GoalStore(tmp_path);goal,stage,children=setup(store)
    run(store,goal,children[0])
    original=store.read();events=store.events()
    projected=tree_view(original)
    parent=projected['tasks'][stage['id']]
    assert parent['status']=='planned' and parent['display_status']=='active'
    assert parent['rollup']['review']==1 and parent['rollup']['planned']==1
    assert parent['rollup']['completed']==0
    assert store.read()==original and store.events()==events
    # The model receives the same explicit distinction as the UI.
    scope=store.context(store.binding(goal['id'],'','assistant'))
    observed=next(t for t in scope['existing_tasks'] if t['id']==stage['id'])
    assert observed['display_status']=='active' and observed['status']=='planned'


def test_all_children_review_is_not_completion_and_parent_cannot_self_pass(tmp_path):
    store=GoalStore(tmp_path);goal,stage,children=setup(store)
    for i,item in enumerate(children):run(store,goal,item,str(i))
    assert task_progress(store.read())[stage['id']]['display_status']=='review'
    before=store.read()
    with pytest.raises(GoalError,match='子任务'):accept(store,goal,stage['id'])
    assert store.read()==before
    for item in children:accept(store,goal,item['id'])
    assert task_progress(store.read())[stage['id']]['display_status']=='review'
    accept(store,goal,stage['id'])
    assert task_progress(store.read())[stage['id']]['display_status']=='completed'


def test_recursive_parent_acceptance_and_stale_children_block_dependency(tmp_path):
    store=GoalStore(tmp_path);goal,stage,children=setup(store)
    leaf=store.action({'action':'create_task','goal_id':goal['id'],'parent_id':children[0]['id'],'title':'Leaf','verification_mode':'manual'})
    run(store,goal,leaf)
    aggregate=task_progress(store.read())
    assert aggregate[children[0]['id']]['display_status']=='review'
    assert aggregate[stage['id']]['display_status']=='active'
    accept(store,goal,leaf['id']);accept(store,goal,children[0]['id']);accept(store,goal,children[1]['id']);accept(store,goal,stage['id'])
    downstream=store.action({'action':'create_task','goal_id':goal['id'],'title':'Dependent','depends_on':[stage['id']]})
    store.binding(goal['id'],downstream['id'],'execute')
    current=store.read()['tasks'][leaf['id']]
    store.action({'action':'reopen_task','goal_id':goal['id'],'task_id':leaf['id'],'revision':current['revision']})
    assert task_progress(store.read())[stage['id']]['accepted'] is False
    with pytest.raises(GoalError,match='前置任务'):store.binding(goal['id'],downstream['id'],'execute')


@pytest.mark.parametrize('child_status,expected',[('active','active'),('failed','blocked'),('blocked','blocked'),('planned','planned')])
def test_child_state_precedence(child_status,expected):
    state={'tasks':{'parent':{'id':'parent','goal_id':'g','parent_id':'','status':'planned','validity':'unknown'},
                    'child':{'id':'child','goal_id':'g','parent_id':'parent','status':child_status,'validity':'unknown'}}}
    assert task_progress(state)['parent']['display_status']==expected


def test_cancelled_and_archived_children_are_not_falsely_counted_as_accepted():
    state={'tasks':{'p':{'id':'p','goal_id':'g','parent_id':'','status':'planned','validity':'unknown'},
        'a':{'id':'a','goal_id':'g','parent_id':'p','status':'cancelled','validity':'unknown'},
        'b':{'id':'b','goal_id':'g','parent_id':'p','status':'completed','validity':'fresh','context_status':'archived'}}}
    value=task_progress(state)['p']
    assert value['excluded']==2 and value['total']==0 and value['display_status']=='planned'
    assert value['accepted'] is False
