#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ROOT_DIR="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"

export VERIFY_PROFILE=pull_request
export VERIFY_EVIDENCE_MODE=candidate
export VERIFY_ARTIFACT_DIR="${VERIFY_CANDIDATE_ARTIFACT_DIR:-$ROOT_DIR/.artifacts/candidate}"
export AI_BOUNDARY_APPROVAL_MODE=deferred
export AI_BOUNDARY_APPROVED=0
unset AI_BOUNDARY_APPROVAL_EVIDENCE

exec "$ENGINE_DIR/verify_release.sh" "$@"
