#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  if [[ "${KEEP_VERIFY_RELEASE_TEST_TMP:-0}" == "1" ]]; then
    printf 'verify release test fixtures kept at %s\n' "$TMP_DIR" >&2
  else
    rm -rf "$TMP_DIR"
  fi
}

trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_failed_summary() {
  local path="$1"
  local expected="$2"
  python3 - "$path" "$expected" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["overall"] == "failed", payload
assert payload["release_ready"] is False, payload
assert any(sys.argv[2] in reason for reason in payload["failure_reasons"]), payload
PY
}

write_fake_tools() {
  local repo="$1"
  local tools="$repo/.tools/bin"
  mkdir -p "$tools"

  cat >"$tools/go" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${FAKE_MUTATE_HEAD:-0}" == "1" && -n "${FAKE_REPO_ROOT:-}" ]]; then
  mkdir -p "$FAKE_REPO_ROOT/.artifacts"
  if mkdir "$FAKE_REPO_ROOT/.artifacts/fake-head-mutated" 2>/dev/null; then
    git -C "$FAKE_REPO_ROOT" commit -q --allow-empty -m "mutate head during release"
  fi
fi

if [[ "${1:-}" == "tool" && "${2:-}" == "cover" ]]; then
  printf 'total:\t(statements)\t100.0%%\n'
  exit 0
fi

if [[ "${FAKE_REQUIRE_CONCURRENCY:-0}" == "1" && \
  ("${1:-}" == "build" || "${1:-}" == "vet") ]]; then
  sync_dir="$FAKE_REPO_ROOT/.artifacts/fake-parallel"
  mkdir -p "$sync_dir"
  : >"$sync_dir/${1:-unknown}"
  peer=vet
  [[ "${1:-}" == "vet" ]] && peer=build
  for _attempt in $(seq 1 100); do
    [[ -e "$sync_dir/$peer" ]] && break
    sleep 0.02
  done
  [[ -e "$sync_dir/$peer" ]] || {
    printf 'parallel gate peer %s did not start\n' "$peer" >&2
    exit 24
  }
fi

if [[ "${1:-}" == "test" ]]; then
  if [[ -n "${FAKE_GO_INVOCATIONS:-}" ]]; then
    printf '%s\n' "$*" >>"$FAKE_GO_INVOCATIONS"
  fi
  for argument in "$@"; do
    case "$argument" in
      -coverprofile=*)
        coverage_file="${argument#-coverprofile=}"
        mkdir -p "$(dirname "$coverage_file")"
        printf 'mode: atomic\n' >"$coverage_file"
        ;;
    esac
  done
  printf 'BenchmarkStub-8 1 1 ns/op\n'
fi

exit 0
SH

  cat >"$tools/gofmt" <<'SH'
#!/usr/bin/env bash
exit 0
SH

  cat >"$tools/fake-tool" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
name="${0##*/}"
if [[ "${FAKE_FAIL_GATE:-}" == "$name" ]]; then
  printf 'forced %s failure\n' "$name" >&2
  exit 23
fi
if [[ "$name" == "govulncheck" && "${FAKE_MUTATE_SNAPSHOT:-0}" == "1" ]]; then
  python3 - "$FAKE_REPO_ROOT/.artifacts/release/change_scope.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["base_sha"] = payload["head_sha"]
payload["merge_base_sha"] = payload["head_sha"]
payload["mode"] = "change"
payload["changes"] = []
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
fi
if [[ "$name" == "benchstat" ]]; then
  if [[ "${FAKE_TRUNCATE_GATES:-0}" == "1" ]]; then
    : >"$FAKE_REPO_ROOT/.artifacts/release/gates.tsv"
  fi
  if [[ "${FAKE_DELETE_ARTIFACT:-0}" == "1" ]]; then
    rm -f "$FAKE_REPO_ROOT/.artifacts/release/coverage.out"
  fi
  if [[ "${FAKE_LOWER_COVERAGE:-0}" == "1" ]]; then
    printf '1.0\n' >"$FAKE_REPO_ROOT/.artifacts/release/coverage_percent.txt"
  fi
  if [[ "${FAKE_TAMPER_SPEC_ARTIFACTS:-0}" == "1" ]]; then
    printf '{"active_specs":[],"schema_version":1,"status":"passed"}\n' \
      >"$FAKE_REPO_ROOT/.artifacts/release/spec_registry.json"
  fi
  printf 'benchmark comparison stable\n'
fi
exit 0
SH

  chmod +x "$tools/go" "$tools/gofmt" "$tools/fake-tool"
  ln -s fake-tool "$tools/golangci-lint"
  ln -s fake-tool "$tools/govulncheck"
  ln -s fake-tool "$tools/gitleaks"
  ln -s fake-tool "$tools/benchstat"
}

write_gate_stubs() {
  local repo="$1"

  cat >"$repo/scripts/check_ai_boundaries.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$AI_BOUNDARY_ARTIFACT_DIR"
python3 -I -B -S - \
  "$AI_BOUNDARY_ARTIFACT_DIR/ai_boundaries.json" \
  "${AI_BOUNDARY_APPROVAL_MODE:-required}" \
  "${AI_BOUNDARY_APPROVED:-0}" \
  "$AI_BOUNDARY_ARTIFACT_DIR/change_scope.json" <<'PY'
import json
import pathlib
import sys
approval_mode = sys.argv[2]
approved = sys.argv[3] == "1"
scope = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"))
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "approval_evidence": "owner-request:test" if approved else None,
            "approval_mode": approval_mode,
            "approval_satisfied": True,
            "approved": approved,
            "classifications": {
                "allowed": [item["path"] for item in scope["changes"]],
                "approval_required": [],
                "forbidden": [],
                "unclassified": [],
            },
            "schema_version": 1,
            "snapshot": {
                "base_sha": scope["base_sha"],
                "clean": scope["clean"],
                "head_sha": scope["head_sha"],
                "head_tree_sha": scope["head_tree_sha"],
                "merge_base_sha": scope["merge_base_sha"],
            },
            "status": "passed",
        },
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
PY
SH

  cat >"$repo/scripts/check_spec_registry.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$SPEC_REGISTRY_ARTIFACT_DIR"
python3 -I -B -S - "$SPEC_REGISTRY_ARTIFACT_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
active = [
    {
        "module": "zeta",
        "path": "specs/zeta/spec.md",
        "spec_id": "SPEC-ZETA-001",
        "status": "approved",
        "workflow_class": "HARNESS-TEST",
    },
    {
        "module": "alpha",
        "path": "specs/alpha/spec.md",
        "spec_id": "SPEC-ALPHA-001",
        "status": "approved",
        "workflow_class": "HARNESS-TEST",
    },
]
registry = {"active_specs": active, "schema_version": 1, "specs": active, "status": "passed"}
(root / "spec_registry.json").write_text(json.dumps(registry, sort_keys=True) + "\n", encoding="utf-8")
PY
SH

  cat >"$repo/scripts/run_checker_self_tests.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'all checker self-tests passed\n'
SH

  chmod +x \
    "$repo/scripts/check_ai_boundaries.sh" \
    "$repo/scripts/check_spec_registry.sh" \
    "$repo/scripts/run_checker_self_tests.sh"
}

setup_repo() {
  local name="$1"
  REPO="$TMP_DIR/$name"
  git init -q -b main "$REPO"
  git -C "$REPO" config user.email "release-test@example.com"
  git -C "$REPO" config user.name "Release Test"

  mkdir -p \
    "$REPO/scripts/lib" \
    "$REPO/.agents/skills" \
    "$REPO/.claude" \
    "$REPO/internal/risk" \
    "$REPO/internal/ledger" \
    "$REPO/docs"
  cp "$ROOT_DIR/scripts/verify_release.sh" "$REPO/scripts/verify_release.sh"
  if [[ -f "$ROOT_DIR/scripts/verify_candidate.sh" ]]; then
    cp "$ROOT_DIR/scripts/verify_candidate.sh" "$REPO/scripts/verify_candidate.sh"
  fi
  cp "$ROOT_DIR/scripts/verify_change.sh" "$REPO/scripts/verify_change.sh"
  cp "$ROOT_DIR/scripts/finalize_approval.py" "$REPO/scripts/finalize_approval.py"
  cp "$ROOT_DIR/scripts/write_release_summary.py" "$REPO/scripts/write_release_summary.py"
  cp "$ROOT_DIR/scripts/collect_changes.py" "$REPO/scripts/collect_changes.py"
  cp "$ROOT_DIR/scripts/lib/change_scope.py" "$REPO/scripts/lib/change_scope.py"
  cp "$ROOT_DIR/scripts/lib/evidence.py" "$REPO/scripts/lib/evidence.py"
  cp "$ROOT_DIR/scripts/lib/harness_config.py" "$REPO/scripts/lib/harness_config.py"
  cp "$ROOT_DIR/scripts/lib/verify_runner.sh" "$REPO/scripts/lib/verify_runner.sh"
  cp "$ROOT_DIR/scripts/harness_profiles.json" "$REPO/scripts/harness_profiles.json"
  cp "$ROOT_DIR/scripts/check_ai_boundaries.py" "$REPO/scripts/check_ai_boundaries.py"
  cp "$ROOT_DIR/scripts/changed_go_packages.py" "$REPO/scripts/changed_go_packages.py"
  cp "$ROOT_DIR/scripts/tool_versions.env" "$REPO/scripts/tool_versions.env"
  chmod +x \
    "$REPO/scripts/verify_release.sh" \
    "$REPO/scripts/verify_change.sh" \
    "$REPO/scripts/finalize_approval.py" \
    "$REPO/scripts/write_release_summary.py" \
    "$REPO/scripts/collect_changes.py"
  chmod +x "$REPO/scripts/lib/verify_runner.sh"
  chmod +x "$REPO/scripts/changed_go_packages.py"

  printf '.artifacts/\n.tools/\njson.py\npathlib.py\nmodule-shadow-marker\n' \
    >"$REPO/.gitignore"
  printf 'allowed:\n  - docs/\napproval_required:\n  - scripts/\nforbidden:\n  - secrets/\n' \
    >"$REPO/.ai-boundaries.yml"
  printf '# Fixture instructions\n' >"$REPO/AGENTS.md"
  ln -s AGENTS.md "$REPO/CLAUDE.md"
  ln -s ../.agents/skills "$REPO/.claude/skills"
  printf '# Risk instructions\n' >"$REPO/internal/risk/AGENTS.md"
  ln -s AGENTS.md "$REPO/internal/risk/CLAUDE.md"
  printf '# Ledger instructions\n' >"$REPO/internal/ledger/AGENTS.md"
  ln -s AGENTS.md "$REPO/internal/ledger/CLAUDE.md"
  printf 'module example.com/release-fixture\n\ngo 1.23\n' >"$REPO/go.mod"
  printf 'package fixture\n' >"$REPO/fixture.go"
  printf 'verify-release:\n\tscripts/verify_release.sh\n' >"$REPO/Makefile"

  write_gate_stubs "$REPO"
  write_fake_tools "$REPO"

  git -C "$REPO" add -A
  git -C "$REPO" commit -q -m "fixture base"
  BASE="$(git -C "$REPO" rev-parse HEAD)"

  printf 'first change\n' >"$REPO/docs/first change \"quoted\".txt"
  git -C "$REPO" add -A
  git -C "$REPO" commit -q -m "first pushed commit"
  printf 'second change\n' >"$REPO/docs/second.txt"
  git -C "$REPO" add -A
  git -C "$REPO" commit -q -m "second pushed commit"
  HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
}

test_missing_and_invalid_compare() {
  setup_repo missing-invalid
  if env -u VERIFY_COMPARE_REF FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/missing.log" 2>&1; then
    fail "release without an explicit compare ref passed"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "VERIFY_COMPARE_REF is required"

  if env VERIFY_COMPARE_REF=refs/heads/does-not-exist FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/invalid.log" 2>&1; then
    fail "release with an invalid compare ref passed"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "does not resolve to a commit"
}

test_dirty_release_fails() {
  setup_repo dirty-release
  printf 'dirty\n' >>"$REPO/docs/second.txt"
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/dirty.log" 2>&1; then
    fail "dirty release passed"
  fi
  python3 - "$REPO/.artifacts/release/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["overall"] == "failed", payload
assert payload["git"]["working_tree_clean_before"] is False, payload
assert payload["gates"][-1]["name"] == "release_context_before", payload
assert payload["gates"][-1]["status"] == "failed", payload
PY
}

test_stable_release_and_failed_rerun() {
  setup_repo stable-release
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/stable.log" 2>&1 || \
    fail "stable release failed"

  python3 - "$REPO/.artifacts/release" "$BASE" "$HEAD_SHA" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
base = sys.argv[2]
head = sys.argv[3]
summary_path = root / "summary.json"
payload = json.loads(summary_path.read_text(encoding="utf-8"))
assert payload["overall"] == "passed", payload
assert payload["release_ready"] is True, payload
assert payload["profile"] == "release", payload
assert payload["git"]["head_sha"] == head, payload
assert payload["git"]["observed_head_sha_after"] == head, payload
assert payload["git"]["compare_sha"] == base, payload
assert payload["git"]["merge_base_sha"] == base, payload
assert payload["git"]["branch"] == "main", payload
assert payload["git"]["working_tree_clean_before"] is True, payload
assert payload["git"]["working_tree_clean_after"] is True, payload
assert payload["coverage"] == {"percentage": 100.0, "threshold": 70.0}, payload
assert [item["spec_id"] for item in payload["active_specs"]] == [
    "SPEC-ALPHA-001", "SPEC-ZETA-001"
], payload
assert payload["active_specs"][1]["path"] == "specs/zeta/spec.md", payload
assert payload["gates"] and all(item["status"] == "passed" for item in payload["gates"]), payload
assert [item["name"] for item in payload["gates"]] == [
    "change_scope",
    "release_context_before",
    "toolchain",
    "symlinks",
    "gofmt",
    "build",
    "vet",
    "golangci",
    "test_unit_coverage",
    "govulncheck",
    "gitleaks",
    "ai_boundaries",
    "coverage_threshold",
    "test_race",
    "migration_safety",
    "prompt_evals",
    "spec_registry",
    "benchmarks",
    "release_context_after",
], payload
assert {item["path"] for item in payload["sealed_artifacts"]} == {
    "ai_boundaries.json",
    "bench/base.txt",
    "bench/benchstat.txt",
    "bench/current.txt",
    "change_scope.json",
    "coverage.out",
    "coverage_percent.txt",
    "spec_registry.json",
}, payload
for gate in payload["gates"]:
    log = root / gate["log_path"]
    assert hashlib.sha256(log.read_bytes()).hexdigest() == gate["log_sha256"], gate
for artifact in payload["artifacts"]:
    path = root / artifact["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"], artifact
manifest = root / "artifact_manifest.json"
assert hashlib.sha256(manifest.read_bytes()).hexdigest() == payload["artifact_manifest_sha256"], payload
scope = json.loads((root / "change_scope.json").read_text(encoding="utf-8"))
paths = [item["path"] for item in scope["changes"]]
assert 'docs/first change "quoted".txt' in paths, paths
assert "docs/second.txt" in paths, paths
assert payload["verifier_inputs"], payload
PY

  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_FAIL_GATE=govulncheck \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/rerun-fail.log" 2>&1; then
    fail "forced failed rerun passed"
  fi
  python3 - "$REPO/.artifacts/release/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["overall"] == "failed", payload
assert payload["release_ready"] is False, payload
assert payload["active_specs"] == [], payload
govuln = next(item for item in payload["gates"] if item["name"] == "govulncheck")
assert govuln["status"] == "failed", payload
assert not any(item["name"] == "spec_registry" for item in payload["gates"]), payload
PY
}

test_custom_gate_candidate_approval_finalization() {
  setup_repo pull-request-docs
  python3 - "$REPO/scripts/harness_profiles.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
policy["custom_gates"] = {
    "approval_check": {"run": "scripts/gates/approval_check.sh"},
}
policy["gate_sets"]["release"].insert(-1, "approval_check")
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  mkdir -p "$REPO/scripts/gates"
  cat >"$REPO/scripts/gates/approval_check.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'approval-compatible custom gate passed\n'
SH
  chmod +x "$REPO/scripts/gates/approval_check.sh"
  git -C "$REPO" add scripts/harness_profiles.json scripts/gates/approval_check.sh
  git -C "$REPO" commit -q -m "configure approval custom gate"
  BASE="$(git -C "$REPO" rev-parse HEAD)"
  printf 'candidate change\n' >"$REPO/docs/candidate.txt"
  git -C "$REPO" add docs/candidate.txt
  git -C "$REPO" commit -q -m "candidate change"
  HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
  env VERIFY_COMPARE_REF="$BASE" \
    FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_candidate.sh" >"$TMP_DIR/pr-docs.log" 2>&1 || \
    fail "docs-only pull-request profile failed"
  python3 - "$REPO/.artifacts/candidate/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
gates = {item["name"]: item for item in payload["gates"]}
assert payload["profile"] == "pull_request", payload
assert payload["mode"] == "candidate", payload
assert payload["release_ready"] is False, payload
assert gates["test_race"]["status"] == "skipped", gates
assert gates["benchmarks"]["status"] == "skipped", gates
assert gates["approval_check"]["status"] == "passed", gates
assert "no protected financial paths changed" in pathlib.Path(
    sys.argv[1]
).parent.joinpath(gates["test_race"]["log_path"]).read_text(encoding="utf-8")
assert not pathlib.Path(sys.argv[1]).parent.joinpath("bench/current.txt").exists()
PY
  git -C "$REPO" checkout -q --detach "$BASE"
  "$REPO/scripts/finalize_approval.py" \
    --repo "$REPO" \
    --candidate-dir .artifacts/candidate \
    --expected-head-sha "$HEAD_SHA" \
    --expected-compare-sha "$BASE" \
    --review-decision APPROVED \
    --output .artifacts/approval/finalization.json || \
    fail "custom-gate candidate evidence did not pass approval finalization"
  python3 - \
    "$REPO/.artifacts/candidate/summary.json" \
    "$REPO/.artifacts/approval/finalization.json" <<'PY'
import json
import pathlib
import sys

summary = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
finalization = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
assert any(gate["name"] == "approval_check" for gate in summary["gates"]), summary
assert finalization["status"] == "passed", finalization
assert finalization["approval_satisfied"] is True, finalization
assert finalization["errors"] == [], finalization
PY
}

test_profile_selection_and_parallel_execution() {
  setup_repo pull-request-risk
  printf 'package risk\n' >"$REPO/internal/risk/change.go"
  git -C "$REPO" add internal/risk/change.go
  git -C "$REPO" commit -q -m "change protected financial package"
  env VERIFY_COMPARE_REF="$BASE" \
    FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_candidate.sh" >"$TMP_DIR/pr-risk.log" 2>&1 || \
    fail "financial pull-request profile failed"
  python3 - "$REPO/.artifacts/candidate/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
gates = {item["name"]: item for item in payload["gates"]}
assert gates["test_race"]["status"] == "passed", gates
assert gates["benchmarks"]["status"] == "passed", gates
root = pathlib.Path(sys.argv[1]).parent
assert "protected financial paths changed" in root.joinpath(
    gates["test_race"]["log_path"]
).read_text(encoding="utf-8")
assert "performance-sensitive paths changed" in root.joinpath(
    gates["benchmarks"]["log_path"]
).read_text(encoding="utf-8")
PY

  setup_repo pull-request-benchmark
  cat >"$REPO/benchmark_test.go" <<'GO'
package fixture

import "testing"

func BenchmarkFixture(b *testing.B) {}
GO
  git -C "$REPO" add benchmark_test.go
  git -C "$REPO" commit -q -m "add benchmark"
  env VERIFY_COMPARE_REF="$BASE" \
    FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_candidate.sh" >"$TMP_DIR/pr-benchmark.log" 2>&1 || \
    fail "benchmark-file pull-request profile failed"
  python3 - "$REPO/.artifacts/candidate/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
gates = {item["name"]: item for item in payload["gates"]}
assert gates["benchmarks"]["status"] == "passed", gates
assert "benchmark file changed" in pathlib.Path(sys.argv[1]).parent.joinpath(
    gates["benchmarks"]["log_path"]
).read_text(encoding="utf-8")
PY

  setup_repo pull-request-forced-performance
  env VERIFY_COMPARE_REF="$BASE" \
    VERIFY_PERFORMANCE_REQUESTED=1 FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_candidate.sh" >"$TMP_DIR/pr-performance.log" 2>&1 || \
    fail "explicit performance pull-request profile failed"
  python3 - "$REPO/.artifacts/candidate/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
gates = {item["name"]: item for item in payload["gates"]}
assert gates["benchmarks"]["status"] == "passed", gates
assert "explicit performance request" in pathlib.Path(sys.argv[1]).parent.joinpath(
    gates["benchmarks"]["log_path"]
).read_text(encoding="utf-8")
PY

  setup_repo invalid-profile
  if env VERIFY_COMPARE_REF="$BASE" VERIFY_PROFILE=unexpected \
    FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/invalid-profile.log" 2>&1; then
    fail "unknown verification profile passed"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "invalid verification profile"

  setup_repo parallel-gates
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_REQUIRE_CONCURRENCY=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/parallel.log" 2>&1 || \
    fail "independent build and vet gates did not run concurrently"
  python3 - "$REPO/.artifacts/release/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
names = [item["name"] for item in payload["gates"]]
assert names.index("gofmt") < names.index("build") < names.index("vet") < names.index("golangci"), names
PY
}

test_configured_and_legacy_symlinks() {
  setup_repo configured-symlinks
  python3 - "$REPO/scripts/harness_profiles.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
policy["symlinks"] = [{"link": "docs/CURRENT.md", "target": "docs/TARGET.md"}]
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  rm -f \
    "$REPO/CLAUDE.md" \
    "$REPO/.claude/skills" \
    "$REPO/internal/risk/CLAUDE.md" \
    "$REPO/internal/ledger/CLAUDE.md"
  printf 'target\n' >"$REPO/docs/TARGET.md"
  ln -s TARGET.md "$REPO/docs/CURRENT.md"
  git -C "$REPO" add -A
  git -C "$REPO" commit -q -m "configure project symlinks"
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/configured-symlinks.log" 2>&1 || \
    fail "configured symlink pair failed"

  setup_repo legacy-v1-symlinks
  python3 - "$REPO/scripts/harness_profiles.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
policy["schema_version"] = 1
policy.pop("custom_gates")
policy.pop("symlinks")
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  git -C "$REPO" add scripts/harness_profiles.json
  git -C "$REPO" commit -q -m "use legacy v1 profile"
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/legacy-v1-symlinks.log" 2>&1 || \
    fail "legacy v1 symlink pairs failed"
}

test_failed_symlink_config_query_fails_gate() {
  local real_python
  setup_repo failed-symlink-query
  real_python="$(command -v python3)"
  cat >"$REPO/.tools/bin/python3" <<SH
#!/usr/bin/env bash
set -euo pipefail
for argument in "\$@"; do
  if [[ "\$argument" == symlinks ]]; then
    printf 'forced symlink config query failure\n' >&2
    exit 23
  fi
done
exec "$real_python" "\$@"
SH
  chmod +x "$REPO/.tools/bin/python3"
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/failed-symlink-query.log" 2>&1; then
    fail "release ignored a failed symlink config query"
  fi
  python3 - "$REPO/.artifacts/release/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
symlinks = next(item for item in payload["gates"] if item["name"] == "symlinks")
assert payload["overall"] == "failed", payload
assert symlinks["status"] == "failed", payload
PY
}

test_head_mutation_fails() {
  setup_repo head-mutation
  initial_head="$HEAD_SHA"
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_MUTATE_HEAD=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/head-mutation.log" 2>&1; then
    fail "release whose HEAD changed during gates passed"
  fi
  python3 - "$REPO/.artifacts/release/summary.json" "$initial_head" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["overall"] == "failed", payload
assert payload["git"]["head_sha"] == sys.argv[2], payload
assert payload["git"]["observed_head_sha_after"] != sys.argv[2], payload
assert payload["git"]["working_tree_clean_after"] is True, payload
assert payload["gates"][-1]["name"] == "release_context_after", payload
assert payload["gates"][-1]["status"] == "failed", payload
PY
}

test_snapshot_gate_and_artifact_tampering_fail() {
  setup_repo snapshot-tampering
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_MUTATE_SNAPSHOT=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/snapshot-tamper.log" 2>&1; then
    fail "release passed after its collected change snapshot was rewritten"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "changed after gate completion"

  setup_repo gate-tampering
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_TRUNCATE_GATES=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/gate-tamper.log" 2>&1; then
    fail "release passed after its gate ledger was truncated"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "exact required gate sequence"

  setup_repo artifact-tampering
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_DELETE_ARTIFACT=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/artifact-tamper.log" 2>&1; then
    fail "release passed after a required artifact was removed"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "sealed artifact"

  setup_repo changed-coverage-artifact
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_LOWER_COVERAGE=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/coverage-tamper.log" 2>&1; then
    fail "release passed after its coverage evidence fell below threshold"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "changed after gate completion"

  setup_repo changed-passed-spec-artifacts
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    FAKE_TAMPER_SPEC_ARTIFACTS=1 \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/spec-artifact-tamper.log" 2>&1; then
    fail "release passed after passed Specification artifacts were replaced"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "changed after gate completion"
}

test_python_module_shadowing_is_ignored() {
  setup_repo python-module-shadowing
  cat >"$REPO/json.py" <<'PY'
import pathlib
pathlib.Path("module-shadow-marker").write_text("loaded\n", encoding="utf-8")
raise RuntimeError("repository json.py must not be imported by mandatory gates")
PY
  cat >"$REPO/pathlib.py" <<'PY'
raise RuntimeError("repository pathlib.py must not be imported by mandatory gates")
PY

  (cd "$REPO" && env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh") >"$TMP_DIR/module-shadow.log" 2>&1 || \
    fail "isolated release gate failed in the presence of ignored module shadows"
  [[ ! -e "$REPO/module-shadow-marker" ]] || \
    fail "mandatory gate imported an ignored repository Python module"
}

test_detached_release_passes() {
  setup_repo detached-release
  git -C "$REPO" switch -q --detach
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/detached.log" 2>&1 || \
    fail "stable detached release failed"
  python3 - "$REPO/.artifacts/release/summary.json" "$HEAD_SHA" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["overall"] == "passed", payload
assert payload["release_ready"] is True, payload
assert payload["git"]["branch"] == "DETACHED", payload
assert payload["git"]["head_sha"] == sys.argv[2], payload
PY
}

test_evidence_writer_rejects_symlink_components() {
  local artifact_dir
  local outside_dir
  setup_repo symlinked-evidence
  artifact_dir="$REPO/.artifacts/manual"
  outside_dir="$TMP_DIR/outside-evidence"
  mkdir -p "$artifact_dir" "$outside_dir"
  printf 'gate output\n' >"$outside_dir/example.log"
  ln -s "$outside_dir" "$artifact_dir/logs"
  gate_sha="$(python3 - "$outside_dir/example.log" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
  printf 'example\tpassed\t0\tlogs/example.log\t%s\n' "$gate_sha" \
    >"$artifact_dir/gates.tsv"
  PYTHONDONTWRITEBYTECODE=1 python3 -B -E -S \
    "$REPO/scripts/collect_changes.py" \
    --repo "$REPO" \
    --base "$BASE" \
    --mode change \
    --format json >"$artifact_dir/change_scope.json"
  snapshot_sha="$(python3 - "$artifact_dir/change_scope.json" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"

  if PYTHONDONTWRITEBYTECODE=1 python3 -B -E -S \
    "$REPO/scripts/write_release_summary.py" \
    --repo "$REPO" \
    --artifact-dir "$artifact_dir" \
    --mode change \
    --overall passed \
    --compare-ref "$BASE" \
    --branch main \
    --started-at 2026-07-15T00:00:00Z \
    --finished-at 2026-07-15T00:00:01Z \
    --clean-before true \
    --clean-after true \
    --snapshot-file "$artifact_dir/change_scope.json" \
    --expected-snapshot-sha256 "$snapshot_sha" \
    --gates-file "$artifact_dir/gates.tsv"; then
    fail "evidence writer accepted a symlinked gate-log parent"
  fi
  assert_failed_summary "$artifact_dir/summary.json" "symlink path component"
}

test_mutable_artifact_root_symlink_fails_before_cleanup() {
  local outside_dir
  setup_repo artifact-root-symlink
  outside_dir="$TMP_DIR/artifact-root-target"
  mkdir -p "$REPO/.artifacts" "$outside_dir/logs"
  printf 'preserve\n' >"$outside_dir/logs/sentinel.txt"
  ln -s "$outside_dir" "$REPO/.artifacts/release"

  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/artifact-root-symlink.log" 2>&1; then
    fail "release accepted a symlinked mutable artifact root"
  fi
  [[ -f "$outside_dir/logs/sentinel.txt" ]] || \
    fail "release cleaned through a symlinked artifact root"
  [[ ! -e "$outside_dir/summary.json" ]] || \
    fail "release wrote evidence through a symlinked artifact root"
  [[ ! -L "$REPO/.artifacts/release" ]] || \
    fail "release left an unsafe artifact symlink at the standard evidence path"
}

test_early_mutable_root_failure_invalidates_prior_pass() {
  local outside_tools
  setup_repo stale-summary-before-tools-failure
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/prior-pass.log" 2>&1 || \
    fail "fixture release did not establish prior passed evidence"
  python3 - "$REPO/.artifacts/release/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["overall"] == "passed" and payload["release_ready"] is True, payload
PY

  outside_tools="$TMP_DIR/outside-tools"
  mkdir -p "$outside_tools"
  printf 'preserve\n' >"$outside_tools/sentinel.txt"
  rm -rf "$REPO/.tools"
  ln -s "$outside_tools" "$REPO/.tools"
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/tools-root-failure.log" 2>&1; then
    fail "release accepted a symlinked tools root"
  fi
  [[ ! -e "$REPO/.artifacts/release/summary.json" ]] || \
    fail "early tools-root failure left an older passed summary current"
  [[ -f "$outside_tools/sentinel.txt" ]] || \
    fail "release modified the symlinked tools target"
}

test_invalid_policy_invalidates_prior_pass() {
  setup_repo invalid-policy-rerun
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/policy-prior-pass.log" 2>&1 || \
    fail "fixture release did not establish policy prior evidence"
  printf '{ invalid\n' >"$REPO/scripts/harness_profiles.json"
  if env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/invalid-policy.log" 2>&1; then
    fail "release accepted an invalid Harness profile policy"
  fi
  [[ ! -e "$REPO/.artifacts/release/summary.json" ]] || \
    fail "invalid profile policy left an older passed summary current"
}

test_coverage_override_cannot_weaken_policy() {
  setup_repo low-coverage-policy
  if env VERIFY_COMPARE_REF="$BASE" COVERAGE_THRESHOLD=0 FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/low-coverage-policy.log" 2>&1; then
    fail "release accepted a coverage threshold below the Harness policy"
  fi
  assert_failed_summary "$REPO/.artifacts/release/summary.json" \
    "coverage threshold is below the Harness policy minimum"
}

test_dirty_verify_change_is_non_release_evidence() {
  setup_repo dirty-change
  printf 'working tree\n' >"$REPO/docs/working tree.txt"
  env VERIFY_COMPARE_REF="$BASE" \
    "$REPO/scripts/verify_change.sh" >"$TMP_DIR/change.log" 2>&1 || \
    fail "dirty verify-change failed"
  python3 - "$REPO/.artifacts/change" <<'PY'
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
scope = json.loads((root / "change_scope.json").read_text(encoding="utf-8"))
assert summary["overall"] == "passed", summary
assert summary["mode"] == "change", summary
assert summary["release_ready"] is False, summary
assert summary["git"]["working_tree_clean_before"] is False, summary
paths = [item["path"] for item in scope["changes"]]
assert 'docs/first change "quoted".txt' in paths, paths
assert "docs/second.txt" in paths, paths
assert "docs/working tree.txt" in paths, paths
assert [item["name"] for item in summary["gates"]] == [
    "change_scope",
    "toolchain",
    "gofmt",
    "vet",
    "golangci",
    "changed_package_tests",
    "ai_boundaries",
    "spec_registry",
], summary
go_gates = {
    item["name"]: item
    for item in summary["gates"]
    if item["name"] in {
        "toolchain", "gofmt", "vet", "golangci", "changed_package_tests"
    }
}
assert set(go_gates) == {
    "toolchain", "gofmt", "vet", "golangci", "changed_package_tests"
}, go_gates
for gate in go_gates.values():
    assert gate["status"] == "skipped", gate
    assert "no changed Go files" in root.joinpath(
        gate["log_path"]
    ).read_text(encoding="utf-8"), gate
for gate_name in ("change_scope", "ai_boundaries", "spec_registry"):
    gate = next(item for item in summary["gates"] if item["name"] == gate_name)
    assert gate["status"] == "passed", gate
PY
}

test_verify_change_selects_go_packages() {
  local invocations="$TMP_DIR/change-go-invocations.txt"
  setup_repo change-go-package
  printf 'package docs\n' >"$REPO/docs/helper.go"
  git -C "$REPO" add docs/helper.go
  : >"$invocations"
  env VERIFY_COMPARE_REF="$BASE" \
    FAKE_GO_INVOCATIONS="$invocations" \
    "$REPO/scripts/verify_change.sh" >"$TMP_DIR/change-go.log" 2>&1 || \
    fail "Go-focused verify-change failed"
  grep -Fqx 'test ./docs' "$invocations" || \
    fail "verify-change did not run tests for the selected Go package"
  python3 - "$REPO/.artifacts/change/summary.json" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
gate = next(item for item in payload["gates"] if item["name"] == "changed_package_tests")
assert gate["status"] == "passed", payload
PY
}

test_member_driven_gate_execution() {
  setup_repo member-driven-change-custom
  python3 - "$REPO/scripts/harness_profiles.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
policy["custom_gates"] = {
    "spec_contract": {"run": "scripts/gates/spec_contract.sh"},
}
policy["gate_sets"]["change"] = [
    "change_scope", "ai_boundaries", "spec_contract",
]
policy["profiles"]["change"]["skippable_gates"] = []
policy["evidence_sets"]["change"]["artifacts"].remove("spec_registry.json")
policy["machine_status_artifacts"].remove("spec_registry.json")
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  mkdir -p "$REPO/scripts/gates"
  cat >"$REPO/scripts/gates/spec_contract.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'project Specification contract passed\n'
SH
  chmod +x "$REPO/scripts/gates/spec_contract.sh"
  git -C "$REPO" add scripts/harness_profiles.json scripts/gates/spec_contract.sh
  git -C "$REPO" commit -q -m "replace builtin spec registry"
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_change.sh" >"$TMP_DIR/member-driven-change-custom.log" 2>&1 || \
    fail "change profile without spec_registry failed"
  python3 - "$REPO/.artifacts/change" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
assert summary["overall"] == "passed", summary
assert [gate["name"] for gate in summary["gates"]] == [
    "change_scope", "ai_boundaries", "spec_contract",
], summary
assert not (root / "spec_registry.json").exists(), summary
PY

  setup_repo member-driven-change-skips
  python3 - "$REPO/scripts/harness_profiles.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
policy["gate_sets"]["change"] = [
    "change_scope", "gofmt", "changed_package_tests", "ai_boundaries",
]
policy["profiles"]["change"]["skippable_gates"] = [
    "gofmt", "changed_package_tests",
]
policy["evidence_sets"]["change"]["artifacts"].remove("spec_registry.json")
policy["machine_status_artifacts"].remove("spec_registry.json")
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  git -C "$REPO" add scripts/harness_profiles.json
  git -C "$REPO" commit -q -m "trim change gates"
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_change.sh" >"$TMP_DIR/member-driven-change-skips.log" 2>&1 || \
    fail "trimmed no-Go change profile failed"
  python3 - "$REPO/.artifacts/change/summary.json" <<'PY'
import json
import pathlib
import sys

summary = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert [gate["name"] for gate in summary["gates"]] == [
    "change_scope", "gofmt", "changed_package_tests", "ai_boundaries",
], summary
assert [gate["name"] for gate in summary["gates"] if gate["status"] == "skipped"] == [
    "gofmt", "changed_package_tests",
], summary
PY

  setup_repo member-driven-release
  python3 - "$REPO/scripts/harness_profiles.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
for gate in ("symlinks", "migration_safety", "prompt_evals", "spec_registry"):
    policy["gate_sets"]["release"].remove(gate)
policy["evidence_sets"]["release"]["artifacts"].remove("spec_registry.json")
policy["machine_status_artifacts"].remove("spec_registry.json")
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  git -C "$REPO" add scripts/harness_profiles.json
  git -C "$REPO" commit -q -m "trim release gates"
  env VERIFY_COMPARE_REF="$BASE" FAKE_REPO_ROOT="$REPO" \
    "$REPO/scripts/verify_release.sh" >"$TMP_DIR/member-driven-release.log" 2>&1 || \
    fail "trimmed release profile failed"
  python3 - "$REPO/.artifacts/release" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
omitted = {"symlinks", "migration_safety", "prompt_evals", "spec_registry"}
assert summary["overall"] == "passed", summary
assert omitted.isdisjoint(gate["name"] for gate in summary["gates"]), summary
assert not (root / "spec_registry.json").exists(), summary
for gate in omitted:
    assert not (root / "logs" / f"{gate}.log").exists(), gate
PY
}

extract_ci_compare_step() {
  python3 -B -E -S - "$ROOT_DIR/.github/actions/setup-harness/action.yml" "$1" <<'PY'
import pathlib
import sys

lines = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
step = next(index for index, line in enumerate(lines) if "- name: Resolve compare commit" in line)
run = next(index for index in range(step + 1, len(lines)) if lines[index].strip() == "run: |")
indent = len(lines[run]) - len(lines[run].lstrip())
body = []
for line in lines[run + 1 :]:
    current = len(line) - len(line.lstrip())
    if line.strip() and current <= indent:
        break
    body.append(line[indent + 2 :] if line.strip() else "")
pathlib.Path(sys.argv[2]).write_text("\n".join(body) + "\n", encoding="utf-8")
PY
  chmod +x "$1"
}

test_ci_compare_selection() {
  local ci_script
  local output_file
  grep -Fq 'github.event.pull_request.base.sha' "$ROOT_DIR/.github/workflows/ci.yml" || \
    fail "CI does not use the exact pull-request base SHA"
  grep -Fq 'github.event.before' "$ROOT_DIR/.github/workflows/ci.yml" || \
    fail "CI does not use github.event.before for pushes"
  grep -Fq '0{40}' "$ROOT_DIR/.github/actions/setup-harness/action.yml" || \
    fail "CI does not handle the all-zero initial push SHA"
  if grep -Fq 'HEAD~1' "$ROOT_DIR/.github/workflows/ci.yml" \
    "$ROOT_DIR/.github/actions/setup-harness/action.yml"; then
    fail "CI still limits push comparison to HEAD~1"
  fi

  setup_repo ci-compare
  ci_script="$TMP_DIR/resolve-ci-compare.sh"
  output_file="$TMP_DIR/github-output.txt"
  extract_ci_compare_step "$ci_script"

  : >"$output_file"
  (cd "$REPO" && env EVENT_NAME=pull_request PR_BASE_SHA="$BASE" \
    PUSH_BEFORE_SHA= GITHUB_OUTPUT="$output_file" bash "$ci_script")
  grep -Fqx "sha=$BASE" "$output_file" || fail "CI did not preserve exact PR base"

  : >"$output_file"
  (cd "$REPO" && env EVENT_NAME=push PR_BASE_SHA= \
    PUSH_BEFORE_SHA="$BASE" GITHUB_OUTPUT="$output_file" bash "$ci_script")
  grep -Fqx "sha=$BASE" "$output_file" || fail "CI did not preserve exact push before SHA"

  : >"$output_file"
  (cd "$REPO" && env EVENT_NAME=push PR_BASE_SHA= \
    PUSH_BEFORE_SHA=0000000000000000000000000000000000000000 \
    GITHUB_OUTPUT="$output_file" bash "$ci_script")
  grep -Fqx "sha=$BASE" "$output_file" || fail "CI initial-push fallback is not the root commit"

  if (cd "$REPO" && env EVENT_NAME=push PR_BASE_SHA= \
    PUSH_BEFORE_SHA=ffffffffffffffffffffffffffffffffffffffff \
    GITHUB_OUTPUT="$output_file" bash "$ci_script") \
    >"$TMP_DIR/ci-unavailable.log" 2>&1; then
    fail "CI silently fell back for an unavailable non-zero push before SHA"
  fi
  if (cd "$REPO" && env EVENT_NAME=push PR_BASE_SHA= PUSH_BEFORE_SHA= \
    GITHUB_OUTPUT="$output_file" bash "$ci_script") \
    >"$TMP_DIR/ci-missing.log" 2>&1; then
    fail "CI silently accepted a missing push before SHA"
  fi
}

test_ci_approval_finalization_is_lightweight() {
  python3 -I -B -S - "$ROOT_DIR/.github/workflows/ci.yml" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
lines = text.splitlines()

def job(name):
    marker = f"  {name}:"
    start = lines.index(marker)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.fullmatch(r"  [a-z0-9_]+:", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])

candidate = job("verify_candidate")
main = job("verify_main")
approval = job("approval_finalize")
assert "pull_request_review:" in text, "review event trigger is missing"
assert "scripts/verify_candidate.sh" in candidate, "PR job does not create candidate evidence"
assert "VERIFY_PROFILE: release" in main, "main push does not run the complete release profile"
assert "scripts/finalize_approval.py" in approval, "review job does not finalize evidence"
assert "actions/download-artifact@v4" in approval, "review job does not reuse candidate evidence"
assert "github.event.pull_request.head.sha" in approval, "review evidence is not bound to PR HEAD"
assert "pull/${PR_NUMBER}/head" in approval, "review job does not fetch the exact candidate Git object"
for forbidden in (
    "actions/setup-go",
    "scripts/install_tools.sh",
    "scripts/verify_candidate.sh",
    "scripts/verify_release.sh",
):
    assert forbidden not in approval, f"review job still runs heavyweight step: {forbidden}"
PY
}

test_tool_and_scan_boundaries() {
  grep -Fqx '.claude/worktrees/' "$ROOT_DIR/.gitignore" || \
    fail ".claude/worktrees is not ignored"
  grep -Fq "git -C \"\$ROOT_DIR\" ls-files" "$ROOT_DIR/scripts/lib/verify_runner.sh" || \
    fail "shared formatting is not based on Git tracked files"
  if grep -Fq 'find "$ROOT_DIR" -name' "$ROOT_DIR/scripts/lib/verify_runner.sh"; then
    fail "release formatting still traverses the repository with find"
  fi
  grep -A8 -F 'disable:' "$ROOT_DIR/.golangci.yml" | grep -Fq -- '- gofmt' || \
    fail "golangci-lint still duplicates the explicit gofmt gate"
  grep -A8 -F 'disable:' "$ROOT_DIR/.golangci.yml" | grep -Fq -- '- govet' || \
    fail "golangci-lint still duplicates the explicit go vet gate"
}

test_missing_and_invalid_compare
test_dirty_release_fails
test_stable_release_and_failed_rerun
test_custom_gate_candidate_approval_finalization
test_profile_selection_and_parallel_execution
test_configured_and_legacy_symlinks
test_failed_symlink_config_query_fails_gate
test_head_mutation_fails
test_snapshot_gate_and_artifact_tampering_fail
test_python_module_shadowing_is_ignored
test_detached_release_passes
test_evidence_writer_rejects_symlink_components
test_mutable_artifact_root_symlink_fails_before_cleanup
test_early_mutable_root_failure_invalidates_prior_pass
test_invalid_policy_invalidates_prior_pass
test_coverage_override_cannot_weaken_policy
test_dirty_verify_change_is_non_release_evidence
test_verify_change_selects_go_packages
test_member_driven_gate_execution

printf 'verify release tests passed\n'
