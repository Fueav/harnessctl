#!/usr/bin/env python3
"""Execute the repository's prompt evidence validator when its gate is selected."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from evidence import EvidenceError, regular_under
from harness_config import POLICY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-sha256", required=True)
    args = parser.parse_args()
    try:
        raw = args.snapshot.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.snapshot_sha256:
            raise ValueError("sealed change snapshot digest mismatch")
        changes = json.loads(raw)["changes"]
        if POLICY["schema_version"] < 5:
            paths = [item["path"] for item in changes]
            if not any(path.startswith("prompts/") for path in paths): return 0
            if not any(path.startswith("evals/") for path in paths):
                raise ValueError("prompt changes require eval changes under evals/")
            runner = args.repo / "evals/run.sh"
            if not os.access(runner, os.X_OK): return 0
        else:
            runner = regular_under(args.repo, "evals/run.sh", "prompt evaluator evals/run.sh")
            if not os.access(runner, os.X_OK):
                raise ValueError("prompt evaluator evals/run.sh must be a regular executable file")
        return subprocess.call([str(runner)], cwd=args.repo)
    except (OSError, ValueError, KeyError, EvidenceError) as error:
        print(f"prompt evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
