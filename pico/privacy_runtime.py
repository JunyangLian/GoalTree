"""Runtime integration; conservative session/Goal taint is intentional in v1."""
from functools import wraps
import json

from .privacy import CURRENT, PrivacyDenied, PrivacyGuard, PrivacyStore, activate


def guard_for(agent):
    store = PrivacyStore(agent.root)
    if not store.enabled:
        if CURRENT.get():raise PrivacyDenied('unguarded_nested_workspace')
        return None
    binding = getattr(agent, 'goal_binding', {}) or {}
    resources = ['session:' + agent.session['id']]
    resources += [kind + ':' + binding[kind + '_id'] for kind in ('goal','task') if binding.get(kind + '_id')]
    guard = PrivacyGuard(store,resources)
    parent = CURRENT.get()
    if parent:
        if parent.store.root != store.root:raise PrivacyDenied('cross_workspace_delegation')
        store.derive(resources[0], parent.resources)
    guard.observe(sources=['file:' + p for p in agent.workspace.project_docs],
                  text=json.dumps(getattr(agent,'goal_context',{}),ensure_ascii=False))
    context=getattr(agent,'goal_context',{}) or {}
    sources=['task:'+row['id'] for field in ('dependencies','ancestors','existing_tasks')
             for row in context.get(field,[]) if row.get('id')]
    sources += ['file:'+p for p in agent.session.get('memory',{}).get('working_files',[]) if isinstance(p,str)]
    guard.observe(sources=sources)
    guard.observe(sources=['artifact:'+r['id'] for r in getattr(agent,'attachment_rows',[])])
    # Active scope is reconstructed from persisted lineage, not prompt text.
    # A caller cannot untaint it by compacting or starting a new model request.
    return guard


def private_run(fn):
    @wraps(fn)
    def run(agent, message):
        guard = guard_for(agent)
        with activate(guard):
            if guard:
                guard.observe(text=message)
                guard.observe(text=json.dumps(agent.session.get('history',[]),ensure_ascii=False))
            return fn(agent,message)
    return run


def private_tool(fn):
    @wraps(fn)
    def run(agent, name, args):
        from .tool_executor import ToolExecutionResult
        try:
            guard = CURRENT.get() or guard_for(agent)
            with activate(guard):
                if guard:
                    guard.policy()
                    if name == 'run_shell' and agent.shell_backend != 'appcontainer':
                        raise PrivacyDenied('host_shell_has_no_egress_isolation')
                    if name == 'mcp_docs':
                        raise PrivacyDenied('local_mcp_has_no_egress_isolation')
                    if name == 'run_shell':
                        # The fixed verifier sees an entire allowed snapshot, not
                        # only the files explicitly read by the model.
                        with guard.store.connect() as db:
                            inputs=[r[0] for r in db.execute("SELECT resource FROM labels WHERE resource LIKE 'file:%'")]
                        guard.observe(sources=inputs)
                result = fn(agent,name,args)
                if guard:
                    binding=getattr(agent,'goal_binding',{}) or {}
                    for kind in ('goal','task'):
                        if binding.get(kind+'_id'):
                            resource=kind+':'+binding[kind+'_id']
                            guard.store.derive(resource,guard.resources)
                            guard.resources=tuple(set(guard.resources)|{resource})
                    paths = result.metadata.get('source_paths',[]) + result.metadata.get('affected_paths',[])
                    paths += list(getattr(agent,'_privacy_search_sources',[]))
                    agent._privacy_search_sources=[]
                    if result.metadata.get('tool_status') in {'ok','partial_success'} and isinstance(args,dict) and args.get('path'):
                        paths += [args['path']]
                    targets = ['file:' + p for p in result.metadata.get('affected_paths',[])]
                    if name in {'write_file','patch_file'} and isinstance(args,dict) and args.get('path'):
                        targets += ['file:' + args['path']]
                    guard.observe(sources=['file:' + p for p in paths],text=result.content,targets=targets)
                    if result.metadata.get('evidence_id'):
                        guard.store.derive('evidence:' + result.metadata['evidence_id'],guard.resources)
                    if name=='recall_evidence' and isinstance(args,dict):
                        guard.observe(sources=['evidence:'+args['evidence_id']])
                    if name in {'write_file','patch_file','apply_changes'}:
                        # Edited outputs inherit the run's sources even if their
                        # current text contains no detectable private strings.
                        for target in targets:guard.store.derive(target,guard.resources)
                return result
        except PrivacyDenied as exc:
            return ToolExecutionResult(str(exc),{'tool_status':'rejected','tool_error_code':'privacy_denied',
                'security_event_type':'privacy_denied','workspace_changed':False,'privacy_rule':exc.rule})
    return run


def private_probe(fn):
    @wraps(fn)
    def run(hub,*args,**kwargs):
        store=PrivacyStore(hub.app.root)
        guard=PrivacyGuard(store,['artifact:extension-probe']) if store.enabled else None
        with activate(guard):return fn(hub,*args,**kwargs)
    return run


def private_client(fn):
    """Bound model clients retain protection outside the Agent loop too."""
    @wraps(fn)
    def run(client,*args,**kwargs):
        root=getattr(client,'privacy_root',None)
        current=CURRENT.get()
        if current or not root:
            if current and root and current.store.root!=PrivacyStore(root).root:
                raise PrivacyDenied('cross_workspace_model_client')
            return fn(client,*args,**kwargs)
        store=PrivacyStore(root)
        guard=PrivacyGuard(store,['artifact:direct-model-client']) if store.enabled else None
        with activate(guard):return fn(client,*args,**kwargs)
    return run
