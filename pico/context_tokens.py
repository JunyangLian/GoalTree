"""Pinned official local tokenizer; chat JSON count plus an explicit safety margin.

No model weights, remote code, model calls or user-text uploads are involved.
Provider-side template/token usage remains authoritative, not this estimate.
"""
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
import urllib.request

REPOSITORY = 'zai-org/GLM-5.3-Flash'
REVISION = '46a050bec13002ad29ee795c16e61cf4fa3c4a9c'
FILES = {'tokenizer.json': '19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d',
         'tokenizer_config.json': '98b1271574f41abf89427ae2dda030d94dc9478f0edc5a8bd240db213c6fd5fc'}
DEFAULT_CACHE = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_CACHE_HOME')
                     or os.environ.get('USERPROFILE') or os.environ.get('HOME') or tempfile.gettempdir()) / 'GoalTree/tokenizers/glm-5.3-flash'


def policy_for_client(client, byte_budget, byte_window):
    from .native_context import NativeContextBudget
    # Opt-in instance configuration only; dynamic mocks/proxies fabricate
    # arbitrary attributes and must not accidentally enable a token profile.
    settings = getattr(client, '__dict__', {})
    return NativeContextBudget(byte_budget, byte_window, token_counter=settings.get('context_counter'),
        input_token_budget=settings.get('input_token_budget'),
        token_context_window=settings.get('token_context_window'))


def prepare_tokenizer(directory=DEFAULT_CACHE):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        path = directory / name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            continue
        url = f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}'
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(50_000_001)
        if len(data) > 50_000_000 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Official tokenizer hash/size check failed')
        temporary = directory / (name + '.download')
        temporary.write_bytes(data)
        temporary.replace(path)
    return directory


class GLMTokenCounter:
    counting = 'official_glm_tokenizer_json_estimate_plus_margin'

    def __init__(self, directory=DEFAULT_CACHE):
        import tokenizers
        from tokenizers import Tokenizer
        directory = Path(directory)
        for name, digest in FILES.items():
            path = directory / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Pinned GLM tokenizer unavailable; run --prepare-context-tokenizer first')
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.identity = {'repository': REPOSITORY, 'revision': REVISION, 'sha256': FILES['tokenizer.json'],
                         'method': self.counting, 'provider_exact': False, 'margin': '4096 + ceil(1% raw count)'}
        self.identity['tokenizers_version'] = tokenizers.__version__

    def count(self, payload):
        for message in payload.get('messages', []):
            if isinstance(message.get('content'), list) and any(p.get('type') not in {'text'} for p in message['content']):
                raise ValueError('Local text tokenizer cannot account for image/audio/video tokens')
        text = json.dumps({k: v for k, v in payload.items() if k in {'messages', 'tools', 'tool_choice'}},
                          ensure_ascii=False, separators=(',', ':'))
        # Chunk boundaries can slightly overcount; never truncate long input.
        raw = sum(len(self.tokenizer.encode(text[i:i + 65536], add_special_tokens=False).ids)
                  for i in range(0, len(text), 65536))
        return raw + 4096 + math.ceil(raw * .01)
