"""Wire/tool observation hooks shared by CodeTrail and ApeCode adapters.

The caller supplies external tool-boundary observations and the actual outbound
request, after any context compaction. This observer has no tool permissions and
never executes model-supplied instructions.
"""
import json
from pathlib import Path

from .process_trace import evidence_metrics, normalize_context, normalize_read
from .repair_suite import append_json, write_json


class ProcessObserver:
    def __init__(self, agent, directory, gold):
        if agent not in {'codetrail', 'apecode'}:
            raise ValueError('Unsupported Agent adapter')
        self.agent = agent
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.gold = gold
        self.reads = []
        self.events = []
        self.request_ids = set()

    def _event(self, value):
        self.events.append(value)
        append_json(self.directory/'events.jsonl', value)
        return value

    def tool_result(self, call_id, name, args, output, *, source_text=None, changed=False):
        if name == 'read_file' and source_text is not None:
            if any(r['call_id'] == call_id for r in self.reads):
                raise ValueError('Duplicate tool ID within session')
            read = normalize_read(self.agent, call_id, args, output, source_text)
            self.reads.append(read)
            self._event(read)
        if changed:
            self._event({'kind': 'edit', 'changed': True, 'call_id': call_id, 'name': name})

    def outbound_request(self, request_id, payload):
        if request_id in self.request_ids:
            raise ValueError('Duplicate request ID within session')
        self.request_ids.add(request_id)
        # Only actual messages, no reasoning or Authorization exported here.
        self._event(normalize_context(request_id, payload['messages'], self.reads))

    def finish(self):
        metrics = evidence_metrics(self.events, self.gold)
        metrics['scope'] += '; successful numbered read_file only; search/paraphrase/memory adoption unassessed'
        write_json(self.directory/'metrics.json', metrics, exclusive=True)
        return metrics
