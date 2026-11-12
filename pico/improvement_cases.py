"""Authored public reproductions, never derived by rewriting private user text."""
import hashlib
import json

CASES = {
    'feedback-minimization-v1': {
        'category':'memory',
        'description':'Ordinary feedback is copied as a source observation into a separate learning database. Existing secret-pattern redaction does not remove arbitrary private research text.',
        'reproduction':'In a temporary workspace, send a synthetic sentence containing the marker PUBLIC_SYNTHETIC_CANARY, with a fake final response. Inspect learning_sources only. The marker must not be copied into the learning source observation; normal conversation history has a separate retention policy.',
        'source_excerpt':'observation = agent.redact_text(query[:1200]); store.observe(..., category="feedback", observation=observation)',
        'requirements':['Keep host receipt IDs for semantic extraction in the already authorized current model request.',
            'Keep explicit safe presentation preferences working and opt-out respected.',
            'Do not send private source observations to the Improver; do not silently erase historical audit data.'],
        'targets':['pico/learning_runtime.py','pico/flywheel.py'],
    },
    'unknown-usage-v1': {
        'category':'context', 'description':'Missing model token usage must remain unknown in process metrics.',
        'reproduction':'Emit a model response with absent usage, followed by a response with measured input/output tokens. Aggregate known amounts and count unknown requests separately.',
        'source_excerpt':'Some compatible providers omit usage; an empty metadata dict is valid observation of missing usage, not zero consumption.',
        'requirements':['Never infer missing tokens as zero.','Exclude prompts, tool arguments, paths and raw error messages from metrics.'],
        'targets':['pico/flywheel.py'],
    },
    'bounded-recovery-v1': {
        'category':'recovery','description':'Recovery should retain failed attempts and never expand a denied tool permission.',
        'reproduction':'Use a fake read-only tool failure, then a valid request; count the failure and recovery separately. A rejected write remains rejected.',
        'source_excerpt':'Recovery is an observed failure followed by success of the same tool type, not proof of causal improvement.',
        'requirements':['No hidden retry after an uncertain write.','No change to tool permissions or frozen acceptance tests.'],
        'targets':['pico/agent_loop.py','pico/workbench.py','pico/flywheel.py'],
    },
}


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def public_packet(case_id):
    if case_id not in CASES:raise ValueError('Choose a shipped, frozen public case')
    case=json.loads(encoded(CASES[case_id]))
    return {'schema':1,'provenance':'GoalTree-authored public synthetic reproduction; no private logs',
            'case_id':case_id,'case_sha256':digest(case),'case':case,
            'constraints':['Proposal only. No tool execution, automatic patch application or release.',
                'Never weaken ACL, sandbox, privacy, tool approvals or frozen acceptance.',
                'The external verifier and operator, not the model, decide whether changes are accepted.']}
