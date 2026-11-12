import json
import pytest
from pico.improver import ImproverStore
from pico.improver_verifier import verify_pair,validate_receipt
from pico.improvement_cases import public_packet
from pico.providers.clients import FakeModelClient

PROPOSAL={'title':'Minimization','hypothesis':'Duplicated source prose',
    'changes':['Minimize source observations'],'targets':['pico/learning_runtime.py'],
    'validation':['External randomized canary checks'],'risks':['Existing history retained'],
    'rollback':'Operator reviewed revert','evidence_case':'feedback-minimization-v1'}

def roots(tmp_path):
    result=[]
    for name,text in (('base','baseline'),('candidate','changed')):
        root=tmp_path/name;(root/'pico').mkdir(parents=True)
        (root/'pico/learning_runtime.py').write_text(text);result.append(root)
    return result

def fixture_runner(root,command,timeout):
    names=('test_feedback_source_minimized','test_opt_out_and_presentation_preference_preserved','test_permission_boundary_unchanged')
    return {'returncode':0,'output':'\r\n'.join(n+' (test_external_contracts.ImprovementContracts) ... ok' for n in names)+'\r\n'}

def test_external_receipt_crlf_binding_cas_and_operator_acceptance(tmp_path):
    base,candidate=roots(tmp_path);packet=public_packet('feedback-minimization-v1')
    receipt=verify_pair(base,candidate,tmp_path/'checks',packet,runner=fixture_runner)
    assert receipt['candidate']['passed'] and receipt['changed_files']==['pico/learning_runtime.py']
    assert validate_receipt(receipt,packet)
    store=ImproverStore(tmp_path/'state');row=store.prepare(packet['case_id'])
    row=store.request(row['id'],FakeModelClient([json.dumps(PROPOSAL)]))
    checked=store.record_verification(row['id'],row['version'],receipt)
    accepted=store.decide(row['id'],checked['version'],'accept','Fixture operator decision for unit testing')
    assert accepted['status']=='accepted' and accepted['body']['decision']['actor']=='operator_cli'
    assert (candidate/'pico/learning_runtime.py').read_text()=='changed'
    assert store.audit_report()['valid']
    with pytest.raises(ValueError):store.decide(row['id'],checked['version'],'accept','Stale decision')

def test_changed_snapshot_or_verifier_cannot_be_accepted(tmp_path):
    base,candidate=roots(tmp_path);packet=public_packet('feedback-minimization-v1')
    receipt=verify_pair(base,candidate,tmp_path/'checks',packet,runner=fixture_runner)
    (tmp_path/'checks/candidate/pico/learning_runtime.py').write_text('later change')
    with pytest.raises(ValueError,match='snapshot'):validate_receipt(receipt,packet)

def test_zero_exit_without_external_test_cases_is_not_success(tmp_path):
    base,candidate=roots(tmp_path);packet=public_packet('feedback-minimization-v1')
    receipt=verify_pair(base,candidate,tmp_path/'checks',packet,runner=lambda *a,**k:{'returncode':0,'output':'I passed all tests'})
    assert not receipt['candidate']['passed']
    with pytest.raises(ValueError):verify_pair(base,candidate,tmp_path/'checks',packet,runner=fixture_runner)

def test_unexpected_file_changes_are_rejected(tmp_path):
    base,candidate=roots(tmp_path);packet=public_packet('feedback-minimization-v1')
    (candidate/'pico/windows_sandbox.py').write_text('unauthorized policy change')
    receipt=verify_pair(base,candidate,tmp_path/'checks',packet,runner=fixture_runner)
    assert receipt['forbidden_changes']==['pico/windows_sandbox.py']
    store=ImproverStore(tmp_path/'state');row=store.prepare(packet['case_id'])
    row=store.request(row['id'],FakeModelClient([json.dumps(PROPOSAL)]))
    with pytest.raises(ValueError,match='target'):store.record_verification(row['id'],row['version'],receipt)
