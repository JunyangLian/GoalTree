"""Wire-level contracts shared by external text and native environments."""
import json
from unittest.mock import patch

import pytest

from pico import EnvironmentProfile, FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.environment import tool_json_schema, validate_tool_arguments
from pico.providers.clients import OpenAICompatibleModelClient
from pico.providers.native_tools import NativeToolConversation
from pico.prompt_prefix import build_prompt_prefix, tool_signature


PROFILE = EnvironmentProfile(
    name='simulated-phone',
    role='You are GoalTree, operating a simulated phone through its applications.',
    instructions=(
        'There is no host file system.',
        'Finish required environment actions before reporting completion.',
    ),
    budget_instruction='Reserve a step for the required environment action.',
)


def external_tools(calls=None):
    def send(args):
        if calls is not None:
            calls.append(args)
        return json.dumps({'sent': True, 'recipients': args['recipients']})

    return {
        'Emails__send_email': {
            'schema': {
                'recipients': 'list[str]',
                'subject': 'str',
                'attendees': 'list[int]=[]',
                'dry_run': 'bool=False',
                'score': 'float=1.0',
                'metadata': 'dict={}',
            },
            'risky': False,
            'effect': 'write',
            'description': 'Send an email in the simulated environment.',
            'run': send,
        }
    }


def test_complete_schema_and_shared_validation_cover_structured_types():
    spec = external_tools()['Emails__send_email']
    schema = tool_json_schema(spec)
    properties = schema['properties']
    assert properties['recipients'] == {'type': 'array', 'items': {'type': 'string'}}
    assert properties['attendees'] == {'type': 'array', 'items': {'type': 'integer'}, 'default': []}
    assert properties['dry_run'] == {'type': 'boolean', 'default': False}
    assert properties['score'] == {'type': 'number', 'default': 1.0}
    assert properties['metadata'] == {'type': 'object', 'default': {}}
    assert schema['required'] == ['recipients', 'subject']
    valid = {'recipients': ['a@example.test'], 'subject': 'Hello', 'dry_run': True}
    validate_tool_arguments(spec, valid)
    with pytest.raises(ValueError, match='array'):
        validate_tool_arguments(spec, {**valid, 'recipients': 'a@example.test'})
    with pytest.raises(ValueError, match='unknown argument'):
        validate_tool_arguments(spec, {**valid, 'recursive': True})


def test_nested_json_schema_is_used_for_validation_without_a_new_dependency():
    spec = {
        'json_schema': {
            'type': 'object',
            'properties': {
                'message': {
                    'type': 'object',
                    'properties': {
                        'recipients': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                        'urgent': {'type': 'boolean'},
                    },
                    'required': ['recipients', 'urgent'],
                    'additionalProperties': False,
                }
            },
            'required': ['message'],
            'additionalProperties': False,
        }
    }
    validate_tool_arguments(spec, {'message': {'recipients': ['a'], 'urgent': False}})
    with pytest.raises(ValueError, match='too few'):
        validate_tool_arguments(spec, {'message': {'recipients': [], 'urgent': False}})
    with pytest.raises(ValueError, match='unknown argument'):
        validate_tool_arguments(spec, {'message': {'recipients': ['a'], 'urgent': False, 'hack': True}})


def test_text_environment_prompt_has_no_phantom_local_coding_examples(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    prefix = build_prompt_prefix(workspace, external_tools(), environment=PROFILE)
    assert PROFILE.role in prefix.text
    assert 'There is no host file system.' in prefix.text
    assert 'Emails__send_email' in prefix.text
    assert 'small local coding agent' not in prefix.text
    assert 'read_file' not in prefix.text
    assert 'write_file' not in prefix.text
    assert 'run_shell' not in prefix.text


def test_native_environment_prompt_schema_and_budget_share_profile_semantics():
    conversation = NativeToolConversation(external_tools(), 'simulated workspace', 'send mail', None,
                                         require_reasoning=False, environment=PROFILE)
    wire = conversation.payload()
    system = wire['messages'][0]['content']
    assert PROFILE.role in system
    assert 'There is no host file system.' in system
    assert 'local coding agent' not in system
    assert '<tool>' not in system
    parameters = wire['tools'][0]['function']['parameters']
    assert parameters['properties']['recipients'] == {'type': 'array', 'items': {'type': 'string'}}
    conversation.set_budget(3)
    budget = conversation.payload()['messages'][2]['content']
    assert '3 tool steps remain' in budget
    assert PROFILE.budget_instruction in budget
    assert 'failing symbol' not in budget


def test_runtime_rejects_invalid_external_arguments_before_side_effects(tmp_path):
    calls = []

    class ExternalPico(Pico):
        def build_tools(self):
            return external_tools(calls)

    agent = ExternalPico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
                         SessionStore(tmp_path / '.pico/sessions'), environment=PROFILE,
                         approval_policy='auto', max_depth=0)
    result = agent.execute_tool('Emails__send_email', {'recipients': 'a', 'subject': 'Hello'})
    assert result.metadata['tool_status'] == 'rejected'
    assert result.metadata['tool_error_code'] == 'invalid_arguments'
    assert calls == []
    result = agent.execute_tool('Emails__send_email', {'recipients': ['a'], 'subject': 'Hello'})
    assert result.metadata['tool_status'] == 'ok'
    assert result.metadata['effect'] == 'write'
    assert calls == [{'recipients': ['a'], 'subject': 'Hello'}]


def test_explicit_text_protocol_cannot_reuse_native_payload(tmp_path):
    client = FakeModelClient(['<final>Done</final>'])
    client.supports_native_tools = True
    client.native_conversation = object()
    agent = Pico(client, WorkspaceContext.build(tmp_path), SessionStore(tmp_path / '.pico/sessions'),
                 environment=PROFILE, wire_protocol='text', max_depth=0)
    assert agent.ask('Answer directly') == 'Done'
    assert agent.effective_wire_protocol == 'text'
    assert client.native_conversation is None
    assert PROFILE.role in client.prompts[0]


def test_explicit_native_protocol_requires_client_support(tmp_path):
    agent = Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
                 SessionStore(tmp_path / '.pico/sessions'), wire_protocol='native')
    with pytest.raises(ValueError, match='does not support'):
        agent.ask('test')


def test_native_agent_wire_executes_typed_external_arguments(tmp_path):
    calls = []

    class ExternalPico(Pico):
        def build_tools(self):
            return external_tools(calls)

    client = OpenAICompatibleModelClient(
        'glm-5.3-flash', 'https://open.bigmodel.cn/api/paas/v4', 'fake', .2, 30,
        api_format='chat_completions',
    )
    sent = []
    responses = [
        {'model': 'glm-5.3-flash', 'choices': [{'message': {'content': '', 'tool_calls': [{
            'id': 'mail-1', 'type': 'function',
            'function': {'name': 'Emails__send_email', 'arguments': json.dumps({
                'recipients': ['a@example.test'], 'subject': 'Hello',
            })},
        }]}, 'finish_reason': 'tool_calls'}], 'usage': {}},
        {'model': 'glm-5.3-flash', 'choices': [{'message': {'content': 'Sent', 'reasoning_content': ''}, 'finish_reason': 'stop'}], 'usage': {}},
    ]

    class Response:
        headers = {'Content-Type': 'application/json'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(responses.pop(0)).encode()

    def urlopen(request, **kwargs):
        sent.append(json.loads(request.data))
        return Response()

    agent = ExternalPico(client, WorkspaceContext.build(tmp_path), SessionStore(tmp_path / '.pico/sessions'),
                         environment=PROFILE, wire_protocol='native', approval_policy='auto',
                         max_steps=1, max_depth=0)
    with patch('urllib.request.urlopen', side_effect=urlopen):
        assert agent.ask('Send the email') == 'Sent'
    assert calls == [{'recipients': ['a@example.test'], 'subject': 'Hello'}]
    assert agent.effective_wire_protocol == 'native'
    system = sent[0]['messages'][0]['content']
    assert PROFILE.role in system and 'local coding agent' not in system
    schema = sent[0]['tools'][0]['function']['parameters']
    assert schema['properties']['recipients']['type'] == 'array'
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report['environment'] == 'simulated-phone'
    assert report['effective_wire_protocol'] == 'native'


def test_json_schema_only_tool_renders_text_native_and_signature(tmp_path):
    spec = {
        'json_schema': {
            'type': 'object',
            'properties': {'value': {'type': ['string', 'null']}},
            'required': ['value'],
            'additionalProperties': False,
        },
        'risky': False,
        'description': 'Return a nullable value.',
        'run': lambda args: args['value'] or '',
    }
    tools = {'Nullable__echo': spec}
    prefix = build_prompt_prefix(WorkspaceContext.build(tmp_path), tools, environment=PROFILE)
    assert 'Nullable__echo' in prefix.text and 'null' in prefix.text
    assert tool_signature(tools)
    conversation = NativeToolConversation(tools, 'workspace', 'echo', None,
                                         require_reasoning=False, environment=PROFILE)
    assert conversation.tools[0]['function']['parameters'] == spec['json_schema']


def test_workbench_json_schema_overrides_legacy_and_preview_includes_evidence_suffix(tmp_path):
    class ExternalPico(Pico):
        def build_tools(self):
            return {
                'Nullable__echo': {
                    'schema': {'value': 'str'},
                    'json_schema': {
                        'type': 'object',
                        'properties': {
                            'value': {'type': ['string', 'null']},
                            'schema_only_field': {'type': 'string'},
                        },
                        'required': ['value', 'schema_only_field'],
                        'additionalProperties': False,
                    },
                    'risky': False,
                    'description': 'Return a long value.',
                    'run': lambda args: 'x' * 5000,
                }
            }

    agent = ExternalPico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
                         SessionStore(tmp_path / '.pico/sessions'), harness_profile='workbench',
                         environment=PROFILE, approval_policy='auto', max_depth=0)
    result = agent.execute_tool('Nullable__echo', {'value': None, 'schema_only_field': 'accepted'})
    assert result.metadata['tool_status'] == 'ok'
    assert 'Evidence ID:' in result.content
    assert result.metadata['output_evidence_suffix_chars'] > 0
    assert result.metadata['output_preview_chars'] == len(result.content)
    assert result.metadata['output_total_chars'] == 5000
    assert result.metadata['output_truncated'] is True


def test_slots_only_legacy_text_client_needs_no_native_attribute(tmp_path):
    class SlotsClient:
        __slots__ = ('privacy_root', 'last_completion_metadata')
        supports_prompt_cache = False
        supports_native_tools = False

        def complete(self, *args, **kwargs):
            return '<final>legacy text</final>'

    client = SlotsClient()
    agent = Pico(client, WorkspaceContext.build(tmp_path), SessionStore(tmp_path / '.pico/sessions'),
                 environment=PROFILE, wire_protocol='text', max_depth=0)
    assert agent.ask('test') == 'legacy text'
    assert not hasattr(client, 'native_conversation')


def test_legacy_native_client_rejects_environment_with_clear_capability_error(tmp_path):
    class LegacyNativeClient(FakeModelClient):
        supports_native_tools = True

        def begin_native_run(self, tools, context, request, verification_command):
            raise AssertionError('must be rejected before the legacy call')

    agent = Pico(LegacyNativeClient([]), WorkspaceContext.build(tmp_path),
                 SessionStore(tmp_path / '.pico/sessions'), environment=PROFILE,
                 wire_protocol='native', max_depth=0)
    with pytest.raises(TypeError, match='cannot accept EnvironmentProfile'):
        agent.ask('test')
