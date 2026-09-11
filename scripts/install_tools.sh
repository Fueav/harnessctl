#!/usr/bin/env bash
set -euo pipefail
ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
export HARNESS_PROJECT_ROOT="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"
export HARNESS_TOOL_VERSIONS="${HARNESS_TOOL_VERSIONS:-$HARNESS_PROJECT_ROOT/scripts/tool_versions.env}"
[[ -f "$HARNESS_TOOL_VERSIONS" && ! -L "$HARNESS_TOOL_VERSIONS" ]] || { printf 'invalid tool version manifest\n' >&2; exit 2; }
source "$HARNESS_TOOL_VERSIONS"
export GOLANGCI_LINT_VERSION GOVULNCHECK_VERSION GITLEAKS_VERSION BENCHSTAT_VERSION
exec python3 -I -B -S "$ENGINE_DIR/install_tools.py" "$@"
