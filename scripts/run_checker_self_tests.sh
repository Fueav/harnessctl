#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export VERIFY_SELF_TESTS=0
unset \
  AI_BOUNDARY_APPROVED \
  AI_BOUNDARY_APPROVAL_EVIDENCE \
  AI_BOUNDARY_APPROVAL_MODE \
  AI_BOUNDARY_ARTIFACT_DIR \
  AI_BOUNDARY_COMPARE_REF \
  AI_BOUNDARY_MODE \
  VERIFY_ARTIFACT_DIR \
  VERIFY_CANDIDATE_ARTIFACT_DIR \
  VERIFY_COMPARE_REF \
  VERIFY_EVIDENCE_MODE \
  VERIFY_PERFORMANCE_REQUESTED \
  VERIFY_PROFILE

"$ROOT_DIR/scripts/collect_changes_test.sh"
"$ROOT_DIR/scripts/check_ai_boundaries_test.sh"
"$ROOT_DIR/scripts/check_spec_registry_test.sh"
"$ROOT_DIR/scripts/workspace_preflight_test.sh"
"$ROOT_DIR/scripts/install_tools_test.sh"
"$ROOT_DIR/scripts/changed_go_packages_test.sh"
"$ROOT_DIR/scripts/harness_config_test.sh"
"$ROOT_DIR/scripts/verify_runner_test.sh"
"$ROOT_DIR/scripts/harness_structure_test.sh"
"$ROOT_DIR/scripts/finalize_approval_test.sh"
"$ROOT_DIR/scripts/verify_release_test.sh"
