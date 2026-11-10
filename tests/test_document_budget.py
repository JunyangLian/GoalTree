"""Document continuation must reach every line through the real tool envelope."""
import json

import pytest

from pico.providers.clients import FakeModelClient
from pico.web import Workbench
from pico.web_context import WebBudget,WebBudgetExceeded
from pico.workspace_manager import WorkspaceManager


def app(root):
    root.mkdir(exist_ok=True)
    return Workbench(root,model_factory=lambda:FakeModelClient([]),harness_profile='workbench',
                     load_env=False,web_access=True,native_input_budget=983040,native_context_window=1048576,
                     web_output_chars=120000,web_result_chars=12000,web_read_lines=120)


def test_full_document_reaches_model_without_second_clip_or_network_refetch(tmp_path,monkeypatch):
    a=app(tmp_path)._build_agent('document-session','never',20,coordinator=True)
    expected=[f'报告人{i:03d}：题目{i:03d}；分会场{i%4}。' for i in range(417)]
    calls=[]
    def get(url,accepted,**kwargs):
        calls.append(url);a.web_context.client.on_request()
        return {'url':url,'content_type':'text/plain','retrieved_at':'fixture','sha256':'a'*64},'\n'.join(expected)
    monkeypatch.setattr(a.web_context.client,'get',get)
    observed=[];start=1;page_id='';largest=0
    while start:
        args={'page_id':page_id,'start':start,'limit':120} if page_id else {'url':'https://example.org/document','limit':120}
        result=a.execute_tool('web_read',args)
        assert result.metadata['tool_status']=='ok'
        # Parse exactly what is returned to the Agent, not the untruncated raw cache.
        body=result.content.split('\nEvidence ID:')[0];data=json.loads(body)
        largest=max(largest,len(body));observed.extend(row['text'] for row in data['lines'])
        start=data['next_start'];page_id=data['page_id']
        assert data['citation'].endswith(f'L{data["start_line"]}-L{data["end_line"]}')
    assert observed==expected and len(calls)==1 and largest>4000
    assert a.web_budget.output_chars<120000 and a.web_budget.pages==1


def test_budget_is_propagated_to_new_folders_and_fresh_turns(tmp_path):
    original=app(tmp_path/'first');second=tmp_path/'second';second.mkdir()
    manager=WorkspaceManager(original)
    other=manager._build(second)
    assert other.config()['web_output_limits']==original.config()['web_output_limits']
    first=other._build_agent('one','never',20,coordinator=True)
    next_turn=other._build_agent('one','never',20,coordinator=True)
    first.web_budget.output_chars=50000
    assert next_turn.web_budget.output_chars==0
    assert next_turn.web_budget.read_limit==120
    assert other.native_context_policy.input_budget==983040


def test_output_stop_identifies_tool_quota_and_keeps_network_guards():
    b=WebBudget(120000,12000,120);b.output_chars=119900
    with pytest.raises(WebBudgetExceeded,match='not model context window'):b.emit({'lines':[]})
    b.output_chars=0;b.requests=8
    with pytest.raises(WebBudgetExceeded):
        with b.network('page'):pytest.fail('must not allow extra network')
    for args in [(10000,12000,120),(120000,12000,True),(True,3800,20)]:
        with pytest.raises(ValueError):WebBudget(*args)
