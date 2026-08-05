#!/usr/bin/env bash
# Shared artifact, gate, seal, and summary lifecycle for Harness verifiers.

: "${ROOT_DIR:?ROOT_DIR is required before sourcing verify_runner.sh}"
: "${ENGINE_DIR:?ENGINE_DIR is required before sourcing verify_runner.sh}"
: "${ARTIFACT_DIR_RAW:?ARTIFACT_DIR_RAW is required}"
: "${COMPARE_REF:=}"
: "${RUNNER_SNAPSHOT_MODE:?RUNNER_SNAPSHOT_MODE is required}"
: "${RUNNER_EVIDENCE_MODE:?RUNNER_EVIDENCE_MODE is required}"
: "${RUNNER_PROFILE:?RUNNER_PROFILE is required}"
: "${RUNNER_LABEL:=verification}"
: "${RUNNER_COVERAGE_THRESHOLD:=}"

EVIDENCE_TOOL="$ENGINE_DIR/lib/evidence.py"
CONFIG_TOOL="$ENGINE_DIR/lib/harness_config.py"; source "$ENGINE_DIR/lib/safe_cleanup.sh"
if declare -A ENABLED_GATES 2>/dev/null; then
  GATE_MAP_ASSOCIATIVE=1
else
  ENABLED_GATES=()
  GATE_MAP_ASSOCIATIVE=0
fi

runner_init() {
  local configured_artifacts configured_gates gate
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$EVIDENCE_TOOL" mutable-root \
    --repo "$ROOT_DIR" --path "$ARTIFACT_DIR_RAW" --prefix .artifacts --invalidate \
    >/dev/null || exit 2
  ARTIFACT_DIR="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$EVIDENCE_TOOL" mutable-root --repo "$ROOT_DIR" \
    --path "$ARTIFACT_DIR_RAW" --prefix .artifacts)" || exit 2
  TOOLS_DIR="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$EVIDENCE_TOOL" mutable-root --repo "$ROOT_DIR" \
    --path "$ROOT_DIR/.tools/bin" --prefix .tools)" || exit 2
  GATES_FILE="$ARTIFACT_DIR/gates.tsv"
  GATE_RESULTS_DIR="$ARTIFACT_DIR/gate-results"
  SNAPSHOT_FILE="$ARTIFACT_DIR/change_scope.json"
  STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  BRANCH="$(git -C "$ROOT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || printf DETACHED)"
  COMPARE_SHA="" HEAD_SHA="" HEAD_TREE_SHA="" MERGE_BASE_SHA=""
  COMPLETE=0
  FAILURE_REASON="$RUNNER_LABEL did not complete"
  SNAPSHOT_SHA256=""
  SEALED_ARTIFACT_ARGUMENTS=()
  PARALLEL_GATE_NAMES=() PARALLEL_GATE_PIDS=() PARALLEL_GATE_RESULTS=()

  mkdir -p "$ARTIFACT_DIR" "$TOOLS_DIR"
  safe_remove_children "$ARTIFACT_DIR" logs bench gate-results
  if ! configured_artifacts="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$CONFIG_TOOL" artifacts)"; then
    printf 'cannot load configured artifacts\n' >&2
    return 2
  fi
  if [[ -n "$configured_artifacts" ]]; then
    while IFS= read -r relative; do
      [[ -n "$relative" ]] && rm -f "$ARTIFACT_DIR/$relative"
    done <<<"$configured_artifacts"
  fi
  rm -f "$ARTIFACT_DIR/summary.json" "$ARTIFACT_DIR/artifact_manifest.json"
  mkdir -p "$ARTIFACT_DIR/logs" "$ARTIFACT_DIR/bench" "$GATE_RESULTS_DIR"
  : >"$GATES_FILE"
  export GOBIN="$TOOLS_DIR"
  export PATH="$TOOLS_DIR:$PATH"

  CLEAN_BEFORE=unknown
  if is_clean; then CLEAN_BEFORE=true; elif [[ "$?" == 1 ]]; then CLEAN_BEFORE=false; fi
  trap runner_summary_on_exit EXIT
  if ! configured_gates="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$CONFIG_TOOL" profile-gates --profile "$RUNNER_PROFILE")"; then
    FAILURE_REASON="invalid verification profile: $RUNNER_PROFILE"
    printf 'cannot load configured gates for profile %s\n' "$RUNNER_PROFILE" >&2
    return 2
  fi
  ENABLED_GATES=()
  while IFS= read -r gate; do
    [[ -n "$gate" ]] || continue
    if (( GATE_MAP_ASSOCIATIVE == 1 )); then
      ENABLED_GATES["$gate"]=1
    else
      ENABLED_GATES[${#ENABLED_GATES[@]}]="$gate"
    fi
  done <<<"$configured_gates"
}

gate_enabled() {
  local gate
  if (( GATE_MAP_ASSOCIATIVE == 1 )); then
    [[ -n "${ENABLED_GATES[$1]+configured}" ]]
    return
  fi
  for gate in "${ENABLED_GATES[@]}"; do
    [[ "$gate" == "$1" ]] && return 0
  done
  return 1
}

is_clean() {
  local output
  if ! output="$(git -C "$ROOT_DIR" status --porcelain=v1 --untracked-files=all)"; then
    return 2
  fi
  [[ -z "$output" ]]
}

file_sha256() {
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$EVIDENCE_TOOL" sha256 "$1"
}

die() {
  FAILURE_REASON="$1"
  printf '%s: %s\n' "$RUNNER_LABEL" "$1" >&2
  exit 2
}

runner_resolve_context() {
  [[ -n "$COMPARE_REF" ]] || die "VERIFY_COMPARE_REF is required"
  COMPARE_SHA="$(git -C "$ROOT_DIR" rev-parse --verify --end-of-options "$COMPARE_REF^{commit}" 2>/dev/null)" || \
    die "VERIFY_COMPARE_REF does not resolve to a commit: $COMPARE_REF"
  HEAD_SHA="$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}')" || die "HEAD does not resolve to a commit"
  HEAD_TREE_SHA="$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{tree}')" || die "HEAD tree does not resolve"
  MERGE_BASE_SHA="$(git -C "$ROOT_DIR" merge-base "$COMPARE_SHA" "$HEAD_SHA")" || \
    die "compare commit and HEAD have no merge base"
}

runner_validate_profile() {
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$CONFIG_TOOL" validate \
    --profile "$RUNNER_PROFILE" --mode "$RUNNER_EVIDENCE_MODE" || \
    die "invalid verification profile or evidence mode: $RUNNER_PROFILE/$RUNNER_EVIDENCE_MODE"
}

runner_gate_reason() {
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$CONFIG_TOOL" decision \
    --profile "$RUNNER_PROFILE" --gate "$1" --snapshot "$SNAPSHOT_FILE" \
    --snapshot-sha256 "$SNAPSHOT_SHA256" --repo "$ROOT_DIR" \
    --base "$COMPARE_SHA" --head "$HEAD_SHA" \
    --performance-requested "${VERIFY_PERFORMANCE_REQUESTED:-0}"
}

collect_scope() {
  local temporary
  temporary="$(mktemp "$ARTIFACT_DIR/.change_scope.XXXXXX")"
  if PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$ENGINE_DIR/collect_changes.py" \
    --repo "$ROOT_DIR" --base "$COMPARE_SHA" --mode "$RUNNER_SNAPSHOT_MODE" \
    --format json >"$temporary"; then
    mv "$temporary" "$SNAPSHOT_FILE"
    SNAPSHOT_SHA256="$(file_sha256 "$SNAPSHOT_FILE")"
  else
    rm -f "$temporary"
    return 1
  fi
}

seal_artifact() {
  local relative="$1" digest
  digest="$(file_sha256 "$ARTIFACT_DIR/$relative")" || {
    FAILURE_REASON="cannot seal artifact $relative"
    return 1
  }
  SEALED_ARTIFACT_ARGUMENTS+=(--sealed-artifact "$relative=$digest")
}

recorded_run() {
  local name="$1" log_relative log started finished duration status=failed command_status=0 digest
  shift
  log_relative="logs/$name.log"; log="$ARTIFACT_DIR/$log_relative"; started="$(date +%s)"
  printf '==> %s\n' "$name"
  if "$@" >"$log" 2>&1; then status=passed; else command_status=$?; FAILURE_REASON="gate $name failed"; fi
  finished="$(date +%s)"; duration=$((finished - started)); digest="$(file_sha256 "$log")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$status" "$duration" "$log_relative" "$digest" >>"$GATES_FILE"
  if [[ "$status" == passed ]]; then printf 'PASS %s\n' "$name"; return 0; fi
  printf 'FAIL %s (see %s)\n' "$name" "$log" >&2
  tail -n 80 "$log" >&2 || true
  return "$command_status"
}

recorded_skip() {
  local name="$1" reason="$2" relative="logs/$1.log" digest
  printf '%s\n' "$reason" >"$ARTIFACT_DIR/$relative"
  digest="$(file_sha256 "$ARTIFACT_DIR/$relative")"
  printf '%s\tskipped\t0\t%s\t%s\n' "$name" "$relative" "$digest" >>"$GATES_FILE"
  printf 'SKIP %s (%s)\n' "$name" "$reason"
}

run_custom_gate_command() {
  local run="$1" script="$ROOT_DIR/$1"
  if [[ -L "$script" || ! -f "$script" || ! -x "$script" ]]; then
    printf 'custom gate script must be a regular non-symlink executable: %s\n' "$run" >&2
    return 1
  fi
  env -u HARNESS_ENGINE_DIR \
    HARNESS_PROJECT_ROOT="$ROOT_DIR" \
    HARNESS_ARTIFACT_DIR="$ARTIFACT_DIR" \
    HARNESS_SNAPSHOT_FILE="$SNAPSHOT_FILE" \
    HARNESS_SNAPSHOT_SHA256="$SNAPSHOT_SHA256" \
    HARNESS_COMPARE_SHA="$COMPARE_SHA" \
    HARNESS_HEAD_SHA="$HEAD_SHA" \
    HARNESS_PROFILE="$RUNNER_PROFILE" \
    HARNESS_EVIDENCE_MODE="$RUNNER_EVIDENCE_MODE" \
    "$script"
}

run_custom_gates() {
  local name run artifact custom_output artifact_output
  if ! custom_output="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
    "$CONFIG_TOOL" custom-gates --profile "$RUNNER_PROFILE")"; then
    FAILURE_REASON="cannot load custom gates for profile $RUNNER_PROFILE"
    printf '%s: %s\n' "$RUNNER_LABEL" "$FAILURE_REASON" >&2
    return 2
  fi
  while IFS=$'\t' read -r name run; do
    [[ -n "$name" && -n "$run" ]] || continue
    local reason status
    if reason="$(runner_gate_reason "$name")"; then
      recorded_run "$name" run_custom_gate_command "$run"
    else
      status=$?
      [[ "$status" == 3 ]] || {
        FAILURE_REASON="cannot select custom gate $name"
        printf '%s: %s\n' "$RUNNER_LABEL" "$FAILURE_REASON" >&2
        return 2
      }
      recorded_skip "$name" "$reason"
      continue
    fi
    if ! artifact_output="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S \
      "$CONFIG_TOOL" gate-artifacts --gate "$name")"; then
      FAILURE_REASON="cannot load artifact declarations for gate $name"
      printf '%s: %s\n' "$RUNNER_LABEL" "$FAILURE_REASON" >&2
      return 2
    fi
    if [[ -n "$artifact_output" ]]; then
      while IFS= read -r artifact; do
        [[ -n "$artifact" ]] && seal_artifact "$artifact"
      done <<<"$artifact_output"
    fi
  done <<<"$custom_output"
}

start_parallel_gate() {
  local name="$1" ordinal relative log result
  shift
  ordinal="${#PARALLEL_GATE_NAMES[@]}"; relative="logs/$name.log"
  log="$ARTIFACT_DIR/$relative"; result="$GATE_RESULTS_DIR/$(printf '%03d' "$ordinal")-$name.tsv"
  printf '==> %s (parallel)\n' "$name"
  (
    local started finished status=passed command_status=0 digest
    started="$(date +%s)"
    if "$@" >"$log" 2>&1; then status=passed; else command_status=$?; status=failed; fi
    finished="$(date +%s)"; digest="$(file_sha256 "$log")"
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$status" "$((finished - started))" "$relative" "$digest" >"$result"
    exit "$command_status"
  ) &
  PARALLEL_GATE_NAMES+=("$name"); PARALLEL_GATE_PIDS+=("$!"); PARALLEL_GATE_RESULTS+=("$result")
}

finish_parallel_batch() {
  local index status=0 name gate_status duration relative digest
  for index in "${!PARALLEL_GATE_PIDS[@]}"; do wait "${PARALLEL_GATE_PIDS[$index]}" || status=1; done
  for index in "${!PARALLEL_GATE_RESULTS[@]}"; do
    IFS=$'\t' read -r name gate_status duration relative digest <"${PARALLEL_GATE_RESULTS[$index]}"
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$gate_status" "$duration" "$relative" "$digest" >>"$GATES_FILE"
    if [[ "$gate_status" == passed ]]; then printf 'PASS %s\n' "$name"; else
      FAILURE_REASON="gate $name failed"; printf 'FAIL %s (see %s)\n' "$name" "$ARTIFACT_DIR/$relative" >&2
      tail -n 80 "$ARTIFACT_DIR/$relative" >&2 || true
    fi
  done
  PARALLEL_GATE_NAMES=(); PARALLEL_GATE_PIDS=(); PARALLEL_GATE_RESULTS=()
  safe_remove_tree "$GATE_RESULTS_DIR" "$ARTIFACT_DIR" gate-results; mkdir -p "$GATE_RESULTS_DIR"
  return "$status"
}

ensure_tools() {
  local command_name
  for command_name in "$@"; do
    command -v "$command_name" >/dev/null 2>&1 || {
      printf 'missing Harness tool %s; run scripts/install_tools.sh\n' "$command_name" >&2
      return 1
    }
  done
}

check_gofmt() {
  local -a tracked=()
  local file files
  while IFS= read -r -d '' file; do
    [[ -f "$ROOT_DIR/$file" ]] && tracked+=("$ROOT_DIR/$file")
  done < <(git -C "$ROOT_DIR" ls-files -z -- '*.go')
  (( ${#tracked[@]} == 0 )) && return 0
  files="$(gofmt -l "${tracked[@]}")"
  [[ -z "$files" ]] || { printf 'gofmt needed:\n%s\n' "$files" >&2; return 1; }
}

check_boundaries() {
  AI_BOUNDARY_MODE="$RUNNER_SNAPSHOT_MODE" AI_BOUNDARY_COMPARE_REF="$COMPARE_SHA" \
    AI_BOUNDARY_ARTIFACT_DIR="$ARTIFACT_DIR" "$ENGINE_DIR/check_ai_boundaries.sh" \
    --snapshot-file "${SNAPSHOT_FILE#"$ROOT_DIR/"}" --snapshot-sha256 "$SNAPSHOT_SHA256"
}

check_spec_registry() {
  SPEC_REGISTRY_ARTIFACT_DIR="$ARTIFACT_DIR" \
    VERIFY_COMPARE_REF="$COMPARE_SHA" "$ENGINE_DIR/check_spec_registry.sh" \
    --snapshot-file "${SNAPSHOT_FILE#"$ROOT_DIR/"}" --snapshot-sha256 "$SNAPSHOT_SHA256"
}

runner_summary_on_exit() {
  local exit_code=$? overall=failed clean_after=unknown finished_at gates_digest
  local -a arguments
  trap - EXIT
  declare -F runner_cleanup_hook >/dev/null && runner_cleanup_hook
  if (( COMPLETE == 1 && exit_code == 0 )); then overall=passed; elif (( exit_code == 0 )); then exit_code=1; fi
  if is_clean; then clean_after=true; elif [[ "$?" == 1 ]]; then clean_after=false; fi
  finished_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"; gates_digest="$(file_sha256 "$GATES_FILE")"
  arguments=(--repo "$ROOT_DIR" --artifact-dir "$ARTIFACT_DIR" --mode "$RUNNER_EVIDENCE_MODE"
    --profile "$RUNNER_PROFILE" --overall "$overall" --compare-ref "$COMPARE_REF" --branch "$BRANCH"
    --started-at "$STARTED_AT" --finished-at "$finished_at" --clean-before "$CLEAN_BEFORE"
    --clean-after "$clean_after" --gates-file "$GATES_FILE" --expected-gates-sha256 "$gates_digest")
  [[ -n "$RUNNER_COVERAGE_THRESHOLD" ]] && arguments+=(--coverage-threshold "$RUNNER_COVERAGE_THRESHOLD")
  (( ${#SEALED_ARTIFACT_ARGUMENTS[@]} > 0 )) && arguments+=("${SEALED_ARTIFACT_ARGUMENTS[@]}")
  [[ -f "$SNAPSHOT_FILE" ]] && arguments+=(--snapshot-file "$SNAPSHOT_FILE")
  [[ -n "$SNAPSHOT_SHA256" ]] && arguments+=(--expected-snapshot-sha256 "$SNAPSHOT_SHA256")
  [[ -n "$HEAD_SHA" ]] && arguments+=(--expected-head-sha "$HEAD_SHA")
  [[ -n "$HEAD_TREE_SHA" ]] && arguments+=(--expected-head-tree-sha "$HEAD_TREE_SHA")
  [[ -n "$COMPARE_SHA" ]] && arguments+=(--expected-compare-sha "$COMPARE_SHA")
  [[ -n "$MERGE_BASE_SHA" ]] && arguments+=(--expected-merge-base-sha "$MERGE_BASE_SHA")
  [[ "$overall" == failed ]] && arguments+=(--failure-reason "$FAILURE_REASON")
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S "$ENGINE_DIR/write_release_summary.py" "${arguments[@]}" || exit_code=1
  [[ "$overall" == passed && "$exit_code" == 0 ]] && printf '%s evidence written to %s\n' "$RUNNER_LABEL" "$ARTIFACT_DIR"
  exit "$exit_code"
}

runner_complete() {
  COMPLETE=1
  FAILURE_REASON=""
}
