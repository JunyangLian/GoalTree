"""Real GLM transport with conservative, append-only CNY budget accounting."""
from decimal import Decimal
import json
import os
from pathlib import Path
import urllib.request
import uuid

from pico.evaluation.repair_suite import append_json, load_json, utcnow, write_json
from scripts.bigmodel_benchmark_adapter import (BigModelChatModel, CONFIG, MODEL, BASE_URL)
from pico.providers.clients import OpenAICompatibleModelClient

RATES = {'input_per_million_cny': '0.8', 'output_per_million_cny': '2.8',
    'cached_input_per_million_cny': '0.23',
    'source': 'https://docs.bigmodel.cn/cn/guide/start/pricing', 'checked_date': '2026-11-06',
    'budget_policy': 'All input treated as cache miss for upper estimate; actual account debit not available'}


class BudgetExceeded(RuntimeError):
    pass


def usage_cost(usage):
    if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens')):
        return None
    return (Decimal(usage['prompt_tokens'])*Decimal(RATES['input_per_million_cny']) +
            Decimal(usage['completion_tokens'])*Decimal(RATES['output_per_million_cny']))/1000000


def reserve_cost(payload):
    # Local conservative framing estimate, not an authoritative provider token count.
    bound = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))+8192
    return (Decimal(bound)*Decimal(RATES['input_per_million_cny'])+
            Decimal(payload['max_tokens'])*Decimal(RATES['output_per_million_cny']))/1000000


class Budget:
    def __init__(self, output):
        self.output = Path(output)
        self.config = load_json(self.output/'budget.json')

    def state(self):
        path = self.output/'ledger.jsonl'
        events = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
        settled = {e['id'] for e in events if e['event'] == 'settled'}
        pending = sum((Decimal(e['reserved_cny']) for e in events
            if e['event'] == 'reserved' and e['id'] not in settled), Decimal(0))
        charged = sum((Decimal(e['tariff_upper_estimate_cny']) for e in events if e['event'] == 'settled'), Decimal(0))
        return charged, pending, events

    def token_state(self):
        """Count cumulative provider usage; failed unknown requests retain reservations."""
        _, _, events = self.state()
        settlements = {e['id']: e for e in events if e['event'] == 'settled'}
        known, pending = 0, 0
        for event in events:
            if event['event'] != 'reserved':
                continue
            if event['id'] in settlements:
                usage = settlements[event['id']]['usage']
                known += usage['prompt_tokens'] + usage['completion_tokens']
            else:
                if type(event.get('reserved_tokens')) is not int:
                    raise ValueError('Token budget cannot infer a missing historical reservation')
                pending += event['reserved_tokens']
        return known, pending

    def before(self, payload, owner):
        charged, pending, _ = self.state()
        upper = reserve_cost(payload)
        reserved_tokens = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))+8192+payload['max_tokens']
        identifier = uuid.uuid4().hex
        allowed = charged+pending+upper <= Decimal(self.config['max_spend_cny'])
        token_limit = self.config.get('max_total_tokens')
        if token_limit is not None:
            used_tokens, pending_tokens = self.token_state()
            allowed = allowed and used_tokens+pending_tokens+reserved_tokens <= token_limit
        append_json(self.output/'ledger.jsonl', {'id': identifier, 'event': 'reserved' if allowed else 'denied',
            'at': utcnow(), 'owner': owner, 'reserved_cny': str(upper),
            'reserved_tokens': reserved_tokens,
            'known_tariff_upper_cny': str(charged), 'unknown_reserved_cny': str(pending)})
        if not allowed:
            raise BudgetExceeded('Frozen money/token budget stopped before transmission; no replacement run')
        return identifier

    def after(self, identifier, body):
        cost = usage_cost(body.get('usage') or {})
        if cost is None:
            append_json(self.output/'ledger.jsonl', {'id': identifier, 'event': 'usage_unknown', 'at': utcnow()})
            return
        append_json(self.output/'ledger.jsonl', {'id': identifier, 'event': 'settled', 'at': utcnow(),
            'tariff_upper_estimate_cny': str(cost), 'usage': body['usage'],
            'model': body.get('model'), 'system_fingerprint': body.get('system_fingerprint')})


def initialize_budget(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'budget.json', {'max_spend_cny': '1', 'rates': RATES,
        'unknown_request_usage': 'Reservation retained, not zero', 'concurrency': 1}, exclusive=True)
    # Availability was bounded independently to 256 output tokens. Retain its
    # known response and the earlier failed environment-proxy request separately.
    for folder, file in [('glm-standard-availability-2026-11-06', 'status.json'),
                         ('glm-live-pilot-2026-11-06', 'availability.json')]:
        directory = output.parent/folder
        if not (directory/file).exists(): continue
        payload, status = load_json(directory/'request.json'), load_json(directory/file)
        identifier = 'import-'+folder
        append_json(output/'ledger.jsonl', {'id': identifier, 'event': 'reserved', 'at': utcnow(),
            'reserved_cny': str(reserve_cost(payload)), 'owner': folder,
            'note': 'Imported after availability stage; its original max_tokens=256 bound was fixed before request'})
        if isinstance(status.get('usage'), dict): Budget(output).after(identifier, {'usage': status['usage'], 'model': status.get('model')})


class LiveCodeTrailClient(OpenAICompatibleModelClient):
    def __init__(self, directory, budget):
        super().__init__(MODEL, BASE_URL, os.environ['BIGMODEL_API_KEY'], 1, 180, 'chat_completions')
        self.top_p, self.max_transport_attempts, self.stream = .95, 1, False
        self.directory, self.budget = Path(directory), budget
        self.directory.mkdir(parents=True, exist_ok=True)
        self.on_transport_event = lambda event: append_json(self.directory/'transport.jsonl', event)

    def complete(self, prompt, max_new_tokens, **kwargs):
        original = self.on_wire_event
        current = None
        def guarded(event):
            nonlocal current
            body = json.loads(event['body'])
            if event['event'] == 'request': current = self.budget.before(body, str(self.directory))
            elif event['event'] == 'response': self.budget.after(current, body)
            if original: original(event)
        self.on_wire_event = guarded
        self.network_route = 'direct'
        try: return super().complete(prompt, max_new_tokens, **kwargs)
        finally: self.on_wire_event = original


class LiveApeCodeModel(BigModelChatModel):
    def __init__(self, directory, hook, budget):
        super().__init__(directory, hook, urllib.request.build_opener(urllib.request.ProxyHandler({})).open)
        self.budget, self.current = budget, None

    def emit(self, event):
        if event['event'] == 'request': self.current = self.budget.before(event['payload'], str(self.directory))
        elif event['event'] == 'response': self.budget.after(self.current, event['body'])
        return super().emit(event)
