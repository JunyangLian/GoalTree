import json

import pytest

from scripts.report_glm_benchmark import jsonlines, native_continuity


def test_jsonl_audit_preserves_unicode_line_separators_inside_model_text(tmp_path):
    path = tmp_path/'wire.jsonl'
    records = [{'event':'response','text':'a\u2028b\u2029c'}, {'event':'request','text':'next'}]
    path.write_bytes(('\r\n'.join(json.dumps(r,ensure_ascii=False) for r in records)+'\r\n').encode('utf-8'))
    assert jsonlines(path) == records


def test_jsonl_audit_does_not_hide_a_genuinely_truncated_record(tmp_path):
    path = tmp_path/'wire.jsonl'
    path.write_text('{"event":"request"}\n{"event":"response","text":"unfinished',encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        jsonlines(path)


def test_continuity_does_not_claim_tool_roundtrip_or_text_preservation_without_evidence():
    call = {'id':'c','function':{'name':'read_file','arguments':'{}'}}
    wire = [
        {'event':'response','body':{'choices':[{'finish_reason':'tool_calls',
            'message':{'role':'assistant','tool_calls':[call],'reasoning_content':'original'}}]}},
        {'event':'request','body':{'messages':[{'role':'assistant','tool_calls':[call],
            'reasoning_content':'rewritten'}]}}]
    result = native_continuity(wire,[])
    assert result['returned_reasoning_replay_checks'] == 1
    assert result.get('returned_reasoning_replayed_exactly',0) == 0
    assert result.get('all_calls_recorded_and_results_returned',0) == 0
