#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

for relative in \
  scripts/check_spec_registry.py \
  scripts/check_spec_registry_test.sh \
  scripts/harness_profiles.json \
  scripts/lib/harness_config.py \
  scripts/lib/evidence.py \
  scripts/lib/safe_cleanup.sh \
  scripts/lib/verify_runner.sh \
  cli.go \
  cmd/harnessctl/main.go; do
  [[ -f "$ROOT_DIR/$relative" ]] || fail "missing shared runtime: $relative"
done

grep -Fq 'scripts/safe_cleanup_test.sh' \
  "$ROOT_DIR/scripts/run_checker_self_tests.sh" || \
  fail "safe cleanup helper is not registered in checker self-tests"
grep -Fq 'scripts/check_spec_registry_test.sh' \
  "$ROOT_DIR/scripts/run_checker_self_tests.sh" || \
  fail "Specification registry checker is not registered in checker self-tests"

python3 -I -B -S - "$ROOT_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
policy = json.loads(
    (root / "scripts/harness_profiles.json").read_text(encoding="utf-8")
)
assert policy["schema_version"] == 3, policy
assert policy["custom_gates"] == {}, policy
assert policy["symlinks"] == [
    {"link": "CLAUDE.md", "target": "AGENTS.md"},
    {"link": ".claude/skills", "target": ".agents/skills"},
    {"link": "internal/risk/CLAUDE.md", "target": "internal/risk/AGENTS.md"},
    {"link": "internal/ledger/CLAUDE.md", "target": "internal/ledger/AGENTS.md"},
], policy
profiles = policy["profiles"]
assert set(profiles) == {"change", "pull_request", "release"}, profiles
expected_release = [
    "change_scope", "release_context_before", "toolchain", "symlinks",
    "gofmt", "build", "vet", "golangci", "test_unit_coverage",
    "govulncheck", "gitleaks", "ai_boundaries",
    "coverage_threshold", "test_race",
    "migration_safety", "prompt_evals", "spec_registry", "benchmarks",
    "release_context_after",
]
assert policy["gate_sets"][profiles["pull_request"]["gate_set"]] == expected_release, profiles
assert policy["gate_sets"][profiles["release"]["gate_set"]] == expected_release, profiles
assert profiles["pull_request"]["skippable_gates"] == [
    "test_race", "benchmarks"
], profiles
assert profiles["release"]["skippable_gates"] == [], profiles
conditions = policy["conditional_gates"]
assert conditions["test_race"]["path_prefixes"] == [
    "internal/risk/", "internal/ledger/", "internal/clearing/"
], conditions
assert "internal/observability/" in conditions["benchmarks"]["path_prefixes"]
assert policy["gate_artifacts"]["benchmarks"]["artifacts"] == [
    "bench/base.txt", "bench/benchstat.txt", "bench/current.txt"
], policy
PY

for script in scripts/verify_change.sh scripts/verify_release.sh; do
  grep -Fq '$ENGINE_DIR/lib/verify_runner.sh' "$ROOT_DIR/$script" || \
    fail "$script does not source the shared runner"
done

for script in scripts/write_release_summary.py scripts/finalize_approval.py; do
  grep -Fq 'lib.evidence' "$ROOT_DIR/$script" || \
    fail "$script does not use the shared evidence library"
  grep -Fq 'lib.harness_config' "$ROOT_DIR/$script" || \
    fail "$script does not use the shared profile policy"
  if grep -Eq '^(REQUIRED_GATES|SKIPPABLE_GATES|PULL_REQUEST_GATES|BASE_REQUIRED_ARTIFACTS|REQUIRED_SEALED_ARTIFACTS)[[:space:]]*=' \
    "$ROOT_DIR/$script"; then
    fail "$script still owns a private gate or artifact rule copy"
  fi
done

grep -Fq 'actions/setup-go@v5' \
  "$ROOT_DIR/.github/workflows/ci.yml" || \
  fail "CI workflow does not initialize Go"

grep -Fq 'VERSION ?= v0.4.0' "$ROOT_DIR/Makefile" || \
  fail "make build does not default to v0.4.0"
grep -Fq -- '-X main.version=$(VERSION)' "$ROOT_DIR/Makefile" || \
  fail "make build does not inject the release version"
for contract in \
  'custom_gates' \
  'schema_version": 3' \
  'scaffold audit' \
  'evidence verify' \
  'HARNESS_PROJECT_ROOT' \
  'HARNESS_ARTIFACT_DIR' \
  'HARNESS_SNAPSHOT_FILE' \
  'HARNESS_SNAPSHOT_SHA256' \
  'HARNESS_COMPARE_SHA' \
  'HARNESS_HEAD_SHA' \
  'HARNESS_PROFILE' \
  'HARNESS_EVIDENCE_MODE' \
  'HARNESS_ENGINE_DIR' \
  'member-driven' \
  'change_scope' \
  'ai_boundaries' \
  'release_context_before' \
  'release_context_after' \
  'coverage_threshold' \
  'test_unit_coverage' \
  'spec_registry' \
  'basename glob' \
  '*.pem' \
  '*secret*'; do
  grep -Fq "$contract" "$ROOT_DIR/README.md" || \
    fail "README is missing the custom-gate contract: $contract"
done
grep -Fq 'github.com/Fueav/harnessctl/cmd/harnessctl@v0.4.0' \
  "$ROOT_DIR/README.md" || fail "README install command is not pinned to v0.4.0"

python3 -I -B -S - "$ROOT_DIR" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
paths = [
    "cli.go",
    "scripts/harness_profiles.json",
    "scripts/lib/harness_config.py",
    "scripts/lib/evidence.py",
    "scripts/lib/verify_runner.sh",
    "scripts/verify_change.sh",
    "scripts/verify_release.sh",
    "scripts/write_release_summary.py",
    "scripts/finalize_approval.py",
    "scripts/verify_evidence.py",
]
count = sum(len((root / path).read_text(encoding="utf-8").splitlines()) for path in paths)
if count > 3100:
    raise SystemExit(f"production verification runtime is {count} lines; budget is 3100")
print(f"production verification runtime: {count} lines")
scaffold_count = len((root / "scaffold_audit.go").read_text(encoding="utf-8").splitlines())
if scaffold_count > 500:
    raise SystemExit(f"scaffold audit runtime is {scaffold_count} lines; budget is 500")
print(f"scaffold audit runtime: {scaffold_count} lines")
PY

printf 'harness structure tests passed\n'
