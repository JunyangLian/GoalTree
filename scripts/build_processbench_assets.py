"""Build synthetic process fixtures; reference repairs stay in the controller only.

No API calls. Agents receive only a phase's files and prompt. This builder and
its oracle substitutions must never be installed into an agent workspace.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.repair_suite import write_json, digest, utcnow


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def build_cases():
    result, references = [], {}
    def add(key, split, category, files, edits, checks, required, description, phase_a=None):
        test = 'import unittest\n\nclass Acceptance(unittest.TestCase):\n'
        for i, check in enumerate(checks):
            test += f'    def test_{i}(self):\n'
            test += ''.join('        '+line+'\n' for line in check.splitlines())
        files = {**files, 'test_acceptance.py': test,
            'README.md': '# ProcessBench synthetic repair\n'+description+'\n'}
        fixed = {}
        for path, old, new in edits:
            before = fixed.get(path, files[path])
            if before.count(old) != 1:
                raise ValueError('Ambiguous controller substitution: '+key)
            fixed[path] = before.replace(old, new)
        gold_lines = []
        for path, start, end in required:
            lines = files[path].splitlines()
            for number in range(start, end+1):
                gold_lines.append({'path': path, 'line': number, 'text': lines[number-1],
                                   'source_sha256': sha(files[path])})
        task = {'id': key, 'root_fault_id': key, 'split': split, 'category': category,
            'origin': {'kind': 'author_created_synthetic', 'license': 'MIT',
                       'description': 'New small fixture; not an upstream repository failure'},
            'development_exposure': 'asset quality checks only; no model execution',
            'files': files, 'sha256': {n: sha(t) for n, t in files.items()},
            'editable_files': sorted({e[0] for e in edits}), 'expected_tests': len(checks),
            'prompt': description+'\n先运行 python -B -m unittest discover -v，定位并修复代码。'
                '只修改允许的实现文件，不改测试；最后重跑原命令，如实报告结果。',
            'gold': {'required_files': sorted({p for p, _, _ in required}), 'lines': gold_lines,
                     'status': 'controller-authored proxy; independent human evidence review pending'},
            'phase_a': phase_a,
            'phase_b_intervention': 'fresh session, frozen files replace phase A files; memory persists only inside scenario' if phase_a else None}
        result.append(task)
        references[key] = fixed

    add('dev_interval_end', 'dev', 'single_file',
        {'interval.py': 'def contains(start, end, value):\n    return start <= value <= end\n'},
        [('interval.py', 'value <= end', 'value < end')],
        ['from interval import contains\nself.assertFalse(contains(1, 3, 3))',
         'from interval import contains\nself.assertTrue(contains(1, 3, 1))',
         'from interval import contains\nself.assertFalse(contains(2, 2, 2))'],
        [('interval.py', 1, 2)], '区间应采用左闭右开语义，修复边界判断。')
    add('dev_sku_contract', 'dev', 'cross_file',
        {'catalog.py': 'def key(sku):\n    return sku.strip().casefold()\n\ndef lookup(items, sku):\n    return items.get(sku)\n',
         'basket.py': 'from catalog import lookup\n\ndef cost(items, sku, count):\n    return lookup(sku, items) * count\n'},
        [('catalog.py', 'items.get(sku)', 'items.get(key(sku))'),
         ('basket.py', 'lookup(sku, items)', 'lookup(items, sku)')],
        ['from basket import cost\nself.assertEqual(cost({"abc": 4}, " ABC ", 2), 8)',
         'from catalog import lookup\nself.assertEqual(lookup({"x": 3}, "X"), 3)',
         'from basket import cost\nx={"x": 2}\nself.assertEqual(cost(x, "x", 0), 0)\nself.assertEqual(x, {"x": 2})'],
        [('catalog.py', 1, 5), ('basket.py', 1, 4)], '购物篮调用目录查询时报错，查询还应沿用目录规范化规则。')
    add('dev_slug_locator', 'dev', 'unknown_location',
        {'api.py': 'from formatters import make_slug\n\ndef route(title):\n    return "/post/" + make_slug(title)\n',
         'formatters.py': 'import re\n\ndef make_slug(title):\n    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip()\n',
         'unrelated.py': 'def title(text):\n    return text.title()\n'},
        [('formatters.py', '.strip()', '.strip("-")')],
        ['from api import route\nself.assertEqual(route(" Hello! "), "/post/hello")',
         'from api import route\nself.assertEqual(route("a & b"), "/post/a-b")',
         'from api import route\nself.assertEqual(route("!!!"), "/post/")'],
        [('api.py', 1, 4), ('formatters.py', 1, 4)], '文章 URL 两端出现额外连字符，定位产生错误的实现。')
    add('dev_zero_timeout', 'dev', 'regression_compatibility',
        {'options.py': 'def timeout(value=None):\n    return value or 30\n'},
        [('options.py', 'value or 30', '30 if value is None else value')],
        ['from options import timeout\nself.assertEqual(timeout(0), 0)',
         'from options import timeout\nself.assertEqual(timeout(), 30)',
         'from options import timeout\nself.assertEqual(timeout(8), 8)'],
        [('options.py', 1, 2)], '兼容禁用超时的旧配置值 0，只有 None 才采用默认值。')
    add('dev_shipping_memory', 'dev', 'memory_reuse',
        {'rules.md': '# Shipping\nAmounts use integer cents. Free shipping starts at 5000 cents inclusive.\n',
         'shipping.py': 'def charge(subtotal):\n    return 600 if subtotal <= 5000 else 0\n'},
        [('shipping.py', 'subtotal <= 5000', 'subtotal < 5000')],
        ['from shipping import charge\nself.assertEqual(charge(5000), 0)',
         'from shipping import charge\nself.assertEqual(charge(4999), 600)',
         'from shipping import charge\nself.assertEqual(charge(5001), 0)'],
        [('rules.md', 2, 2), ('shipping.py', 1, 2)], '延续前一会话的运费规则调查，修复免邮门槛。',
        {'files': {'rules.md': '# Shipping\nAmounts use integer cents. Free shipping starts at 5000 cents inclusive.\n'},
         'prompt': '调查并记住运费规则及来源；不要改文件。后续会话会继续相关任务。',
         'observation': 'Native memory capability/opportunity only; no causal memory benefit inferred from final pass'})
    add('dev_retry_policy_change', 'dev', 'stale_memory',
        {'policy.py': 'MAX_TRIES = 4\n', 'retry.py': 'from policy import MAX_TRIES\n\ndef allowed(attempt):\n    return attempt < 2\n'},
        [('retry.py', 'attempt < 2', 'attempt < MAX_TRIES')],
        ['from retry import allowed\nself.assertTrue(allowed(3))',
         'from retry import allowed\nself.assertFalse(allowed(4))',
         'from retry import allowed\nself.assertTrue(allowed(0))'],
        [('policy.py', 1, 1), ('retry.py', 1, 4)], '重试策略已更新，请核对当前版本并修复执行逻辑。',
        {'files': {'policy.py': 'MAX_TRIES = 2\n'}, 'prompt': '读取当前重试次数上限并记录带来源的记忆，不修改文件。',
         'stale_paths': ['policy.py']})
    add('test_stable_ranking', 'test', 'single_file',
        {'ranking.py': 'def best(rows, k):\n    return sorted(rows, key=lambda row: (row[1], row[0]), reverse=True)[:k]\n'},
        [('ranking.py', '(row[1], row[0])', 'row[1]')],
        ['from ranking import best\nself.assertEqual(best([("a", 2), ("z", 2)], 2), [("a", 2), ("z", 2)])',
         'from ranking import best\nself.assertEqual(best([("a", 1), ("b", 3)], 1), [("b", 3)])',
         'from ranking import best\nself.assertEqual(best([("a", 2)], 0), [])'],
        [('ranking.py', 1, 2)], '排行榜按分数降序，同分必须保持原输入顺序。')
    add('test_millisecond_contract', 'test', 'cross_file',
        {'config.py': 'def seconds(options):\n    return options.get("timeout_ms", 1000)\n',
         'transport.py': 'from config import seconds\n\ndef options_for_client(options):\n    return {"timeout_seconds": seconds(options) * 1000}\n'},
        [('config.py', 'options.get("timeout_ms", 1000)', 'options.get("timeout_ms", 1000) / 1000'),
         ('transport.py', 'seconds(options) * 1000', 'seconds(options)')],
        ['from transport import options_for_client\nself.assertEqual(options_for_client({"timeout_ms": 250}), {"timeout_seconds": 0.25})',
         'from config import seconds\nself.assertEqual(seconds({}), 1)',
         'from transport import options_for_client\nself.assertEqual(options_for_client({"timeout_ms": 0}), {"timeout_seconds": 0})'],
        [('config.py', 1, 2), ('transport.py', 1, 4)], '配置毫秒单位与客户端秒单位之间的契约错误，修复转换和调用。')
    add('test_specific_route_locator', 'test', 'unknown_location',
        {'gateway.py': 'from matcher import choose\n\ndef dispatch(path, routes):\n    return choose(path, routes)\n',
         'matcher.py': 'def choose(path, routes):\n    return routes.get("*", routes.get(path))\n',
         'health.py': 'def healthy():\n    return True\n'},
        [('matcher.py', 'routes.get("*", routes.get(path))', 'routes[path] if path in routes else routes.get("*")')],
        ['from gateway import dispatch\nself.assertEqual(dispatch("/a", {"*": "fallback", "/a": "exact"}), "exact")',
         'from gateway import dispatch\nself.assertEqual(dispatch("/missing", {"*": "fallback"}), "fallback")',
         'from gateway import dispatch\nself.assertIsNone(dispatch("/a", {"*": "fallback", "/a": None}))'],
        [('gateway.py', 1, 4), ('matcher.py', 1, 2)], '路由系统错误地优先选择通配符，需要定位并保留精确路由的合法空值。')
    add('test_legacy_boolean', 'test', 'regression_compatibility',
        {'flags.py': 'def enabled(value):\n    if isinstance(value, bool):\n        return value\n    return value.lower() == "true"\n'},
        [('flags.py', 'value.lower() == "true"', 'value.strip().casefold() in {"true", "1", "yes"}')],
        ['from flags import enabled\nself.assertTrue(enabled(" YES "))\nself.assertTrue(enabled("1"))',
         'from flags import enabled\nself.assertTrue(enabled(True))\nself.assertFalse(enabled(False))',
         'from flags import enabled\nself.assertFalse(enabled("false"))\nself.assertTrue(enabled("TrUe"))'],
        [('flags.py', 1, 4)], '配置升级后应继续接受旧字符串 yes/1 和布尔值，并兼容两端空白。')
    add('test_rounding_memory', 'test', 'memory_reuse',
        {'policy.md': '# Invoice\nRound decimal halves away from zero, using Decimal and ROUND_HALF_UP.\n',
         'invoice.py': 'from decimal import Decimal, ROUND_HALF_EVEN\n\ndef cents(text):\n    return int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))\n'},
        [('invoice.py', 'from decimal import Decimal, ROUND_HALF_EVEN', 'from decimal import Decimal, ROUND_HALF_UP'),
         ('invoice.py', 'rounding=ROUND_HALF_EVEN', 'rounding=ROUND_HALF_UP')],
        ['from invoice import cents\nself.assertEqual(cents("1.005"), 101)',
         'from invoice import cents\nself.assertEqual(cents("-1.005"), -101)',
         'from invoice import cents\nself.assertEqual(cents("1.004"), 100)'],
        [('policy.md', 2, 2), ('invoice.py', 1, 4)], '在新会话延续发票舍入策略调查，修复金额计算。',
        {'files': {'policy.md': '# Invoice\nRound decimal halves away from zero, using Decimal and ROUND_HALF_UP.\n'},
         'prompt': '调查发票金额舍入约定并记忆来源，不修改文件。',
         'observation': 'Source remains readable; final correctness alone cannot prove memory use'})
    add('test_quota_policy_change', 'test', 'stale_memory',
        {'limits.py': 'BATCH_LIMIT = 8\n', 'batch.py': 'from limits import BATCH_LIMIT\n\ndef batches(items):\n    return [items[i:i+3] for i in range(0, len(items), 3)]\n'},
        [('batch.py', 'items[i:i+3]', 'items[i:i+BATCH_LIMIT]'),
         ('batch.py', 'len(items), 3', 'len(items), BATCH_LIMIT')],
        ['from batch import batches\nself.assertEqual(list(map(len, batches(list(range(9))))), [8, 1])',
         'from batch import batches\nself.assertEqual(batches([]), [])',
         'from batch import batches\nself.assertEqual(batches([1, 2]), [[1, 2]])'],
        [('limits.py', 1, 1), ('batch.py', 1, 4)], '批次上限已变更，核对当前限制并修复数据分批，不依赖旧记忆常量。',
        {'files': {'limits.py': 'BATCH_LIMIT = 3\n'},
         'prompt': '调查并记住当前批次上限及其来源，不修改文件。', 'stale_paths': ['limits.py']})
    return result, references


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tasks, references = build_cases()
    write_json(output/'tasks.json', tasks, exclusive=True)
    write_json(output/'controller-only-reference.json', references, exclusive=True)
    write_json(output/'asset-lock.json', {'at': utcnow(), 'status': 'asset_lock_not_formal_experiment_freeze',
        'unique_scenarios': len(tasks), 'origin': 'all synthetic; no upstream故障 claim',
        'dev': [t['id'] for t in tasks if t['split']=='dev'],
        'test': [t['id'] for t in tasks if t['split']=='test'],
        'builder_sha256': digest(__file__),
        'assets': {n: digest(output/n) for n in ('tasks.json', 'controller-only-reference.json')},
        'model_runs': 0, 'qualification': 'pending external AppContainer red/green self-check'}, exclusive=True)
    return tasks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    tasks = build(args.output)
    print(json.dumps({'scenarios': len(tasks), 'model_runs': 0}))
