#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHECKER="$ROOT_DIR/scripts/check_spec_registry.py"
source "$ROOT_DIR/scripts/lib/safe_cleanup.sh"
TMP_DIR="$(mktemp -d)"
TMP_NAME="${TMP_DIR##*/}"
trap 'safe_remove_tree "$TMP_DIR" "$(dirname "$TMP_DIR")" "$TMP_NAME"' EXIT

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

write_manifest() {
  local repo="$1"
  mkdir -p "$repo/docs"
  cat >"$repo/docs/harness-workflows.json" <<'JSON'
{
  "version": 3,
  "workflow_classes": [
    "HARNESS-FOCUSED-CHANGE",
    "HARNESS-MAINTENANCE",
    "HARNESS-SPEC-FIRST-FEATURE",
    "HARNESS-VERIFICATION-INCIDENT"
  ]
}
JSON
}

write_legacy_manifest() {
  local repo="$1"
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
run_check || fail "valid version 3 registry was rejected"
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

setup_repo legacy_v2
write_legacy_manifest "$REPO"
git -C "$REPO" add docs/harness-workflows.json
git -C "$REPO" commit -qm "use legacy workflow manifest"
run_check || fail "valid legacy version 2 manifest was rejected"

setup_repo duplicate_v3
python3 - "$REPO/docs/harness-workflows.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["workflow_classes"].append("HARNESS-FOCUSED-CHANGE")
path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
PY
expect_failure "duplicate version 3 workflow passed" "duplicate workflow id"

setup_repo object_in_v3
write_legacy_manifest "$REPO"
python3 - "$REPO/docs/harness-workflows.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["version"] = 3
path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
PY
expect_failure "semantic objects in version 3 passed" "must be a non-empty workflow id"

setup_repo unsupported_version
python3 - "$REPO/docs/harness-workflows.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["version"] = 4
path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
PY
expect_failure "unsupported workflow manifest version passed" "version must be 2 or 3"

setup_repo missing_v3
python3 - "$REPO/docs/harness-workflows.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["workflow_classes"].pop()
path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
PY
expect_failure "incomplete version 3 workflow set passed" "exactly the four core workflows"

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

write_delivery_record() {
  python3 - "$REPO" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
manifest = {"schema_version": 1, "managed_paths": [{"path": "AGENTS.md", "strategy": "copy"}], "retired_paths": []}
raw = (json.dumps(manifest) + "\n").encode()
(root / "harness/scaffold_manifest.json").write_bytes(raw)
lock = {"schema_version": 1, "template_commit": "a" * 40, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "resolved_paths": []}
(root / "harness/scaffold.lock").write_text(json.dumps(lock) + "\n")
PY
}

setup_repo initial_delivery
write_delivery_record
printf 'new repository module\n' >"$REPO/harness/delivery_module.py"
expect_failure "bootstrap was inferred without an explicit delivery request" "Harness line budget exceeded"
(
  export HARNESS_SCAFFOLD_DELIVERY=bootstrap AI_BOUNDARY_APPROVED=0
  export AI_BOUNDARY_APPROVAL_EVIDENCE=owner-request:bootstrap-fixture
  expect_failure "unapproved bootstrap bypassed the line budget" "Harness line budget exceeded"
  export AI_BOUNDARY_APPROVED=1 AI_BOUNDARY_APPROVAL_EVIDENCE=
  expect_failure "bootstrap without approval evidence passed" "Harness line budget exceeded"
  export AI_BOUNDARY_APPROVAL_EVIDENCE=owner-request:bootstrap-fixture
  run_check || fail "approved first template delivery was rejected"
  python3 - "$REPO/.artifacts/spec_registry.json" <<'PY'
import json, pathlib, sys
report = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert report["harness_line_budget"]["added"] > report["harness_line_budget"]["deleted"]
assert report["initial_scaffold_delivery"]["template_commit"] == "a" * 40
assert report["initial_scaffold_delivery"]["approval_evidence"] == "owner-request:bootstrap-fixture"
PY
  printf 'tampered\n' >>"$REPO/harness/scaffold_manifest.json"
  expect_failure "stale delivery record bypassed the line budget" "Harness line budget exceeded"
  write_delivery_record
  python3 - "$REPO/AGENTS.md" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("rule\n" * 121)
PY
  expect_failure "bootstrap bypassed absolute prompt limits" "AGENTS.md exceeds 120 lines"
  git -C "$REPO" checkout -- AGENTS.md
  git -C "$REPO" add harness
  git -C "$REPO" commit -qm "first delivery"
  printf 'later maintenance\n' >>"$REPO/harness/delivery_module.py"
  expect_failure "bootstrap flag bypassed subsequent maintenance budgets" "Harness line budget exceeded"
)

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
