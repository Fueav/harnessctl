#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"

trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

install_checker() {
  local repo="$1"

  mkdir -p "$repo/scripts/lib"
  cp "$ROOT_DIR/scripts/check_ai_boundaries.sh" "$repo/scripts/check_ai_boundaries.sh"
  cp "$ROOT_DIR/scripts/collect_changes.py" "$repo/scripts/collect_changes.py"
  cp "$ROOT_DIR/scripts/lib/change_scope.py" "$repo/scripts/lib/change_scope.py"
  if [[ -f "$ROOT_DIR/scripts/check_ai_boundaries.py" ]]; then
    cp "$ROOT_DIR/scripts/check_ai_boundaries.py" "$repo/scripts/check_ai_boundaries.py"
  fi
  chmod +x "$repo/scripts/check_ai_boundaries.sh" "$repo/scripts/collect_changes.py"
}

init_repo() {
  local repo="$1"

  git init -q "$repo"
  git -C "$repo" config user.email "harness-test@example.com"
  git -C "$repo" config user.name "Harness Test"
  install_checker "$repo"
}

write_standard_policy() {
  local repo="$1"

  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - docs/
  - README.md
approval_required:
  - .ai-boundaries.yml
  - scripts/
  - internal/risk/
forbidden:
  - secrets/
POLICY
}

commit_all() {
  local repo="$1"
  local message="$2"

  git -C "$repo" add -A
  git -C "$repo" commit -q -m "$message"
}

run_check() {
  local repo="$1"
  local base="$2"
  local artifact_dir="$3"
  local approved="${4:-0}"

  AI_BOUNDARY_APPROVED="$approved" \
    AI_BOUNDARY_APPROVAL_EVIDENCE="$([[ "$approved" == "1" ]] && printf 'owner-request:test' || true)" \
    AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    AI_BOUNDARY_MODE=change \
    VERIFY_COMPARE_REF="$base" \
    "$repo/scripts/check_ai_boundaries.sh"
}

test_approval_signal_requires_evidence() {
  local repo="$TMP_DIR/approval-evidence"
  local artifact_dir="$TMP_DIR/artifacts/approval-evidence"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"
  printf 'changed\n' >>"$repo/scripts/check_ai_boundaries.sh"

  if AI_BOUNDARY_APPROVED=1 AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    AI_BOUNDARY_MODE=change VERIFY_COMPARE_REF="$base" \
    "$repo/scripts/check_ai_boundaries.sh"; then
    fail "approval signal without evidence passed"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" \
    "AI_BOUNDARY_APPROVAL_EVIDENCE"
}

assert_error_contains() {
  local artifact="$1"
  local expected="$2"

  python3 - "$artifact" "$expected" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
errors = payload.get("errors", [])
assert any(sys.argv[2] in error for error in errors), errors
PY
}

assert_unevaluated_change() {
  local artifact="$1"
  local expected_path="$2"
  local expected_source="$3"

  python3 - "$artifact" "$expected_path" "$expected_source" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
record = next(item for item in payload["changes"] if item["path"] == sys.argv[2])
assert record["sources"] == [sys.argv[3]], record
assert record["evaluation_status"] == "not_evaluated", record
assert record["trusted_classification"] is None, record
assert record["candidate_classification"] is None, record
assert record["effective_classification"] is None, record
PY
}

test_allowed_path_passes() {
  local repo="$TMP_DIR/allowed"
  local artifact_dir="$TMP_DIR/artifacts/allowed"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/docs"
  printf 'allowed\n' >"$repo/docs/guide.md"
  commit_all "$repo" "change allowed path"

  run_check "$repo" "$base" "$artifact_dir" || fail "allowed path was rejected"
  python3 - "$artifact_dir/ai_boundaries.json" "$base" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["status"] == "passed", payload
assert payload["snapshot"]["base_sha"] == sys.argv[2]
assert payload["classifications"]["allowed"] == ["docs/guide.md"]
assert payload["errors"] == []
PY
}

test_approval_required_needs_explicit_approval() {
  local repo="$TMP_DIR/approval"
  local artifact_dir="$TMP_DIR/artifacts/approval"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  mkdir -p "$repo/internal/risk"
  printf 'base\n' >"$repo/internal/risk/limit.go"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  printf 'changed\n' >>"$repo/internal/risk/limit.go"
  commit_all "$repo" "change approval path"

  if run_check "$repo" "$base" "$artifact_dir"; then
    fail "approval-required path passed without approval"
  fi
  run_check "$repo" "$base" "$artifact_dir" 1 || fail "approved path was rejected"
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["classifications"]["approval_required"] == ["internal/risk/limit.go"]
assert payload["approved"] is True
PY
}

test_forbidden_path_always_fails() {
  local repo="$TMP_DIR/forbidden"
  local artifact_dir="$TMP_DIR/artifacts/forbidden"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/secrets"
  printf 'secret\n' >"$repo/secrets/key.txt"
  commit_all "$repo" "change forbidden path"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "forbidden path passed with approval"
  fi
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["classifications"]["forbidden"] == ["secrets/key.txt"]
assert payload["status"] == "failed"
PY
}

test_basename_glob_classification_and_validation() {
  local repo="$TMP_DIR/basename-glob"
  local artifact_dir="$TMP_DIR/artifacts/basename-glob"
  local base

  init_repo "$repo"
  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - certs/
approval_required:
  - "*.key"
forbidden:
  - "*.pem"
POLICY
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/certs" "$repo/nested/deeper"
  printf 'certificate\n' >"$repo/certs/server.pem"
  printf 'private key\n' >"$repo/nested/deeper/service.key"
  commit_all "$repo" "change glob-matched files"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "forbidden basename glob passed with approval"
  fi
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["classifications"]["forbidden"] == ["certs/server.pem"], payload
assert payload["classifications"]["approval_required"] == [
    "nested/deeper/service.key"
], payload
record = next(item for item in payload["changes"] if item["path"] == "certs/server.pem")
assert record["candidate_classification"] == "forbidden", record
assert record["trusted_classification"] == "forbidden", record
assert record["effective_classification"] == "forbidden", record
PY

  python3 -I -B -S - "$repo/scripts/check_ai_boundaries.py" <<'PY'
import importlib.util
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("boundary_checker", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for pattern in ("?", "[ab].pem", "dir/*.pem", "*", "**"):
    raw = (
        "allowed:\n"
        "  - docs/\n"
        "approval_required:\n"
        f"  - {pattern}\n"
        "forbidden:\n"
        "  - secrets/\n"
    ).encode()
    try:
        module.parse_policy(raw, "test policy")
    except module.BoundaryError as error:
        assert "basename glob" in str(error), (pattern, error)
    else:
        raise AssertionError(f"unsupported pattern was accepted: {pattern}")
PY
}

test_unclassified_path_fails_closed() {
  local repo="$TMP_DIR/unclassified"
  local artifact_dir="$TMP_DIR/artifacts/unclassified"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/misc"
  printf 'unknown\n' >"$repo/misc/unknown.txt"
  commit_all "$repo" "change unclassified path"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "unclassified path passed"
  fi
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["classifications"]["unclassified"] == ["misc/unknown.txt"]
assert payload["status"] == "failed"
PY
}

test_missing_candidate_policy_fails_with_artifact() {
  local repo="$TMP_DIR/missing-candidate"
  local artifact_dir="$TMP_DIR/artifacts/missing-candidate"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  rm "$repo/.ai-boundaries.yml"
  commit_all "$repo" "delete candidate policy"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "missing candidate policy passed"
  fi
  [[ -f "$artifact_dir/ai_boundaries.json" ]] || fail "missing-policy failure wrote no artifact"
  assert_error_contains "$artifact_dir/ai_boundaries.json" "candidate policy"
  assert_unevaluated_change \
    "$artifact_dir/ai_boundaries.json" ".ai-boundaries.yml" "committed"
}

test_missing_trusted_policy_fails() {
  local repo="$TMP_DIR/missing-trusted"
  local artifact_dir="$TMP_DIR/artifacts/missing-trusted"
  local base

  init_repo "$repo"
  printf 'base\n' >"$repo/README.md"
  commit_all "$repo" "base without policy"
  base="$(git -C "$repo" rev-parse HEAD)"

  write_standard_policy "$repo"
  commit_all "$repo" "add candidate policy"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "missing trusted policy passed"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "trusted policy"
}

test_malformed_policy_fails() {
  local repo="$TMP_DIR/malformed"
  local artifact_dir="$TMP_DIR/artifacts/malformed"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - docs/
  - docs/
approval_required:
  - scripts/
forbidden:
  - secrets/
POLICY
  commit_all "$repo" "malform candidate policy"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "malformed policy passed"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "duplicate"
  assert_unevaluated_change \
    "$artifact_dir/ai_boundaries.json" ".ai-boundaries.yml" "committed"

  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - docs/
approval_required:
  - scripts/
POLICY
  commit_all "$repo" "remove required policy section"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "policy missing a required section passed"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "forbidden"
  assert_unevaluated_change \
    "$artifact_dir/ai_boundaries.json" ".ai-boundaries.yml" "committed"
}

test_candidate_collector_cannot_hide_its_own_change() {
  local repo="$TMP_DIR/collector-trust"
  local artifact_dir="$TMP_DIR/artifacts/collector-trust"
  local marker="$repo/.malicious-collector-ran"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  cat >"$repo/scripts/collect_changes.py" <<'PY'
#!/usr/bin/env python3
import argparse
import json
import pathlib
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True, type=pathlib.Path)
parser.add_argument("--base", required=True)
parser.add_argument("--mode", required=True)
parser.add_argument("--format", required=True)
args = parser.parse_args()

def git(*command):
    return subprocess.check_output(
        ["git", "-C", str(args.repo), *command], text=True
    ).strip()

base_sha = git("rev-parse", "--verify", f"{args.base}^{{commit}}")
head_sha = git("rev-parse", "--verify", "HEAD^{commit}")
clean = not subprocess.check_output(
    ["git", "-C", str(args.repo), "status", "--porcelain=v1", "-z"]
)
(args.repo / ".malicious-collector-ran").write_text("executed\n", encoding="utf-8")
payload = {
    "mode": args.mode,
    "head_sha": head_sha,
    "head_tree_sha": git("rev-parse", "--verify", f"{head_sha}^{{tree}}"),
    "base_sha": base_sha,
    "merge_base_sha": git("merge-base", base_sha, head_sha),
    "clean": clean,
    "changes": [],
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
PY
  commit_all "$repo" "hide collector self-change"

  if run_check "$repo" "$base" "$artifact_dir"; then
    fail "unapproved candidate collector change passed"
  fi
  [[ ! -e "$marker" ]] || fail "unapproved candidate collector was executed"
  assert_error_contains "$artifact_dir/ai_boundaries.json" "collector runtime"
  assert_unevaluated_change \
    "$artifact_dir/ai_boundaries.json" "scripts/collect_changes.py" "committed"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "approved but disagreeing candidate collector passed"
  fi
  [[ -f "$marker" ]] || fail "approved candidate collector was not exercised"
  assert_error_contains \
    "$artifact_dir/ai_boundaries.json" "candidate collector change scope disagrees"
}

test_candidate_collector_cannot_load_unbound_adjacent_helper() {
  local repo="$TMP_DIR/collector-unbound-runtime"
  local artifact_dir="$TMP_DIR/artifacts/collector-unbound-runtime"
  local marker="$TMP_DIR/unbound-helper-loaded"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  python3 - "$repo/scripts/collect_changes.py" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
needle = "from __future__ import annotations\n"
assert source.count(needle) == 1, source
path.write_text(
    source.replace(needle, needle + "\nimport adjacent_helper\n"),
    encoding="utf-8",
)
PY
  printf 'scripts/adjacent_helper.py\n' >>"$repo/.git/info/exclude"
  cat >"$repo/scripts/adjacent_helper.py" <<'PY'
import pathlib

(pathlib.Path(__file__).resolve().parents[2] / "unbound-helper-loaded").write_text(
    "loaded\n",
    encoding="utf-8",
)
PY
  commit_all "$repo" "make collector depend on ignored adjacent helper"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "candidate collector loaded unbound adjacent helper"
  fi
  [[ ! -e "$marker" ]] || fail "unbound adjacent helper was loaded"
  assert_error_contains "$artifact_dir/ai_boundaries.json" "adjacent_helper"
  python3 - \
    "$artifact_dir/ai_boundaries.json" \
    "$repo/scripts/collect_changes.py" \
    "$repo/scripts/lib/change_scope.py" <<'PY'
import hashlib
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
candidate = payload["change_scope"]["candidate"]
assert candidate["collector_sha256"] == hashlib.sha256(
    pathlib.Path(sys.argv[2]).read_bytes()
).hexdigest(), candidate
assert candidate["library_sha256"] == hashlib.sha256(
    pathlib.Path(sys.argv[3]).read_bytes()
).hexdigest(), candidate
PY
}

test_first_collector_introduction_uses_builtin_fallback() {
  local repo="$TMP_DIR/collector-first-introduction"
  local artifact_dir="$TMP_DIR/artifacts/collector-first-introduction"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  rm "$repo/scripts/collect_changes.py" "$repo/scripts/lib/change_scope.py"
  commit_all "$repo" "base before canonical collector"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/scripts/lib"
  cp "$ROOT_DIR/scripts/collect_changes.py" "$repo/scripts/collect_changes.py"
  cp "$ROOT_DIR/scripts/lib/change_scope.py" "$repo/scripts/lib/change_scope.py"
  commit_all "$repo" "introduce canonical collector"

  run_check "$repo" "$base" "$artifact_dir" 1 || \
    fail "first collector introduction did not use the builtin fallback"
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["change_scope"]["strategy"] == "builtin_fallback_candidate_union", payload
assert payload["change_scope"]["trusted"]["source"] == "builtin_git_fallback", payload
assert payload["change_scope"]["agreement"] is True, payload
assert [item["path"] for item in payload["changes"]] == [
    "scripts/collect_changes.py",
    "scripts/lib/change_scope.py",
]
PY
}

test_candidate_policy_must_be_regular_file() {
  local repo="$TMP_DIR/policy-symlink"
  local artifact_dir="$TMP_DIR/artifacts/policy-symlink"
  local external_policy="$TMP_DIR/external-policy.yml"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  cp "$repo/.ai-boundaries.yml" "$external_policy"
  rm "$repo/.ai-boundaries.yml"
  ln -s "$external_policy" "$repo/.ai-boundaries.yml"
  commit_all "$repo" "replace candidate policy with symlink"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "candidate policy symlink was followed"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "regular file"
  assert_unevaluated_change \
    "$artifact_dir/ai_boundaries.json" ".ai-boundaries.yml" "committed"
}

test_invalid_base_fails_without_fallback() {
  local repo="$TMP_DIR/invalid-base"
  local artifact_dir="$TMP_DIR/artifacts/invalid-base"

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"

  if run_check "$repo" "refs/heads/does-not-exist" "$artifact_dir" 1; then
    fail "invalid base fell back to local changes"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "does-not-exist"
}

test_trusted_policy_prevents_self_weakening() {
  local repo="$TMP_DIR/self-weakening"
  local artifact_dir="$TMP_DIR/artifacts/self-weakening"
  local base

  init_repo "$repo"
  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - docs/
approval_required:
  - .ai-boundaries.yml
  - scripts/
forbidden:
  - guarded/
POLICY
  mkdir -p "$repo/guarded"
  printf 'base\n' >"$repo/guarded/value.txt"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - docs/
  - guarded/
approval_required:
  - .ai-boundaries.yml
  - scripts/
forbidden:
  - blocked/
POLICY
  printf 'changed\n' >>"$repo/guarded/value.txt"
  commit_all "$repo" "weaken policy"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "candidate policy weakened trusted forbidden classification"
  fi
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
record = next(item for item in payload["changes"] if item["path"] == "guarded/value.txt")
assert record["trusted_classification"] == "forbidden", record
assert record["candidate_classification"] == "allowed", record
assert record["effective_classification"] == "forbidden", record
assert payload["policy"]["trusted"]["sha256"] != payload["policy"]["candidate"]["sha256"]
PY
}

test_bootstrap_paths_are_never_freely_allowed() {
  local repo="$TMP_DIR/bootstrap"
  local artifact_dir="$TMP_DIR/artifacts/bootstrap"
  local base

  init_repo "$repo"
  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - .ai-boundaries.yml
  - docs/
approval_required:
  - internal/risk/
forbidden:
  - secrets/
POLICY
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - .ai-boundaries.yml
  - docs/
  - README.md
approval_required:
  - internal/risk/
forbidden:
  - secrets/
POLICY
  commit_all "$repo" "change bootstrap policy"

  if run_check "$repo" "$base" "$artifact_dir"; then
    fail "bootstrap policy path passed without approval"
  fi
  run_check "$repo" "$base" "$artifact_dir" 1 || fail "approved bootstrap path was rejected"
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
record = next(item for item in payload["changes"] if item["path"] == ".ai-boundaries.yml")
assert record["trusted_classification"] == "allowed", record
assert record["candidate_classification"] == "allowed", record
assert record["bootstrap_classification"] == "approval_required", record
assert record["effective_classification"] == "approval_required", record
PY
}

test_exact_file_and_directory_prefix_matching() {
  local repo="$TMP_DIR/matching"
  local artifact_dir="$TMP_DIR/artifacts/matching"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  printf 'base\n' >"$repo/README.md"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  printf 'changed\n' >>"$repo/README.md"
  printf 'not exact\n' >"$repo/README.md.bak"
  mkdir -p "$repo/docs" "$repo/docsish"
  printf 'prefix\n' >"$repo/docs/guide.md"
  printf 'not prefix\n' >"$repo/docsish/guide.md"
  commit_all "$repo" "exercise matching"

  if run_check "$repo" "$base" "$artifact_dir" 1; then
    fail "near-match paths were treated as classified"
  fi
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["classifications"]["allowed"] == ["README.md", "docs/guide.md"]
assert payload["classifications"]["unclassified"] == ["README.md.bak", "docsish/guide.md"]
PY
}

test_rename_cannot_escape_protected_source() {
  local repo="$TMP_DIR/rename"
  local artifact_dir="$TMP_DIR/artifacts/rename"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  mkdir -p "$repo/internal/risk"
  printf 'protected\n' >"$repo/internal/risk/limit.txt"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/docs"
  git -C "$repo" mv internal/risk/limit.txt docs/limit.txt
  commit_all "$repo" "move protected path"

  if run_check "$repo" "$base" "$artifact_dir"; then
    fail "protected rename source escaped approval"
  fi
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
records = {item["path"]: item for item in payload["changes"]}
assert records["docs/limit.txt"]["effective_classification"] == "allowed"
assert records["internal/risk/limit.txt"]["effective_classification"] == "approval_required"
assert records["internal/risk/limit.txt"]["sources"] == ["committed"]
PY
}

test_quotes_and_spaces_produce_valid_deterministic_json() {
  local repo="$TMP_DIR/special-name"
  local artifact_dir="$TMP_DIR/artifacts/special-name"
  local first="$TMP_DIR/special-name-first.json"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/docs"
  printf 'special\n' >"$repo/docs/quote \"space\".md"
  commit_all "$repo" "add special filename"

  run_check "$repo" "$base" "$artifact_dir" || fail "special filename was rejected"
  cp "$artifact_dir/ai_boundaries.json" "$first"
  run_check "$repo" "$base" "$artifact_dir" || fail "repeat special filename check failed"
  cmp -s "$first" "$artifact_dir/ai_boundaries.json" || fail "boundary JSON was not deterministic"

  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

raw = pathlib.Path(sys.argv[1]).read_bytes()
payload = json.loads(raw)
assert payload["classifications"]["allowed"] == ['docs/quote "space".md']
assert raw.endswith(b"\n")
assert len(payload["policy"]["trusted"]["sha256"]) == 64
assert len(payload["policy"]["candidate"]["sha256"]) == 64
PY
}

test_one_policy_classification_is_sufficient() {
  local repo="$TMP_DIR/one-policy"
  local artifact_dir="$TMP_DIR/artifacts/one-policy"
  local base

  init_repo "$repo"
  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - legacy.txt
approval_required:
  - .ai-boundaries.yml
  - scripts/
forbidden:
  - secrets/
POLICY
  printf 'base\n' >"$repo/legacy.txt"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  cat >"$repo/.ai-boundaries.yml" <<'POLICY'
allowed:
  - modern.txt
approval_required:
  - .ai-boundaries.yml
  - scripts/
forbidden:
  - secrets/
POLICY
  printf 'changed\n' >>"$repo/legacy.txt"
  commit_all "$repo" "change one-sided classification"

  run_check "$repo" "$base" "$artifact_dir" 1 || fail "trusted-only classification was rejected"
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
record = next(item for item in payload["changes"] if item["path"] == "legacy.txt")
assert record["trusted_classification"] == "allowed", record
assert record["candidate_classification"] == "unclassified", record
assert record["effective_classification"] == "allowed", record
PY
}

test_default_mode_writes_change_artifact() {
  local repo="$TMP_DIR/default-mode"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"

  mkdir -p "$repo/docs"
  printf 'change\n' >"$repo/docs/default-mode.md"
  commit_all "$repo" "exercise default mode"

  AI_BOUNDARY_APPROVED=1 \
    AI_BOUNDARY_APPROVAL_EVIDENCE=owner-request:test \
    VERIFY_COMPARE_REF="$base" \
    "$repo/scripts/check_ai_boundaries.sh" || fail "default-mode boundary check failed"

  [[ -f "$repo/.artifacts/change/ai_boundaries.json" ]] || \
    fail "default boundary artifact was not written under .artifacts/change"
  python3 - "$repo/.artifacts/change/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["mode"] == "change", payload
PY
}

test_repository_policy_covers_all_tracked_paths() {
  python3 - "$ROOT_DIR" <<'PY'
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
sections = {"allowed": [], "approval_required": [], "forbidden": []}
current = None
for raw_line in (root / ".ai-boundaries.yml").read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if not raw_line[:1].isspace() and line.endswith(":"):
        current = line[:-1]
        continue
    if line.startswith("- ") and current in sections:
        sections[current].append(line[2:].strip())

entries = [entry for values in sections.values() for entry in values]
tracked = subprocess.check_output(
    ["git", "-C", str(root), "ls-files", "-z"]
).split(b"\0")
paths = [item.decode() for item in tracked if item]

def matches(path, entry):
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry

unclassified = [
    path for path in paths
    if not any(matches(path, entry) for entry in entries)
]
assert not unclassified, unclassified
PY
}

test_shell_entrypoint_is_compatibility_wrapper() {
  python3 - "$ROOT_DIR/scripts/check_ai_boundaries.sh" <<'PY'
import pathlib
import sys

actual = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
assert "HARNESS_ENGINE_DIR" in actual, actual
assert "HARNESS_PROJECT_ROOT" in actual, actual
assert 'exec python3 -I -B -S "$ENGINE_DIR/check_ai_boundaries.py" "$@"' in actual, actual
PY
}

snapshot_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

test_sealed_snapshot_must_match_independent_scope() {
  local repo="$TMP_DIR/sealed-snapshot"
  local artifact_dir="$TMP_DIR/artifacts/sealed-snapshot"
  local snapshot="$repo/.artifacts/input/change_scope.json"
  local base
  local digest

  init_repo "$repo"
  write_standard_policy "$repo"
  printf '.artifacts/\n' >"$repo/.gitignore"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"
  mkdir -p "$repo/docs"
  printf 'change\n' >"$repo/docs/sealed.md"
  commit_all "$repo" "candidate"

  mkdir -p "$(dirname "$snapshot")"
  python3 "$ROOT_DIR/scripts/collect_changes.py" \
    --repo "$repo" --base "$base" --mode change --format json >"$snapshot"
  digest="$(snapshot_sha256 "$snapshot")"

  AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    python3 "$repo/scripts/check_ai_boundaries.py" \
      --repo "$repo" --base "$base" --mode change \
      --snapshot-file .artifacts/input/change_scope.json --snapshot-sha256 "$digest" || \
    fail "boundary checker rejected an exact sealed snapshot"

  printf '\n' >>"$snapshot"
  if AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    python3 "$repo/scripts/check_ai_boundaries.py" \
      --repo "$repo" --base "$base" --mode change \
      --snapshot-file .artifacts/input/change_scope.json --snapshot-sha256 "$digest"; then
    fail "boundary checker accepted a snapshot with a stale digest"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "digest"

  python3 "$ROOT_DIR/scripts/collect_changes.py" \
    --repo "$repo" --base "$base" --mode change --format json >"$snapshot"
  python3 - "$snapshot" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["changes"] = []
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
  digest="$(snapshot_sha256 "$snapshot")"
  if AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    python3 "$repo/scripts/check_ai_boundaries.py" \
      --repo "$repo" --base "$base" --mode change \
      --snapshot-file .artifacts/input/change_scope.json --snapshot-sha256 "$digest"; then
    fail "boundary checker accepted a sealed scope disagreement"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" \
    "sealed change snapshot change scope disagrees"

  rm -f "$snapshot"
  ln -s "$repo/docs/sealed.md" "$snapshot"
  digest="$(snapshot_sha256 "$snapshot")"
  if AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    python3 "$repo/scripts/check_ai_boundaries.py" \
      --repo "$repo" --base "$base" --mode change \
      --snapshot-file .artifacts/input/change_scope.json --snapshot-sha256 "$digest"; then
    fail "boundary checker accepted a symlinked sealed snapshot"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" "must not be a symlink"
}

test_deferred_approval_preserves_fail_closed_classification() {
  local repo="$TMP_DIR/deferred-approval"
  local artifact_dir="$TMP_DIR/artifacts/deferred-approval"
  local base

  init_repo "$repo"
  write_standard_policy "$repo"
  commit_all "$repo" "base"
  base="$(git -C "$repo" rev-parse HEAD)"
  mkdir -p "$repo/internal/risk"
  printf 'package risk\n' >"$repo/internal/risk/deferred.go"
  commit_all "$repo" "approval-required candidate"

  AI_BOUNDARY_APPROVAL_MODE=deferred \
    AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    AI_BOUNDARY_MODE=release \
    VERIFY_COMPARE_REF="$base" \
    "$repo/scripts/check_ai_boundaries.sh" || \
    fail "deferred candidate classification rejected approval-required paths"
  python3 - "$artifact_dir/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["status"] == "passed", payload
assert payload["approval_mode"] == "deferred", payload
assert payload["approved"] is False, payload
assert payload["approval_satisfied"] is False, payload
assert payload["classifications"]["approval_required"] == [
    "internal/risk/deferred.go"
], payload
PY

  mkdir -p "$repo/secrets"
  printf 'secret\n' >"$repo/secrets/key.txt"
  commit_all "$repo" "forbidden candidate"
  if AI_BOUNDARY_APPROVAL_MODE=deferred \
    AI_BOUNDARY_ARTIFACT_DIR="$artifact_dir" \
    AI_BOUNDARY_MODE=release \
    VERIFY_COMPARE_REF="$base" \
    "$repo/scripts/check_ai_boundaries.sh"; then
    fail "deferred approval allowed a forbidden path"
  fi
  assert_error_contains "$artifact_dir/ai_boundaries.json" \
    "forbidden AI boundary paths changed"
}

test_missing_candidate_policy_fails_with_artifact
test_approval_signal_requires_evidence
test_unclassified_path_fails_closed
test_candidate_policy_must_be_regular_file
test_candidate_collector_cannot_hide_its_own_change
test_candidate_collector_cannot_load_unbound_adjacent_helper
test_first_collector_introduction_uses_builtin_fallback
test_missing_trusted_policy_fails
test_malformed_policy_fails
test_invalid_base_fails_without_fallback
test_trusted_policy_prevents_self_weakening
test_allowed_path_passes
test_approval_required_needs_explicit_approval
test_forbidden_path_always_fails
test_basename_glob_classification_and_validation
test_bootstrap_paths_are_never_freely_allowed
test_exact_file_and_directory_prefix_matching
test_rename_cannot_escape_protected_source
test_quotes_and_spaces_produce_valid_deterministic_json
test_one_policy_classification_is_sufficient
test_default_mode_writes_change_artifact
test_repository_policy_covers_all_tracked_paths
test_shell_entrypoint_is_compatibility_wrapper
test_sealed_snapshot_must_match_independent_scope
test_deferred_approval_preserves_fail_closed_classification

printf 'check_ai_boundaries tests passed\n'
