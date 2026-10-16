#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
unset AI_BOUNDARY_APPROVED AI_BOUNDARY_APPROVAL_EVIDENCE AI_BOUNDARY_APPROVAL_MODE AI_BOUNDARY_ARTIFACT_DIR AI_BOUNDARY_COMPARE_REF AI_BOUNDARY_MODE VERIFY_COMPARE_REF
"$ROOT_DIR/scripts/safe_cleanup_test.sh"
"$ROOT_DIR/scripts/resources_test.py"
"$ROOT_DIR/scripts/collect_changes_test.sh"
"$ROOT_DIR/scripts/check_ai_boundaries_test.sh"
python3 "$ROOT_DIR/scripts/workbench_test.py"
