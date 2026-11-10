"""Offline UI integration fixture: scripted decisions, real tools and approvals.

Creates a fresh workspace; never uses API keys, frozen benchmark tasks or scores.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pico.providers.clients import FakeModelClient
from pico.web import Workbench, make_server


class DemoClient(FakeModelClient):
    model = 'GoalTree offline scripted fixture (not a model evaluation)'

    def __init__(self):
        super().__init__([])
        self.step = 0

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        self.step += 1
        marker = 'GoalTree current scoped task state:\n'
        context = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0] if marker in prompt else {}
        if context.get('mode') == 'assistant':
            request = context.get('user_request', '')
            # Explicit offline fixture decisions. Production uses the configured
            # model and native tools; this is not a keyword router in the harness.
            tasks = context.get('existing_tasks', [])
            if (any(word in request for word in ('规划', '计划', '拆分', '安排'))
                    and not any(word in request for word in ('资料整理', '整理资料', '交付物', '工作区说明'))):
                return ('<final>当前运行的是固定离线演示脚本，不能理解或规划这个新目标；'
                        '这次没有创建或修改任务树。请启动真实模型服务后再发送这条需求。'
                        '离线模式仅支持资料整理演示流程，不代表 Agent 的规划能力。</final>')
            if any(word in request for word in ('调整', '改成', '修改任务')) and tasks:
                if self.step == 1:
                    selected = next((t for t in tasks if '生成' in t['title']), tasks[-1])
                    return self.tool('goal_action', {'proposal': json.dumps({'action': 'update_task', 'task_id': selected['id'],
                        'revision': selected['revision'], 'title': '整理阶段摘要', 'description': '先整理阶段摘要，再检查交付结果。'}, ensure_ascii=False)})
                return '<final>任务树已更新。这是离线脚本演示，实际模型效果尚未评测。</final>'
            if any(word in request for word in ('规划', '计划', '拆分', '安排')) and '为什么' not in request:
                if not context.get('goal'):
                    return self.tool('goal_action', {'proposal': json.dumps({'action': 'create_goal', 'title': '资料整理计划',
                        'description': request, 'constraints': '离线集成演示；文件修改和命令执行仍需审批。'}, ensure_ascii=False)})
                if not tasks:
                    steps = [{'id': 'all', 'title': '完成资料整理', 'description': '组织资料、生成结果并复核。', 'verification_mode': 'manual'},
                        {'id': 'source', 'parent_id': 'all', 'title': '读取工作区说明', 'description': '读取 README.md，确认当前资料和约束。', 'acceptance': '核对真实读取结果', 'verification_mode': 'manual'},
                        {'id': 'draft', 'parent_id': 'all', 'depends_on': ['source'], 'title': '生成整理结果', 'description': '生成 delivery.txt，并验证产物。', 'acceptance': '现有验证命令通过'},
                        {'id': 'review', 'parent_id': 'all', 'depends_on': ['draft'], 'title': '复核交付结果', 'description': '重新读取并验证产物。', 'acceptance': '当前文件和验证结果一致'}]
                    return self.tool('plan_tasks', {'steps': json.dumps(steps, ensure_ascii=False)})
                return ('<final>已保存固定的资料整理演示树：\n'
                        '1. 完成资料整理\n  1.1 读取工作区说明：核对 README.md。\n'
                        '  1.2 生成整理结果：准备 delivery.txt 并验证。\n'
                        '  1.3 复核交付结果：重新读取并核验。\n'
                        '这只是预设离线夹具，没有理解任意新目标，也没有开始执行。</final>')
            if '执行' in request and tasks:
                selected = next((t for t in tasks if t['title'] == '读取工作区说明'), None)
                if selected and self.step == 1:
                    return self.tool('run_task', {'task_id': selected['id'], 'instruction': '读取 README.md，报告真实内容。'})
                return '<final>已在任务的独立上下文中执行。结果仍需按验收条件核对；这是离线脚本演示。</final>'
            return '<final>可以直接讨论目标、调整计划，或继续执行。当前是离线脚本演示，不会把普通问答自动变成任务。</final>'
        if context.get('mode') == 'plan':
            if self.step == 1:
                steps = [
                    {'id': 'delivery', 'title': '准备目标交付物', 'description': '组织说明、产物与验收；此父节点表示任务分组。', 'acceptance': '人工检查三个子任务的产物'},
                    {'id': 'inspect', 'parent_id': 'delivery', 'title': '读取工作区说明', 'description': '读取 README.md，确认本地工作区和约束。', 'acceptance': '依据真实读取结果说明工作区用途', 'verification_mode': 'manual'},
                    {'id': 'deliver', 'parent_id': 'delivery', 'depends_on': ['inspect'], 'title': '生成并验证交付物', 'description': '写入 delivery.txt，然后执行现有验证命令。', 'acceptance': 'delivery.txt 包含 GoalTree，当前测试通过'},
                    {'id': 'review', 'parent_id': 'delivery', 'depends_on': ['deliver'], 'title': '复核交付结果', 'description': '读取 delivery.txt，并重新验证。', 'acceptance': '依据当前产物和测试结果报告'},
                ]
                return self.tool('plan_tasks', {'steps': json.dumps(steps, ensure_ascii=False)})
            return '<final>离线脚本已通过真实规划工具保存任务树。可点击节点查看独立上下文、依赖与验收条件。这不代表模型规划效果。</final>'
        if context.get('mode') == 'execute':
            inspect = context.get('task', {}).get('title') == '读取工作区说明'
            review = context.get('task', {}).get('title') == '复核交付结果'
            if self.step == 1:
                return self.tool('read_file', {'path': 'delivery.txt' if review else 'README.md'})
            if inspect:
                return '<final>已通过真实读取查看本地工作区说明。此任务需要你根据读取证据人工验收；脚本不会把说明性回答当作测试通过。</final>'
            if review and self.step == 2:
                return self.tool('run_shell', {'command': 'python -B -m unittest discover -v'})
            if review:
                return '<final>已实际读取交付物并调用验证命令，请查看真实结果。这是离线集成夹具。</final>'
            if self.step == 2:
                return self.tool('write_file', {'path': 'delivery.txt', 'content': 'GoalTree local delivery\n'})
            if self.step == 3:
                return self.tool('run_shell', {'command': 'python -B -m unittest discover -v'})
            return '<final>离线脚本已完成真实工具调用。请以右侧实际审批、文件差异和测试结果为准；这不是模型自主完成成绩。</final>'
        if self.step == 1:
            return self.tool('read_file', {'path': 'README.md'})
        return '<final>这是独立的只读聊天上下文。此脚本仅验证交互与隔离机制，不会规划任务或改写文件；真实模型效果尚未评估。</final>'

    @staticmethod
    def tool(name, args):
        return '<tool>' + json.dumps({'name': name, 'args': args}, ensure_ascii=False) + '</tool>'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8771)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--shell-backend', choices=('host', 'appcontainer'), default='host')
    args = parser.parse_args(argv)
    directory = args.output or Path(tempfile.mkdtemp(prefix='goaltree-demo-'))
    directory = directory.resolve()
    # The demo must never replace an existing user workspace.
    if directory.exists() and any(directory.iterdir()):
        parser.error('--output must be an empty or new directory')
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'README.md').write_text('# GoalTree integration fixture\nLocal-only workspace. No network or credentials.\n', encoding='utf-8')
    (directory / 'test_delivery.py').write_text(
        "import unittest\nfrom pathlib import Path\n\nclass DeliveryTest(unittest.TestCase):\n"
        "    def test_delivery(self):\n        self.assertIn('GoalTree', Path('delivery.txt').read_text(encoding='utf-8'))\n", encoding='utf-8')
    app = Workbench(directory, model_factory=DemoClient, harness_profile='workbench',
                    verification_command='python -B -m unittest discover -v', shell_backend=args.shell_backend)
    for title, description in [('整理本地交付物', '读取说明，生成交付物并验证。'), ('准备研究资料', '整理资料的任务规划演示，与另一个目标的聊天和任务状态隔离。')]:
        app.tree_action({'action': 'create_goal', 'title': title, 'description': description,
                         'constraints': '所有数据保存在本地；本演示使用脚本化决策，不使用模型 API。'})
    first = next(iter(app.goals.read()['goals']))
    app.tree_action({'action': 'select', 'goal_id': first})
    server = make_server(app, args.port)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(json.dumps({'url': url, 'workspace': str(directory), 'model_calls': 0, 'mode': 'scripted-offline', 'shell_backend': args.shell_backend}, ensure_ascii=False), flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
