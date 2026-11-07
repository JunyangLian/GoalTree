from decimal import Decimal
import io
import json
from unittest.mock import patch

import pytest

from pico.evaluation.repair_suite import write_json
from scripts.glm_live_support import (Budget, BudgetExceeded, usage_cost, reserve_cost,
                                      LiveCodeTrailClient, LiveApeCodeModel)
from pico.providers.glm_protocol import measured_zero_reasoning
from scripts.bigmodel_benchmark_adapter import BigModelChatModel, CONFIG
from pico.providers.clients import OpenAICompatibleModelClient
from pico.evaluation.agent_session import SessionAudit


def budget(tmp_path, maximum='1'):
    write_json(tmp_path/'budget.json', {'max_spend_cny': maximum})
    return Budget(tmp_path)


def test_tariff_and_unknown_usage_have_precise_units():
    assert usage_cost({'prompt_tokens': 20, 'completion_tokens': 217}) == Decimal('.0006236')
    for invalid in [{}, {'prompt_tokens': True, 'completion_tokens': 1},
                    {'prompt_tokens': -1, 'completion_tokens': 1}]:
        assert usage_cost(invalid) is None


@pytest.mark.parametrize('reasoning,tokens,allowed', [
    (None, 0, True), (None, None, False), (None, 10, False), (None, False, False),
    (7, 0, False), ('existing reasoning', 0, False), ('', 0, False)])
def test_zero_reasoning_exemption_requires_explicit_integer_zero_and_no_generated_text(reasoning, tokens, allowed):
    assert measured_zero_reasoning({'reasoning_content': reasoning},
        {'completion_tokens_details': {'reasoning_tokens': tokens}}) is allowed


def test_missing_or_failed_response_keeps_the_entire_reservation(tmp_path):
    gate = budget(tmp_path)
    payload = {'max_tokens': 4096, 'messages': []}
    identifier = gate.before(payload, 'failed-request')
    gate.after(identifier, {})
    charged, pending, _ = gate.state()
    assert charged == 0 and pending == reserve_cost(payload) and pending > 0


def test_settlement_counts_failed_task_usage_and_releases_only_known_usage(tmp_path):
    gate = budget(tmp_path)
    identifier = gate.before({'max_tokens': 4096}, 'task-that-eventually-failed')
    gate.after(identifier, {'model': 'glm-5.3-flash', 'usage': {'prompt_tokens': 100, 'completion_tokens': 100}})
    charged, pending, _ = gate.state()
    assert charged == Decimal('.00036') and pending == 0


def test_budget_denies_before_any_transport_and_ids_are_unique(tmp_path):
    gate = budget(tmp_path, '.001')
    with pytest.raises(BudgetExceeded):
        gate.before({'max_tokens': 4096}, 'denied')
    assert gate.state()[2][-1]['event'] == 'denied'
    gate = budget(tmp_path/'other')
    assert gate.before({'max_tokens': 32}, 'same-owner') != gate.before({'max_tokens': 32}, 'same-owner')


class Response(io.BytesIO):
    headers = {'Content-Type': 'application/json'}
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def response():
    return Response(json.dumps({'model': 'glm-5.3-flash', 'choices': [{'finish_reason': 'stop',
        'message': {'role': 'assistant', 'reasoning_content': 'fixture thinking', 'content': 'READY'}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 20}}).encode())


def test_live_apecode_uses_explicit_transport_and_preserves_process_callback(tmp_path):
    gate = budget(tmp_path/'budget')
    events = []
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'unit-fixture-credential'}):
        model = LiveApeCodeModel(tmp_path/'run', events.append, gate)
    sent = []
    def direct(request, timeout):
        sent.append(request.full_url)
        assert gate.state()[1] > 0  # reservation was persisted before transmission.
        return response()
    model.opener = direct
    assert model.complete(messages=[], tools=[])['content'] == 'READY'
    assert sent == ['https://open.bigmodel.cn/api/paas/v4/chat/completions']
    assert [e['event'] for e in events] == ['request', 'response']
    assert gate.state()[0] > 0 and gate.state()[1] == 0


def test_live_codetrail_forces_direct_route_and_restores_observer_after_complete(tmp_path):
    gate = budget(tmp_path/'budget')
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'unit-fixture-credential'}):
        model = LiveCodeTrailClient(tmp_path/'run', gate)
    events = []
    model.on_wire_event = events.append
    model.network_route = 'environment'  # workbench initializer sets this before complete.
    class Opener:
        def open(self, request, timeout):
            assert gate.state()[1] > 0
            return response()
    with patch('urllib.request.build_opener', return_value=Opener()):
        assert model.complete('availability', 64) == 'READY'
    assert model.network_route == 'direct' and model.on_wire_event == events.append
    assert [e['event'] for e in events] == ['request', 'response']
    assert gate.state()[0] > 0 and gate.state()[1] == 0


def zero_response():
    return {'model': 'glm-5.3-flash', 'choices': [{'finish_reason': 'tool_calls', 'message':
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'zero-call', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{"path":"a.py"}'}}]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'completion_tokens_details': {'reasoning_tokens': 0}}}


def test_glm_zero_token_tool_round_passes_both_adapters_without_invented_reasoning(tmp_path):
    body = zero_response()
    with patch.dict('os.environ', {'BIGMODEL_API_KEY': 'unit-fixture-credential'}):
        ape = BigModelChatModel(tmp_path/'ape')
    with patch('urllib.request.urlopen', return_value=Response(json.dumps(body).encode())):
        message = ape.complete(messages=[], tools=[])
    assert 'reasoning_content' not in message
    code = OpenAICompatibleModelClient('glm-5.3-flash', CONFIG['base_url'], 'unit-fixture-credential', 1, 20, 'chat_completions')
    code.begin_native_run({}, 'context', 'inspect', None)
    with patch('urllib.request.urlopen', return_value=Response(json.dumps(body).encode())):
        reply = code.complete('inspect', 128)
    assert reply.action_kind == 'tool' and code.native_conversation.require_reasoning is False
    assert 'reasoning_content' not in code.native_conversation.messages[-1]
    assert code.last_completion_metadata['glm_measured_zero_reasoning_turn'] is True
    assert measured_zero_reasoning(message, body['usage'])


def test_shared_observer_tracks_zero_token_native_id_without_protocol_rejection(tmp_path):
    class Executor: backend = 'unit'
    audit = SessionAudit('codetrail', {'gold': {'required_files': [], 'lines': []}}, tmp_path,
                         tmp_path/'audit', Executor(), 'b')
    audit.wire({'event': 'request', 'payload': {'messages': []}})
    audit.wire({'event': 'response', 'body': zero_response()})
    assert len(audit.pending) == 1 and audit.pending[0]['id'] == 'zero-call'
    assert not (tmp_path/'audit/protocol.jsonl').exists()


@pytest.mark.parametrize('reasoning_tokens', [24, None])
def test_glm_missing_text_does_not_invent_reasoning_or_block_valid_native_calls(tmp_path, reasoning_tokens):
    body=zero_response()
    body['usage']['completion_tokens_details']['reasoning_tokens']=reasoning_tokens
    with patch.dict('os.environ', {'BIGMODEL_API_KEY':'unit-fixture-credential'}):
        ape=BigModelChatModel(tmp_path/'ape')
    with patch('urllib.request.urlopen',return_value=Response(json.dumps(body).encode())):
        message=ape.complete(messages=[],tools=[])
    assert 'reasoning_content' not in message
    code=OpenAICompatibleModelClient('glm-5.3-flash',CONFIG['base_url'],'unit-fixture-credential',1,20,'chat_completions')
    code.begin_native_run({},'context','inspect',None)
    with patch('urllib.request.urlopen',return_value=Response(json.dumps(body).encode())):
        reply=code.complete('inspect',128)
    assert reply.action_kind=='tool'
    assert code.last_completion_metadata['glm_reasoning_text_missing'] is True
    code.observe_native_tool('actual unit tool response')
    payload=code.native_conversation.payload()
    assistant=next(m for m in payload['messages'] if m['role']=='assistant')
    assert 'reasoning_content' not in assistant
    assert payload['messages'][-1]['tool_call_id']=='zero-call'
    class Executor: backend='unit'
    audit=SessionAudit('codetrail',{'gold':{'required_files':[],'lines':[]}},tmp_path,tmp_path/'audit',Executor(),'b')
    audit.wire({'event':'request','payload':{'messages':[]}})
    audit.wire({'event':'response','body':body})
    assert audit.pending[0]['id']=='zero-call'
    assert not (tmp_path/'audit/protocol.jsonl').exists()


@pytest.mark.parametrize('malformed', [7, False])
def test_glm_malformed_reasoning_types_are_still_rejected_by_both_adapters(tmp_path,malformed):
    body=zero_response();body['choices'][0]['message']['reasoning_content']=malformed
    with patch.dict('os.environ',{'BIGMODEL_API_KEY':'unit-fixture-credential'}):
        ape=BigModelChatModel(tmp_path/'ape')
    code=OpenAICompatibleModelClient('glm-5.3-flash',CONFIG['base_url'],'unit-fixture-credential',1,20,'chat_completions')
    code.begin_native_run({},'context','inspect',None)
    for model in (ape,code):
        with patch('urllib.request.urlopen',return_value=Response(json.dumps(body).encode())):
            with pytest.raises(RuntimeError,match='Malformed GLM reasoning_content'):
                model.complete(messages=[],tools=[]) if model is ape else model.complete('inspect',128)


def test_glm_existing_reasoning_remains_byte_exact_and_other_providers_remain_strict():
    text='original reasoning\n  keep spacing\n'
    body=zero_response();body['choices'][0]['message']['reasoning_content']=text
    code=OpenAICompatibleModelClient('glm-5.3-flash',CONFIG['base_url'],'unit-fixture-credential',1,20,'chat_completions')
    code.begin_native_run({},'context','inspect',None)
    with patch('urllib.request.urlopen',return_value=Response(json.dumps(body).encode())):
        assert code.complete('inspect',128).action_kind=='tool'
    assert code.native_conversation.messages[-1]['reasoning_content']==text
    deep=OpenAICompatibleModelClient('deepseek-flash','https://api.deepseek.com','unit-fixture-credential',1,20,'chat_completions')
    deep.begin_native_run({},'context','inspect',None)
    assert deep.native_conversation.require_reasoning is True
    missing=zero_response();missing['model']='deepseek-flash'
    with patch('urllib.request.urlopen',return_value=Response(json.dumps(missing).encode())):
        assert deep.complete('inspect',128).action_kind=='retry'
