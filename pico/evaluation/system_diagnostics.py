"""Agent-neutral, posthoc diagnostics from observed tool/wire boundaries.

No semantic relevance/faithfulness inference; missing observations stay unknown.
"""
from collections import Counter
import json
import re

from .process_trace import ratio
from .seven_dimensions import percentile

EDITS = {'patch_file','write_file','replace_in_file','apply_changes'}
SHELL = {'run_shell','exec_command'}
INVALID = {'invalid_arguments','unknown_tool','tool_not_allowed'}
DENIED = {'scenario_policy_denied','approval_denied','verification_budget_reserved'}


def invalid(tool):
    meta=tool.get('metadata',{})
    return meta.get('tool_error_code') in INVALID or str(tool.get('output','')).startswith(
        ('Invalid tool arguments:', 'Unknown tool:'))


def denied(tool):
    meta=tool.get('metadata',{})
    return tool.get('rejected') is True or meta.get('tool_status')=='rejected' or meta.get('tool_error_code') in DENIED


def changed_paths(tool):
    before,after=tool.get('before',{}),tool.get('snapshot',{})
    return {p for p in before.keys()|after.keys() if before.get(p)!=after.get(p)}


def unittest_progress(result, expected):
    """Partial test progress, not a substitute for full functional acceptance."""
    if not result or type(expected) is not int or expected<=0:
        return None
    outcomes={}
    for line in result.get('output','').splitlines():
        m=re.fullmatch(r'(.+?) \(([^)]+)\) \.\.\. (ok|FAIL|ERROR|skipped(?: .*)?)',line.strip())
        if m:
            key=(m[1],m[2])
            if key in outcomes:
                return None  # ambiguous duplicate/subtest output is not repaired.
            outcomes[key]=m[3]
    if len(outcomes)!=expected or result.get('tests_run')!=expected:
        return None
    return ratio(sum(value=='ok' for value in outcomes.values()),expected)


def span_key(span):
    return tuple(span[k] for k in ('path','source_sha256','line','text'))


def evidence_transport(events):
    """Retrieved spans visible on the next observed request, any retained copy.

    No next request (e.g. budget abort) is censored, not a context-loss event.
    Numbered read_file only; this measures transport, not semantic relevance.
    """
    delivered=opportunities=unobserved=reads=0
    pending=set()
    for event in events:
        if event.get('kind')=='read' and event.get('ok'):
            pending.update(span_key(s) for s in event.get('snippets',[]));reads+=1
        elif event.get('kind')=='context':
            visible={span_key(s) for s in event.get('snippets',[])}
            opportunities+=len(pending);delivered+=len(pending&visible);pending.clear()
    unobserved=len(pending)
    return {'visible_at_next_request':ratio(delivered,opportunities),'without_next_observed_request_spans':unobserved,
        'successful_numbered_read_events':reads,'scope':'Observed exact versioned spans only; not Recall, memory quality or semantic context loss'}


def diagnostics(tools, wire, events, *, external=None, expected_tests=None, session_complete=True):
    seen=set();repeats=0;first_edit=None;admitted_edits=[];failed_edits=[];recovered=[];denials=[]
    for index,t in enumerate(tools):
        signature=(t['name'],json.dumps(t.get('args',{}),sort_keys=True,ensure_ascii=False),
            json.dumps(t.get('before',{}),sort_keys=True,ensure_ascii=False))
        repeats+=signature in seen;seen.add(signature)
        if changed_paths(t) and first_edit is None:first_edit=index
        if denied(t):denials.append(index)
        if t['name'] in EDITS and not denied(t) and not invalid(t):
            admitted_edits.append(index)
            # No-effect successful writes can be intentional; require failure signal.
            if not changed_paths(t) and (t.get('metadata',{}).get('tool_status')=='error'
                    or str(t.get('output','')).startswith(('error:','no replacements made','Tool execution failed:'))):
                path=t.get('args',{}).get('path')
                # Multi-file/unknown-target edits cannot establish same-file
                # recovery. Keep them outside this specifically scoped metric.
                if not isinstance(path,str) or not path:
                    continue
                failed_edits.append(index)
                later=next((j for j in range(index+1,len(tools)) if path and tools[j]['name'] in EDITS
                    and path in changed_paths(tools[j]) and not denied(tools[j])),None)
                if later is not None:recovered.append({'failed_tool_index':index,'repaired_tool_index':later,'path':path})
    edit_changed=sum(bool(changed_paths(tools[i])) for i in admitted_edits)
    source_before_edit=any(t['name'] in {'read_file','search','grep_files'}
        and not denied(t) and not invalid(t) and t.get('metadata',{}).get('tool_status')=='ok'
        for t in tools[:first_edit]) if first_edit is not None else None
    shell_denials=[i for i in denials if tools[i]['name'] in SHELL]
    shell_recovered=sum(any(t['name'] in SHELL and not denied(t) and not invalid(t)
        and type(t.get('exit_code')) is int for t in tools[i+1:]) for i in shell_denials)
    responses=[e.get('body',{}) for e in wire if e.get('event')=='response']
    prompt=[];output=[];truncated_output=[];cache=[];truncated=0
    for response in responses:
        usage=response.get('usage') or {};p=usage.get('prompt_tokens');c=usage.get('completion_tokens')
        if type(p) is int and p>=0:prompt.append(p)
        if type(c) is int and c>=0:output.append(c)
        details=usage.get('prompt_tokens_details') or {};hit=details.get('cached_tokens')
        if type(hit) is int and type(p) is int and 0<=hit<=p:cache.append((hit,p))
        if any(choice.get('finish_reason')=='length' for choice in response.get('choices',[])):
            truncated+=1
            if type(c) is int and c>=0:truncated_output.append(c)
    return {
        'tools':{'contract_admitted_proxy':ratio(sum(not denied(t) and not invalid(t) for t in tools),len(tools)),
            'reported_invalid_arguments_or_unknown_tool':ratio(sum(invalid(t) for t in tools),len(tools)),
            'reported_error_codes':dict(Counter(t.get('metadata',{}).get('tool_error_code') or 'unspecified'
                for t in tools if t.get('metadata',{}).get('tool_status') in {'error','rejected'})),
            'same_state_same_action_repeats':ratio(repeats,len(tools)),
            'edit_effect_rate':ratio(edit_changed,len(admitted_edits)),
            'failed_patch_same_file_recovery':ratio(len(recovered),len(failed_edits)) if session_complete else None,
            'failed_patch_opportunities':len(failed_edits),'patch_recovery_pairs':recovered,
            'denied_shell_then_admitted_shell':ratio(shell_recovered,len(shell_denials)) if session_complete else None,
            'source_read_before_first_edit':source_before_edit,
            'calls_through_first_actual_edit':first_edit+1 if first_edit is not None else None,
            'scope':'Policy/interface proxy, not semantic tool-selection accuracy. Expected red tests are valid actions.'},
        'context':{'transport':evidence_transport(events),
            'provider_prompt_tokens_p50':percentile(prompt,.5),'provider_prompt_tokens_peak':max(prompt) if prompt else None,
            'prompt_usage_observed':ratio(len(prompt),len(responses)),
            'context_amplification':max(prompt)/prompt[0] if prompt and prompt[0] and len(prompt)==len(responses) else None,
            'cache_token_share_observed_subset':ratio(sum(x for x,_ in cache),sum(p for _,p in cache)),
            'cache_usage_observed_responses':ratio(len(cache),len(responses)),
            'recall_at_k':None,'semantic_context_loss':None,'long_term_memory_benefit':None},
        'outcome':{'frozen_acceptance_test_progress':unittest_progress(external,expected_tests),
            'pass_to_pass_regression_preservation':None},
        'protocol':{'truncated_responses':ratio(truncated,len(responses)),
            'truncated_output_token_share':ratio(sum(truncated_output),sum(output))
                if len(output)==len(responses) and len(truncated_output)==truncated else None},
        'observability':{'session_finished_record_observed':session_complete,'tool_records':len(tools),
            'wire_requests':sum(e.get('event')=='request' for e in wire),'wire_responses':len(responses)},
        'unassessed':{'repeated_run_stability':'Each frozen cohort has one trial/task; versions cannot be pooled as repeats',
            'semantic_faithfulness':'Requires calibrated annotation, not a success-keyword heuristic',
            'attack_success_and_benign_utility':'Requires labelled attack/benign paired tasks, not policy-denial counts'}}
