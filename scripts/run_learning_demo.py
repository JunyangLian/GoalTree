"""Offline learning integration demo: scripted decisions, actual tools/tests.

Creates a fresh workspace and never reads API keys or frozen benchmark tasks.
The report verifies lifecycle wiring, not model quality or causal improvement.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pico.evolution import EvolutionStore
from pico.goals import GoalStore
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico, SessionStore
from pico.web import Workbench, make_server
from pico.workspace import WorkspaceContext


def tool(tool_name, **args):
    return '<tool>' + json.dumps({'name': tool_name, 'args': args}, ensure_ascii=False) + '</tool>'


def run_fixture(root, goals, goal, task, outputs, verify=None, trial=None):
    binding = goals.binding(goal['id'], task['id'], 'execute')
    agent = Pico(FakeModelClient(outputs), WorkspaceContext.build(root, repo_root_override=root),
                 SessionStore(root/'.pico/sessions'), session={'id': binding['session_id'], 'history': [], 'memory': {}},
                 harness_profile='workbench', approval_policy='auto', verification_command=verify,
                 max_steps=8, max_depth=0)
    agent.goal_binding = binding
    agent.memory.isolated_session = True
    agent.learning_trial = trial
    web_id = 'demo_' + task['id']
    goals.start(binding, web_id)
    result = agent.ask(task['description'])
    state = agent.current_task_state
    goals.finish(binding, {'id': web_id, 'run_id': state.run_id, 'session_id': agent.session['id'],
                           'status': state.status, 'answer': result, 'verification': agent.last_verification})
    return agent


def build_demo(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    root = directory/'workspace'
    root.mkdir()
    (root/'README.md').write_text('Offline learning integration fixture. No external model calls.\n', encoding='utf-8')
    tests = root/'tests'
    tests.mkdir()
    (root/'counter.py').write_text('def increment(value):\n    return value\n', encoding='utf-8')
    (tests/'test_counter.py').write_text('import unittest\nfrom counter import increment\n'
        'class CounterTest(unittest.TestCase):\n    def test_increment(self):\n        self.assertEqual(increment(2), 3)\n', encoding='utf-8')
    command = subprocess.list2cmdline([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
    goals = GoalStore(root)
    goal = goals.action({'action': 'create_goal', 'title': '记忆与技能学习演示',
                         'description': '固定脚本决策；真实文件、测试与审计。不是模型效果评测。'})
    first = goals.action({'action': 'create_task', 'goal_id': goal['id'], 'title': '修复递增函数',
                          'description': '修复 counter.py，并运行当前工作区测试。'})
    seed = run_fixture(root, goals, goal, first, [
        tool('read_file', path='counter.py', start=1, end=20),
        tool('patch_file', path='counter.py', old_text='return value', new_text='return value + 1'),
        tool('run_shell', command=command, timeout=20), '<final>递增函数已修复，当前测试通过。</final>'], command)
    assert seed.last_verification['status'] == 'passed'
    store = EvolutionStore(root)
    skill = next(r for r in store.list(goal['id'], seed.session['id']) if r['kind'] == 'skill')
    feedback = store.observe('operator:format', category='feedback', session_id=seed.session['id'], goal_id=goal['id'],
                             observation='报告先给提纲，明确听众与时长后再写正文。')
    memory = store.propose('memory', 'report-format', {'text': '报告先给提纲。', 'conditions': '报告准备任务'},
                           [feedback['id']], goal_id=goal['id'])
    store.automatic(goal_id=goal['id'], session_id=seed.session['id'])
    memory = store.get(memory['id'])
    corrected = store.propose('memory', 'report-format',
        {'text': '报告先确认已有的听众与时长，再给提纲；不重复询问。', 'conditions': '报告准备任务'},
        [feedback['id']], goal_id=goal['id'])
    store.automatic(goal_id=goal['id'], session_id=seed.session['id'])
    assert store.get(corrected['id'])['status'] == 'active'
    assert store.get(memory['id'])['status'] == 'superseded'
    (root/'normalizer.py').write_text('def normalize(value):\n    return value.lower()\n', encoding='utf-8')
    (tests/'test_normalizer.py').write_text('import unittest\nfrom normalizer import normalize\n'
        'class NormalizeTest(unittest.TestCase):\n    def test_trim(self):\n        self.assertEqual(normalize(" A "), "a")\n', encoding='utf-8')
    second = goals.action({'action': 'create_task', 'goal_id': goal['id'], 'title': '修复字符串归一化',
                           'description': '修复 normalizer.py 的空白处理，保留已有递增行为，运行全部测试。'})
    trial = run_fixture(root, goals, goal, second, [
        tool('read_file', path='normalizer.py', start=1, end=20),
        tool('patch_file', path='normalizer.py', old_text='value.lower()', new_text='value.strip().lower()'),
        tool('run_shell', command=command, timeout=20), '<final>归一化修复，当前两项测试通过。</final>'],
        command)
    assert trial.last_verification['status'] == 'passed'
    assert store.transfer_passed(skill)
    active_skill = store.get(skill['id'])
    third = goals.action({'action': 'create_task', 'goal_id': goal['id'], 'title': '查看报告提纲经验',
                          'description': '报告准备：读取已有经验与操作技能，不修改文件。', 'verification_mode': 'manual'})
    reuse = run_fixture(root, goals, goal, third, [tool('load_skill', name=skill['id']),
        '<final>已读取经验与技能。本轮仅查看，报告内容尚未验收。</final>'])
    assert reuse.session['working_state']['learning']['memories']
    checks = {'seed_verification_passed': seed.last_verification['status'] == 'passed',
              'distinct_trial_task': first['id'] != second['id'], 'trial_verification_passed': trial.last_verification['status'] == 'passed',
              'skill_activation_after_transfer': active_skill['status'] == 'active',
              'memory_correction_automatically_active': store.get(corrected['id'])['status'] == 'active',
              'memory_recalled_in_new_task': bool(reuse.session['working_state']['learning']['memories']),
              'skill_loaded_in_new_task': any(u['mode'] == 'skill_loaded' for u in store.usages(skill['id'])),
              'viewing_not_counted_as_success': store.usages(skill['id'])[-1]['outcome'] == 'unverified',
              'audit_valid': store.audit()['valid']}
    assert all(checks.values()), checks
    report = {'mode': 'scripted-offline', 'external_model_calls': 0, 'scope': 'Integration demonstration, not a benchmark or efficacy claim',
              'checks': checks, 'runs': [{'task_id': t['id'], 'run_id': a.current_task_state.run_id,
                  'verification': a.last_verification['status'], 'tool_steps': a.current_task_state.tool_steps}
                  for t, a in [(first, seed), (second, trial), (third, reuse)]],
              'audit': store.audit(), 'workspace': str(root), 'goal_id': goal['id'],
              'skill_id': skill['id'], 'memory_id': memory['id']}
    (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (directory/'final-files.json').write_text(json.dumps({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [root/'counter.py', root/'normalizer.py', *tests.glob('*.py')]}, indent=2)+'\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New output directory; existing evidence is never overwritten')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8775)
    args = parser.parse_args(argv)
    directory = args.output or Path(tempfile.mkdtemp(prefix='goaltree-learning-parent-'))/'demo'
    report = build_demo(directory)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if args.serve:
        class DemoClient(FakeModelClient):
            model = 'GoalTree learning offline fixture (not a model evaluation)'
            def __init__(self):
                super().__init__(['<final>这是离线演示。学习在后台自动推进；此回复未验证新任务。</final>'])
        app = Workbench(report['workspace'], model_factory=DemoClient, harness_profile='workbench', load_env=False)
        server = make_server(app, args.port)
        print(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port), 'goal_id': report['goal_id']}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
