"""Evidence-bound memory and text-skill evolution. No model or shell execution.

Host observations are immutable receipts. Models propose candidates; a host
evidence gate manages scoped activation. A successful independent task is evidence of
transfer, not proof that a recalled procedure caused the success.
"""
import hashlib
import json
import math
import uuid

from .knowledge import KnowledgeStore


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


class EvolutionStore:
    def __init__(self, workspace, knowledge=None):
        self.knowledge = knowledge or KnowledgeStore(workspace)
        self.clock = self.knowledge.clock
        with self.knowledge.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS learning_sources (
                    id TEXT PRIMARY KEY, identity TEXT UNIQUE, body TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_items (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, fact_key TEXT NOT NULL,
                    scope TEXT NOT NULL, goal_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                    sources TEXT NOT NULL, bases TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL, reviewer TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS learning_usage (
                    id TEXT PRIMARY KEY, item_id TEXT NOT NULL, version INTEGER NOT NULL,
                    run_id TEXT NOT NULL, session_id TEXT NOT NULL, goal_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, mode TEXT NOT NULL, outcome TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '', UNIQUE(item_id,version,run_id,mode));
                CREATE TABLE IF NOT EXISTS learning_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    item_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    previous_sha256 TEXT NOT NULL, sha256 TEXT NOT NULL);
            ''')

    @staticmethod
    def text(value, limit=2000):
        KnowledgeStore.safe_text(value)
        if len(value) > limit:
            raise ValueError('Learning text exceeds budget')
        return value.strip()

    def event(self, db, item_id, kind, body):
        previous = db.execute('SELECT sha256 FROM learning_events ORDER BY seq DESC LIMIT 1').fetchone()
        record = {'at': self.clock(), 'item_id': item_id, 'kind': kind,
                  'body': body, 'previous_sha256': previous[0] if previous else ''}
        db.execute('INSERT INTO learning_events(at,item_id,kind,body,previous_sha256,sha256) VALUES (?,?,?,?,?,?)',
                   (record['at'], item_id, kind, encoded(body), record['previous_sha256'], digest(record)))

    def observe(self, identity, *, category, session_id, goal_id='', task_id='', run_id='',
                outcome='unverified', observation='', evidence_paths=(), detail=None):
        """Host-only receipt API. Never registered as a model tool."""
        if category not in {'run', 'acceptance', 'feedback'} or outcome not in {'passed', 'failed', 'unverified', 'corrected'}:
            raise ValueError('Invalid host observation')
        body = {'category': category, 'session_id': session_id, 'goal_id': goal_id,
                'task_id': task_id, 'run_id': run_id, 'outcome': outcome,
                'observation': self.text(observation), 'detail': detail or {},
                'evidence': [self.knowledge.evidence(p) for p in evidence_paths]}
        self.text(encoded(body), 6000)
        with self.knowledge.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM learning_sources WHERE identity=?', (identity,)).fetchone()
            if old:
                if old['body'] != encoded(body):
                    raise ValueError('Host receipt identity conflict')
                return self._source(old)
            key = 'ls_' + uuid.uuid4().hex
            db.execute('INSERT INTO learning_sources VALUES (?,?,?,?)', (key, identity, encoded(body), digest(body)))
            self.event(db, key, 'observed', {'sha256': digest(body)})
        return self.source(key)

    @staticmethod
    def _source(row):
        return {'id': row['id'], 'sha256': row['sha256'], **json.loads(row['body'])}

    def source(self, key):
        with self.knowledge.connect() as db:
            row = db.execute('SELECT * FROM learning_sources WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('Unknown host learning source')
        if digest(json.loads(row['body'])) != row['sha256']:
            raise ValueError('Host learning source integrity failure')
        return self._source(row)

    @staticmethod
    def visible(item, goal_id='', session_id=''):
        return (item['scope'] == 'workspace' or
                item['scope'] == 'goal' and bool(goal_id) and item['goal_id'] == goal_id or
                item['scope'] == 'session' and bool(session_id) and item['session_id'] == session_id)

    def sources(self, goal_id='', session_id='', limit=12):
        with self.knowledge.connect() as db:
            rows = db.execute('SELECT * FROM learning_sources ORDER BY rowid DESC LIMIT 1000').fetchall()
        return [self._source(r) for r in rows if
                (goal_id and json.loads(r['body'])['goal_id'] == goal_id) or
                (session_id and json.loads(r['body'])['session_id'] == session_id)][:limit]

    def payload(self, kind, body):
        if not isinstance(body, dict):
            raise ValueError('Learning payload must be an object')
        if kind == 'memory' and set(body) == {'text', 'conditions'}:
            return {k: self.text(body[k], 2000) for k in body}
        if kind == 'skill' and set(body) == {'description', 'when', 'steps', 'acceptance'}:
            if not isinstance(body['steps'], list) or not 1 <= len(body['steps']) <= 8:
                raise ValueError('Text skill requires 1..8 steps')
            result = {k: self.text(body[k], 1200) for k in ('description', 'when', 'acceptance')}
            result['steps'] = [self.text(s, 600) for s in body['steps']]
            self.text(encoded(result), 6000)
            return result
        raise ValueError('Unknown learning kind or payload fields; executable skills are not supported')

    @staticmethod
    def decode(row):
        result = dict(row)
        for key in ('body', 'sources', 'bases'):
            result[key] = json.loads(result[key])
        return result

    def get(self, key):
        with self.knowledge.connect() as db:
            row = db.execute('SELECT * FROM learning_items WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('Unknown learning item')
        return self.decode(row)

    def propose(self, kind, key, body, source_ids, *, scope='goal', goal_id='', session_id='',
                ttl_seconds=30 * 86400, replaces=()):
        key = self.text(key, 120)
        body = self.payload(kind, body)
        if scope not in {'workspace', 'goal', 'session'} or scope == 'goal' and not goal_id or scope == 'session' and not session_id:
            raise ValueError('Explicit learning scope required')
        if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 8 or any(not isinstance(s, str) for s in source_ids):
            raise ValueError('Candidates require 1..8 real host receipt IDs')
        if not isinstance(replaces, (list, tuple)) or len(replaces) > 8:
            raise ValueError('Bounded predecessor IDs required')
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (float, int)) or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 365 * 86400:
            raise ValueError('TTL must be finite and between 0 and 365 days')
        receipts = [self.source(s) for s in dict.fromkeys(source_ids)]
        if scope != 'workspace' and any(r['goal_id'] != goal_id and r['session_id'] != session_id for r in receipts):
            raise ValueError('Learning source belongs to a different context')
        refs = [{'id': r['id'], 'sha256': r['sha256']} for r in receipts]
        if not self.fresh({'sources': refs, 'expires': self.clock() + ttl_seconds}):
            raise ValueError('Learning source is already stale')
        goal_id = goal_id if scope == 'goal' else ''
        session_id = session_id if scope == 'session' else ''
        with self.knowledge.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM learning_items WHERE kind=? AND scope=? AND goal_id=? AND session_id=?',
                              (kind, scope, goal_id, session_id)).fetchall()
            bases = {}
            duplicate = None
            for row in rows:
                if row['status'] == 'candidate' and row['fact_key'] == key and row['body'] == encoded(body) and row['sources'] == encoded(refs):
                    duplicate = row
                if row['status'] == 'active' and (row['fact_key'] == key or row['id'] in replaces):
                    bases[row['id']] = row['version']
            if any(s not in bases for s in replaces):
                raise ValueError('Merge predecessors must be active items in the same scope and kind')
            if duplicate is not None and json.loads(duplicate['bases']) == bases:
                return self.decode(duplicate)
            item_id = 'learn_' + uuid.uuid4().hex
            db.execute('INSERT INTO learning_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (item_id, kind, key, scope, goal_id, session_id, 'candidate', 1,
                        encoded(body), encoded(refs), encoded(bases), self.clock(), self.clock() + ttl_seconds, ''))
            self.event(db, item_id, 'proposed', {'sources': refs, 'bases': bases, 'body_sha256': digest(body)})
        from .privacy import CURRENT,PrivacyStore
        privacy=PrivacyStore(self.knowledge.root)
        if privacy.enabled:
            sources=['file:'+e['path'] for r in receipts for e in r['evidence']]
            sources += ['goal:'+r['goal_id'] for r in receipts if r.get('goal_id')]
            sources += list(CURRENT.get().resources) if CURRENT.get() else []
            privacy.derive('memory:'+item_id,sources)
        return self.get(item_id)

    def fresh(self, item, path_filter=None):
        if item['expires'] <= self.clock():
            return False
        for ref in item['sources']:
            try:
                receipt = self.source(ref['id'])
            except ValueError:
                return False
            if receipt['sha256'] != ref['sha256']:
                return False
            detail = receipt['detail']
            if detail.get('task_spec_version') and (self.knowledge.root / '.pico/goaltree.sqlite3').exists():
                from .goals import GoalStore
                state = GoalStore(self.knowledge.root).read()
                task = state['tasks'].get(receipt['task_id'])
                goal = state['goals'].get(receipt['goal_id'])
                if (not task or not goal or task['spec_version'] != detail['task_spec_version'] or
                        goal['spec_version'] != detail.get('goal_spec_version')):
                    return False
            for e in receipt['evidence']:
                if path_filter and not path_filter(e['path']):
                    return False
                try:
                    if self.knowledge.evidence(e['path']) != e:
                        return False
                except (ValueError, OSError):
                    return False
        return True

    def list(self, goal_id='', session_id='', *, path_filter=None, limit=50):
        with self.knowledge.connect() as db:
            rows = db.execute('SELECT * FROM learning_items ORDER BY created DESC LIMIT 1000').fetchall()
        result = []
        for row in rows:
            item = self.decode(row)
            if not self.visible(item, goal_id, session_id):
                continue
            if item['status'] in {'active', 'candidate'} and not self.fresh(item):
                status = 'expired' if item['expires'] <= self.clock() else 'stale'
                try:
                    self.transition(item['id'], item['version'], status, 'host_freshness_check')
                except ValueError:
                    pass
                item = self.get(item['id'])
            # Revoked paths must not leak historical learning content either.
            if path_filter:
                try:
                    if any(not path_filter(e['path']) for ref in item['sources'] for e in self.source(ref['id'])['evidence']):
                        continue
                except ValueError:
                    continue
            result.append(item)
        return result[:limit]

    def transition(self, key, version, status, reason):
        if status not in {'retracted', 'stale', 'expired'}:
            raise ValueError('Invalid learning transition')
        reason = self.text(reason, 600)
        with self.knowledge.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE learning_items SET status=?,version=version+1 WHERE id=? AND version=? AND status IN ('candidate','active')",
                                 (status, key, version)).rowcount
            if changed != 1:
                raise ValueError('Learning version/status conflict')
            self.event(db, key, status, {'version': version + 1, 'reason': reason})
        return self.get(key)

    def usages(self, key):
        with self.knowledge.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM learning_usage WHERE item_id=? ORDER BY rowid', (key,))]

    def transfer_passed(self, item, path_filter=None):
        seeds = [self.source(s['id']) for s in item['sources']]
        for usage in self.usages(item['id']):
            if usage['mode'] != 'trial' or usage['outcome'] != 'passed' or not usage['source_id']:
                continue
            receipt = self.source(usage['source_id'])
            if not self.fresh({'sources': [{'id': receipt['id'], 'sha256': receipt['sha256']}],
                               'expires': float('inf')}, path_filter):
                continue
            # Different runs of the same task do not constitute transfer.
            task = receipt['task_id']
            if task and all(task != s['task_id'] for s in seeds):
                return True
        return False

    def activate(self, key, version, *, reviewer, rollback=False, path_filter=None):
        reviewer = self.text(reviewer, 120)
        with self.knowledge.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM learning_items WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('Unknown learning item')
            item = self.decode(row)
            expected = 'superseded' if rollback else 'candidate'
            if item['version'] != version or item['status'] != expected or rollback and not item['reviewer']:
                raise ValueError('Learning version/status conflict')
            if not self.fresh(item, path_filter):
                raise ValueError('Learning sources are stale or expired')
            if item['kind'] == 'skill' and not self.transfer_passed(item, path_filter):
                raise ValueError('Skill needs a passed evidence-bound trial on a different task')
            heads = db.execute("SELECT * FROM learning_items WHERE kind=? AND scope=? AND goal_id=? AND session_id=? AND status='active'",
                               (item['kind'], item['scope'], item['goal_id'], item['session_id'])).fetchall()
            affected = {r['id']: r['version'] for r in heads if r['fact_key'] == item['fact_key'] or r['id'] in item['bases']}
            if not rollback and affected != item['bases']:
                raise ValueError('Learning predecessor changed; rebase the candidate')
            for old_id, old_version in affected.items():
                db.execute("UPDATE learning_items SET status='superseded',version=version+1 WHERE id=?", (old_id,))
                self.event(db, old_id, 'superseded', {'by': key, 'version': old_version + 1})
            db.execute("UPDATE learning_items SET status='active',version=version+1,reviewer=? WHERE id=?", (reviewer, key))
            self.event(db, key, 'rolled_back' if rollback else 'activated', {'version': version + 1, 'reviewer': reviewer})
        return self.get(key)

    def expose(self, key, version, *, run_id, session_id, goal_id='', task_id='', trial=False):
        item = self.get(key)
        if item['version'] != version or item['status'] != ('candidate' if trial else 'active'):
            raise ValueError('Learning version/status conflict')
        if not self.visible(item, goal_id, session_id) or not self.fresh(item):
            raise ValueError('Learning is outside scope or stale')
        if not run_id:
            raise ValueError('Learning exposure requires a real run ID')
        with self.knowledge.connect() as db:
            mode = 'trial' if trial else 'skill_loaded' if item['kind'] == 'skill' else 'presented'
            usage_id = 'lu_' + uuid.uuid4().hex
            changed = db.execute('INSERT OR IGNORE INTO learning_usage VALUES (?,?,?,?,?,?,?,?,?,?)',
                                 (usage_id, key, version, run_id, session_id, goal_id, task_id, mode, 'unverified', '')).rowcount
            if changed:
                self.event(db, key, mode, {'run_id': run_id, 'version': version, 'task_id': task_id})
        return item

    def settle(self, receipt):
        """Associate host outcomes, never an Agent's success assertion, with exposures."""
        if receipt['category'] not in {'run', 'acceptance'}:
            return
        with self.knowledge.connect() as db:
            rows = db.execute('SELECT * FROM learning_usage WHERE run_id=? AND session_id=?',
                              (receipt['run_id'], receipt['session_id'])).fetchall()
            for row in rows:
                # An unverified run must not overwrite a later operator acceptance.
                if receipt['outcome'] == 'unverified' and row['outcome'] != 'unverified':
                    continue
                db.execute('UPDATE learning_usage SET outcome=?,source_id=? WHERE id=?',
                           (receipt['outcome'], receipt['id'], row['id']))
                self.event(db, row['item_id'], 'outcome_observed', {'usage_id': row['id'], 'source_id': receipt['id'], 'outcome': receipt['outcome']})

    def search(self, query, goal_id='', session_id='', *, path_filter=None, limit=3, retriever=None):
        if retriever is not None:
            # Scope, status, freshness and authorization precede *all* ranking
            # and optional remote embedding. Never rank then filter private data.
            active=[item for item in self.list(goal_id,session_id,path_filter=path_filter,limit=1000)
                    if item['status']=='active']
            records=[{'id':item['id'],'text':item['fact_key']+'\n'+encoded(item['body']),
                      'learning_item':item} for item in active]
            # Enabling code embeddings does not authorize uploading private
            # learned preferences or Goal content to an embedding provider.
            ranked=retriever.rank(records,query,limit,allow_dense=False) if records else []
            self.last_search_audit={**(retriever.last_audit if records else {'mode':'lexical','candidates':0,'selected_ids':[]}),
                                    'collection':'active_learning','scope_filter':'before_ranking'}
            return [row['learning_item'] for row in ranked]
        from .features.memory import _tokenize
        tokens = _tokenize(query)
        rows = []
        for item in self.list(goal_id, session_id, path_filter=path_filter, limit=1000):
            if item['status'] != 'active':
                continue
            overlap = len(tokens & _tokenize(item['fact_key'] + ' ' + encoded(item['body'])))
            if overlap:
                rows.append((overlap, item['created'], item))
        return [r[2] for r in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True)[:limit]]

    def automatic(self, goal_id='', session_id='', *, path_filter=None):
        """Advance learning at run boundaries; no model or tool permissions.

        Direct user feedback can inform advisory preferences. Arbitrary prose
        from a run cannot approve itself. Skills need an independent trial.
        Two distinct externally failed tasks disable suspect guidance; API
        outages, cancellations and unverified answers do not count against it.
        """
        changes = []
        rows = self.list(goal_id, session_id, path_filter=path_filter, limit=1000)
        for item in rows:
            if item['scope'] == 'workspace':
                continue  # Automatic learning never expands Goal/session scope.
            if item['status'] in {'active', 'candidate'}:
                if item['status'] == 'active' and item['kind'] == 'memory' and all(
                        self.source(ref['id'])['category'] == 'feedback' for ref in item['sources']):
                    # A failed code test does not revoke the user's preference.
                    # Only a later correction/source invalidation may replace it.
                    continue
                failed_tasks = set()
                for use in self.usages(item['id']):
                    if use['version'] != item['version'] or use['outcome'] != 'failed' or not use['source_id']:
                        continue
                    source = self.source(use['source_id'])
                    if source['task_id'] and source['detail'].get('verification', {}).get('status') == 'failed':
                        failed_tasks.add(source['task_id'])
                if len(failed_tasks) >= 2:
                    self.transition(item['id'], item['version'], 'retracted',
                                    'automatic: guidance associated with failed verification on two distinct tasks; no causal claim')
                    changes.append({'id': item['id'], 'action': 'disabled_after_failures'})
                    # Restore only a recorded predecessor in this same scope,
                    # still fresh and already tested if it is a skill.
                    for predecessor in item['bases']:
                        old = self.get(predecessor)
                        if old['status'] != 'superseded' or not self.fresh(old, path_filter):
                            continue
                        try:
                            restored = self.activate(old['id'], old['version'], reviewer='automatic_regression_recovery',
                                                     rollback=True, path_filter=path_filter)
                        except ValueError:
                            continue
                        changes.append({'id': restored['id'], 'action': 'restored_predecessor'})
                        break
                    continue
                if item['status'] == 'active':
                    continue
            if item['status'] != 'candidate' or not self.fresh(item, path_filter):
                continue
            sources = [self.source(ref['id']) for ref in item['sources']]
            direct_feedback = all(s['category'] == 'feedback' for s in sources)
            mechanical_failure = (len(sources) == 1 and item['body'] == sources[0]['detail'].get('failure_memory'))
            eligible = (item['kind'] == 'memory' and (direct_feedback or mechanical_failure)) or self.transfer_passed(item, path_filter)
            if not eligible:
                continue
            # Identical content from several traces is not another learned skill.
            duplicate = next((old for old in self.list(goal_id, session_id, path_filter=path_filter, limit=1000)
                              if old['status'] == 'active' and old['kind'] == item['kind'] and old['scope'] == item['scope']
                              and old['goal_id'] == item['goal_id'] and old['session_id'] == item['session_id']
                              and old['body'] == item['body']), None)
            if duplicate:
                self.transition(item['id'], item['version'], 'retracted', 'automatic: duplicate of ' + duplicate['id'])
                changes.append({'id': item['id'], 'action': 'duplicate_consolidated'})
                continue
            try:
                active = self.activate(item['id'], item['version'], reviewer='automatic_evidence_gate', path_filter=path_filter)
            except ValueError:
                # A competing revision must be explicitly rebased by the model,
                # never silently used to replace the current memory.
                continue
            changes.append({'id': active['id'], 'action': 'activated'})
        return changes

    def choose_trial(self, query, goal_id='', session_id='', task_id='', *, path_filter=None):
        """One relevant candidate, only as advisory context in an authorized task."""
        from .features.memory import _tokenize
        tokens = _tokenize(query)
        candidates = []
        for item in self.list(goal_id, session_id, path_filter=path_filter, limit=1000):
            if item['status'] != 'candidate' or item['scope'] == 'workspace':
                continue
            sources = [self.source(ref['id']) for ref in item['sources']]
            if task_id and any(s['task_id'] == task_id for s in sources):
                continue
            overlap = len(tokens & _tokenize(item['fact_key'] + ' ' + encoded(item['body'])))
            if overlap:
                candidates.append((overlap, item['created'], item))
        return max(candidates, key=lambda r: (r[0], r[1]))[2] if candidates else None

    def audit(self):
        with self.knowledge.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM learning_events ORDER BY seq')]
        previous = ''
        for row in rows:
            record = {k: row[k] for k in ('at', 'item_id', 'kind', 'previous_sha256')}
            record['body'] = json.loads(row['body'])
            if row['previous_sha256'] != previous or digest(record) != row['sha256']:
                return {'valid': False, 'events': len(rows), 'failed_seq': row['seq']}
            previous = row['sha256']
        with self.knowledge.connect() as db:
            for row in db.execute('SELECT body,sha256 FROM learning_sources'):
                if digest(json.loads(row['body'])) != row['sha256']:
                    return {'valid': False, 'events': len(rows), 'reason': 'source_content_mismatch'}
            proposals = {r['item_id']: json.loads(r['body']) for r in rows if r['kind'] == 'proposed'}
            for row in db.execute('SELECT id,body FROM learning_items'):
                if digest(json.loads(row['body'])) != proposals.get(row['id'], {}).get('body_sha256'):
                    return {'valid': False, 'events': len(rows), 'reason': 'candidate_content_mismatch'}
        return {'valid': True, 'events': len(rows), 'head_sha256': previous,
                'limit': 'Local hash-chain consistency; not external tamper-proof storage'}
