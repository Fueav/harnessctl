#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/safe_cleanup.sh"
TMP_DIR="$(mktemp -d)"
TMP_NAME="${TMP_DIR##*/}"
trap 'safe_remove_tree "$TMP_DIR" "$(dirname "$TMP_DIR")" "$TMP_NAME"' EXIT

python3 -I -B -S - "$ROOT_DIR/scripts/changed_go_packages.py" "$TMP_DIR" <<'PY'
import hashlib
import json
import os
import pathlib
import subprocess
import sys

selector = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])


def fixture(name, changes, files=()):
    repo = root / name
    repo.mkdir()
    for relative in files:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package fixture\n", encoding="utf-8")
    artifact = repo / ".artifacts" / "change"
    artifact.mkdir(parents=True)
    snapshot = artifact / "change_scope.json"
    payload = {
        "base_sha": "1" * 40,
        "changes": [
            {"path": path, "sources": ["committed"]} for path in sorted(changes)
        ],
        "clean": False,
        "head_sha": "2" * 40,
        "head_tree_sha": "3" * 40,
        "merge_base_sha": "1" * 40,
        "mode": "change",
    }
    snapshot.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    return repo, snapshot, digest


def run(repo, snapshot, digest):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            os.fspath(selector),
            "--repo",
            os.fspath(repo),
            "--snapshot-file",
            os.fspath(snapshot),
            "--snapshot-sha256",
            digest,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


repo, snapshot, digest = fixture(
    "one-package", ["internal/quote/quote.go"], ["internal/quote/quote.go"]
)
result = run(repo, snapshot, digest)
assert result.returncode == 0, result.stderr
assert result.stdout.split(b"\0") == [b"./internal/quote", b""], result.stdout

repo, snapshot, digest = fixture(
    "multiple-packages",
    ["cmd/api/main.go", "internal/space dir/value_test.go", "internal/quote/quote.go"],
    ["cmd/api/main.go", "internal/space dir/value_test.go", "internal/quote/quote.go"],
)
result = run(repo, snapshot, digest)
assert result.returncode == 0, result.stderr
assert result.stdout.split(b"\0") == [
    b"./cmd/api",
    b"./internal/quote",
    b"./internal/space dir",
    b"",
], result.stdout

for metadata in ("go.mod", "go.sum"):
    repo, snapshot, digest = fixture(f"metadata-{metadata}", [metadata], [metadata])
    result = run(repo, snapshot, digest)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"./...\0", (metadata, result.stdout)

repo, snapshot, digest = fixture("deleted-go", ["internal/old/old.go"])
result = run(repo, snapshot, digest)
assert result.returncode == 0, result.stderr
assert result.stdout == b"./...\0", result.stdout

repo, snapshot, digest = fixture("docs-only", ["docs/guide.md"], ["docs/guide.md"])
result = run(repo, snapshot, digest)
assert result.returncode == 0, result.stderr
assert result.stdout == b"", result.stdout

repo, snapshot, digest = fixture("tampered", ["docs/guide.md"], ["docs/guide.md"])
snapshot.write_text("{}\n", encoding="utf-8")
result = run(repo, snapshot, digest)
assert result.returncode != 0, "tampered snapshot digest passed"

repo, snapshot, digest = fixture("malformed", ["docs/guide.md"], ["docs/guide.md"])
snapshot.write_text("not json\n", encoding="utf-8")
digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
result = run(repo, snapshot, digest)
assert result.returncode != 0, "malformed snapshot passed"

repo, snapshot, digest = fixture("symlink", ["docs/guide.md"], ["docs/guide.md"])
outside = root / "outside-snapshot.json"
snapshot.replace(outside)
snapshot.symlink_to(outside)
result = run(repo, snapshot, digest)
assert result.returncode != 0, "symlinked snapshot passed"

print("changed Go package selector tests passed")
PY
