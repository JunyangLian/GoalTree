"""Build the frozen small-model benchmark assets (buggy code + tests).

The suite is intentionally small and dependency-free: each task is a single
module with a deliberate bug and a unittest file that pins the expected
behaviour. Verification runs the tests in a pristine copy, so editing the test
file in the agent workspace cannot turn a failure into a pass.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmarks" / "small_model_v1"

TASKS = [
    {
        "id": "clamp_value",
        "cohort": "dev",
        "module": "clamp_value.py",
        "buggy": (
            "def clamp(value, low, high):\n"
            "    if low > high:\n"
            "        raise ValueError('low must be <= high')\n"
            "    if value < low:\n"
            "        return high\n"
            "    if value > high:\n"
            "        return high\n"
            "    return value\n"
        ),
        "fixed": (
            "def clamp(value, low, high):\n"
            "    if low > high:\n"
            "        raise ValueError('low must be <= high')\n"
            "    if value < low:\n"
            "        return low\n"
            "    if value > high:\n"
            "        return high\n"
            "    return value\n"
        ),
        "tests": (
            "import unittest\n"
            "from clamp_value import clamp\n\n"
            "class ClampTests(unittest.TestCase):\n"
            "    def test_below(self):\n"
            "        self.assertEqual(clamp(-3, 0, 10), 0)\n"
            "    def test_above(self):\n"
            "        self.assertEqual(clamp(12, 0, 10), 10)\n"
            "    def test_inside(self):\n"
            "        self.assertEqual(clamp(5, 0, 10), 5)\n"
            "    def test_bad_bounds(self):\n"
            "        with self.assertRaises(ValueError):\n"
            "            clamp(1, 5, 2)\n"
        ),
        "prompt": (
            "The function clamp in clamp_value.py returns the wrong bound. "
            "Fix clamp_value.py so clamping a value below `low` returns `low`, "
            "and keep the existing validation. Run the tests to confirm."
        ),
    },
    {
        "id": "slugify_title",
        "cohort": "dev",
        "module": "slugify_title.py",
        "buggy": (
            "import re\n\n"
            "def slugify(text):\n"
            "    text = text.lower()\n"
            "    text = re.sub(r'[^a-z0-9]+', '-', text)\n"
            "    return text\n"
        ),
        "fixed": (
            "import re\n\n"
            "def slugify(text):\n"
            "    text = text.lower()\n"
            "    text = re.sub(r'[^a-z0-9]+', '-', text)\n"
            "    return text.strip('-')\n"
        ),
        "tests": (
            "import unittest\n"
            "from slugify_title import slugify\n\n"
            "class SlugifyTests(unittest.TestCase):\n"
            "    def test_words(self):\n"
            "        self.assertEqual(slugify('Hello World'), 'hello-world')\n"
            "    def test_padding(self):\n"
            "        self.assertEqual(slugify('  A  B  '), 'a-b')\n"
            "    def test_symbols(self):\n"
            "        self.assertEqual(slugify('C++ & Python!'), 'c-python')\n"
            "    def test_already_slug(self):\n"
            "        self.assertEqual(slugify('already-slug'), 'already-slug')\n"
        ),
        "prompt": (
            "slugify in slugify_title.py leaves leading and trailing dashes. "
            "Fix it so surrounding punctuation or spaces do not produce a dash "
            "at either end, and run the tests."
        ),
    },
    {
        "id": "parse_env_line",
        "cohort": "dev",
        "module": "parse_env_line.py",
        "buggy": (
            "def parse_env_line(line):\n"
            "    if not line or line.lstrip().startswith('#'):\n"
            "        return None\n"
            "    key, value = line.split('=')\n"
            "    return key.strip(), value.strip()\n"
        ),
        "fixed": (
            "def parse_env_line(line):\n"
            "    if not line or line.lstrip().startswith('#'):\n"
            "        return None\n"
            "    if '=' not in line:\n"
            "        return None\n"
            "    key, value = line.split('=', 1)\n"
            "    value = value.strip()\n"
            "    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\\\"\\'':\n"
            "        value = value[1:-1]\n"
            "    return key.strip(), value\n"
        ),
        "tests": (
            "import unittest\n"
            "from parse_env_line import parse_env_line\n\n"
            "class EnvLineTests(unittest.TestCase):\n"
            "    def test_plain(self):\n"
            "        self.assertEqual(parse_env_line('A=1'), ('A', '1'))\n"
            "    def test_spaces_and_quotes(self):\n"
            "        self.assertEqual(parse_env_line('B = \"x y\"'), ('B', 'x y'))\n"
            "    def test_comment_and_blank(self):\n"
            "        self.assertIsNone(parse_env_line('  # note'))\n"
            "        self.assertIsNone(parse_env_line(''))\n"
            "    def test_empty_value(self):\n"
            "        self.assertEqual(parse_env_line('EMPTY='), ('EMPTY', ''))\n"
            "    def test_no_equals(self):\n"
            "        self.assertIsNone(parse_env_line('NO_EQUALS'))\n"
            "    def test_value_with_equals(self):\n"
            "        self.assertEqual(parse_env_line('TOKEN=a=b'), ('TOKEN', 'a=b'))\n"
        ),
        "prompt": (
            "parse_env_line in parse_env_line.py crashes on lines without '=' and "
            "keeps surrounding quotes. Fix it: return None when there is no '=', "
            "split only on the first '=', and strip one matching pair of quotes. "
            "Run the tests."
        ),
    },
    {
        "id": "dedupe_keep_order",
        "cohort": "dev",
        "module": "dedupe_keep_order.py",
        "buggy": (
            "def dedupe(items):\n"
            "    return list(set(items))\n"
        ),
        "fixed": (
            "def dedupe(items):\n"
            "    seen = set()\n"
            "    result = []\n"
            "    for item in items:\n"
            "        if item not in seen:\n"
            "            seen.add(item)\n"
            "            result.append(item)\n"
            "    return result\n"
        ),
        "tests": (
            "import unittest\n"
            "from dedupe_keep_order import dedupe\n\n"
            "class DedupeTests(unittest.TestCase):\n"
            "    def test_order(self):\n"
            "        self.assertEqual(dedupe([3, 1, 3, 2, 1]), [3, 1, 2])\n"
            "    def test_strings(self):\n"
            "        self.assertEqual(dedupe(['b', 'a', 'b']), ['b', 'a'])\n"
            "    def test_empty(self):\n"
            "        self.assertEqual(dedupe([]), [])\n"
        ),
        "prompt": (
            "dedupe in dedupe_keep_order.py loses the original order. Fix it so "
            "the first occurrence of each item is kept in order, then run the tests."
        ),
    },
    {
        "id": "chunk_list",
        "cohort": "dev",
        "module": "chunk_list.py",
        "buggy": (
            "def chunk(items, size):\n"
            "    if size <= 0:\n"
            "        raise ValueError('size must be positive')\n"
            "    return [items[i:i + size] for i in range(0, len(items) - size, size)]\n"
        ),
        "fixed": (
            "def chunk(items, size):\n"
            "    if size <= 0:\n"
            "        raise ValueError('size must be positive')\n"
            "    return [items[i:i + size] for i in range(0, len(items), size)]\n"
        ),
        "tests": (
            "import unittest\n"
            "from chunk_list import chunk\n\n"
            "class ChunkTests(unittest.TestCase):\n"
            "    def test_tail_included(self):\n"
            "        self.assertEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])\n"
            "    def test_size_larger_than_list(self):\n"
            "        self.assertEqual(chunk([1, 2], 5), [[1, 2]])\n"
            "    def test_invalid_size(self):\n"
            "        with self.assertRaises(ValueError):\n"
            "            chunk([1], 0)\n"
        ),
        "prompt": (
            "chunk in chunk_list.py drops the last partial chunk. Fix the range so "
            "every item is returned exactly once, then run the tests."
        ),
    },
    {
        "id": "merge_counts",
        "cohort": "holdout",
        "module": "merge_counts.py",
        "buggy": (
            "def merge_counts(left, right):\n"
            "    result = dict(left)\n"
            "    result.update(right)\n"
            "    return result\n"
        ),
        "fixed": (
            "def merge_counts(left, right):\n"
            "    result = dict(left)\n"
            "    for key, value in right.items():\n"
            "        result[key] = result.get(key, 0) + value\n"
            "    return result\n"
        ),
        "tests": (
            "import unittest\n"
            "from merge_counts import merge_counts\n\n"
            "class MergeTests(unittest.TestCase):\n"
            "    def test_overlap_sums(self):\n"
            "        self.assertEqual(merge_counts({'a': 2}, {'a': 3, 'b': 1}), {'a': 5, 'b': 1})\n"
            "    def test_empty(self):\n"
            "        self.assertEqual(merge_counts({}, {'x': -1}), {'x': -1})\n"
            "    def test_no_overlap(self):\n"
            "        self.assertEqual(merge_counts({'a': 1}, {'b': 2}), {'a': 1, 'b': 2})\n"
        ),
        "prompt": (
            "merge_counts in merge_counts.py overwrites shared keys instead of "
            "adding them. Fix it to sum counts for keys present in both inputs, "
            "then run the tests."
        ),
    },
    {
        "id": "format_bytes",
        "cohort": "holdout",
        "module": "format_bytes.py",
        "buggy": (
            "def format_bytes(size):\n"
            "    return f'{size / 1000:.1f} KB'\n"
        ),
        "fixed": (
            "def format_bytes(size):\n"
            "    if size < 1024:\n"
            "        return f'{size} B'\n"
            "    for unit in ('KB', 'MB', 'GB'):\n"
            "        size /= 1024\n"
            "        if size < 1024 or unit == 'GB':\n"
            "            return f'{size:.1f} {unit}'\n"
        ),
        "tests": (
            "import unittest\n"
            "from format_bytes import format_bytes\n\n"
            "class FormatTests(unittest.TestCase):\n"
            "    def test_bytes(self):\n"
            "        self.assertEqual(format_bytes(0), '0 B')\n"
            "        self.assertEqual(format_bytes(512), '512 B')\n"
            "    def test_kilobytes(self):\n"
            "        self.assertEqual(format_bytes(1024), '1.0 KB')\n"
            "        self.assertEqual(format_bytes(1536), '1.5 KB')\n"
            "    def test_megabytes(self):\n"
            "        self.assertEqual(format_bytes(1048576), '1.0 MB')\n"
        ),
        "prompt": (
            "format_bytes in format_bytes.py always prints kilobytes using base "
            "1000. Fix it: bytes below 1024 print as '<n> B'; otherwise use 1024-based "
            "KB/MB/GB with one decimal. Run the tests."
        ),
    },
    {
        "id": "top_k",
        "cohort": "holdout",
        "module": "top_k.py",
        "buggy": (
            "def top_k(values, k):\n"
            "    return sorted(values)[:k]\n"
        ),
        "fixed": (
            "def top_k(values, k):\n"
            "    return sorted(values, reverse=True)[:k]\n"
        ),
        "tests": (
            "import unittest\n"
            "from top_k import top_k\n\n"
            "class TopKTests(unittest.TestCase):\n"
            "    def test_descending(self):\n"
            "        self.assertEqual(top_k([3, 1, 2], 2), [3, 2])\n"
            "    def test_k_larger_than_input(self):\n"
            "        self.assertEqual(top_k([1], 5), [1])\n"
            "    def test_zero(self):\n"
            "        self.assertEqual(top_k([1, 2], 0), [])\n"
            "    def test_duplicates(self):\n"
            "        self.assertEqual(top_k([2, 2, 1], 2), [2, 2])\n"
        ),
        "prompt": (
            "top_k in top_k.py returns the smallest values. Fix it to return the "
            "k largest values in descending order, then run the tests."
        ),
    },
]

PROTOCOL = {
    "schema_version": 1,
    "suite": "small_model_v1",
    "verify_command": "python -B -m unittest discover -v",
    "counts": {"total": len(TASKS), "dev": sum(t["cohort"] == "dev" for t in TASKS),
               "holdout": sum(t["cohort"] == "holdout" for t in TASKS)},
    "policy": ("Prompts and tests are frozen. Dev tasks may guide harness work; holdout tasks "
               "are measured only after the harness is frozen. Editing tests in the workspace "
               "cannot pass the pristine external verifier."),
}


def main() -> int:
    SUITE.mkdir(parents=True, exist_ok=True)
    tasks = []
    references = {}
    for item in TASKS:
        files = {item["module"]: item["buggy"],
                 "test_" + item["module"]: item["tests"]}
        tasks.append({
            "id": item["id"],
            "cohort": item["cohort"],
            "category": "single_file_repair",
            "files": files,
            "prompt": item["prompt"],
            "editable_files": [item["module"]],
            "expected_tests": item["tests"].count("    def test_"),
        })
        references[item["id"]] = {item["module"]: item["fixed"]}
    (SUITE / "tasks.json").write_text(
        json.dumps({"schema_version": 1, "tasks": tasks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (SUITE / "reference_solutions.json").write_text(
        json.dumps(references, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (SUITE / "protocol.json").write_text(
        json.dumps(PROTOCOL, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print(json.dumps({"tasks": len(tasks), "suite": str(SUITE)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
