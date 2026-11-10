"""Planning transport/policy contracts. Scripted responses are not model-quality scores."""
import json
from pathlib import Path

import pytest

from pico.providers.clients import FakeModelClient
from pico.providers.native_tools import NativeToolConversation
from pico.web import Workbench
from scripts.run_goaltree_demo import DemoClient


REQUEST = '我想做一个生信agent，你帮我规划一下，目标是做鱼类组学的，不做大模型。'
STEPS = [
    {'id': 'scope', 'title': '明确鱼类组学需求', 'description': '暂按已有测序数据进行规划，物种与组学类型待确认。',
     'acceptance': '形成明确边界与待确认项的需求说明', 'verification_mode': 'manual'},
    {'id': 'species', 'parent_id': 'scope', 'title': '确认物种与数据类型', 'description': '区分基因组、转录组与群体重测序；不训练大模型。',
     'acceptance': '记录物种、数据来源与分析目的', 'verification_mode': 'manual'},
    {'id': 'workflow', 'title': '设计可复现分析流程', 'description': '根据数据类型选择现有生信工具，不训练大模型。',
     'acceptance': '形成输入、输出、运行环境与验证方式的说明', 'verification_mode': 'manual'},
    {'id': 'qc', 'parent_id': 'workflow', 'depends_on': ['species'], 'title': '规划质控与结果核验',
     'description': '定义质量检查与结果解释规则；当前只规划，不下载或运行。',
     'acceptance': '列出质量报告、日志与可检查产物', 'verification_mode': 'manual'},
]
ANSWER = ('先做前期规划，不开始运行分析，也不训练大模型。\n'
          '1. 明确鱼类组学需求\n  1.1 确认物种与数据类型，产出需求说明。\n'
          '2. 设计可复现分析流程\n  2.1 规划质控与结果核验，产出流程与验收说明。\n'
          '假设你已有或准备获取测序数据。你计划研究什么鱼？优先做哪一类组学分析？')


class PlanningClient(FakeModelClient):
    """Explicit fixture exercises tools; it does not infer biological requirements."""
    def __init__(self):
        super().__init__([
            '<tool>' + json.dumps({'name': 'goal_action', 'args': {'proposal': json.dumps({
                'action': 'create_goal', 'title': '鱼类组学生信 Agent 前期规划',
                'description': REQUEST, 'constraints': '只做前期规划；不训练大模型；物种与组学类型待确认。'}, ensure_ascii=False)}}) + '</tool>',
            '<tool>' + json.dumps({'name': 'plan_tasks', 'args': {'steps': json.dumps(STEPS, ensure_ascii=False)}}) + '</tool>',
            '<final>' + ANSWER + '</final>',
        ])


class NativePlanningClient(PlanningClient):
    supports_native_tools = True

    def begin_native_run(self, tools, context, request, command):
        self.native_conversation = NativeToolConversation(tools, context, request, command, require_reasoning=False)
        self.sent = []

    def complete(self, prompt, *args, **kwargs):
        self.sent.append(self.native_conversation.payload())
        response = super().complete(prompt, *args, **kwargs)
        if response.startswith('<tool>'):
            call = json.loads(response[len('<tool>'):-len('</tool>')])
            message = {'tool_calls': [{'id': 'plan-' + str(len(self.sent)), 'type': 'function',
                'function': {'name': call['name'], 'arguments': json.dumps(call['args'])}}]}
        else:
            message = {'content': response[len('<final>'):-len('</final>')]}
        return self.native_conversation.accept({'choices': [{'message': message}]}, {})

    def observe_native_tool(self, result):
        self.native_conversation.observe(result)

    def native_notice(self, message):
        self.native_conversation.notice(message)


def run(app, prompt):
    task = app.task(app.submit({'mode': 'assistant', 'prompt': prompt, 'max_steps': 6})['id'])
    with task.condition:
        assert task.condition.wait_for(lambda: task.finished is not None, 30)
    assert task.status == 'completed', task.error
    return task


@pytest.mark.parametrize('client', [PlanningClient, NativePlanningClient], ids=['text', 'native'])
def test_domain_plan_preserves_hierarchy_constraints_and_answer_without_execution(tmp_path, client):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=client)
    result = run(app, REQUEST)
    state = app.goals.read()
    assert len(state['goals']) == 1 and len(state['tasks']) == 4
    goal = state['goals'][result.binding['goal_id']]
    assert '不训练大模型' in goal['constraints']
    tasks = {t['title']: t for t in state['tasks'].values()}
    assert tasks['确认物种与数据类型']['parent_id'] == tasks['明确鱼类组学需求']['id']
    assert tasks['规划质控与结果核验']['parent_id'] == tasks['设计可复现分析流程']['id']
    assert tasks['规划质控与结果核验']['depends_on'] == [tasks['确认物种与数据类型']['id']]
    assert all(t['status'] == 'planned' and not t['runs'] and t['verification_mode'] == 'manual' for t in tasks.values())
    assert result.answer == ANSWER
    assert '1.1' in app.tree_context(goal['id'], mode='assistant')['messages'][-1]['content']
    assert len(app.tasks) == 1  # No delegated execution for a planning-only request.
    assert not list(tmp_path.glob('delivery.txt'))
    prefix = result.agent.prefix
    assert 'general-purpose goal planning' in prefix and 'a small local coding agent' not in prefix
    if client is NativePlanningClient:
        payload = result.agent.model_client.sent[-1]
        system = payload['messages'][0]['content']
        assert 'general-purpose goal planning' in system
        assert 'local coding agent' not in system
        assert 'Inspect implementation and actual failing assertions' not in system
        assert 'not starting implementation' in system
        assert 'parent_id' in system and 'Never reply only' in system
        goal_tool = next(t['function'] for t in payload['tools'] if t['function']['name'] == 'goal_action')
        assert 'constraints:string' in goal_tool['description'] and 'NEVER an array' in goal_tool['description']
        assert REQUEST in json.dumps(payload, ensure_ascii=False)
        assert not result.agent.model_client.native_conversation.pending


def test_offline_fixture_does_not_substitute_generic_tasks_for_new_domain(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=DemoClient)
    before = app.goals.read()
    result = run(app, REQUEST)
    assert '不能理解或规划这个新目标' in result.answer
    after = app.goals.read()
    assert after['goals'] == before['goals'] == {}
    assert after['tasks'] == before['tasks'] == {}
    assert '已根据目标整理任务树' not in result.answer


def test_coding_native_prompt_keeps_repair_instructions():
    from pico.tools import BASE_TOOL_SPECS
    state = NativeToolConversation(BASE_TOOL_SPECS, 'source', 'repair a bug', 'python -m unittest')
    state.set_budget(3)
    assert 'Inspect implementation and actual failing assertions' in state.messages[0]['content']
    assert 'python -m unittest' in state.messages[0]['content']


def test_native_planner_can_correct_rejected_constraints_without_weakening_schema(tmp_path):
    class RecoveringPlanner(NativePlanningClient):
        def __init__(self):
            super().__init__()
            bad = {'action': 'create_goal', 'title': 'Rejected draft', 'constraints': ['invalid array']}
            self.outputs.insert(0, '<tool>' + json.dumps({'name': 'goal_action', 'args': {
                'proposal': json.dumps(bad)}}) + '</tool>')

    app = Workbench(tmp_path, harness_profile='workbench', model_factory=RecoveringPlanner,
                    native_input_budget=131072, native_context_window=262144)
    result = run(app, REQUEST)
    assert result.answer == ANSWER
    assert len(app.goals.read()['goals']) == 1
    failures = [e for e in result.events if e.get('event') == 'tool_executed' and e.get('tool_status') == 'error']
    assert len(failures) == 1 and '约束' in failures[0]['result']
    payload = result.agent.model_client.sent[1]
    assert any(m['role'] == 'tool' and '约束' in m['content'] for m in payload['messages'])
    assert all(t['status'] == 'planned' and not t['runs'] for t in app.goals.read()['tasks'].values())


def test_default_launcher_is_live_and_demo_is_explicit():
    root = Path(__file__).resolve().parents[1]
    launcher = (root / 'start-goaltree.cmd').read_text(encoding='utf-8')
    assert 'run_goaltree_glm.py' in launcher and 'run_goaltree_demo.py' not in launcher
    assert 'run_goaltree_demo.py' in (root / 'start-goaltree-demo.cmd').read_text(encoding='utf-8')


def test_live_glm_factory_uses_explicit_standard_api_without_fallback(tmp_path, monkeypatch):
    from scripts.run_goaltree_glm import load_key, model_factory
    monkeypatch.delenv('BIGMODEL_API_KEY', raising=False)
    monkeypatch.setenv('PICO_OPENAI_API_KEY', 'unrelated-provider-fixture')
    path = tmp_path / '.env.glm.local'
    path.write_text('BIGMODEL_API_KEY=local-standard-api-fixture\n', encoding='utf-8')
    key = load_key(path)
    client = model_factory(key)()
    assert client.model == 'glm-5.3-flash'
    assert client.base_url == 'https://open.bigmodel.cn/api/paas/v4'
    assert client.api_key == 'local-standard-api-fixture'
    assert client.supports_native_tools and client.max_transport_attempts == 2
    assert client.native_wire_options['thinking']['clear_thinking'] is False
    assert client.temperature == 1 and client.top_p == .95
    path.write_text('BIGMODEL_API_KEY=\n', encoding='utf-8')
    assert not load_key(path)  # Never reuse a different provider's credential.


def test_live_entry_registers_appcontainer_verifier_without_running_it(tmp_path, monkeypatch):
    from scripts import run_goaltree_glm as entry
    monkeypatch.setenv('BIGMODEL_API_KEY', 'offline-standard-api-fixture')
    captured = {}

    def workbench(root, **kwargs):
        captured.update(kwargs)
        app = Workbench(root, **kwargs)
        agent = app._build_agent(None, 'ask', 3)
        assert agent.shell_backend == 'appcontainer'
        assert agent.model_client.model == entry.MODEL
        return app

    class Server:
        server_port = 8773
        def serve_forever(self):
            raise KeyboardInterrupt
        def server_close(self):
            captured['closed'] = True

    monkeypatch.setattr(entry, 'Workbench', workbench)
    monkeypatch.setattr(entry, 'make_server', lambda app, port, **kwargs: Server())
    assert entry.main(['--workspace', str(tmp_path)]) == 0
    assert captured['verification_command'] == 'python -B -m unittest discover -v'
    assert captured['network_route'] == 'direct'
    assert captured['native_input_budget'] == 983040
    assert captured['native_context_window'] == 1048576
    assert captured['web_output_chars'] == 120000
    assert captured['web_result_chars'] == 12000
    assert captured['web_read_lines'] == 120
    assert captured['closed']
