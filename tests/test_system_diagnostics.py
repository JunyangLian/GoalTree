from pico.evaluation.system_diagnostics import diagnostics,evidence_transport,unittest_progress


def tool(name='patch_file', *, path='a.py', before='old', after='old', status='error', code='', output='error: no match'):
    return {'name':name,'args':{'path':path},'metadata':{'tool_status':status,'tool_error_code':code},
        'before':{path:before},'snapshot':{path:after},'output':output,'exit_code':None}


def test_expected_red_test_is_valid_action_not_an_argument_error():
    t=tool('run_shell',output='FAILED (failures=1)',code='tool_failed');t['exit_code']=1
    result=diagnostics([t],[],[])['tools']
    assert result['contract_admitted_proxy']['value']==1
    assert result['reported_invalid_arguments_or_unknown_tool']['value']==0


def test_patch_recovery_requires_same_file_actual_change_and_observed_session_end():
    failed=tool();other=tool(path='b.py',after='new',status='ok');fixed=tool(after='new',status='ok')
    assert diagnostics([failed,other],[],[])['tools']['failed_patch_same_file_recovery']['value']==0
    r=diagnostics([failed,other,fixed],[],[])['tools']
    assert r['failed_patch_same_file_recovery']['value']==1
    assert r['patch_recovery_pairs'][0]['repaired_tool_index']==2
    assert diagnostics([failed],[],[],session_complete=False)['tools']['failed_patch_same_file_recovery'] is None


def test_policy_denials_are_not_failed_patches_and_noop_writes_are_not_errors():
    denied=tool(status='rejected',code='scenario_policy_denied')
    noop=tool(status='ok',output='unchanged')
    r=diagnostics([denied,noop],[],[])['tools']
    assert r['failed_patch_opportunities']==0
    assert r['edit_effect_rate']['numerator']==0 and r['edit_effect_rate']['denominator']==1


def test_unknown_multifile_patch_target_is_not_a_measured_failed_recovery():
    failed=tool('apply_changes');failed['args']={'changes':[]}
    r=diagnostics([failed],[],[])['tools']
    assert r['failed_patch_opportunities']==0
    assert r['failed_patch_same_file_recovery']['value'] is None


def test_same_action_after_state_change_is_not_counted_as_a_loop():
    first=tool('read_file',status='ok',output='source')
    changed=tool('read_file',before='new',after='new',status='ok',output='new source')
    r=diagnostics([first,first,changed],[],[])['tools']['same_state_same_action_repeats']
    assert r['numerator']==1 and r['denominator']==3


def test_missing_next_request_is_censored_and_old_file_version_does_not_match():
    span={'path':'a.py','source_sha256':'v1','line':1,'text':'same'}
    read={'kind':'read','ok':True,'snippets':[span]}
    r=evidence_transport([read]);assert r['visible_at_next_request']['value'] is None
    assert r['without_next_observed_request_spans']==1
    r=evidence_transport([read,{'kind':'context','snippets':[{**span,'source_sha256':'v2'}]}])
    assert r['visible_at_next_request']['value']==0


def test_partial_tests_need_complete_unambiguous_verbose_results():
    result={'tests_run':2,'output':'test_a (m.C) ... ok\ntest_b (m.C) ... FAIL\n'}
    assert unittest_progress(result,2)['value']==.5
    assert unittest_progress(result,3) is None
    assert unittest_progress({**result,'output':result['output']*2},2) is None


def test_cache_and_truncation_usage_are_not_invented_when_usage_is_absent():
    wire=[{'event':'response','body':{'choices':[{'finish_reason':'length'}],'usage':{}}}]
    r=diagnostics([],wire,[])
    assert r['protocol']['truncated_output_token_share'] is None
    assert r['context']['cache_token_share_observed_subset']['value'] is None
    assert r['context']['context_amplification'] is None


def test_cache_share_uses_only_observed_subset_and_truncation_uses_output_usage():
    wire=[
        {'event':'response','body':{'choices':[{'finish_reason':'tool_calls'}],
            'usage':{'prompt_tokens':100,'completion_tokens':10,'prompt_tokens_details':{'cached_tokens':20}}}},
        {'event':'response','body':{'choices':[{'finish_reason':'length'}],
            'usage':{'prompt_tokens':300,'completion_tokens':90}}}]
    r=diagnostics([],wire,[])
    assert r['context']['cache_token_share_observed_subset']['value']==.2
    assert r['context']['cache_usage_observed_responses']['value']==.5
    assert r['context']['context_amplification']==3
    assert r['protocol']['truncated_output_token_share']['value']==.9
