"""Exact-command execution backends. Missing Docker never means host execution."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .cross_agent import clean_environment, snapshot, unittest_result
from .public_benchmark import docker_preflight
from .repair_suite import install, write_json, safe_path
from ..windows_sandbox import VERIFY, run_verification, snapshot as filtered_snapshot


class ExecutionUnavailable(RuntimeError):
    pass


class IsolatedExecutor:
    def __init__(self, backend, evidence, image=None):
        if backend not in {'appcontainer', 'docker'}:
            raise ValueError('Only OS-isolated backends are allowed')
        if backend == 'docker' and (not isinstance(image, str) or not re.fullmatch(
                r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}', image)):
            raise ValueError('Docker requires an immutable image digest')
        self.backend, self.image = backend, image
        self.evidence = Path(evidence).resolve()
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.counter = 0
        self.poisoned = False

    def execute(self, workspace, command, timeout=25):
        if self.poisoned:
            raise ExecutionUnavailable('Previous container cleanup unresolved; no further execution')
        if command != VERIFY or type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError('Only the frozen exact unittest command and bounded timeout are allowed')
        self.counter += 1
        directory = self.evidence/f'{self.counter:04d}'
        directory.mkdir()
        at = time.monotonic()
        audit = {'backend': self.backend, 'command': command, 'timeout': timeout,
                 'image': self.image, 'network': False, 'host_fallback': False}
        try:
            if self.backend == 'appcontainer':
                result = run_verification(workspace, command, timeout)
            else:
                ready = docker_preflight()
                audit['preflight'] = ready
                if not ready['ready']:
                    raise ExecutionUnavailable('Linux Docker is unavailable; no fallback')
                docker = shutil.which('docker')
                image_info = subprocess.run([docker, 'image', 'inspect', self.image],
                    capture_output=True, text=True, timeout=20, check=True, env=clean_environment())
                data = json.loads(image_info.stdout)[0]
                if data.get('Os') != 'linux' or not any(
                        d.endswith('@'+self.image.split('@')[1]) for d in data.get('RepoDigests') or []):
                    raise ExecutionUnavailable('Pinned Linux image not present; no automatic pull')
                audit['image_id'] = data['Id']
                payload = directory/'execution-input'
                payload.mkdir()
                audit['snapshot'] = filtered_snapshot(workspace, payload)
                name = 'codetrail-verify-'+uuid.uuid4().hex
                audit['container'] = name
                args = self.docker_command(docker, payload, name)
                write_json(directory/'lease.json', {'container': name, 'image': self.image}, exclusive=True)
                try:
                    process = subprocess.run(args, capture_output=True, timeout=timeout,
                                             env=clean_environment())
                    result = {'returncode': process.returncode,
                              'output': (process.stdout+process.stderr).decode('utf-8', errors='replace')}
                finally:
                    # Remove only the container created by this execution's nonce.
                    self.poisoned = True
                    cleanup = subprocess.run([docker, 'rm', '-f', name], capture_output=True,
                                             timeout=20, env=clean_environment())
                    audit['cleanup_exit_code'] = cleanup.returncode
                    if cleanup.returncode != 0:
                        self.poisoned = True
                        raise ExecutionUnavailable('Container cleanup not confirmed; stop the batch')
                    write_json(directory/'closed.json', {'container': name, 'cleanup_exit_code': 0}, exclusive=True)
                    self.poisoned = False
            audit.update(status='exited', exit_code=result['returncode'])
            return result
        except Exception as exc:
            audit.update(status='failed_closed', error_type=type(exc).__name__)
            raise
        finally:
            audit['elapsed_ms'] = round((time.monotonic()-at)*1000)
            write_json(directory/'audit.json', audit, exclusive=True)

    def docker_command(self, docker, payload, name):
        payload = Path(payload).resolve()
        if ',' in str(payload) or not re.fullmatch(r'codetrail-verify-[a-f0-9]{32}', name):
            raise ValueError('Unsafe bind mount or container identity')
        return [docker, 'run', '--name', name, '--pull', 'never', '--network', 'none',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--pids-limit', '64', '--memory', '512m', '--cpus', '1', '--user', '65534:65534',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
            '--mount', f'type=bind,src={payload},dst=/workspace,readonly',
            '--workdir', '/workspace', '--entrypoint', 'python', self.image,
            '-B', '-m', 'unittest', 'discover', '-v']


def cleanup_abandoned(evidence):
    """Controller recovery after worker termination, limited to its recorded nonce leases."""
    evidence = Path(evidence).resolve()
    outcomes = []
    for lease in sorted(evidence.glob('*/lease.json')):
        if lease.is_symlink() or lease.parent.is_symlink() or any(
                getattr(p.lstat(), 'st_file_attributes', 0) & 0x400 for p in (lease, lease.parent)) or not lease.resolve().is_relative_to(evidence):
            raise ExecutionUnavailable('Unsafe lease location')
        if (lease.parent/'closed.json').exists():
            continue
        value = json.loads(lease.read_text(encoding='utf-8'))
        name = value['container']
        if not re.fullmatch(r'codetrail-verify-[a-f0-9]{32}', name):
            raise ExecutionUnavailable('Unowned container name; refuse recovery')
        docker = shutil.which('docker')
        if not docker:
            raise ExecutionUnavailable('Docker unavailable for recorded cleanup')
        process = subprocess.run([docker, 'rm', '-f', name], capture_output=True,
                                 timeout=20, env=clean_environment())
        if process.returncode != 0:
            # A missing container is safe, but only after the daemon explicitly
            # confirms its absence by a successful inventory with no such name.
            inventory = subprocess.run([docker, 'ps', '-a', '--filter', 'name=^/'+name+'$',
                                        '--format', '{{.Names}}'], capture_output=True,
                                       timeout=20, env=clean_environment())
            if inventory.returncode != 0 or inventory.stdout.strip():
                raise ExecutionUnavailable('Recorded container cleanup unresolved')
        write_json(lease.parent/'closed.json', {'container': name, 'controller_recovered': True}, exclusive=True)
        outcomes.append(name)
    return outcomes


def verify(task, implementation, destination, executor):
    destination = Path(destination)
    install(destination, task['files'])
    for name, data in implementation.items():
        if name not in task['editable_files']:
            raise ValueError('Noneditable verification overlay')
        safe_path(destination, name).write_bytes(data)
    before = snapshot(destination)
    try:
        raw = executor.execute(destination, VERIFY)
        result = unittest_result(raw['output'], raw['returncode'], task['expected_tests'])
    except Exception as exc:
        result = {'accepted': False, 'error_type': type(exc).__name__}
    result.update(workspace_unchanged=before == snapshot(destination), backend=executor.backend,
                  host_fallback=False)
    result['accepted'] = bool(result['accepted'] and result['workspace_unchanged'])
    write_json(destination.parent/(destination.name+'-result.json'), result, exclusive=True)
    return result
