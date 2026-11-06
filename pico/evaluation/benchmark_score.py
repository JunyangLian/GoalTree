"""Versioned task-macro repair scores, never a relative leaderboard index.

Missing planned runs are unknown, not silently dropped or assigned failures.
Retries cannot create tasks. Cost/latency cannot compensate for a wrong patch.
"""
from collections import defaultdict

VERSION = 'verified-repair-score-v1'
CATEGORIES = ('single_file', 'cross_file', 'unknown_location', 'regression_compatibility')


def score(plan, results, repetitions=1):
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError('Invalid repeat count')
    slots = {p['run_key']: p for p in plan}
    if not slots or len(slots) != len(plan):
        raise ValueError('Empty or duplicate plan slots')
    if any(p['category'] not in CATEGORIES for p in plan):
        raise ValueError('Unknown task category')
    rows = {r['run_key']: r for r in results}
    if len(rows) != len(results) or not set(rows) <= set(slots):
        raise ValueError('Duplicate or unplanned results')
    cells = defaultdict(list)
    for slot in plan:
        cells[(slot['agent'], slot['task_id'])].append(slot)
        if slot['run_key'] in rows:
            row = rows[slot['run_key']]
            if any(row[k] != slot[k] for k in ('agent', 'task_id', 'repeat')):
                raise ValueError('Result identity differs from frozen plan')
    if any(len(c) != repetitions or {p['repeat'] for p in c} != set(range(1, repetitions+1)) for c in cells.values()):
        raise ValueError('Unbalanced task repetitions')
    for agent in {p['agent'] for p in plan}:
        if {p['category'] for p in plan if p['agent'] == agent} != set(CATEGORIES):
            raise ValueError('All four frozen categories required')
    task_definitions = {}
    for slot in plan:
        if slot['task_id'] in task_definitions and task_definitions[slot['task_id']] != slot['category']:
            raise ValueError('Task category changed')
        task_definitions[slot['task_id']] = slot['category']
    if any({p['task_id'] for p in plan if p['agent'] == a} != set(task_definitions) for a in {p['agent'] for p in plan}):
        raise ValueError('Agents must receive identical frozen tasks')
    agents = {}
    for agent in sorted({p['agent'] for p in plan}):
        planned = [p for p in plan if p['agent'] == agent]
        observed = [rows[p['run_key']] for p in planned if p['run_key'] in rows]
        categories = {}
        for category in CATEGORIES:
            tasks = {p['task_id'] for p in planned if p['category'] == category}
            passed_f = passed_v = missing = 0
            stable = []
            for task in tasks:
                task_slots = cells[(agent, task)]
                verified = []
                for slot in task_slots:
                    row = rows.get(slot['run_key'])
                    if row is None:
                        missing += 1
                        verified.append(None)
                        continue
                    functional = row.get('functional_passed') is True and row.get('scope_valid') is True
                    valid = functional and row.get('workflow_passed') is True and row.get('audit_valid') is True
                    passed_f += functional
                    passed_v += valid
                    verified.append(valid)
                stable.append(all(v is True for v in verified))
            denominator = len(tasks)*repetitions
            categories[category] = {'tasks': len(tasks), 'planned_runs': denominator,
                'functional_passes': passed_f, 'verified_passes': passed_v, 'unobserved': missing,
                'functional_score': 100*passed_f/denominator if missing == 0 else None,
                'verified_score': 100*passed_v/denominator if missing == 0 else None,
                'verified_bounds': [100*passed_v/denominator, 100*(passed_v+missing)/denominator],
                'stable_tasks': sum(stable),
                'stable_score': 100*sum(stable)/len(tasks) if missing == 0 and repetitions >= 3 else None}
        complete = all(c['unobserved'] == 0 for c in categories.values())
        def total(key):
            values = [r.get(key) for r in observed]
            return sum(values) if len(values) == len(planned) and all(type(v) is int and v >= 0 for v in values) else None
        agents[agent] = {'planned_runs': len(planned), 'recorded_runs': len(observed),
            'unique_tasks': len({p['task_id'] for p in planned}), 'complete': complete,
            'verified_score': sum(c['verified_score'] for c in categories.values())/4 if complete else None,
            'functional_score': sum(c['functional_score'] for c in categories.values())/4 if complete else None,
            'verified_bounds': [sum(c['verified_bounds'][i] for c in categories.values())/4 for i in (0, 1)],
            'stable_score': sum(c['stable_score'] for c in categories.values())/4 if complete and repetitions >= 3 else None,
            'input_tokens': total('input_tokens'), 'output_tokens': total('output_tokens'),
            'tool_steps': total('tool_steps'), 'categories': categories}
    return {'version': VERSION, 'repetitions': repetitions, 'agents': agents,
        'unique_tasks': len(task_definitions), 'planned_runs': len(plan),
        'formula': '100 * mean_over_4_categories(mean_over_tasks(mean_over_frozen_repeats(verified_success)))',
        'efficiency_and_safety': 'Separate diagnostics; no efficiency bonus, no inferred attack-defense score',
        'uncertainty': 'Fixed known regression set; repeats do not establish unseen-task generalization'}
