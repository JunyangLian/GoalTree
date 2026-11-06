"""Trusted fixed-command launcher; task tests execute only inside AppContainer.

Allows the Agent loop and the isolated test runtime to have different Python
versions. Never runs tests in this host process or invokes a supplied shell.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.windows_sandbox import run_verification, VERIFY


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--timeout', type=int, required=True)
    a = p.parse_args()
    if not 1 <= a.timeout <= 120: p.error('Invalid deadline')
    # run_verification checks the real kernel AppContainer token and fails
    # closed. The only host code executed is this trusted isolation launcher.
    workspace = a.workspace.resolve()
    before = set((workspace/'.pico/sandbox').glob('*/audit.json'))
    result = run_verification(workspace, VERIFY, a.timeout)
    added = set((workspace/'.pico/sandbox').glob('*/audit.json'))-before
    if len(added) != 1: raise RuntimeError('Expected one fresh kernel audit')
    result['audit'] = json.loads(next(iter(added)).read_text(encoding='utf-8'))
    print(json.dumps(result, ensure_ascii=False), flush=True)
