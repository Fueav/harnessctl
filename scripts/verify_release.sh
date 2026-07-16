#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ROOT_DIR="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"
ARTIFACT_DIR_RAW="${VERIFY_ARTIFACT_DIR:-$ROOT_DIR/.artifacts/release}"
COMPARE_REF="${VERIFY_COMPARE_REF:-}"
VERIFY_PROFILE="${VERIFY_PROFILE:-release}"
VERIFY_EVIDENCE_MODE="${VERIFY_EVIDENCE_MODE:-release}"
VERIFY_PERFORMANCE_REQUESTED="${VERIFY_PERFORMANCE_REQUESTED:-0}"
COVERAGE_THRESHOLD="${COVERAGE_THRESHOLD:-}"
RUNNER_SNAPSHOT_MODE=release
RUNNER_EVIDENCE_MODE="$VERIFY_EVIDENCE_MODE"
RUNNER_PROFILE="$VERIFY_PROFILE"
RUNNER_LABEL=verify-release
RUNNER_COVERAGE_THRESHOLD="$COVERAGE_THRESHOLD"

source "$ENGINE_DIR/lib/verify_runner.sh"
runner_init
if [[ -z "$COVERAGE_THRESHOLD" ]]; then
  COVERAGE_THRESHOLD="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$ENGINE_DIR/lib/harness_config.py" coverage)" || \
    die "cannot load the Harness coverage policy"
  RUNNER_COVERAGE_THRESHOLD="$COVERAGE_THRESHOLD"
fi
BENCH_WORKTREE="$ARTIFACT_DIR/bench/base-worktree"

runner_cleanup_hook() {
  if git -C "$ROOT_DIR" worktree list --porcelain 2>/dev/null | \
    grep -Fqx "worktree $BENCH_WORKTREE"; then
    git -C "$ROOT_DIR" worktree remove --force "$BENCH_WORKTREE" >/dev/null 2>&1 || true
  fi
  rm -rf "$BENCH_WORKTREE"
  git -C "$ROOT_DIR" worktree prune >/dev/null 2>&1 || true
}

check_symlinks() {
  [[ -L "$ROOT_DIR/CLAUDE.md" && "$(readlink "$ROOT_DIR/CLAUDE.md")" == AGENTS.md ]]
  [[ -L "$ROOT_DIR/.claude/skills" && "$(readlink "$ROOT_DIR/.claude/skills")" == ../.agents/skills ]]
  [[ -L "$ROOT_DIR/internal/risk/CLAUDE.md" && "$(readlink "$ROOT_DIR/internal/risk/CLAUDE.md")" == AGENTS.md ]]
  [[ -L "$ROOT_DIR/internal/ledger/CLAUDE.md" && "$(readlink "$ROOT_DIR/internal/ledger/CLAUDE.md")" == AGENTS.md ]]
}

check_gitleaks() {
  local scan_dir="$ARTIFACT_DIR/gitleaks-tree"
  rm -rf "$scan_dir"; mkdir -p "$scan_dir"
  git -C "$ROOT_DIR" archive --format=tar "$HEAD_SHA" | tar -xf - -C "$scan_dir"
  gitleaks detect --source "$scan_dir" --no-git --redact
  rm -rf "$scan_dir"
}

check_coverage() {
  local coverage
  coverage="$(go tool cover -func="$ARTIFACT_DIR/coverage.out" | \
    awk '/^total:/ { sub(/%/, "", $3); print $3 }')"
  awk -v got="$coverage" -v want="$COVERAGE_THRESHOLD" \
    'BEGIN { exit !(got + 0 >= want + 0) }'
  printf '%s\n' "$coverage" >"$ARTIFACT_DIR/coverage_percent.txt"
}

changed_files_nul() {
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - "$SNAPSHOT_FILE" <<'PY'
import json
import os
import pathlib
import sys
for change in json.loads(pathlib.Path(sys.argv[1]).read_text())["changes"]:
    os.write(1, os.fsencode(change["path"]) + b"\0")
PY
}

check_migrations() {
  local file status=0
  while IFS= read -r -d '' file; do
    [[ "$file" == migrations/* ]] || continue
    if git -C "$ROOT_DIR" cat-file -e "$COMPARE_SHA:$file" 2>/dev/null; then
      printf 'historical migration changed: %s\n' "$file" >&2; status=1
    fi
    if [[ -f "$ROOT_DIR/$file" ]] && grep -Eiq \
      '\b(DROP|TRUNCATE|DELETE[[:space:]]+FROM|ALTER[[:space:]]+TABLE.+DROP)\b' "$ROOT_DIR/$file"; then
      printf 'destructive migration statement requires owner-approved runbook: %s\n' "$file" >&2
      status=1
    fi
  done < <(changed_files_nul)
  return "$status"
}

check_prompt_evals() {
  local file prompt_changed=0 eval_changed=0
  while IFS= read -r -d '' file; do
    [[ "$file" == prompts/* ]] && prompt_changed=1
    [[ "$file" == evals/* ]] && eval_changed=1
  done < <(changed_files_nul)
  (( prompt_changed == 0 )) && return 0
  (( eval_changed == 1 )) || {
    printf 'prompt changes require eval changes under evals/\n' >&2
    return 1
  }
  [[ ! -x "$ROOT_DIR/evals/run.sh" ]] || "$ROOT_DIR/evals/run.sh"
}

run_benchmarks() {
  go test -run '^$' -bench=. -benchmem ./... >"$ARTIFACT_DIR/bench/current.txt"
  runner_cleanup_hook
  git -C "$ROOT_DIR" worktree add --detach "$BENCH_WORKTREE" "$COMPARE_SHA" >/dev/null
  (cd "$BENCH_WORKTREE" && go test -run '^$' -bench=. -benchmem ./... \
    >"$ARTIFACT_DIR/bench/base.txt")
  benchstat "$ARTIFACT_DIR/bench/base.txt" "$ARTIFACT_DIR/bench/current.txt" \
    >"$ARTIFACT_DIR/bench/benchstat.txt"
  runner_cleanup_hook
}

run_selected_benchmarks() {
  printf 'selection: %s\n' "$1"
  run_benchmarks
}

run_race_tests() {
  printf 'selection: %s\n' "$1"
  go test -race ./...
}

check_initial_context() {
  [[ "$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}')" == "$HEAD_SHA" ]] || {
    printf 'HEAD changed while collecting the initial release context\n' >&2; return 1;
  }
  [[ "$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{tree}')" == "$HEAD_TREE_SHA" ]] || {
    printf 'HEAD tree changed while collecting the initial release context\n' >&2; return 1;
  }
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - \
    "$SNAPSHOT_FILE" "$HEAD_SHA" "$HEAD_TREE_SHA" "$COMPARE_SHA" "$MERGE_BASE_SHA" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
expected = dict(zip(
    ("head_sha", "head_tree_sha", "base_sha", "merge_base_sha", "mode"),
    (*sys.argv[2:], "release"),
))
if any(payload.get(key) != value for key, value in expected.items()):
    raise SystemExit("change snapshot does not match resolved release context")
if payload.get("clean") is not True:
    raise SystemExit("change snapshot reports a dirty release candidate")
PY
  is_clean || { printf 'release verification requires a clean working tree\n' >&2; return 1; }
}

check_final_context() {
  [[ "$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}')" == "$HEAD_SHA" ]] || {
    printf 'HEAD changed during release verification\n' >&2; return 1;
  }
  [[ "$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{tree}')" == "$HEAD_TREE_SHA" ]] || {
    printf 'HEAD tree changed during release verification\n' >&2; return 1;
  }
  [[ "$(git -C "$ROOT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || printf DETACHED)" == "$BRANCH" ]] || {
    printf 'branch changed during release verification\n' >&2; return 1;
  }
  is_clean || { printf 'working tree became dirty during release verification\n' >&2; return 1; }
}

run_conditional_gate() {
  local gate="$1" command="$2" reason status
  if reason="$(runner_gate_reason "$gate")"; then
    recorded_run "$gate" "$command" "$reason"
  else
    status=$?
    [[ "$status" == 3 ]] || die "cannot select conditional gate $gate"
    recorded_skip "$gate" "$reason"
  fi
}

cd "$ROOT_DIR"
case "$VERIFY_PERFORMANCE_REQUESTED" in 0|1) ;; *) die "VERIFY_PERFORMANCE_REQUESTED must be 0 or 1" ;; esac
runner_validate_profile
runner_resolve_context

recorded_run change_scope collect_scope
seal_artifact change_scope.json
recorded_run release_context_before check_initial_context
recorded_run toolchain ensure_tools golangci-lint govulncheck gitleaks benchstat
start_parallel_gate symlinks check_symlinks
start_parallel_gate gofmt check_gofmt
start_parallel_gate build go build ./...
start_parallel_gate vet go vet ./...
start_parallel_gate golangci golangci-lint run ./...
finish_parallel_batch

start_parallel_gate test_unit_coverage \
  go test -coverpkg=./... -coverprofile="$ARTIFACT_DIR/coverage.out" ./...
start_parallel_gate govulncheck govulncheck ./...
start_parallel_gate gitleaks check_gitleaks
start_parallel_gate ai_boundaries check_boundaries
finish_parallel_batch
seal_artifact ai_boundaries.json

recorded_run coverage_threshold check_coverage
seal_artifact coverage.out
seal_artifact coverage_percent.txt
run_conditional_gate test_race run_race_tests
start_parallel_gate migration_safety check_migrations
start_parallel_gate prompt_evals check_prompt_evals
start_parallel_gate spec_registry check_spec_registry
finish_parallel_batch
seal_artifact spec_registry.json

run_conditional_gate benchmarks run_selected_benchmarks
if [[ -f "$ARTIFACT_DIR/bench/current.txt" ]]; then
  seal_artifact bench/current.txt
  seal_artifact bench/base.txt
  seal_artifact bench/benchstat.txt
fi
recorded_run release_context_after check_final_context
runner_complete
