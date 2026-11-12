"""Operator-only learning review and audit; not registered as an Agent tool."""
import argparse
import json
from pathlib import Path

from .evolution import EvolutionStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--goal', default='')
    parser.add_argument('--session', default='operator')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    commands.add_parser('audit')
    feedback = commands.add_parser('feedback')
    feedback.add_argument('--text', required=True)
    propose = commands.add_parser('propose')
    propose.add_argument('--file', type=Path, required=True, help='JSON with kind,key,body,source_ids and optional replaces')
    propose.add_argument('--scope', choices=('goal', 'session', 'workspace'), default='session')
    for name in ('activate', 'retract', 'rollback'):
        command = commands.add_parser(name)
        command.add_argument('id')
        command.add_argument('--version', type=int, required=True)
    args = parser.parse_args(argv)
    store = EvolutionStore(args.workspace)
    scope = {'goal_id': args.goal, 'session_id': args.session}
    if args.command == 'list':
        result = {'items': store.list(**scope), 'sources': store.sources(**scope)}
    elif args.command == 'audit':
        result = store.audit()
    elif args.command == 'feedback':
        import uuid
        result = store.observe('operator:' + uuid.uuid4().hex, category='feedback', outcome='corrected',
                               observation=args.text, **scope)
    elif args.command == 'propose':
        data = json.loads(args.file.read_text(encoding='utf-8'))
        result = store.propose(data['kind'], data['key'], data['body'], data['source_ids'],
                               scope=args.scope, replaces=data.get('replaces', []), **scope)
    elif args.command == 'retract':
        result = store.transition(args.id, args.version, 'retracted', 'operator_cli')
    else:
        result = store.activate(args.id, args.version, reviewer='operator_cli', rollback=args.command == 'rollback')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == 'audit' and not result['valid']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
