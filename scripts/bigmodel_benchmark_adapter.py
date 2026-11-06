"""Explicit standard-API GLM adapters; Coding Plan keys/endpoints are not substituted."""
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.providers.clients import OpenAICompatibleModelClient
from pico.evaluation.repair_suite import append_json, utcnow

MODEL = 'glm-5.3-flash'
BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'
OPTIONS = {'thinking': {'type': 'enabled', 'clear_thinking': False}, 'reasoning_effort': 'max'}
CONFIG = {'provider': 'bigmodel_standard_api', 'model': MODEL, 'base_url': BASE_URL,
    'api_format': 'chat_completions', 'temperature': 1, 'top_p': .95,
    'max_tokens': 4096, 'timeout_seconds': 180, 'transport_attempts': 1,
    'max_model_requests': 30, 'stream': False, **OPTIONS,
    'verification_status': 'official documentation + offline contract tests; not live verified',
    'credential_env': 'BIGMODEL_API_KEY', 'coding_plan_allowed': False}


def make_codetrail_client():
    model = OpenAICompatibleModelClient(MODEL, BASE_URL, os.environ['BIGMODEL_API_KEY'],
        CONFIG['temperature'], CONFIG['timeout_seconds'], 'chat_completions')
    model.top_p = CONFIG['top_p']
    model.max_transport_attempts = CONFIG['transport_attempts']
    model.stream = False
    return model


class BigModelChatModel:
    """ApeCode public ChatModel protocol; original native messages replayed intact.

    No model call is made by construction. Formal execution requires a separately
    frozen controller/permission/budget plan, not just importing this class.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.key = os.environ['BIGMODEL_API_KEY']
        self.requests = []

    def complete(self, *, messages, tools):
        if len(self.requests) >= CONFIG['max_model_requests']:
            raise RuntimeError('Frozen model-request budget exhausted')
        payload = {'model': MODEL, 'messages': messages, 'tools': tools, 'tool_choice': 'auto',
            'temperature': CONFIG['temperature'], 'top_p': CONFIG['top_p'],
            'max_tokens': CONFIG['max_tokens'], 'stream': False, **OPTIONS}
        if self.key in json.dumps(payload):
            raise RuntimeError('Credential in model input; refused before transmission')
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {'input_tokens': None, 'output_tokens': None, 'complete': False}
        self.requests.append(record)
        append_json(self.directory/'wire.jsonl', {'event': 'request', 'at': utcnow(), 'payload': payload})
        request = urllib.request.Request(BASE_URL+'/chat/completions',
            data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+self.key,
                                                       'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=CONFIG['timeout_seconds']) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            # Never persist arbitrary provider error text that may contain credentials.
            append_json(self.directory/'transport.jsonl', {'at': utcnow(), 'status': exc.code,
                'usage': 'unknown', 'retry': False})
            raise RuntimeError(f'BigModel HTTP{exc.code}; no automatic retry') from None
        if self.key in body:
            raise RuntimeError('Credential in provider response; refuse export')
        data = json.loads(body)
        append_json(self.directory/'wire.jsonl', {'event': 'response', 'at': utcnow(), 'body': data})
        usage = data.get('usage') or {}
        record.update(input_tokens=usage.get('prompt_tokens'), output_tokens=usage.get('completion_tokens'),
            complete=type(usage.get('prompt_tokens')) is int and type(usage.get('completion_tokens')) is int)
        if data.get('model') != MODEL:
            raise RuntimeError('Returned model mismatch; no silent substitution')
        choice = data['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise RuntimeError('Truncated response; no incomplete tool call executed')
        message = choice['message']
        if not isinstance(message.get('reasoning_content'), str):
            raise RuntimeError('Thinking response missing reasoning_content; protocol not verified')
        return message
