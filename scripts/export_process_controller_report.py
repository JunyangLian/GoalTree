"""Publish scoped offline mechanism evidence; keep full raw archives local."""
import argparse
import csv
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.public_benchmark import docker_preflight
from pico.evaluation.repair_suite import digest, load_json, write_json, utcnow


def export(output):
    artifacts = ROOT/'artifacts'
    batch = artifacts/'process-controller-v3-2026-11-06'
    audit_dir = artifacts/'process-controller-independent-audit-v3-2026-11-06'
    audited = load_json(audit_dir/'audit.json')
    assert audited['independently_verified'] == audited['scenarios'] == 8
    assert audited['original_records_unchanged'] and audited['api_calls'] == 0
    for name, sha in load_json(audit_dir/'original-record-hashes.json').items():
        assert digest(batch/name) == sha, name
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tests = []
    coverage = set()
    for name in ('process-controller-full-2026-11-06.xml', 'process-controller-final-targeted-2026-11-06.xml'):
        tree = ET.parse(artifacts/name)
        cases = tree.findall('.//testcase')
        assert cases and not any(tree.findall('.//'+tag) for tag in ('failure', 'error', 'skipped'))
        coverage |= {(c.get('classname'), c.get('name')) for c in cases}
        tests.append({'file': name, 'passed': len(cases), 'sha256': digest(artifacts/name)})
        shutil.copyfile(artifacts/name, output/name)
    collected = set()
    for line in (artifacts/'process-controller-final-collected-2026-11-06.txt').read_text(encoding='utf-8').splitlines():
        if line.startswith('tests/') and '::' in line:
            path, name = line.split('::', 1)
            collected.add((path[:-3].replace('/', '.'), name))
    assert collected <= coverage, 'Some currently collected tests have no passing execution'
    metrics = {}
    for agent in ('codetrail', 'apecode'):
        rows = [r for r in audited['rows'] if r['agent'] == agent]
        phases = [p for r in rows for p in r['phases']]
        repair = [p for p in phases if p['phase'] == 'b']
        def pooled(key):
            values = [p['evidence'][key] for p in repair if p['evidence'][key] is not None]
            n, d = sum(x['numerator'] for x in values), sum(x['denominator'] for x in values)
            return {'numerator': n, 'denominator': d, 'value': n/d if d else None}
        metrics[agent] = {'scenario_runs': len(rows), 'sessions': len(phases),
            'mechanism_functional_passes': sum(r['functional_passed'] for r in rows),
            'mechanism_workflow_passes': sum(r['workflow_passed'] for r in rows),
            'external_reverification_passes': sum(r['second_external_verification']['accepted'] for r in rows),
            'tool_steps': sum(p['tool_steps'] for p in phases),
            'scripted_model_requests': sum(p['model_requests'] for p in phases),
            'policy_denials': sum(p['policy_denials'] for p in phases),
            'injected_patch_failures': sum(len(p['patch_failures']) for p in phases),
            'scripted_patch_recoveries': sum(x['later_same_file_corrected'] for p in phases for x in p['patch_failures']),
            'repair_phase_evidence': {k: pooled(k) for k in ('recall_at_5', 'necessary_evidence_coverage',
                'immediate_context_loss', 'repeated_reads')},
            'tokens': None, 'cost_cny': None, 'memory_adoption_effectiveness': None,
            'native_persistent_memory_supported': agent == 'codetrail', 'overall_score': None}
    history = []
    for name, note in [('process-controller-2026-11-06', 'pre-normalization metadata; hard deadline field was misnamed'),
                       ('process-controller-v2-2026-11-06', 'normalized patch status; read-only phase still had repair verification instruction'),
                       ('process-controller-v3-2026-11-06', 'final adapter includes read-only prompt fix and safe external overlay')]:
        directory = artifacts/name
        history.append({'batch': name, 'freeze_sha256': digest(directory/'freeze.json'),
            'runs': len(load_json(directory/'runs.json')), 'summary_sha256': digest(directory/'summary.json'),
            'note': note, 'model_benchmark_runs': 0})
    # Store full, unchanged synthetic raw traces locally, separate from public metrics.
    raw = artifacts/'process-controller-raw-evidence-2026-11-06.zip'
    with zipfile.ZipFile(raw, 'x', zipfile.ZIP_DEFLATED) as archive:
        for cohort in history:
            directory = artifacts/cohort['batch']
            for path in sorted(directory.rglob('*')):
                if path.is_file(): archive.write(path, cohort['batch']+'/'+path.relative_to(directory).as_posix())
        for path in sorted(audit_dir.rglob('*')):
            if path.is_file(): archive.write(path, 'independent-audit/'+path.relative_to(audit_dir).as_posix())
        for name in ('LICENSE', 'UPSTREAM_NOTICE.md'):
            if (ROOT/name).exists(): archive.write(ROOT/name, 'legal/'+name)
    shutil.copyfile(audit_dir/'audit.json', output/'audit.json')
    (output/'runs.csv').write_text((batch/'runs.csv').read_text(encoding='utf-8'),
                                   encoding='utf-8', newline='\n')
    write_json(output/'process-metrics.json', metrics, exclusive=True)
    write_json(output/'history.json', history, exclusive=True)
    write_json(output/'protocol.json', load_json(batch/'protocol.json'), exclusive=True)
    write_json(output/'source-freeze.json', load_json(batch/'freeze.json'), exclusive=True)
    shutil.copyfile(batch/'source.zip', output/'source.zip')
    # This source archive is unchanged; provide original licensing alongside it.
    for name in ('LICENSE', 'UPSTREAM_NOTICE.md'):
        if (ROOT/name).exists(): shutil.copyfile(ROOT/name, output/name)
    with (output/'process-metrics.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, ['agent', 'scenario_runs', 'sessions', 'tool_steps',
            'scripted_model_requests', 'policy_denials', 'injected_patch_failures', 'scripted_patch_recoveries'],
            extrasaction='ignore', lineterminator='\n')
        writer.writeheader(); writer.writerows({'agent': a, **m} for a, m in metrics.items())
    summary = {'at': utcnow(), 'scope': 'offline scripted dual-Agent controller integration, not model benchmark',
        'final_batch': history[-1], 'scenario_runs': 8, 'sessions': 12,
        'independent_external_passes': 8, 'tests': tests, 'distinct_passing_tests': len(coverage),
        'final_collected_tests': len(collected), 'all_current_tests_covered': collected <= coverage,
        'docker': docker_preflight(), 'docker_actual_executions': 0, 'api_calls': 0,
        'new_benchmark_tasks': 0, 'new_model_benchmark_runs': 0,
        'raw_evidence_local': raw.relative_to(ROOT).as_posix(), 'raw_evidence_sha256': digest(raw),
        'old_batches_preserved': True, 'overall_score': None,
        'pending': ['actual Docker kernel/SWE-bench image qualification',
                    'standard API live preflight and monetary-budget guarded dataset scheduling',
                    'independent process evidence-gold review', 'public benchmark model runs and HF release']}
    write_json(output/'summary.json', summary, exclusive=True)
    private = load_json(ROOT/'validation/benchmark-program-offline-2026-11-06-v2/historical-evidence-check.json')
    for name, sha in private.items(): assert digest(ROOT/name) == sha
    write_json(output/'historical-evidence-check.json', private, exclusive=True)
    for path in output.rglob('*'):
        if path.is_file() and path.suffix != '.zip':
            text = path.read_text(encoding='utf-8')
            assert not re.search(r'\b[0-9a-fA-F]{32}\.[A-Za-z0-9]{16}\b|\bsk-[A-Za-z0-9]{20,}\b', text)
    write_json(output/'manifest.json', {'files': {p.relative_to(output).as_posix(): digest(p)
        for p in sorted(output.rglob('*')) if p.is_file()}, 'raw_traces_kept_locally': True,
        'credential_shape_scan': 'passed'}, exclusive=True)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    export(a.output)
