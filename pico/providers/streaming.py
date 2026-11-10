"""Bounded Chat Completions SSE assembly; partial calls are never executable."""
import json
import hashlib
import time
from http.client import IncompleteRead


class IncompleteSSEError(ValueError):
    """A response that must never be committed as an assistant/tool turn."""
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__('Incomplete SSE response; no tool call accepted '
                         f'({diagnostics["termination"]}; finish={diagnostics["finish_reason"]!r}; '
                         f'DONE={diagnostics["done_seen"]})')


def read_chat_stream(response, cancellation=None, on_delta=None, on_progress=None, on_diagnostics=None, max_seconds=None):
    content, reasoning, calls = [], [], {}
    finish, usage, model, fingerprint, done = None, None, None, None, False
    total = 0
    events, digest, started = 0, hashlib.sha256(), time.perf_counter()
    read_seconds,callback_seconds,max_read_seconds=0.0,0.0,0.0
    def diagnostics(termination):
        data={'termination':termination,'done_seen':done,'finish_reason':finish,
              'received_bytes':total,'data_events':events,'raw_sha256':digest.hexdigest(),
              'answer_chars':sum(map(len,content)),'reasoning_chars':sum(map(len,reasoning)),
              'tool_call_count':len(calls),
              'tool_argument_chars':sum(len(c['function']['arguments']) for c in calls.values()),
              'usage_received':usage is not None,'elapsed_ms':round((time.perf_counter()-started)*1000),
              'read_wait_ms':round(read_seconds*1000),'consumer_callback_ms':round(callback_seconds*1000),
              'max_read_wait_ms':round(max_read_seconds*1000)}
        if on_diagnostics:on_diagnostics(dict(data))
        return data
    last_phase, last_signal = None, 0
    def signal(phase):
        nonlocal last_phase, last_signal,callback_seconds
        instant = time.monotonic()
        if on_progress and (phase != last_phase or instant - last_signal >= 2):
            # No reasoning text, tool arguments or inferred decisions leave here.
            callback_started=time.perf_counter()
            try:on_progress({'phase': phase})
            finally:callback_seconds+=time.perf_counter()-callback_started
            last_phase, last_signal = phase, instant
    while True:
        if cancellation:
            cancellation.check()
        # Socket timeout only bounds inactivity, not a continuously trickling
        # response. Check total stream elapsed time between bounded reads too.
        if max_seconds is not None and time.perf_counter()-started>=max_seconds:
            raise IncompleteSSEError(diagnostics('stream_deadline'))
        read_started=time.perf_counter()
        try:
            try:
                line = response.readline(131073)
            finally:
                spent=time.perf_counter()-read_started
                read_seconds+=spent;max_read_seconds=max(max_read_seconds,spent)
        except (OSError, IncompleteRead) as exc:
            if cancellation:cancellation.check()
            termination='read_timeout' if isinstance(exc,TimeoutError) else 'read_error'
            raise IncompleteSSEError(diagnostics(termination)) from exc
        if not line:
            break
        total += len(line)
        digest.update(line)
        if len(line) > 131072 or total > 8_000_000:
            raise ValueError('SSE response exceeds bounded size')
        if not line.startswith(b'data:'):
            continue
        text = line[5:].strip().decode('utf-8')
        if text == '[DONE]':
            done = True
            break
        event = json.loads(text)
        events += 1
        if event.get('error'):
            raise RuntimeError('Streaming provider error: ' + str(event['error']))
        model = event.get('model') or model
        fingerprint = event.get('system_fingerprint') or fingerprint
        if event.get('usage') is not None:
            usage = event['usage']
        choices = event.get('choices', [])
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get('delta') or {}
        if delta.get('content'):
            signal('answer')
            content.append(delta['content'])
            if on_delta:
                callback_started=time.perf_counter()
                try:on_delta(delta['content'])
                finally:callback_seconds+=time.perf_counter()-callback_started
        if delta.get('reasoning_content') is not None:
            if delta['reasoning_content']:
                signal('analysis')
            reasoning.append(delta['reasoning_content'])
        for call in delta.get('tool_calls') or []:
            signal('tool_arguments')
            index = call.get('index')
            if type(index) is not int or not 0 <= index < 8:
                raise ValueError('Invalid streamed tool call index')
            item = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
            if call.get('type') and call['type'] != 'function':
                raise ValueError('Unsupported streamed call type')
            if call.get('id'):
                item['id'] += call['id']
            function = call.get('function') or {}
            item['function']['name'] += function.get('name') or ''
            item['function']['arguments'] += function.get('arguments') or ''
        finish = choice.get('finish_reason') or finish
    if not done or finish is None:
        if cancellation:cancellation.check()
        raise IncompleteSSEError(diagnostics('missing_finish_reason' if done else 'unexpected_eof'))
    message = {'role': 'assistant', 'content': ''.join(content) or None}
    if reasoning:
        message['reasoning_content'] = ''.join(reasoning)
    if calls:
        if sorted(calls) != list(range(len(calls))):
            raise ValueError('Noncontiguous streamed tool calls')
        message['tool_calls'] = [calls[i] for i in sorted(calls)]
    diagnostics('complete')
    return {'choices': [{'message': message, 'finish_reason': finish}], 'usage': usage or {},
            'model': model, 'system_fingerprint': fingerprint}
