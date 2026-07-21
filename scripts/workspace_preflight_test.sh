#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFLIGHT="$ROOT_DIR/scripts/workspace_preflight.sh"
source "$ROOT_DIR/scripts/lib/safe_cleanup.sh"
TMP_DIR="$(mktemp -d)"
TMP_NAME="${TMP_DIR##*/}"
REPO=""
LINKED_WORKTREE=""

cleanup() {
  if [[ -n "$REPO" && -n "$LINKED_WORKTREE" && -d "$REPO/.git" ]]; then
    git -C "$REPO" worktree remove --force "$LINKED_WORKTREE" >/dev/null 2>&1 || true
  fi
  safe_remove_tree "$TMP_DIR" "$(dirname "$TMP_DIR")" "$TMP_NAME"
}

trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  local output="$1"
  local expected="$2"
  [[ "$output" == *"$expected"* ]] || \
    fail "expected output to contain: $expected"
}

[[ -x "$PREFLIGHT" ]] || fail "workspace preflight does not exist or is not executable"

REPO="$TMP_DIR/repo"
LINKED_WORKTREE="$TMP_DIR/merged worktree"
git init -q -b main "$REPO"
git -C "$REPO" config user.email "preflight-test@example.com"
git -C "$REPO" config user.name "Preflight Test"
printf '.artifacts/\njson.py\n' >"$REPO/.gitignore"
printf 'base\n' >"$REPO/README.md"
git -C "$REPO" add .gitignore README.md
git -C "$REPO" commit -q -m "fixture base"
BASE_SHA="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" update-ref refs/remotes/origin/main "$BASE_SHA"
git -C "$REPO" config remote.origin.url "$REPO/.git"
git -C "$REPO" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
git -C "$REPO" config branch.main.remote origin
git -C "$REPO" config branch.main.merge refs/heads/main

clean_output="$(cd "$REPO" && "$PREFLIGHT")"
assert_contains "$clean_output" "branch=main state=clean"
assert_contains "$clean_output" "upstream=origin/main ahead=0 behind=0 local_refs_only=true"
assert_contains "$clean_output" "release_summary=missing"
[[ ! -e "$REPO/.artifacts" ]] || fail "preflight created an artifact directory"

SHADOW_MARKER="$TMP_DIR/import-shadow-executed"
printf '__import__("pathlib").Path(__import__("os").environ["SHADOW_MARKER"]).touch()\n' \
  >"$REPO/json.py"
shadow_output="$(cd "$REPO" && env SHADOW_MARKER="$SHADOW_MARKER" "$PREFLIGHT")"
assert_contains "$shadow_output" "branch=main state=clean"
[[ ! -e "$SHADOW_MARKER" ]] || fail "preflight imported an ignored repository module"

printf 'dirty\n' >"$REPO/dirty note.txt"
dirty_output="$(cd "$REPO" && "$PREFLIGHT")"
assert_contains "$dirty_output" "branch=main state=dirty"
rm "$REPO/dirty note.txt"

printf 'local\n' >>"$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" commit -q -m "local commit"
LOCAL_SHA="$(git -C "$REPO" rev-parse HEAD)"
REMOTE_TREE="$(git -C "$REPO" rev-parse "$BASE_SHA^{tree}")"
REMOTE_SHA="$(printf 'remote commit\n' | git -C "$REPO" commit-tree "$REMOTE_TREE" -p "$BASE_SHA")"
git -C "$REPO" update-ref refs/remotes/origin/main "$REMOTE_SHA"

mkdir -p "$REPO/.artifacts/release"
python3 -B -E -S - "$REPO/.artifacts/release/summary.json" "$BASE_SHA" <<'PY'
import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text(
    json.dumps(
        {"git": {"head_sha": sys.argv[2]}, "overall": "passed"},
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
SUMMARY_HASH_BEFORE="$(git -C "$REPO" hash-object "$REPO/.artifacts/release/summary.json")"

git -C "$REPO" branch merged-topic "$BASE_SHA"
git -C "$REPO" worktree add -q "$LINKED_WORKTREE" merged-topic
LINKED_WORKTREE_REPORTED="$(cd "$(dirname "$LINKED_WORKTREE")" && pwd -P)/$(basename "$LINKED_WORKTREE")"

status_before="$(git -C "$REPO" status --porcelain=v1 --untracked-files=all)"
full_output="$(cd "$REPO" && "$PREFLIGHT")"
status_after="$(git -C "$REPO" status --porcelain=v1 --untracked-files=all)"
SUMMARY_HASH_AFTER="$(git -C "$REPO" hash-object "$REPO/.artifacts/release/summary.json")"

assert_contains "$full_output" "branch=main state=clean"
assert_contains "$full_output" "upstream=origin/main ahead=1 behind=1 local_refs_only=true"
assert_contains "$full_output" "release_summary=stale"
assert_contains "$full_output" "summary_head=$BASE_SHA"
assert_contains "$full_output" "current_head=$LOCAL_SHA"
assert_contains "$full_output" "overall=passed"
assert_contains "$full_output" "merged_worktree_candidate"
assert_contains "$full_output" "path=\"$LINKED_WORKTREE_REPORTED\""
assert_contains "$full_output" "branch=\"merged-topic\""
assert_contains "$full_output" "state=clean"
[[ "$status_before" == "$status_after" ]] || fail "preflight changed repository status"
[[ "$SUMMARY_HASH_BEFORE" == "$SUMMARY_HASH_AFTER" ]] || fail "preflight modified release evidence"

FSMONITOR="$TMP_DIR/fsmonitor.sh"
FSMONITOR_MARKER="$TMP_DIR/fsmonitor-executed"
cat >"$FSMONITOR" <<'SH'
#!/usr/bin/env bash
: >"${FSMONITOR_MARKER:?}"
printf '\n'
SH
chmod +x "$FSMONITOR"
git -C "$REPO" config core.fsmonitor "$FSMONITOR"
fsmonitor_output="$(cd "$REPO" && env FSMONITOR_MARKER="$FSMONITOR_MARKER" "$PREFLIGHT")"
assert_contains "$fsmonitor_output" "branch=main state=clean"
[[ ! -e "$FSMONITOR_MARKER" ]] || fail "preflight executed the configured fsmonitor"
git -C "$REPO" config --unset core.fsmonitor

if grep -Eq 'git([^[:alnum:]]|.*[[:space:]])(fetch|pull|push)([[:space:]]|$)' "$PREFLIGHT"; then
  fail "preflight contains a network Git operation"
fi

printf 'workspace preflight tests passed\n'
