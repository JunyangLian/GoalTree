"""One frozen public 1M-input capacity/needle probe, not a long-task score."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.context_tokens import GLMTokenCounter
from pico.native_context import NativeContextBudget
from pico.providers.clients import OpenAICompatibleModelClient
from pico.privacy import PrivacyStore
from pico.persistence import write_json_atomic
from pico.tools import BASE_TOOL_SPECS
from scripts.eval_goal_stress import sources, without_reasoning
from scripts.run_goaltree_glm import load_key


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--live', action='store_true')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    def save(name, obj): write_json_atomic(args.output / name, obj)
    key = load_key(ROOT / '.env.glm.local') if args.live else ''
    if args.live and not key: raise ValueError('Ordinary local GLM key missing; no fallback')
    if key: os.environ['BIGMODEL_API_KEY'] = key
    counter = GLMTokenCounter()
    # Each repeated " a" is one token in the pinned tokenizer. No real data.
    text = ('Synthetic public capacity test. Extract FIRST_MARK, MIDDLE_MARK, LAST_MARK; '
            'return only JSON with first,middle,last. Never use tools.\nFIRST_MARK=birch-274\n')
    text += ' a' * 502500 + '\nMIDDLE_MARK=cedar-683\n' + ' a' * 502500 + '\nLAST_MARK=maple-951\n'
    client = OpenAICompatibleModelClient('glm-5.3-flash', 'https://open.bigmodel.cn/api/paas/v4', key, 1, 300, 'chat_completions')
    client.top_p, client.max_transport_attempts, client.stream, client.network_route = .95, 1, True, 'direct'
    client.native_wire_options = {'thinking': {'type': 'enabled', 'clear_thinking': False}, 'reasoning_effort': 'low'}
    client.begin_native_run({'read_file': BASE_TOOL_SPECS['read_file']}, 'Public synthetic capacity probe; no filesystem actions authorized.', text, None)
    conversation = client.native_conversation
    conversation.context_budget = NativeContextBudget(33554432, 67108864, token_counter=counter,
        input_token_budget=1032192, token_context_window=1048576)
    gold = {'first': 'birch-274', 'middle': 'cedar-683', 'last': 'maple-951'}
    frozen = sources()
    payload = {'model': client.model, 'max_tokens': 1024, 'temperature': 1, 'top_p': .95, 'stream': True,
               'stream_options': {'include_usage': True}, 'tool_stream': True, **conversation.payload()}
    prepared, audit = conversation.context_budget.prepare(payload, 2)
    save('freeze.json', {'sources': frozen, 'gold': gold, 'counting': counter.identity,
                         'protocol': {'window_tokens': 1048576, 'input_estimate_limit': 1032192, 'output_tokens': 1024,
                                      'attempts': 1, 'timeout_seconds': 300, 'synthetic_repeat_tokens': 1005000,
                                      'scope': 'single input capacity and three needle observations; no long-agent, stability or generalization claim'},
                         'prepared_sha256': hashlib.sha256(json.dumps(prepared).encode()).hexdigest()})
    save('prepared.json', {'audit': audit, 'payload_without_reasoning': without_reasoning(prepared)})
    if not args.live:
        save('result.json', {'status': 'prepared_not_run', 'model_calls': 0})
        print(json.dumps({'status': 'prepared_not_run', 'audit': audit})); return
    workspace = args.output / 'workspace'; workspace.mkdir()
    PrivacyStore(workspace).initialize(client.base_url)
    client.privacy_root = workspace
    # Reserve the full advertised window, not unknown usage as zero. Probe has
    # at most 1024 output tokens and no retries; stays within 2M tokens / CNY5.
    save('reservation.json', {'status': 'reserved_before_dispatch', 'reserved_total_tokens': 1049600,
         'input_tariff_cny_per_million': .8, 'output_tariff_cny_per_million': 2.8,
         'estimated_cny_upper': .841728, 'actual_billed_cost': None,
         'pricing_source': 'https://docs.bigmodel.cn/cn/guide/start/pricing.md', 'checked_date': '2026-11-11'})
    if sources() != frozen: raise RuntimeError('Source drift before dispatch')
    started = time.monotonic(); answer = ''; error = None
    try: answer = str(client.complete('unused legacy prompt', 1024))
    except Exception as exc: error = {'type': type(exc).__name__, 'detail': str(exc)[:500]}
    meta = client.last_completion_metadata
    try:
        value = json.loads(answer.strip().removeprefix('```json').removesuffix('```').strip())
    except (ValueError, TypeError): value = {}
    checks = {k: value.get(k) == v for k, v in gold.items()} if isinstance(value, dict) else {k: False for k in gold}
    usage = meta.get('provider_usage') or {}
    result = {'status': 'failed' if error else 'returned', 'error': error, 'answer': answer,
              'metadata': meta, 'elapsed_seconds': round(time.monotonic()-started, 3),
              'provider_input_at_least_1M': type(usage.get('prompt_tokens')) is int and usage['prompt_tokens'] >= 1000000,
              'needle_checks': checks, 'usage_unknown': not all(type(usage.get(k)) is int for k in ('prompt_tokens','completion_tokens')),
              'sources_unchanged': sources() == frozen, 'actual_billed_cost': None}
    save('result.json', result)
    print(json.dumps({k: v for k, v in result.items() if k not in {'answer','metadata'}}, ensure_ascii=False))


if __name__ == '__main__': main()
