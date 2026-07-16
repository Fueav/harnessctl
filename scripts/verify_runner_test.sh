#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS_ROOT="$ROOT_DIR"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

PROJECT_ROOT="$TMP_DIR/repo"
mkdir -p "$PROJECT_ROOT/scripts/gates" "$TMP_DIR/artifacts"
git init -q -b main "$PROJECT_ROOT"
cat >"$PROJECT_ROOT/scripts/gates/project_check.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'custom gate ran\n'
SH
chmod +x "$PROJECT_ROOT/scripts/gates/project_check.sh"

ROOT_DIR="$PROJECT_ROOT"
ENGINE_DIR="$HARNESS_ROOT/scripts"
ARTIFACT_DIR_RAW="$PROJECT_ROOT/.artifacts/test"
RUNNER_SNAPSHOT_MODE=change
RUNNER_EVIDENCE_MODE=change
RUNNER_PROFILE=change
source "$HARNESS_ROOT/scripts/lib/verify_runner.sh"

cat >"$TMP_DIR/config-query-fails.py" <<'PY'
raise SystemExit(23)
PY
CONFIG_TOOL="$TMP_DIR/config-query-fails.py"
if (runner_init; status=$?; trap - EXIT; exit "$status"); then
  fail "runner_init ignored a failed artifacts query"
fi

ARTIFACT_DIR="$TMP_DIR/artifacts"
SNAPSHOT_FILE="$ARTIFACT_DIR/change_scope.json"
SNAPSHOT_SHA256="$(printf 'a%.0s' {1..64})"
COMPARE_SHA="$(printf 'b%.0s' {1..40})"
HEAD_SHA="$(printf 'c%.0s' {1..40})"

recorded_run() {
  shift
  "$@"
}

seal_artifact() {
  return 0
}

CONFIG_TOOL="$TMP_DIR/config-query-fails.py"
if run_custom_gates; then
  fail "run_custom_gates ignored a failed custom-gates query"
fi

cat >"$TMP_DIR/artifact-query-fails.py" <<'PY'
import sys
if sys.argv[1] == "custom-gates":
    print("project_check\tscripts/gates/project_check.sh")
    raise SystemExit(0)
if sys.argv[1] == "gate-artifacts":
    raise SystemExit(23)
raise SystemExit(2)
PY
CONFIG_TOOL="$TMP_DIR/artifact-query-fails.py"
if run_custom_gates; then
  fail "run_custom_gates ignored a failed gate-artifacts query"
fi

printf 'verify runner tests passed\n'
