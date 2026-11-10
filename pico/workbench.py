"""Small complete workbench profile; the frozen classic profile stays available."""
import hashlib
import json

from .changes import ChangeStore
from .extensions import PolicyHooks, StdioMCP, load_skill, skill_catalog
from .evidence import update_plan
from .instructions import discover_rules, render_rules
from .retrieval import RepositoryIndex, SiliconFlowEmbedding


SPECS = {
    'code_search': ({'query': 'str', 'limit': 'int=5'}, False, 'Search allowed source using symbols, BM25 and configured real embeddings.'),
    'symbol_info': ({'name': 'str'}, False, 'Locate Python definitions and statically observed calls; dynamic relationships are unknown.'),
    'update_plan': ({'steps': 'str'}, False, 'Persist a JSON array of steps with id,title,acceptance,status,depends_on,evidence. Completed steps require evidence IDs.'),
    'recall_evidence': ({'evidence_id': 'str', 'start': 'int=1', 'end': 'int=60'}, False, 'Read this session\'s redacted original tool evidence by ID and line range. Historical evidence may be stale.'),
    'find_evidence': ({'query': 'str', 'limit': 'int=5'}, False, 'Find this session\'s historical redacted tool evidence IDs after context compaction. Results are not current verification.'),
    'memory_propose': ({'text': 'str', 'key': 'str', 'evidence_paths': "str='[]'", 'category': "str='fact'"}, False, 'Save a candidate fact/preference/workflow/experience. Only host operator can confirm prose; never grants permission.'),
    'apply_changes': ({'changes': 'str'}, True, 'Apply a JSON array of {path,expected_sha256,content}; preflight all files, preserve existing dirty work. null hash means new file. Normal approval required.'),
    'rewind_changes': ({'change_id': 'str'}, True, 'Rewind own applied change set only if no later edits conflict. Normal approval required.'),
    'load_skill': ({'name': 'str'}, False, 'Load a repository skill on demand as untrusted text; no scripts or permissions are granted.'),
    'mcp_docs': ({'query': 'str'}, True, 'Call bundled read-only documentation MCP over stdio after normal approval. No arbitrary server installation.'),
}


class Capabilities:
    def __init__(self, agent, embedding_mode='off'):
        self.agent = agent
        if embedding_mode not in {'off', 'siliconflow'}:
            raise ValueError('Embedding mode must be off or siliconflow')
        self.hooks = PolicyHooks(agent.root)
        self.policy_at_start = self.hooks.fingerprint()
        self.index = RepositoryIndex(agent.root, SiliconFlowEmbedding(network_route=agent.network_route) if embedding_mode == 'siliconflow' else None,
                                     path_filter=self.allowed_path)
        self.changes = ChangeStore(agent)
        self.rules = []
        self.refresh_rules(agent.workspace.cwd)
        self.skills = [s for s in skill_catalog(agent.root) if self.allowed_path(s['path'])]

    def refresh_rules(self, path):
        rules = discover_rules(self.agent.root, path, path_filter=self.allowed_path)
        if rules != self.rules:
            self.rules = rules
            self.agent.session['rule_sources'] = [{k: r[k] for k in ('path', 'scope', 'sha256')} for r in rules]
        return render_rules(rules)

    def allowed_path(self, raw):
        try:
            path = self.agent.path(str(raw))
            return self.hooks.allows_path(path)
        except (ValueError, OSError):
            return False

    def registry(self):
        return {**{name: {'schema': schema, 'risky': risky, 'description': description,
                       'run': lambda args, name=name: self.run(name, args)}
                for name, (schema, risky, description) in SPECS.items()}, **self.goal_registry()}

    def goal_registry(self):
        tools = {}
        if getattr(self.agent, 'goal_planner', None):
            tools['plan_tasks'] = {'schema': {'steps': 'str'}, 'risky': False,
            'description': 'Persist 1..20 GoalTree tasks as a JSON array of id,title,description,acceptance,parent_id,depends_on. '
                           'Optional verification_mode is configured (default) or manual (requires operator acceptance, never self-completion). '
                           'Use temporary ids to link new tasks. Existing unexecuted planned IDs may be updated. '
                           'For multi-stage planning use stage nodes and child tasks linked by parent_id, '
                           'with concrete descriptions and acceptance conditions. Research/documents use manual verification. '
                           'Never delete history or declare completed. This replaces no existing tasks implicitly.',
            'run': lambda args: self.run('plan_tasks', args)}
        if getattr(self.agent, 'goal_action', None):
            tools['goal_action'] = {'schema': {'proposal': 'str'}, 'risky': False,
                'description': 'Maintain the current goal tree using a JSON object. Actions: create_goal (new conversation only), '
                               'create_task, update_goal, update_task, reopen_task, archive_task, record_progress, set_status (planned/blocked/cancelled). '
                               'proposal is a JSON-encoded string. create_goal fields: action, title:string (1..160 chars), '
                               'description:string (max 6000), constraints:string (max 4000), acceptance:string (max 2000). '
                               'constraints must be a single text string, NEVER an array or object; use newlines for multiple constraints. '
                               'create_task additionally requires title and may use parent_id:string, depends_on:array of task IDs, '
                               'verification_mode:manual/configured. update_goal uses goal_id and revision; '
                               'update_task uses task_id and revision. Only send fields for the requested action. '
                               'record_progress uses task_id, revision, status:active/review, note (max1200). '
                               'review requires output (actual deliverable text,max20000) OR evidence_id from this session, never both. '
                               'This records conversation work without rerunning it; it is NOT acceptance. '
                               'For newly learned facts update_goal context_notes; this preserves task contracts. '
                               'Changing goal description/constraints/acceptance invalidates task contracts; use only for actual scope changes. '
                               'Updates require the observed revision. No acceptance or permission changes. '
                               'Do not create goals for ordinary questions. Never touch another goal.',
                'run': lambda args: self.run('goal_action', args)}
            tools['run_task'] = {'schema': {'task_id': 'str', 'instruction': "str='Execute this task against its acceptance conditions.'"}, 'risky': False, 'observes_effects': True,
                'description': 'Execute an existing task of this goal in its isolated session, then return actual verification and evidence. '
                               'Shares the current turn tool budget. File/Shell effects still require their original approvals. '
                               'Dependencies must have current acceptance. Do not claim success without inspecting the result.',
                'run': lambda args: self.run('run_task', args)}
            tools['confirm_task'] = {'schema': {'task_id': 'str', 'reason': 'str','include_children':'int=0'}, 'risky': True,
                'description': 'Request operator approval to accept a manually reviewed task with explicit supporting evidence. '
                               'include_children=1 requests ONE approval for a stage plus its listed eligible descendants; '
                               'all leaf tasks must already be submitted for review or currently accepted. '
                               'Approval is required; never invoke just to make the tree look complete.',
                'run': lambda args: self.run('confirm_task', args)}
        return tools

    def validate(self, name, args):
        schema = self.registry()[name]['schema']
        if not isinstance(args, dict) or set(args) - set(schema):
            raise ValueError('Unknown workbench tool argument')
        for key, declaration in schema.items():
            if key not in args:
                if '=' not in declaration:
                    raise ValueError('Missing argument: ' + key)
                continue
            value = args[key]
            if declaration.startswith('int'):
                if type(value) is not int:
                    raise ValueError('Integer argument required')
            elif not isinstance(value, str) or not value.strip() or len(value) > 60000:
                raise ValueError('Bounded non-empty string argument required')
        if name == 'apply_changes':
            self.changes.prepare(json.loads(args['changes']))

    def legacy_edit(self, name, args):
        path = self.agent.path(args['path'])
        before = path.read_bytes() if path.exists() else None
        if name == 'write_file':
            after = args['content']
        else:
            text = before.decode('utf-8').replace('\r\n', '\n')
            if text.count(args['old_text']) != 1:
                raise ValueError('Patch base changed after approval; no edit applied')
            after = text.replace(args['old_text'], args['new_text'], 1)
        result = self.changes.apply([{'path': args['path'], 'expected_sha256': hashlib.sha256(before).hexdigest() if before is not None else None,
                                     'content': after}])
        return json.dumps(result, ensure_ascii=False)

    def run(self, name, args):
        agent = self.agent
        self.last_sources = []
        if name == 'plan_tasks':
            result = agent.goal_planner(json.loads(args['steps']))
        elif name == 'goal_action':
            result = agent.goal_action(json.loads(args['proposal']))
        elif name == 'run_task':
            result = agent.goal_runner(args['task_id'], args.get('instruction', 'Execute this task against its acceptance conditions.'))
        elif name == 'confirm_task':
            result = agent.goal_confirmation(args['task_id'], args['reason'],args.get('include_children',0))
        elif name == 'code_search':
            result = self.index.search(args['query'], args.get('limit', 5))
            self.last_sources = sorted({r['path'] for r in result})
            agent.evidence.event(agent.session['id'], getattr(agent.current_task_state, 'run_id', ''),
                                 'retrieval_ranked', self.index.last_audit)
            result = {'matches': result, 'audit': self.index.last_audit}
        elif name == 'symbol_info':
            result = {'matches': self.index.symbols(args['name']), 'scope': 'Python AST names/calls; not a complete call graph'}
            self.last_sources = sorted({r['path'] for r in result['matches']})
        elif name == 'update_plan':
            result = update_plan(agent, json.loads(args['steps']))
        elif name == 'recall_evidence':
            row = agent.evidence.get(args['evidence_id'], agent.session['id'])
            start, end = args.get('start', 1), args.get('end', 60)
            if start < 1 or end < start or end-start > 120:
                raise ValueError('Evidence range must be 1..121 lines')
            lines = row['content'].splitlines()
            result = {'evidence_id': row['id'], 'historical': True, 'source': row['metadata'],
                      'total_lines': len(lines), 'lines': lines[start-1:end]}
            source_path = row['metadata'].get('args', {}).get('path')
            if source_path and row['metadata'].get('source_sha256'):
                try:
                    current = hashlib.sha256(agent.path(source_path).read_bytes()).hexdigest()
                    result['source_current'] = current == row['metadata']['source_sha256']
                except (ValueError, OSError):
                    result['source_current'] = False
        elif name == 'find_evidence':
            result = agent.evidence.find(agent.session['id'], args['query'], args.get('limit', 5))
        elif name == 'memory_propose':
            if not agent.feature_enabled('memory'):
                raise ValueError('Memory is disabled in this run')
            category = args.get('category', 'fact')
            if category not in {'fact', 'preference', 'workflow', 'experience'}:
                raise ValueError('Invalid memory category')
            paths = json.loads(args.get('evidence_paths', '[]'))
            if not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(p, str) for p in paths):
                raise ValueError('Evidence paths must be an array of <=8 paths')
            if any(not self.allowed_path(path) for path in paths):
                raise ValueError('Memory evidence source is not authorized')
            scope = {'scope': 'session', 'session_id': agent.session['id']} if getattr(agent, 'goal_binding', None) else {}
            result = agent.memory.knowledge.propose(args['text'], category + ':' + args['key'], paths, **scope)
        elif name == 'apply_changes':
            result = self.changes.apply(json.loads(args['changes']))
        elif name == 'rewind_changes':
            result = self.changes.rewind(args['change_id'])
        elif name == 'load_skill':
            match = next((s for s in self.skills if s['name'] == args['name']), None)
            if match is None or not self.allowed_path(match['path']):
                raise ValueError('Skill source is not authorized')
            self.last_sources = [match['path']]
            return load_skill(agent, args['name'])
        elif name == 'mcp_docs':
            paths = [agent.root/'README.md'] + sorted((agent.root/'docs').glob('*.md'))[:30]
            allowed = [p.relative_to(agent.root).as_posix() for p in paths if self.allowed_path(p)]
            self.last_sources = allowed
            result = StdioMCP(agent.root, agent.shell_env(), agent.cancellation, allowed_paths=allowed).call('search_docs', {'query': args['query']})
        else:
            raise ValueError('Unknown workbench tool')
        return json.dumps(result, ensure_ascii=False)

    def experience(self, task_state):
        """Historical mechanical execution fact, not a model-generated claim."""
        agent = self.agent
        verification = agent.last_verification
        if verification.get('status') != 'passed' or not verification.get('evidence_id'):
            return None
        paths = sorted(set(agent.session.get('working_state', {}).get('changed_files', [])))[:8]
        if not paths:
            return None
        record = {'type': 'verified_execution_history', 'run': task_state.run_id,
                  'command': verification['command'], 'exit_code': 0, 'paths': paths,
                  'evidence_id': verification['evidence_id'], 'snapshot_sha256': verification.get('snapshot_sha256'),
                  'scope': 'This test passed for this snapshot; revalidate after changes. Not a repair recipe.'}
        scope = {'scope': 'session', 'session_id': agent.session['id']} if getattr(agent, 'goal_binding', None) else {}
        candidate = agent.memory.knowledge.propose(json.dumps(record, ensure_ascii=False),
            'experience:' + hashlib.sha256(json.dumps(paths).encode()).hexdigest()[:16], paths, **scope)
        # Only the deterministic host observation above is confirmed here. No
        # arbitrary model text uses this path, and general prose stays candidate.
        activated = agent.memory.knowledge.activate(candidate['id'], candidate['version'], reviewer='host_verified_execution')
        agent.evidence.link(record['evidence_id'], activated['id'], 'supports_execution_history')
        for evidence in activated['evidence']:
            agent.evidence.link(evidence['path'] + '@' + evidence['sha256'], activated['id'], 'supports_memory')
        return activated
