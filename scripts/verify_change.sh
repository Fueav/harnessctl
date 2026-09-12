#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ROOT_DIR="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"
ARTIFACT_DIR_RAW="${VERIFY_CHANGE_ARTIFACT_DIR:-$ROOT_DIR/.artifacts/change}"
COMPARE_REF="${VERIFY_COMPARE_REF:-}"
RUNNER_SNAPSHOT_MODE=change
RUNNER_EVIDENCE_MODE=change
RUNNER_PROFILE=change
RUNNER_LABEL=verify-change
RUNNER_COVERAGE_THRESHOLD=""

source "$ENGINE_DIR/lib/verify_runner.sh"
runner_init

PACKAGES_FILE="$ARTIFACT_DIR/changed_go_packages.nul"
PACKAGE_SELECTION_ERROR="$ARTIFACT_DIR/changed_go_packages.stderr"
rm -f "$PACKAGES_FILE" "$PACKAGE_SELECTION_ERROR"

select_changed_packages() {
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$ENGINE_DIR/changed_go_packages.py" \
    --repo "$ROOT_DIR" --snapshot-file "$SNAPSHOT_FILE" \
    --snapshot-sha256 "$SNAPSHOT_SHA256" \
    >"$PACKAGES_FILE" 2>"$PACKAGE_SELECTION_ERROR"
}

run_changed_package_tests() {
  local -a packages=()
  local package
  while IFS= read -r -d '' package; do packages+=("$package"); done <"$PACKAGES_FILE"
  (( ${#packages[@]} )) || packages=(./...)
  printf 'selected Go packages:\n'; printf '  %s\n' "${packages[@]}"
  run_with_test_resources go test "${packages[@]}"
}

report_package_selection_failure() {
  cat "$PACKAGE_SELECTION_ERROR" >&2
  return 1
}

cd "$ROOT_DIR"
runner_validate_profile
runner_resolve_context

recorded_run change_scope collect_scope
seal_artifact change_scope.json
if ! select_changed_packages; then
  for gate in toolchain gofmt vet golangci; do
    gate_enabled "$gate" && recorded_skip "$gate" "Go package selection failed"
  done
  gate_enabled changed_package_tests && \
    recorded_run changed_package_tests report_package_selection_failure
else
  run_go_gate() {
    local name="$1"; shift
    gate_enabled "$name" || return 0
    if [[ -s "$PACKAGES_FILE" ]] || [[ $'\n'"$CONDITIONAL_GATE_NAMES"$'\n' == *$'\n'"$name"$'\n'* ]]; then
      recorded_run "$name" "$@"
    else
      recorded_skip "$name" "no changed Go files"
    fi
  }
  run_go_gate toolchain ensure_tools go gofmt golangci-lint
  run_go_gate gofmt check_gofmt
  run_go_gate vet go vet ./...
  run_go_gate golangci golangci-lint run ./...
  run_go_gate changed_package_tests run_changed_package_tests
fi
recorded_run ai_boundaries check_boundaries
seal_artifact ai_boundaries.json
if gate_enabled prompt_evals; then
  recorded_run prompt_evals python3 -I -B -S "$ENGINE_DIR/check_prompt_evals.py" \
    --repo "$ROOT_DIR" --snapshot "$SNAPSHOT_FILE" --snapshot-sha256 "$SNAPSHOT_SHA256"
fi
if gate_enabled spec_registry; then
  recorded_run spec_registry check_spec_registry
  seal_artifact spec_registry.json
fi
run_custom_gates

runner_complete
