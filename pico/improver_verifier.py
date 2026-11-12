"""Host-owned, fixed external checks of copied candidates under existing AppContainer."""
from pathlib import Path
import re
import uuid

from .improvement_cases import digest
from .windows_sandbox import snapshot, run_verification, VERIFY


def tree_hash(root):
    root=Path(root)
    from hashlib import sha256
    result={}
    for package in ('pico','goaltree'):
        if (root/package).is_symlink():raise ValueError('Verified package links are forbidden')
        for p in (root/package).rglob('*'):
            if '__pycache__' in p.parts:continue
            if p.is_symlink():raise ValueError('Verified snapshot links are forbidden')
            if p.is_file():
                p.resolve().relative_to(root.resolve())
                if p.stat().st_nlink!=1:raise ValueError('Verified hardlinks are forbidden')
                result[p.relative_to(root).as_posix()]=sha256(p.read_bytes()).hexdigest()
    return result


def verify_pair(baseline,candidate,output,packet,runner=run_verification):
    output=Path(output).resolve()
    if output.exists():raise ValueError('Use a new verification directory; historical checks are immutable')
    output.mkdir(parents=True)
    trusted=Path(__file__).with_name('improver_contracts.py').read_bytes()
    from hashlib import sha256
    receipt={'schema':1,'verifier_sha256':sha256(trusted).hexdigest(),'case_sha256':packet['case_sha256'],
             'command':VERIFY,'backend':'windows_appcontainer','host_fallback':False,'output':str(output)}
    hashes={}
    for name,source in (('baseline',baseline),('candidate',candidate)):
        source=Path(source).resolve();stage=output/name;stage.mkdir()
        snapshot(source,stage,lambda p:p.relative_to(source).parts[0] in {'pico','goaltree'})
        # The exact same host test is injected after the filtered copy. Candidate tests are never executed.
        (stage/'test_external_contracts.py').write_bytes(trusted)
        hashes[name]=tree_hash(stage)
        before=digest(hashes[name])
        try:
            result=runner(stage,VERIFY,timeout=120)
            text=result.get('output',result.get('stdout',''))+'\n'+result.get('stderr','')
            matches=[re.fullmatch(r'(test_\w+) \([^\n]+\) \.\.\. (ok|FAIL|ERROR)',line.strip()) for line in text.splitlines()]
            cases={m[1]:m[2] for m in matches if m}
            expected={'test_feedback_source_minimized','test_opt_out_and_presentation_preference_preserved','test_permission_boundary_unchanged'}
            passed=result.get('returncode')==0 and set(cases)==expected and sum(bool(m) for m in matches)==3 and all(v=='ok' for v in cases.values())
            receipt[name]={'passed':passed,'exit_code':result.get('returncode'),'cases':cases,'error':None}
            (output/(name+'.txt')).write_text(text,encoding='utf-8')
        except Exception as exc:
            receipt[name]={'passed':False,'exit_code':None,'cases':{},'error':type(exc).__name__}
        receipt[name]['snapshot_unchanged']=digest(tree_hash(stage))==before
        receipt[name]['passed'] &= receipt[name]['snapshot_unchanged']
        receipt[name+'_sha256']=before
    changed=sorted(k for k in set(hashes['baseline'])|set(hashes['candidate']) if hashes['baseline'].get(k)!=hashes['candidate'].get(k))
    receipt['changed_files']=changed
    receipt['forbidden_changes']=[p for p in changed if p not in packet['case']['targets']]
    receipt['improved_checks']=[k for k,v in receipt['candidate']['cases'].items()
                               if v=='ok' and receipt['baseline']['cases'].get(k) in {'FAIL','ERROR'}]
    receipt['receipt_sha256']=digest(receipt)
    from .persistence import write_json_atomic
    write_json_atomic(output/'receipt.json',receipt)
    return receipt


def validate_receipt(receipt,packet):
    value=dict(receipt);stamp=value.pop('receipt_sha256',None)
    if digest(value)!=stamp or receipt.get('case_sha256')!=packet['case_sha256']:
        raise ValueError('Verification receipt integrity/case mismatch')
    from hashlib import sha256
    if receipt.get('verifier_sha256')!=sha256(Path(__file__).with_name('improver_contracts.py').read_bytes()).hexdigest():
        raise ValueError('External verifier changed after validation')
    if receipt.get('command')!=VERIFY or receipt.get('backend')!='windows_appcontainer' or receipt.get('host_fallback') is not False:
        raise ValueError('Wrong external execution boundary')
    root=Path(receipt['output'])
    for name in ('baseline','candidate'):
        if digest(tree_hash(root/name))!=receipt[name+'_sha256']:
            raise ValueError('Verified candidate snapshot changed')
        if sha256((root/name/'test_external_contracts.py').read_bytes()).hexdigest()!=receipt['verifier_sha256']:
            raise ValueError('Injected external verifier changed')
    return True
