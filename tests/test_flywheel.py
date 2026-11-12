import json
from types import SimpleNamespace

import pytest

from pico.flywheel import FlywheelStore,PrivacySettings,RunMetrics
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico,SessionStore
from pico.workspace import WorkspaceContext


def summary():
    m=RunMetrics();m.observe('model_parsed',{'completion_metadata':{}})
    return m.finish(SimpleNamespace(status='completed',tool_steps=0),30,False)


def test_projection_drops_unknown_labels_arguments_errors_and_private_values(tmp_path):
    m=RunMetrics()
    for start in (1,1,201):
        m.observe('tool_executed',{'name':'read_file','tool_status':'ok',
            'args':{'path':'PRIVATE_PATH/email@example.org','start':start,'end':start+9},'result':'PRIVATE_CONTENT'})
    m.observe('tool_executed',{'name':'CUSTOM_PRIVATE_PLUGIN','tool_status':'error',
        'tool_error_code':'private stack with secrets','args':{'password':'do not retain'}})
    m.observe('tool_executed',{'name':'CUSTOM_PRIVATE_PLUGIN','tool_status':'ok'})
    m.observe('model_parsed',{'completion_metadata':{'input_tokens':50,'output_tokens':3,'unknown_usage_attempts':1}})
    value=m.finish(SimpleNamespace(status='completed',tool_steps=5),80,False)
    assert value['reads']==3 and value['repeated_reads']==1 and value['recovered_tools']==1
    assert value['tokens']['input_tokens']=={'known':50,'unknown_calls':1,'total':None}
    db=FlywheelStore(tmp_path);db.record('PRIVATE_RUN_ID',value)
    with db.connect() as conn:rows=[dict(r) for r in conn.execute('SELECT * FROM runs')]
    text=json.dumps(rows)
    assert all(s not in text for s in ('PRIVATE','password','example.org','secret','CUSTOM'))
    assert db.summary()['tokens']['input_tokens']['unknown_calls']==1


def test_metric_opt_in_suppression_ttl_and_clear(tmp_path):
    clock=[10000000];db=FlywheelStore(tmp_path,clock=lambda:clock[0])
    db.record('one',summary());db.record('one',summary())
    assert db.summary()['runs']==1
    with pytest.raises(ValueError):db.summary(export=True)
    db.settings.change('aggregate',1,0)
    assert db.summary(export=True)['suppressed']
    for i in range(9):db.record(str(i),summary())
    assert db.summary(export=True)['runs']==10
    clock[0]+=86401
    assert db.summary()['suppressed']
    db.record('fresh',summary())
    with db.connect() as conn:assert conn.execute('SELECT count(*) FROM runs').fetchone()[0]==1
    assert db.clear()['session_history_deleted'] is False
    assert db.summary()['suppressed']


@pytest.mark.parametrize('field,value',[('status','PRIVATE_TEXT'),('verified','PRIVATE_TEXT'),('elapsed_ms','PRIVATE_TEXT'),
    ('tokens',{'input_tokens':{'known':'private'}}),('unknown_field','private')])
def test_metric_shape_rejects_free_text(tmp_path,field,value):
    record=summary();record[field]=value
    with pytest.raises(ValueError):FlywheelStore(tmp_path).record('one',record)


def agent(root,outputs):
    return Pico(FakeModelClient(outputs),WorkspaceContext.build(root),SessionStore(root/'.pico/sessions'),harness_profile='workbench',max_depth=0)


def test_current_feedback_receipt_excludes_ordinary_prose_but_semantic_memory_still_works(tmp_path):
    a=agent(tmp_path,['<final>收到。</final>'])
    a.ask('请调整这份材料。PRIVATE_RESEARCH_CANARY 是临时内容。')
    sources=a.capabilities.learning.store.sources(session_id=a.session['id'])
    assert sources and 'PRIVATE_RESEARCH_CANARY' not in json.dumps(sources)
    assert FlywheelStore(tmp_path).summary()['completed_runs']==1
    assert FlywheelStore(tmp_path).summary()['verified_runs']==0


def test_privacy_off_blocks_learning_and_metrics_without_deleting_chat(tmp_path):
    PrivacySettings(tmp_path).change('off',30,0)
    a=agent(tmp_path,['<final>收到。</final>'])
    a.ask('请记住，报告先给简短提纲。')
    assert a.capabilities.learning.store.list(session_id=a.session['id'])==[]
    assert FlywheelStore(tmp_path).summary()['suppressed']
    assert a.session['history']
    with pytest.raises(ValueError):PrivacySettings(tmp_path).change('local',30,0)


@pytest.mark.parametrize('private',['a@example.org','D:\\private\\sample.txt','sk-'+'a'*30,'13812345678','保密：尚未公开的数据'])
def test_sensitive_feedback_not_copied_into_learning(tmp_path,private):
    a=agent(tmp_path,['<final>收到。</final>']);a.ask('请记住，报告先给提纲，'+private)
    s=a.capabilities.learning.store
    assert s.list(session_id=a.session['id'])==[]
    assert not [r for r in s.sources(session_id=a.session['id']) if r['category']=='feedback']
