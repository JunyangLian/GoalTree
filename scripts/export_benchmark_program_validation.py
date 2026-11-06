"""Package offline evidence without upstream gold patches, references or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.repair_suite import digest, load_json, write_json, utcnow


def export(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT/'artifacts'
    hf = artifacts/'processbench-hf-draft-v2-2026-11-06'
    qualification = load_json(artifacts/'processbench-v0.1-qualification-2026-11-06/summary.json')
    assert qualification['qualified'] == qualification['scenarios'] == 12
    report_files = ['benchmark-program-full-2026-11-06.xml',
                    'benchmark-program-final-regression-v2-2026-11-06.xml']
    suites = []
    covered = set()
    for name in report_files:
        tree = ET.parse(artifacts/name)
        cases = tree.findall('.//testcase')
        assert cases and not tree.findall('.//failure') and not tree.findall('.//error')
        assert not tree.findall('.//skipped')
        covered |= {(c.get('classname'), c.get('name')) for c in cases}
        suites.append({'file': name, 'passed': len(cases), 'sha256': digest(artifacts/name)})
        shutil.copyfile(artifacts/name, output/name)
    # Independently verify every currently collected test is covered by either
    # the full run or the post-fix targeted run; no adding duplicate test counts.
    collected = set()
    for line in (artifacts/'benchmark-program-final-collected-2026-11-06.txt').read_text(encoding='utf-8').splitlines():
        if line.startswith('tests/') and '::' in line:
            path, name = line.split('::', 1)
            collected.add((path[:-3].replace('/', '.'), name))
    assert collected <= covered, 'Some final tests lack a passing execution'
    for name in ('README.md', 'LICENSE', 'manifest.json', 'protocol-draft.json', 'qualification.json',
                 'data/dev.jsonl', 'data/test.jsonl', 'annotations/evidence.json'):
        target = output/'hf-draft'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(hf/name, target)
    upstream = load_json(artifacts/'swebench-lite-selection-2026-11-06/selection-lock.json')
    public_selection = {k: upstream[k] for k in ('dataset', 'revision', 'source_sha256',
        'harness_commit', 'selection_seed', 'status', 'population', 'assets')}
    public_selection['selected'] = [{k: r[k] for k in ('instance_id', 'repo', 'base_commit')} for r in upstream['selected']]
    write_json(output/'public-selection.json', public_selection, exclusive=True)
    observer = load_json(artifacts/'processbench-observer-probe-v3-2026-11-06/summary.json')
    transitions = load_json(artifacts/'processbench-scenario-probe-2026-11-06/summary.json')
    write_json(output/'observer-probe.json', observer, exclusive=True)
    write_json(output/'scenario-probe.json', transitions, exclusive=True)
    write_json(output/'qualification.json', qualification, exclusive=True)
    history = {}
    for name, expected in {
        'validation/cross-agent-deepseek-pilot-2026-11-05/evidence.zip': '9ee564b9dd073fccb3a5c39e90bf3d3e0a4333bc18ce1f167e09bbc0cd1e5a46',
        'benchmarks/results/final-agent-90-2026-10-25/evidence.zip': '2fc5e42cf764504137d4243b509171bcba46d3df6a254896e827ed42487d1c76',
    }.items():
        actual = digest(ROOT/name)
        assert actual == expected
        history[name] = actual
    write_json(output/'historical-evidence-check.json', history, exclusive=True)
    sources = [ROOT/'pico/providers/clients.py', ROOT/'tests/test_benchmark_program.py',
        *[ROOT/'pico/evaluation'/n for n in ('public_benchmark.py', 'process_trace.py',
                                          'process_observer.py', 'process_scenario.py')],
        *[ROOT/'scripts'/n for n in ('prepare_public_benchmark.py', 'build_processbench_assets.py',
            'validate_processbench.py', 'export_processbench_dataset.py', 'bigmodel_benchmark_adapter.py',
            'check_process_observers.py', 'check_process_scenarios.py', 'export_benchmark_program_validation.py')]]
    write_json(output/'source-hashes.json', {p.relative_to(ROOT).as_posix(): digest(p) for p in sources}, exclusive=True)
    summary = {'at': utcnow(), 'status': 'offline_foundation_complete; formal_model_control_and_environment_pending',
        'paid_api_calls': 0, 'new_agent_benchmark_runs': 0, 'new_agent_scores': None,
        'scenarios': 12, 'dev': 6, 'test': 6, 'all_synthetic': True,
        'red_green_verifications': 24, 'test_methods_per_version': 36,
        'full_tests_and_postfix_regression': suites, 'distinct_passing_tests': len(covered),
        'final_collected_tests': len(collected), 'all_current_tests_have_passing_execution': collected <= covered,
        'swebench': '5 candidate IDs locked; official Docker qualification not started',
        'hf': 'local draft only; unpublished',
        'retained_failures': ['initial observer probe missing required ask_approval argument',
            'initial regression command referenced nonexistent test_streaming.py; 0 tests ran'],
        'fixed_defects': ['required upstream ToolContext approval callback',
            'BigModel v4 endpoint incorrectly received a v1 suffix'],
        'pending': ['Linux Docker + image/empty/gold patch qualification',
            'independent evidence-label review', 'complete multi-session two-Agent model controller integration',
            'standard API + spending ceiling + live preflight + formal freeze + model runs',
            'HF publication quality review and upload']}
    write_json(output/'summary.json', summary, exclusive=True)
    # Only the self-contained HF-compatible draft enters the downloadable ZIP.
    with zipfile.ZipFile(output/'hf-draft.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for p in sorted((output/'hf-draft').rglob('*')):
            if p.is_file():
                archive.write(p, p.relative_to(output/'hf-draft').as_posix())
    patterns = [r'\bsk-[A-Za-z0-9]{20,}\b', r'\b[0-9a-fA-F]{32}\.[A-Za-z0-9]{16}\b']
    for p in output.rglob('*'):
        if p.is_file() and p.suffix != '.zip':
            text = p.read_text(encoding='utf-8')
            assert not any(re.search(pattern, text) for pattern in patterns), 'Credential-shaped data'
    write_json(output/'manifest.json', {'files': {p.relative_to(output).as_posix(): digest(p)
        for p in sorted(output.rglob('*')) if p.is_file()}, 'reference_repairs_exported': False,
        'upstream_gold_patches_exported': False, 'credential_shape_scan': 'passed'}, exclusive=True)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    export(a.output)
