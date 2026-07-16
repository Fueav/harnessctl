#!/usr/bin/env bash
set -euo pipefail
ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ROOT_DIR="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"
export HARNESS_PROJECT_ROOT="$ROOT_DIR"
exec python3 -I -B -S "$ENGINE_DIR/check_ai_boundaries.py" "$@"
