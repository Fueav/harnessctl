#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHECKER="$ROOT_DIR/scripts/check_spec_registry.py"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

write_manifest() {
  local repo="$1"
  mkdir -p "$repo/docs"
  cat >"$repo/docs/harness-workflows.json" <<'JSON'
{
  "version": 2,
  "workflow_classes": [
    {"id":"HARNESS-FOCUSED-CHANGE","use_when":"bounded","artifact_policy":"checklist","verification":"focused checks","stop_rule":"target met","evidence":["test output"]},
    {"id":"HARNESS-SPEC-FIRST-FEATURE","use_when":"new semantics","artifact_policy":"spec required","verification":"release gate","stop_rule":"approved evidence","evidence":["spec"]},
    {"id":"HARNESS-VERIFICATION-INCIDENT","use_when":"diagnosis","artifact_policy":"evidence only","verification":"reproduce","stop_rule":"cause known","evidence":["diagnostics"]},
    {"id":"HARNESS-MAINTENANCE","use_when":"framework upkeep","artifact_policy":"checklist","verification":"focused checks","stop_rule":"budget met","evidence":["diff"]}
  ]
}
JSON
}

write_spec() {
  local repo="$1" module="$2" workflow="${3:-HARNESS-SPEC-FIRST-FEATURE}"
  local module_upper
  module_upper="$(printf '%s' "$module" | tr '[:lower:]' '[:upper:]')"
  mkdir -p "$repo/specs/$module"
  cat >"$repo/specs/$module/spec.md" <<EOF
---
spec_id: SPEC-${module_upper}-001
module: $module
status: approved
workflow_class: $workflow
---

# $module

## Specification

Contract.

## Implementation Plan

Test first.

## Closeout Evidence

Pending.
EOF
}

write_registry() {
  local repo="$1"
  shift
  mkdir -p "$repo/specs"
  python3 - "$repo/specs/index.json" "$@" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {"version": 1, "specs": [f"specs/{name}/spec.md" for name in sys.argv[2:]]}
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

setup_repo() {
  local name="$1"
  REPO="$TMP_DIR/$name"
  git init -q -b main "$REPO"
  git -C "$REPO" config user.email harness-test@example.com
  git -C "$REPO" config user.name "Harness Test"
  write_manifest "$REPO"
  write_spec "$REPO" alpha
  write_registry "$REPO" alpha
  mkdir -p "$REPO/specs/retired_empty"
  mkdir -p "$REPO/docs" "$REPO/.artifacts" "$REPO/harness"
  printf '# Repository instructions\n' >"$REPO/AGENTS.md"
  printf '# Workflows\n\nExisting line.\n' >"$REPO/docs/harness-workflows.md"
  printf '{}\n' >"$REPO/harness/harness_profiles.json"
  git -C "$REPO" add .
  git -C "$REPO" commit -qm baseline
}

run_check() {
  python3 -I -B -S "$CHECKER" \
    --repo "$REPO" \
    --compare-ref HEAD \
    --artifact-dir "$REPO/.artifacts"
}

expect_failure() {
  local message="$1" expected="$2"
  if run_check >"$TMP_DIR/out" 2>"$TMP_DIR/err"; then
    fail "$message"
  fi
  grep -Fq "$expected" "$TMP_DIR/err" || {
    cat "$TMP_DIR/err" >&2
    fail "failure did not mention: $expected"
  }
}

setup_repo valid
run_check || fail "valid compact registry was rejected"
python3 - "$REPO/.artifacts/spec_registry.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["status"] == "passed", payload
assert payload["workflow_classes"] == [
    "HARNESS-FOCUSED-CHANGE",
    "HARNESS-MAINTENANCE",
    "HARNESS-SPEC-FIRST-FEATURE",
    "HARNESS-VERIFICATION-INCIDENT",
], payload
assert payload["specs"][0]["path"] == "specs/alpha/spec.md", payload
PY

setup_repo invalid_workflow
sed -i.bak 's/HARNESS-SPEC-FIRST-FEATURE/HARNESS-NOT-REAL/' \
  "$REPO/specs/alpha/spec.md"
rm "$REPO/specs/alpha/spec.md.bak"
expect_failure "unknown workflow class passed" "unknown workflow_class"

setup_repo registry_drift
write_spec "$REPO" beta
expect_failure "unregistered spec passed" "registry and filesystem differ"

setup_repo harness_budget
printf '# Workflows\n\nExisting line.\nAdded line.\n' \
  >"$REPO/docs/harness-workflows.md"
expect_failure "net-positive Harness change passed" "Harness line budget exceeded"
printf '# Workflows\n\nReplacement line.\n' >"$REPO/docs/harness-workflows.md"
run_check || fail "line-neutral Harness refactor was rejected"

setup_repo release_runtime_budget
mkdir -p "$REPO/scripts"
printf 'print("new release logic")\n' >"$REPO/scripts/finalize_approval.py"
expect_failure "net-positive release runtime change passed" "Harness line budget exceeded"

setup_repo agents_absolute_budget
python3 - "$REPO/AGENTS.md" <<'PY'
import pathlib
import sys
pathlib.Path(sys.argv[1]).write_text("rule\n" * 121, encoding="utf-8")
PY
git -C "$REPO" add AGENTS.md
git -C "$REPO" commit -qm "oversize agents"
expect_failure "oversized AGENTS.md passed" "AGENTS.md exceeds 120 lines"

setup_repo manifest_absolute_budget
python3 - "$REPO/docs/harness-workflows.json" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
path.write_text(path.read_text(encoding="utf-8") + "\n" * 200, encoding="utf-8")
PY
git -C "$REPO" add docs/harness-workflows.json
git -C "$REPO" commit -qm "oversize manifest"
expect_failure "oversized workflow manifest passed" \
  "docs/harness-workflows.json exceeds 200 lines"

setup_repo skill_absolute_budget
mkdir -p "$REPO/.agents/skills/large"
python3 - "$REPO/.agents/skills/large/SKILL.md" <<'PY'
import pathlib
import sys
pathlib.Path(sys.argv[1]).write_text("instruction\n" * 61, encoding="utf-8")
PY
git -C "$REPO" add .agents/skills/large/SKILL.md
git -C "$REPO" commit -qm "oversize skill"
expect_failure "oversized SKILL.md passed" ".agents/skills/large/SKILL.md exceeds 60 lines"

printf 'check_spec_registry tests passed\n'
