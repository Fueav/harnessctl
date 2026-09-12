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
if gate_enabled coverage_threshold; then
  if [[ -z "$COVERAGE_THRESHOLD" ]]; then
    COVERAGE_THRESHOLD="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
      "$ENGINE_DIR/lib/harness_config.py" coverage)" || \
      die "cannot load the Harness coverage policy"
  fi
  RUNNER_COVERAGE_THRESHOLD="$COVERAGE_THRESHOLD"
else
  COVERAGE_THRESHOLD=""
  RUNNER_COVERAGE_THRESHOLD=""
fi
BENCH_WORKTREE="$ARTIFACT_DIR/bench/base-worktree"

runner_cleanup_hook() {
  if git -C "$ROOT_DIR" worktree list --porcelain 2>/dev/null | \
    grep -Fqx "worktree $BENCH_WORKTREE"; then
    git -C "$ROOT_DIR" worktree remove --force "$BENCH_WORKTREE" >/dev/null 2>&1 || true
  fi
  safe_remove_tree "$BENCH_WORKTREE" "$ARTIFACT_DIR/bench" base-worktree
  git -C "$ROOT_DIR" worktree prune >/dev/null 2>&1 || true
}

check_symlinks() {
  local link target expected actual configured count=0
  if ! configured="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$CONFIG_TOOL" symlinks)"; then
    printf 'cannot load configured symlinks\n' >&2
    return 1
  fi
  while IFS=$'\t' read -r link target; do
    [[ -n "$link" && -n "$target" ]] || continue
    count=$((count + 1))
    [[ -L "$ROOT_DIR/$link" ]] || {
      printf 'configured symlink is missing: %s\n' "$link" >&2
      return 1
    }
    expected="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - "$link" "$target" <<'PY'
import posixpath
import sys
print(posixpath.relpath(sys.argv[2], posixpath.dirname(sys.argv[1]) or "."))
PY
)" || return 1
    actual="$(readlink "$ROOT_DIR/$link")" || return 1
    [[ "$actual" == "$expected" ]] || {
      printf 'configured symlink %s points to %s, expected %s\n' \
        "$link" "$actual" "$expected" >&2
      return 1
    }
  done <<<"$configured"
  (( count > 0 )) || printf 'no symlinks configured\n'
}

check_gitleaks() {
  local scan_dir="$ARTIFACT_DIR/gitleaks-tree"
  safe_remove_tree "$scan_dir" "$ARTIFACT_DIR" gitleaks-tree; mkdir -p "$scan_dir"
  git -C "$ROOT_DIR" archive --format=tar "$HEAD_SHA" | tar -xf - -C "$scan_dir"
  gitleaks detect --source "$scan_dir" --no-git --redact
  safe_remove_tree "$scan_dir" "$ARTIFACT_DIR" gitleaks-tree
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
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$ENGINE_DIR/check_prompt_evals.py" \
    --repo "$ROOT_DIR" --snapshot "$SNAPSHOT_FILE" --snapshot-sha256 "$SNAPSHOT_SHA256"
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

cd "$ROOT_DIR"
case "$VERIFY_PERFORMANCE_REQUESTED" in 0|1) ;; *) die "VERIFY_PERFORMANCE_REQUESTED must be 0 or 1" ;; esac
runner_validate_profile
runner_resolve_context

recorded_run change_scope collect_scope
seal_artifact change_scope.json
if gate_enabled coverage_threshold && ! gate_will_run coverage_threshold >/dev/null; then
  RUNNER_COVERAGE_THRESHOLD=""
fi
recorded_run release_context_before check_initial_context
gate_enabled toolchain && \
  recorded_run toolchain ensure_tools golangci-lint govulncheck gitleaks benchstat
gate_enabled symlinks && start_parallel_gate symlinks check_symlinks
gate_enabled gofmt && start_parallel_gate gofmt check_gofmt
gate_enabled build && start_parallel_gate build go build ./...
gate_enabled vet && start_parallel_gate vet go vet ./...
gate_enabled golangci && start_parallel_gate golangci golangci-lint run ./...
finish_parallel_batch

if gate_enabled test_unit_coverage; then
  start_parallel_gate test_unit_coverage \
    run_with_test_resources go test -coverpkg=./... -coverprofile="$ARTIFACT_DIR/coverage.out" ./...
fi
gate_enabled govulncheck && start_parallel_gate govulncheck govulncheck ./...
gate_enabled gitleaks && start_parallel_gate gitleaks check_gitleaks
start_parallel_gate ai_boundaries check_boundaries
finish_parallel_batch
seal_artifact ai_boundaries.json
[[ ! -f "$ARTIFACT_DIR/coverage.out" ]] || seal_artifact coverage.out

if gate_enabled coverage_threshold; then
  recorded_run coverage_threshold check_coverage
  [[ ! -f "$ARTIFACT_DIR/coverage_percent.txt" ]] || seal_artifact coverage_percent.txt
fi
gate_enabled test_race && recorded_run test_race run_with_test_resources go test -race ./...
gate_enabled migration_safety && start_parallel_gate migration_safety check_migrations
gate_enabled prompt_evals && start_parallel_gate prompt_evals check_prompt_evals
gate_enabled spec_registry && start_parallel_gate spec_registry check_spec_registry
finish_parallel_batch
gate_enabled spec_registry && seal_artifact spec_registry.json

gate_enabled benchmarks && recorded_run benchmarks run_benchmarks
if gate_enabled benchmarks && [[ -f "$ARTIFACT_DIR/bench/current.txt" ]]; then
  seal_artifact bench/current.txt
  seal_artifact bench/base.txt
  seal_artifact bench/benchstat.txt
fi
run_custom_gates
recorded_run release_context_after check_final_context
runner_complete
