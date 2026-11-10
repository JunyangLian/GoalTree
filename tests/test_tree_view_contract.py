"""Selection responses must retain the actual audit feed used by node details."""
from pico.web import Workbench
from pico.providers.clients import FakeModelClient


def test_selecting_a_goal_keeps_its_persisted_task_changes(tmp_path):
    app=Workbench(tmp_path,model_factory=lambda:FakeModelClient([]),load_env=False)
    goal=app.tree_action({'action':'create_goal','title':'Goal'})['result']
    task=app.tree_action({'action':'create_task','goal_id':goal['id'],'title':'Old title'})['result']
    app.tree_action({'action':'update_task','goal_id':goal['id'],'task_id':task['id'],
                     'revision':task['revision'],'title':'Updated title'})
    selected=app.tree_action({'action':'select','goal_id':goal['id']})['tree']
    changes=[e for e in selected['events'] if task['id'] in e['payload']['changes']['tasks']]
    assert changes[-1]['payload']['changes']['tasks'][task['id']]['title']=='Updated title'
    assert selected['tasks'][task['id']]['title']=='Updated title'
