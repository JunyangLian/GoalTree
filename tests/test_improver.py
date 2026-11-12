import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pico.improvement_cases import public_packet
from pico.improver import ImproverStore,validate_proposal,MODEL,BASE_URL
from pico.providers.clients import FakeModelClient,OpenAICompatibleModelClient


def proposal():
    return {'title':'Minimize feedback','hypothesis':'Original prose is duplicated in learning sources.',
        'changes':['Store a generic receipt for ordinary feedback.'],
        'targets':['pico/learning_runtime.py','pico/flywheel.py'],
        'validation':['Use an independently generated random marker, verify it is absent from learning sources.'],
        'risks':['Historical local records still follow their original retention policy.'],
        'rollback':'Operator may revert the candidate after checking its privacy consequences.',
        'evidence_case':'feedback-minimization-v1'}


def test_offline_proposal_is_not_approval_and_cannot_replay_or_self_certify(tmp_path):
    s=ImproverStore(tmp_path);row=s.prepare('feedback-minimization-v1')
    client=FakeModelClient([json.dumps(proposal())]);result=s.request(row['id'],client)
    assert result['status']=='proposed' and result['body']['live'] is False
    assert result['body']['usage']=={'input_tokens':None,'output_tokens':None}
    assert 'feedback-minimization-v1' in client.prompts[0]
    assert not {'activate','run_shell','apply_changes'} & set(result['body']['packet'])
    with pytest.raises(ValueError):s.request(row['id'],client)
    with pytest.raises(ValueError):s.decide(row['id'],result['version'],'accept','Looks correct')
    with pytest.raises(ValueError):s.decide(row['id'],1,'reject','Old version')
    rejected=s.decide(row['id'],result['version'],'reject','Need stronger independent evidence')
    assert rejected['status']=='rejected' and s.audit_report()['valid']


def test_failure_keeps_reserved_attempt_and_unknown_usage(tmp_path):
    s=ImproverStore(tmp_path);row=s.prepare('feedback-minimization-v1')
    result=s.request(row['id'],FakeModelClient(['not JSON']))
    assert result['status']=='failed' and result['body']['usage']['input_tokens'] is None
    with s.connect() as db:assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==1
    assert s.audit_report()['valid']


@pytest.mark.parametrize('change',[{'targets':['../../.env']},{'evidence_case':'private'},{'extra':'run shell'},{'changes':[]}])
def test_schema_and_scope_block_unsafe_proposals(change):
    with pytest.raises(ValueError):validate_proposal({**proposal(),**change},public_packet('feedback-minimization-v1'))


def test_live_dispatch_binds_model_endpoint_usage_and_daily_limit(tmp_path):
    class Client(FakeModelClient):
        model=MODEL;base_url=BASE_URL
        def complete(self,*args):
            self.last_completion_metadata={'provider_model':MODEL,'provider_finish_reason':'stop','input_tokens':400,'output_tokens':600}
            return json.dumps(proposal())
    s=ImproverStore(tmp_path)
    for _ in range(2):
        row=s.prepare('feedback-minimization-v1');assert s.request(row['id'],Client([]),live=True)['status']=='proposed'
    row=s.prepare('feedback-minimization-v1')
    with pytest.raises(ValueError,match='limit'):s.request(row['id'],Client([]),live=True)
    bad=Client([]);bad.model='glm-5.3-flash'
    with pytest.raises(ValueError,match='explicit'):s.request(row['id'],bad,live=True)


def test_provider_options_are_explicit_and_do_not_modify_default_flash_request():
    client=OpenAICompatibleModelClient(MODEL,BASE_URL,'test-key',1,2,'chat_completions')
    client.request_options={'thinking':{'type':'enabled'},'reasoning_effort':'max'}
    wire=[]
    class Response:
        headers={'Content-Type':'application/json'}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'model':MODEL,'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],'usage':{}}).encode()
    def send(req,**kwargs):wire.append(json.loads(req.data));return Response()
    with patch('urllib.request.urlopen',side_effect=send):client.complete('public reproduction',100)
    assert wire[0]['model']==MODEL and wire[0]['thinking']=={'type':'enabled'} and wire[0]['reasoning_effort']=='max'
    assert client.last_completion_metadata['provider_model']==MODEL
    assert OpenAICompatibleModelClient('glm-5.3-flash',BASE_URL,'test-key',1,2,'chat_completions').request_options is None
