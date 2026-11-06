"""Export a local Hugging Face-compatible DRAFT; no upload, no reference repairs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.repair_suite import digest, load_json, write_json
from scripts.bigmodel_benchmark_adapter import CONFIG

CARD = '''---
pretty_name: CodeTrail ProcessBench v0.1 Draft
language:
- en
- zh
- code
license: mit
task_categories:
- text-generation
tags:
- code
- agents
- evaluation
- synthetic
configs:
- config_name: default
  data_files:
  - split: dev
    path: data/dev.jsonl
  - split: test
    path: data/test.jsonl
---
# CodeTrail ProcessBench v0.1 — Draft

This is a **local unpublished draft**, not a validated leaderboard or an online
evaluation service. It contains 12 new small synthetic scenarios, 6 dev and 6
test; all are controller-authored, not real upstream faults. Four repair types,
memory reuse opportunities and stale-memory transitions are covered in each
split. Test labels are public; no model-pretraining or permanent unseen claim.

## Intended use and limitations

Compare coding harnesses with pinned models, budgets and permissions. Task,
repeat, scenario and session counts must be reported separately. Multi-session
scenarios preserve only their own memory. Final test passing alone does not
prove memory use. Required-evidence labels are proxies awaiting independent
human review; alternate correct implementations may use different evidence.
Small examples do not establish whole-repository repair competence.

## Loading

`load_dataset("json", data_files={"dev":"data/dev.jsonl", "test":"data/test.jsonl"})`

Only `files` and `prompt` for the current phase may be installed/sent to agents.
Never paste an entire dataset row into the model: metadata and acceptance scope
belong to the controller. `annotations/` is evaluator-only. Reference repairs
are excluded from this export. Before model runs, pin this export and all
controller, environment, model, permissions and metric revisions separately.

## Validation and results

`qualification.json` is OS-isolated faulty/reference asset self-check evidence,
not Agent success or a memory-effect score. No paid model run is included.
No Agent ranking, total score or synthetic missing metric is published here.
The package is blocked from automatic publication while human gold review and
at least two-Agent controlled evaluation are pending.

## License and provenance

These new synthetic fixture files, tests and annotations are released under
MIT (see LICENSE). No SWE-bench or third-party repository payload is bundled.
Public benchmark sources retain their own licensing and are a separate track.
'''

LICENSE = '''MIT License

Copyright (c) 2026 CodeTrail contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''


def export(assets, qualification, destination):
    assets, qualification, destination = map(Path, (assets, qualification, destination))
    lock = load_json(assets/'asset-lock.json')
    for name, value in lock['assets'].items():
        if digest(assets/name) != value:
            raise ValueError('Changed process assets')
    checked = load_json(qualification/'summary.json')
    if checked['asset_lock_sha256'] != digest(assets/'asset-lock.json'):
        raise ValueError('Qualification is from another asset version')
    if checked['qualified'] != checked['scenarios']:
        raise ValueError('Asset qualification failed')
    tasks = load_json(assets/'tasks.json')
    destination.mkdir(parents=True, exist_ok=False)
    (destination/'data').mkdir()
    for split in ('dev', 'test'):
        with (destination/'data'/f'{split}.jsonl').open('x', encoding='utf-8', newline='\n') as stream:
            for task in tasks:
                if task['split'] == split:
                    stream.write(json.dumps({k: task[k] for k in ('id', 'root_fault_id', 'split', 'category',
                        'origin', 'development_exposure', 'files', 'sha256', 'editable_files',
                        'expected_tests', 'prompt', 'phase_a', 'phase_b_intervention')}, ensure_ascii=False)+'\n')
    write_json(destination/'annotations/evidence.json', {t['id']: t['gold'] for t in tasks}, exclusive=True)
    write_json(destination/'qualification.json', checked, exclusive=True)
    write_json(destination/'protocol-draft.json', {'status': 'draft_not_formal_freeze',
        'agents': ['codetrail', 'apecode'], 'test_scenarios': 6, 'repetitions': 2,
        'scenario_runs': 24, 'model_sessions': 32, 'concurrency': 1,
        'model': CONFIG, 'max_tool_calls_per_session': 20,
        'session_deadline_seconds': 300, 'verification_command': 'python -B -m unittest discover -v',
        'shell_backend': 'windows_appcontainer', 'host_fallback': False,
        'reference_repairs_in_agent_workspace': False,
        'test_files_read_only': True, 'test_set_model_runs': 0,
        'process_metrics': 'exact attributed versioned read_file spans; search/paraphrase/adoption unassessed',
        'memory_comparison': 'native capability track; unsupported memory disclosed, not silently emulated',
        'still_required_before_model_execution': ['independent evidence review',
            'end-to-end multi-session model controller integration and freeze',
            'standard API credential + explicit cost ceiling + live protocol preflight'],
        'not_started': 'No task/model calls in this export'}, exclusive=True)
    (destination/'README.md').write_text(CARD, encoding='utf-8', newline='\n')
    (destination/'LICENSE').write_text(LICENSE, encoding='utf-8', newline='\n')
    files = {p.relative_to(destination).as_posix(): digest(p) for p in sorted(destination.rglob('*')) if p.is_file()}
    write_json(destination/'manifest.json', {'schema_version': 1, 'status': 'unpublished_draft',
        'files': files, 'model_runs': 0, 'publication_ready': False,
        'missing': ['independent human evidence-gold review', 'frozen two-Agent model evaluation']}, exclusive=True)
    return files


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assets', type=Path, required=True)
    p.add_argument('--qualification', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps({'exported': export(a.assets, a.qualification, a.output)}))
