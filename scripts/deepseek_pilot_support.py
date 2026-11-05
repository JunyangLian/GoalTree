"""Official-provider transport and conservative budget gate for the small pilot."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from pico.evaluation.repair_suite import append_json, load_json, utcnow, write_json

MODEL = 'deepseek-flash'
BASE_URL = 'https://api.deepseek.com/v1'
WIRE_OPTIONS = {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'}


class BudgetLimit(RuntimeError):
    pass


def balance():
    req = urllib.request.Request('https://api.deepseek.com/user/balance',
              headers={'Authorization': 'Bearer '+os.environ['DEEPSEEK_API_KEY']})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as r:
        body = json.load(r)
    cny = next((b for b in body.get('balance_infos', []) if b.get('currency') == 'CNY'), None)
    if not body.get('is_available') or cny is None:
        raise BudgetLimit('No available CNY balance; no paid request launched')
    return Decimal(cny['total_balance'])


def cost_cny(usage, peak=False):
    """Published token tariff estimate, separate from observed account balance delta."""
    keys = ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'completion_tokens')
    if not all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in keys): return None
    multiplier = Decimal(2 if peak else 1)
    return multiplier * (Decimal(usage[keys[0]])*Decimal('.02') + Decimal(usage[keys[1]]) +
                         Decimal(usage[keys[2]])*4) / 1000000


def reservation(payload):
    # Treat serialized UTF-8 bytes as tokens, add framing allowance, use peak miss/output rates.
    # A conservative local estimate, not a provider-supplied exact pre-token count.
    n = len(json.dumps(payload,ensure_ascii=False).encode()) + 4096
    return (Decimal(n)*2 + Decimal(payload['max_tokens'])*8) / 1000000


class Budget:
    def __init__(self, output, directory):
        self.output = Path(output); self.directory = Path(directory)
        self.initial = load_json(self.output/'budget-initial.json')
        self.requests = 0

    def before(self, payload):
        self.requests += 1
        if self.requests > 30: raise BudgetLimit('Per-run model request limit30 reached')
        current = balance()
        initial = Decimal(self.initial['initial_balance_cny'])
        maximum = Decimal(self.initial['max_spend_cny'])
        ledger = self.output/'budget-ledger.jsonl'
        prior = [json.loads(l) for l in ledger.read_text(encoding='utf-8').splitlines()] if ledger.exists() else []
        settled = {e['id'] for e in prior if e['event'] == 'settled'}
        pending = sum((Decimal(e['reserved_cny']) for e in prior if e['event']=='reserved' and e['id'] not in settled),Decimal(0))
        charged = sum((Decimal(e['tariff_estimate_cny']) for e in prior if e['event']=='settled' and e['tariff_estimate_cny'] is not None),Decimal(0))
        observed = max(Decimal(0),initial-current)
        upper = reservation(payload)
        allowed = max(observed,charged) + pending + upper <= maximum and current-upper >= Decimal('.21')
        event = {'id':str(self.directory.relative_to(self.output))+'-'+str(self.requests), 'event':'reserved' if allowed else 'denied',
                 'at':utcnow(),'balance_cny':str(current),'observed_account_spend_cny':str(observed),
                 'reserved_cny':str(upper),'unknown_pending_cny':str(pending),'max_spend_cny':str(maximum)}
        append_json(ledger,event)
        if not allowed: raise BudgetLimit('Budget guard stopped before paid request; remaining tasks stay unstarted')
        return event['id']

    def after(self, identifier, data):
        china = datetime.now(timezone(timedelta(hours=8)))
        peak = china.weekday()<5 and (9<=china.hour<12 or 14<=china.hour<18)
        cost = cost_cny(data.get('usage') or {},peak)
        if cost is None:
            append_json(self.output/'budget-ledger.jsonl',{'event':'usage_unknown','id':identifier,'at':utcnow()})
            return
        append_json(self.output/'budget-ledger.jsonl',{'event':'settled','id':identifier,'at':utcnow(),
            'tariff_estimate_cny':str(cost),'peak_tariff':peak,'usage':data['usage'],
            'model':data.get('model'),'system_fingerprint':data.get('system_fingerprint')})


class DeepSeekChatModel:
    """ApeCode ChatModel adapter: raw official native calls and reasoning, no DSML fallback."""
    def __init__(self,directory):
        self.directory = Path(directory); self.requests = []
        self.key = os.environ['DEEPSEEK_API_KEY']
        self.budget = Budget(self.directory.parent.parent,self.directory)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def complete(self, *, messages, tools):
        payload = {'model':MODEL,'messages':messages,'tools':tools,'tool_choice':'auto',
                   'max_tokens':4096,'top_p':.95,'stream':False,**WIRE_OPTIONS}
        identifier = self.budget.before(payload)
        usage = {'input_tokens':None,'output_tokens':None,'complete':False};self.requests.append(usage)
        append_json(self.directory/'wire.jsonl',{'event':'request','at':utcnow(),'payload':payload})
        req=urllib.request.Request(BASE_URL+'/chat/completions',data=json.dumps(payload).encode(),
                headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
        try:
            with self.opener.open(req,timeout=180) as r:body=r.read().decode()
        except urllib.error.HTTPError as exc:
            body=exc.read().decode()
            if self.key not in body:append_json(self.directory/'transport.jsonl',{'at':utcnow(),'status':exc.code,'body':body})
            raise RuntimeError(f'Official provider HTTP{exc.code}; no transport retry') from exc
        if self.key in body:raise RuntimeError('Credential in response; refuse export')
        data=json.loads(body);append_json(self.directory/'wire.jsonl',{'event':'response','at':utcnow(),'body':data})
        self.budget.after(identifier,data)
        if data.get('model')!=MODEL:raise RuntimeError('Returned model mismatch; no silent fallback')
        returned=data.get('usage') or {}
        usage.update(input_tokens=returned.get('prompt_tokens'),output_tokens=returned.get('completion_tokens'),complete=True)
        if data['choices'][0].get('finish_reason')=='length':raise RuntimeError('Output token limit reached; no incomplete call executed')
        return data['choices'][0]['message']
