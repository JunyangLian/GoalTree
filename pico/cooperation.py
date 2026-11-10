"""Collaboration checkpoints are separate from irreversible-operation safety approval."""
import json


def checkpoints(settings, mode, name, args, risky, context):
    """Return explicit host-enforced checkpoints, never infer a permission grant."""
    if mode != 'assistant':
        return [('implementation_step', args)] if settings['implementation'] == 'step' and not risky else []
    if name == 'run_task':
        return [('implementation_task', args)] if settings['implementation'] != 'autonomous' else []
    if name not in {'goal_action', 'plan_tasks'}:
        return []
    level = settings['planning']
    if level == 'autonomous':
        return []
    if name == 'goal_action':
        proposal = json.loads(args['proposal'])
        critical = proposal.get('action') in {'create_goal', 'archive_task', 'reopen_task', 'set_status'}
        if proposal.get('action')=='update_goal':
            critical=bool({'description','constraints','acceptance'} & proposal.keys())
        return [('planning_step', proposal)] if level == 'step' or critical else []
    steps = json.loads(args['steps'])
    if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
        raise ValueError('规划必须是任务对象数组。')
    if level == 'step':
        return [('planning_step', {'step': i + 1, 'total': len(steps), 'task': s}) for i, s in enumerate(steps)]
    existing = context.get('existing_tasks', [])
    stages = lambda tasks: sorted(t.get('title', '') for t in tasks if not t.get('parent_id'))
    # Partial proposals are additive; compare only new/updated top-level nodes.
    by_id = {t['id']: t for t in existing}
    candidate = dict(by_id)
    for index, step in enumerate(steps):
        key = step.get('id', 'new-' + str(index))
        candidate[key] = {**by_id.get(key, {}), **step}
    return [('planning_direction', {'stages': stages(list(candidate.values())), 'proposal': steps})] if stages(existing) != stages(list(candidate.values())) else []


def instructions(settings):
    levels = {
        'step': 'Ask the user at each concrete step and wait for confirmation; do not bundle unconfirmed decisions.',
        'balanced': 'Ask the user for major direction, scope, architecture and acceptance decisions; handle routine details yourself.',
        'autonomous': 'Choose reasonable details and advance within the user objective without routine collaboration questions; report assumptions and outcomes.',
    }
    return {'settings': settings, 'planning': levels[settings['planning']],
            'implementation': levels[settings['implementation']],
            'security': 'These are collaboration preferences, NOT tool permissions. All existing safety approvals, scopes and policy hooks still apply. Never change these settings yourself. A planning request never authorizes implementation.'}
