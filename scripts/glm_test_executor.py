"""Version-pinned test-runtime bridge preserving the existing kernel policy."""
import json
from pathlib import Path
import subprocess
import time

from pico.evaluation.cross_agent import clean_environment
from pico.evaluation.isolated_execution import IsolatedExecutor, ExecutionUnavailable
from pico.evaluation.repair_suite import digest, write_json
from pico.windows_sandbox import VERIFY, snapshot as filtered_snapshot

ROOT = Path(__file__).resolve().parents[1]


class PinnedTestExecutor(IsolatedExecutor):
    def __init__(self, backend, evidence, runtime):
        super().__init__(backend, evidence)
        if backend != 'appcontainer': raise ValueError('This bridge is AppContainer only')
        self.runtime = runtime

    def execute(self, workspace, command, timeout=25):
        if self.poisoned: raise ExecutionUnavailable('Prior bridge state unresolved')
        if command != VERIFY or type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError('Only the frozen exact unittest command is allowed')
        workspace = Path(workspace).resolve()
        if self.evidence == workspace or self.evidence.is_relative_to(workspace):
            raise ValueError('Execution evidence must be outside the workload')
        executable = Path(self.runtime['python'])
        if digest(executable) != self.runtime['python_sha256']:
            raise ExecutionUnavailable('Pinned test interpreter changed')
        self.counter += 1
        directory = self.evidence/f'{self.counter:04d}'
        directory.mkdir()
        audit = {'backend':'appcontainer','command':VERIFY,'test_runtime':self.runtime,
            'host_fallback':False,'host_acl_changed':False,'capabilities':[]}
        started = time.monotonic()
        payload = directory/'execution-input'
        payload.mkdir()
        audit['snapshot'] = filtered_snapshot(workspace, payload)
        args = [str(executable), '-I', '-X', 'utf8', '-B', str(ROOT/'scripts/glm_verification_bridge.py'),
            '--workspace', str(payload.resolve()), '--timeout', str(timeout)]
        try:
            process = subprocess.run(args, cwd=ROOT, env=clean_environment(), capture_output=True,
                                     timeout=timeout+90)
            if process.returncode != 0:
                # Persist bounded launcher errors; arbitrary provider/credential
                # strings are not involved, environment is the clean allowlist.
                (directory/'launcher-error.txt').write_bytes(process.stderr[-65536:])
                raise ExecutionUnavailable('AppContainer launcher failed; no host fallback')
            result = json.loads(process.stdout.decode('utf-8'))
            kernel = result.get('audit') or {}
            if kernel.get('kernel_token_is_appcontainer') is not True or kernel.get('capabilities') != []:
                raise ExecutionUnavailable('Kernel isolation proof missing; refuse result')
            write_json(directory/'kernel-audit.json', kernel, exclusive=True)
            audit.update(status='exited',exit_code=result['returncode'])
            return result
        except subprocess.TimeoutExpired:
            self.poisoned = True
            raise ExecutionUnavailable('Launcher deadline; stop batch until cleanup is confirmed') from None
        finally:
            audit['elapsed_ms'] = round((time.monotonic()-started)*1000)
            write_json(directory/'audit.json', audit, exclusive=True)
