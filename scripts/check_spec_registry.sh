#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ROOT_DIR="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"
exec python3 -I -B -S "$ENGINE_DIR/check_spec_registry.py" --repo "$ROOT_DIR" "$@"
