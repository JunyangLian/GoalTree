from decimal import Decimal
import io
import json
from unittest.mock import patch

import pytest

from scripts.deepseek_pilot_support import (Budget,BudgetLimit,DeepSeekChatModel,MODEL,cost_cny,reservation)


def initial(root,maximum='2.50'):
    (root/'budget-initial.json').write_text(json.dumps({'initial_balance_cny':'2.71','max_spend_cny':maximum}))


def test_tariff_separates_cached_uncached_output_and_unknown():
    usage={'prompt_cache_hit_tokens':20000,'prompt_cache_miss_tokens':100000,'completion_tokens':5000}
    assert cost_cny(usage)==Decimal('.1204')
    assert cost_cny(usage,True)==Decimal('.2408')
    assert cost_cny({'prompt_tokens':100}) is None


def test_conservative_reservation_exceeds_output_only_peak_cost():
    payload={'messages':[{'role':'user','content':'中文 text'}],'max_tokens':4096}
    assert reservation(payload)>Decimal(4096)*8/1000000


def test_low_balance_blocks_before_paid_request(tmp_path):
    initial(tmp_path);directory=tmp_path/'runs/a';directory.mkdir(parents=True)
    with patch('scripts.deepseek_pilot_support.balance',return_value=Decimal('.23')):
        with pytest.raises(BudgetLimit,match='before paid request'):Budget(tmp_path,directory).before({'max_tokens':4096})
    events=[json.loads(l) for l in (tmp_path/'budget-ledger.jsonl').read_text().splitlines()]
    assert events[-1]['event']=='denied'


def test_unknown_request_reservation_is_not_released_as_zero(tmp_path):
    initial(tmp_path,'.06');directory=tmp_path/'runs/a';directory.mkdir(parents=True)
    budget=Budget(tmp_path,directory)
    with patch('scripts.deepseek_pilot_support.balance',return_value=Decimal('2.71')):
        identifier=budget.before({'max_tokens':4096})
        budget.after(identifier,{'usage':{}})
        with pytest.raises(BudgetLimit):budget.before({'max_tokens':4096})


def test_known_usage_releases_reservation_but_retains_real_charge(tmp_path):
    initial(tmp_path,'.06');directory=tmp_path/'runs/a';directory.mkdir(parents=True)
    budget=Budget(tmp_path,directory)
    with patch('scripts.deepseek_pilot_support.balance',return_value=Decimal('2.71')):
        identifier=budget.before({'max_tokens':4096})
        budget.after(identifier,{'model':MODEL,'usage':{'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100,'completion_tokens':20}})
        assert budget.before({'max_tokens':4096})


def test_real_worker_import_name_uses_official_adapter():
    # Subprocess avoids altering other tests' module globals, and mirrors the CLI entry.
    import subprocess,sys
    code='import importlib; from scripts import run_deepseek_pilot as r; m=importlib.import_module("apecode_pilot_adapter"); assert m is r.adapter; assert m.run_apecode is r.apecode_run'
    result=subprocess.run([sys.executable,'-c',code],capture_output=True)
    assert result.returncode==0,result.stderr.decode(errors='replace')


@pytest.mark.parametrize('returned_model,raises',[(MODEL,False),('different-model',True)])
def test_official_transport_uses_official_thinking_and_preserves_reasoning(tmp_path,returned_model,raises):
    directory=tmp_path/'runs/a';directory.mkdir(parents=True);initial(tmp_path)
    with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'private-fixture'}):model=DeepSeekChatModel(directory)
    response={'model':returned_model,'choices':[{'message':{'role':'assistant','reasoning_content':'fixture reasoning',
        'content':'done'},'finish_reason':'stop'}],'usage':{'prompt_tokens':120,'completion_tokens':18,
        'prompt_cache_hit_tokens':100,'prompt_cache_miss_tokens':20}}
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    requests=[]
    def open_request(req,timeout):
        requests.append(json.loads(req.data));assert req.full_url=='https://api.deepseek.com/v1/chat/completions'
        return Response(json.dumps(response).encode())
    model.opener.open=open_request
    with patch('scripts.deepseek_pilot_support.balance',return_value=Decimal('2.71')):
        if raises:
            with pytest.raises(RuntimeError,match='model mismatch'):model.complete(messages=[],tools=[])
        else:assert model.complete(messages=[],tools=[])['reasoning_content']=='fixture reasoning'
    payload=requests[0]
    assert payload['model']=='deepseek-flash' and payload['thinking']=={'type':'enabled'}
    assert payload['reasoning_effort']=='low' and payload['top_p']==.95
    assert not {'temperature','enable_thinking','thinking_budget'} & payload.keys()
    assert 'private-fixture' not in (directory/'wire.jsonl').read_text()
