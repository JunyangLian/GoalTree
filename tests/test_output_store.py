"""Bounded original-output storage and exact offset pagination."""
import json
import re

import pytest

from pico import EnvironmentProfile, FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.output_store import ToolOutputStore


PROFILE = EnvironmentProfile(name='external', role='Operate the external environment.')


def test_store_reads_exact_offsets_and_enforces_bounds(tmp_path):
    store = ToolOutputStore(tmp_path, 'session-a')
    raw = '0123456789' * 1000
    saved = store.put(raw, tool='Big__result', run_id='run-a')
    assert saved['total_chars'] == len(raw)
    assert saved['stored_truncated'] is False
    first = store.read(saved['output_id'], offset=1, limit=4000)
    assert first['text'] == raw[:4000]
    assert first['next_offset'] == 4001 and first['has_more'] is True
    middle = store.read(saved['output_id'], offset=4001, limit=4000)
    assert middle['text'] == raw[4000:8000]
    assert middle['next_offset'] == 8001 and middle['has_more'] is True
    last = store.read(saved['output_id'], offset=8001, limit=4000)
    assert last['text'] == raw[8000:]
    assert last['returned_chars'] == 2000
    assert last['next_offset'] is None and last['has_more'] is False
    with pytest.raises(ValueError, match='offset exceeds'):
        store.read(saved['output_id'], offset=len(raw) + 2)
    with pytest.raises(ValueError, match='limit'):
        store.read(saved['output_id'], limit=12001)
    with pytest.raises(ValueError, match='Unknown output ID'):
        store.read('out_0000000000000000')
    with pytest.raises(ValueError, match='Invalid tool output ID'):
        store.read('../other-session')


def test_store_caps_stored_content_and_session_count(tmp_path, monkeypatch):
    from pico import output_store as module
    monkeypatch.setattr(module, 'MAX_STORED_CHARS', 10)
    monkeypatch.setattr(module, 'MAX_OUTPUTS_PER_SESSION', 1)
    store = ToolOutputStore(tmp_path, 'session-a')
    saved = store.put('x' * 20, tool='Big__result', run_id='run-a')
    assert saved['stored_chars'] == 10 and saved['stored_truncated'] is True
    page = store.read(saved['output_id'], offset=10, limit=2)
    assert page['text'] == 'x' and page['has_more'] is False
    assert page['total_chars'] == 20 and page['stored_truncated'] is True
    assert store.put('another', tool='Big__result', run_id='run-b') is None


def external_agent(tmp_path, raw, **kwargs):
    class ExternalPico(Pico):
        def build_tools(self):
            return {
                'Big__result': {
                    'schema': {},
                    'risky': False,
                    'effect': 'read',
                    'description': 'Return a large result.',
                    'run': lambda args: raw,
                }
            }

    return ExternalPico(
        FakeModelClient([]), WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / '.pico/sessions'), environment=PROFILE,
        approval_policy='auto', max_depth=0, **kwargs,
    )


def test_external_runtime_stores_and_pages_truncated_output(tmp_path):
    raw = '0123456789' * 1000
    agent = external_agent(tmp_path, raw)
    assert 'read_tool_output' in agent.tools
    result = agent.execute_tool('Big__result', {})
    assert result.metadata['output_truncated'] is True
    assert result.metadata['output_total_chars'] == len(raw)
    assert result.metadata['output_stored_chars'] == len(raw)
    assert result.metadata['output_next_offset'] == 4001
    output_id = result.metadata['output_id']
    assert output_id in result.content and 'offset=4001' in result.content

    page = agent.execute_tool('read_tool_output', {
        'output_id': output_id, 'offset': result.metadata['output_next_offset'], 'limit': 4000,
    })
    assert page.metadata['tool_status'] == 'ok'
    assert page.metadata['effect'] == 'read'
    assert page.metadata['output_preview_chars'] == len(page.content)
    payload = json.loads(page.content)
    assert payload['text'] == raw[4000:8000]
    assert payload['next_offset'] == 8001 and payload['has_more'] is True

    final = agent.execute_tool('read_tool_output', {
        'output_id': output_id, 'offset': payload['next_offset'], 'limit': 4000,
    })
    payload = json.loads(final.content)
    assert payload['text'] == raw[8000:]
    assert payload['next_offset'] is None and payload['has_more'] is False


def test_output_reader_respects_allowlist_and_default_profile_is_unchanged(tmp_path):
    raw = 'x' * 5000
    agent = external_agent(tmp_path, raw, allowed_tools=['Big__result'])
    assert 'read_tool_output' not in agent.tools
    result = agent.execute_tool('Big__result', {})
    assert result.metadata['output_truncated'] is True
    assert 'output_id' not in result.metadata
    assert 'Output ID:' not in result.content
    assert 'read_tool_output(output_id=' not in result.content

    class LocalPico(Pico):
        pass

    local = LocalPico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
                      SessionStore(tmp_path / '.pico/sessions'), max_depth=0)
    assert 'read_tool_output' not in local.tools
    assert local.store_tool_output(raw, 'Big__result') is None


def test_agent_follows_output_id_and_pages_before_budget_finalization(tmp_path):
    raw = '0123456789' * 1000

    class PaginationClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return '<tool>{"name":"Big__result","args":{}}</tool>'
            if len(self.prompts) == 2:
                output_id = re.search(r'Output ID: (out_[0-9a-f]{16})', prompt).group(1)
                return ('<tool>{"name":"read_tool_output","args":{"output_id":"' + output_id +
                        '","offset":4001,"limit":4000}}</tool>')
            return '<final>Read the next page.</final>'

    agent = external_agent(tmp_path, raw, max_steps=2)
    agent.model_client = PaginationClient([])
    assert agent.ask('Read the large result') == 'Read the next page.'
    tools = [item['name'] for item in agent.session['history'] if item['role'] == 'tool']
    assert tools == ['Big__result', 'read_tool_output']
    tool_items = [item for item in agent.session['history'] if item['role'] == 'tool']
    paged = json.loads(tool_items[-1]['content'])
    assert paged['text'] == raw[4000:8000]
    assert paged['next_offset'] == 8001
    state = agent.current_task_state
    assert state.status == 'completed'
    assert state.resource_exhausted == 'tool_budget'
    assert state.task_outcome == 'unknown'
    report = agent.run_store.load_report(state.run_id)
    assert report['resource_exhausted'] == 'tool_budget'
    assert report['task_outcome'] == 'unknown'


def test_redaction_preview_storage_and_offsets_share_one_coordinate_space(tmp_path, monkeypatch):
    secret = 'sk-test-1234567890-1234567890-1234567890'
    monkeypatch.setenv('PICO_OPENAI_API_KEY', secret)
    raw = ('a' * 3990) + secret + ('b' * 8000)
    agent = external_agent(tmp_path, raw)
    safe = agent.redact_text(raw)
    assert len(safe) > 4000 and secret not in safe

    result = agent.execute_tool('Big__result', {})
    assert result.metadata['output_raw_chars'] == len(raw)
    assert result.metadata['output_total_chars'] == len(safe)
    assert result.metadata['output_stored_chars'] == len(safe)
    assert result.metadata['output_next_offset'] == 4001
    assert safe[:4000] in result.content
    assert secret not in result.content

    reconstructed = safe[:4000]
    offset = result.metadata['output_next_offset']
    for _ in range(10):
        page = agent.execute_tool('read_tool_output', {
            'output_id': result.metadata['output_id'], 'offset': offset, 'limit': 4000,
        })
        payload = json.loads(page.content)
        reconstructed += payload['text']
        assert secret not in page.content
        if not payload['has_more']:
            break
        offset = payload['next_offset']
    else:
        raise AssertionError('pagination did not terminate')
    assert reconstructed == safe
    stored = agent.tool_outputs._path(result.metadata['output_id']).read_text(encoding='utf-8')
    assert secret not in stored
