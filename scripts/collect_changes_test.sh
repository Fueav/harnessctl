#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT_DIR/scripts/collect_changes.py"
source "$ROOT_DIR/scripts/lib/safe_cleanup.sh"
TMP_DIR="$(mktemp -d)"
TMP_NAME="${TMP_DIR##*/}"

trap 'safe_remove_tree "$TMP_DIR" "$(dirname "$TMP_DIR")" "$TMP_NAME"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

init_repo() {
  local repo="$1"

  git init -q "$repo"
  git -C "$repo" config user.email "harness-test@example.com"
  git -C "$repo" config user.name "Harness Test"
}

test_python39_compatibility() {
  local python39="/usr/bin/python3"
  local version
  local import_status=0
  local help_status=0

  if [[ ! -x "$python39" ]]; then
    printf 'SKIP: Python 3.9 compatibility (/usr/bin/python3 unavailable)\n'
    return 0
  fi

  version="$("$python39" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "$version" != "3.9" ]]; then
    printf 'SKIP: Python 3.9 compatibility (/usr/bin/python3 is %s)\n' "$version"
    return 0
  fi

  PYTHONPATH="$ROOT_DIR/scripts" "$python39" \
    -c 'from lib.change_scope import ChangeRecord; ChangeRecord("path", ("committed",))' \
    >"$TMP_DIR/python39-import.out" 2>"$TMP_DIR/python39-import.err" || import_status=$?
  "$python39" "$SCRIPT" --help \
    >"$TMP_DIR/python39-help.out" 2>"$TMP_DIR/python39-help.err" || help_status=$?

  if (( import_status != 0 || help_status != 0 )); then
    cat "$TMP_DIR/python39-import.err" >&2
    cat "$TMP_DIR/python39-help.err" >&2
    fail "collector must import and show --help with Python 3.9"
  fi
}

test_complete_change_union() {
  local repo="$TMP_DIR/change-union"
  local output="$TMP_DIR/change-union.json"
  local nul_output="$TMP_DIR/change-union.nul"
  local base_sha

  init_repo "$repo"
  printf '*.ignored\n' >"$repo/.gitignore"
  printf 'base\n' >"$repo/tracked.txt"
  printf 'base\n' >"$repo/multi-source.txt"
  git -C "$repo" add .gitignore tracked.txt multi-source.txt
  git -C "$repo" commit -q -m "base"
  base_sha="$(git -C "$repo" rev-parse HEAD)"

  printf 'committed\n' >"$repo/committed.txt"
  printf 'committed\n' >"$repo/multi-source.txt"
  git -C "$repo" add committed.txt multi-source.txt
  git -C "$repo" commit -q -m "branch change"

  printf 'staged\n' >"$repo/staged.txt"
  printf 'staged\n' >"$repo/multi-source.txt"
  git -C "$repo" add staged.txt multi-source.txt
  printf 'unstaged\n' >>"$repo/tracked.txt"
  printf 'unstaged\n' >>"$repo/multi-source.txt"
  printf 'untracked\n' >"$repo/untracked space \"quote\".txt"
  printf 'ignored\n' >"$repo/not-collected.ignored"

  "$SCRIPT" \
    --repo "$repo" \
    --base "$base_sha" \
    --mode change \
    --format json >"$output"

  python3 - "$repo" "$base_sha" "$output" <<'PY'
import json
import pathlib
import subprocess
import sys

repo = pathlib.Path(sys.argv[1])
base_sha = sys.argv[2]
payload = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))

expected_changes = {
    "committed.txt": ["committed"],
    "multi-source.txt": ["committed", "staged", "unstaged"],
    "staged.txt": ["staged"],
    "tracked.txt": ["unstaged"],
    'untracked space "quote".txt': ["untracked"],
}
actual_changes = {
    item["path"]: item["sources"]
    for item in payload["changes"]
}
assert actual_changes == expected_changes, actual_changes
assert [item["path"] for item in payload["changes"]] == sorted(expected_changes)
assert payload["mode"] == "change"
assert payload["base_sha"] == base_sha
assert payload["merge_base_sha"] == base_sha
assert payload["head_sha"] == subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
).strip()
assert payload["head_tree_sha"] == subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True
).strip()
assert payload["clean"] is False
assert "not-collected.ignored" not in actual_changes
PY

  "$SCRIPT" \
    --repo "$repo" \
    --base "$base_sha" \
    --mode change \
    --format nul >"$nul_output"

  python3 - "$nul_output" <<'PY'
import pathlib
import sys

raw = pathlib.Path(sys.argv[1]).read_bytes()
assert raw.endswith(b"\0")
paths = [entry.decode() for entry in raw.split(b"\0") if entry]
assert paths == sorted([
    "committed.txt",
    "multi-source.txt",
    "staged.txt",
    "tracked.txt",
    'untracked space "quote".txt',
]), paths
PY

  PYTHONPATH="$ROOT_DIR/scripts" python3 - "$repo" "$base_sha" <<'PY'
import dataclasses
import pathlib
import sys

from lib.change_scope import ChangeRecord, is_clean, resolve_commit, snapshot

repo = pathlib.Path(sys.argv[1])
base_sha = sys.argv[2]
record = ChangeRecord("path", ("committed",))
try:
    record.path = "mutated"
except dataclasses.FrozenInstanceError:
    pass
else:
    raise AssertionError("ChangeRecord must be immutable")

assert resolve_commit(repo, base_sha) == base_sha
assert is_clean(repo) is False
result = snapshot(repo, base_sha)
assert result["base_sha"] == base_sha
assert all(isinstance(item, ChangeRecord) for item in result["changes"])
PY
}

test_invalid_base_fails_closed() {
  local repo="$TMP_DIR/invalid-base"
  local stderr_file="$TMP_DIR/invalid-base.err"

  init_repo "$repo"
  printf 'base\n' >"$repo/file.txt"
  git -C "$repo" add file.txt
  git -C "$repo" commit -q -m "base"

  if "$SCRIPT" \
    --repo "$repo" \
    --base "refs/heads/does-not-exist" \
    --mode change \
    --format json >"$TMP_DIR/invalid-base.json" 2>"$stderr_file"; then
    fail "invalid base unexpectedly succeeded"
  fi

  grep -q "refs/heads/does-not-exist" "$stderr_file" || \
    fail "invalid-base error did not identify the rejected revision"
}

test_rename_reports_old_and_new_paths() {
  local repo="$TMP_DIR/rename"
  local output="$TMP_DIR/rename.json"
  local base_sha

  init_repo "$repo"
  mkdir -p "$repo/internal/risk"
  printf 'protected\n' >"$repo/internal/risk/limit.txt"
  git -C "$repo" add internal/risk/limit.txt
  git -C "$repo" commit -q -m "base"
  base_sha="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/docs"
  git -C "$repo" mv internal/risk/limit.txt docs/limit.txt
  git -C "$repo" commit -q -m "move protected path"

  "$SCRIPT" \
    --repo "$repo" \
    --base "$base_sha" \
    --mode release \
    --format json >"$output"

  python3 - "$output" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
changes = {
    item["path"]: item["sources"]
    for item in payload["changes"]
}

assert payload["mode"] == "release"
assert payload["clean"] is True
assert changes == {
    "docs/limit.txt": ["committed"],
    "internal/risk/limit.txt": ["committed"],
}, changes
PY
}

test_collector_library_must_be_regular_file() {
  local repo="$TMP_DIR/symlinked-library-repo"
  local runtime="$TMP_DIR/symlinked-library-runtime/scripts"
  local stderr_file="$TMP_DIR/symlinked-library.err"
  local base_sha

  init_repo "$repo"
  printf 'base\n' >"$repo/file.txt"
  git -C "$repo" add file.txt
  git -C "$repo" commit -q -m "base"
  base_sha="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$runtime/lib"
  cp "$ROOT_DIR/scripts/collect_changes.py" "$runtime/collect_changes.py"
  ln -s "$ROOT_DIR/scripts/lib/change_scope.py" "$runtime/lib/change_scope.py"
  if python3 -I -B -S "$runtime/collect_changes.py" \
    --repo "$repo" --base "$base_sha" --mode release --format json \
    >"$TMP_DIR/symlinked-library.json" 2>"$stderr_file"; then
    fail "collector accepted a symlinked canonical library"
  fi
  grep -Fq 'regular non-symlink file' "$stderr_file" || \
    fail "collector did not explain its symlinked-library rejection"
}

test_python39_compatibility
test_complete_change_union
test_invalid_base_fails_closed
test_rename_reports_old_and_new_paths
test_collector_library_must_be_regular_file

printf 'collect_changes tests passed\n'
