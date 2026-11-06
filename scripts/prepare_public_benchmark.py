"""Prepare pinned SWE-bench candidate inputs or hand predictions to its official grader."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.public_benchmark import prepare, docker_preflight, verifier_handoff


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parquet', type=Path)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--harness', type=Path)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--verification-output', type=Path)
    args = parser.parse_args()
    if args.parquet:
        print(json.dumps(prepare(args.parquet, args.output), ensure_ascii=False))
    elif args.predictions:
        if not all([args.harness, args.python, args.verification_output]):
            parser.error('Handoff requires --harness, --python and --verification-output')
        print(json.dumps({'official_process_succeeded': verifier_handoff(args.output,
            args.predictions, args.harness, args.python, args.verification_output)}))
    else:
        print(json.dumps(docker_preflight()))
