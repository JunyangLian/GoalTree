"""A large web snapshot must not hold the stream writer's condition lock."""
import threading
import io
import json

import pytest

from pico.web import Task


def test_task_snapshot_redaction_does_not_block_new_delta_and_keeps_snapshot_consistent(monkeypatch):
    from pico import web
    task=Task('prompt','session','never',20)
    original={'event':'model_delta','text':'first','created_at':'fixture'}
    task.events=[original]
    entered,appended=threading.Event(),threading.Event()
    original_redact=web.redact_artifact
    def redact(data):
        entered.set()
        assert appended.wait(5),'SSE writer was blocked by task.view() redaction'
        return original_redact(data)
    monkeypatch.setattr(web,'redact_artifact',redact)
    def append_delta():
        assert entered.wait(5)
        with task.condition:
            task.events.append({'event':'model_delta','text':'second'})
            appended.set()
    worker=threading.Thread(target=append_delta)
    worker.start()
    try:result=task.view()
    finally:worker.join(5)
    assert result['events']==[original] and len(task.events)==2
    assert not worker.is_alive()


def test_stream_diagnostics_separate_local_callback_time_from_read_wait(monkeypatch):
    from pico.providers import streaming
    ticks=[0.0]
    monkeypatch.setattr(streaming.time,'perf_counter',lambda:ticks[0])
    raw=b'data: '+json.dumps({'choices':[{'delta':{'content':'visible'},'finish_reason':'stop'}]}).encode()+b'\n\ndata: [DONE]\n\n'
    class DelayedRead(io.BytesIO):
        def readline(self,*args):
            ticks[0]+=.01
            return super().readline(*args)
    def consumer(text):ticks[0]+=.04
    audits=[]
    streaming.read_chat_stream(DelayedRead(raw),on_delta=consumer,on_diagnostics=audits.append)
    assert audits[0]['consumer_callback_ms']==40
    assert audits[0]['read_wait_ms']==30 and audits[0]['max_read_wait_ms']==10
    assert audits[0]['elapsed_ms']==70


def test_continuously_trickling_stream_cannot_evade_elapsed_budget(monkeypatch):
    from pico.providers import streaming
    ticks=[0.0]
    monkeypatch.setattr(streaming.time,'perf_counter',lambda:ticks[0])
    class Trickle:
        def readline(self,*args):
            ticks[0]+=.1
            return b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
    with pytest.raises(streaming.IncompleteSSEError) as error:
        streaming.read_chat_stream(Trickle(),max_seconds=.25)
    assert error.value.diagnostics['termination']=='stream_deadline'
    assert error.value.diagnostics['answer_chars']==3
