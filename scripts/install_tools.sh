#!/usr/bin/env bash
set -euo pipefail

ENGINE_DIR="${HARNESS_ENGINE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ROOT_DIR="${HARNESS_PROJECT_ROOT:-$(cd "$ENGINE_DIR/.." && pwd)}"
VERSIONS_FILE="${HARNESS_TOOL_VERSIONS:-$ROOT_DIR/scripts/tool_versions.env}"

if [[ ! -f "$VERSIONS_FILE" || -L "$VERSIONS_FILE" ]]; then
  printf 'tool version manifest must be a regular non-symlink file: %s\n' \
    "$VERSIONS_FILE" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$VERSIONS_FILE"

: "${GOLANGCI_LINT_VERSION:?missing GOLANGCI_LINT_VERSION}"
: "${GOVULNCHECK_VERSION:?missing GOVULNCHECK_VERSION}"
: "${GITLEAKS_VERSION:?missing GITLEAKS_VERSION}"
: "${BENCHSTAT_VERSION:?missing BENCHSTAT_VERSION}"

GOBIN="${GOBIN:-$ROOT_DIR/.tools/bin}"
if [[ -L "$GOBIN" ]]; then
  printf 'GOBIN must not be a symlink: %s\n' "$GOBIN" >&2
  exit 2
fi
mkdir -p "$GOBIN"
export GOBIN
TOOLS_LOCK="$GOBIN/.harness-tools-lock.json"

if PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - \
  "$VERSIONS_FILE" "$TOOLS_LOCK" "$GOBIN" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys

versions = pathlib.Path(sys.argv[1])
lock_path = pathlib.Path(sys.argv[2])
gobin = pathlib.Path(sys.argv[3])
try:
    lock_info = lock_path.lstat()
    if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
        raise ValueError
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    expected_manifest = hashlib.sha256(versions.read_bytes()).hexdigest()
    if payload.get("schema_version") != 1 or payload.get("manifest_sha256") != expected_manifest:
        raise ValueError
    tools = payload.get("tools")
    if not isinstance(tools, dict) or set(tools) != {
        "benchstat", "gitleaks", "golangci-lint", "govulncheck"
    }:
        raise ValueError
    for name, expected in tools.items():
        path = gobin / name
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError
        if info.st_mode & 0o111 == 0:
            raise ValueError
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError
except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
    raise SystemExit(1)
PY
then
  printf 'pinned harness tools restored from validated cache: %s\n' "$GOBIN"
  exit 0
fi

go install "github.com/golangci/golangci-lint/cmd/golangci-lint@$GOLANGCI_LINT_VERSION"
go install "golang.org/x/vuln/cmd/govulncheck@$GOVULNCHECK_VERSION"
go install "github.com/zricethezav/gitleaks/v8@$GITLEAKS_VERSION"
go install "golang.org/x/perf/cmd/benchstat@$BENCHSTAT_VERSION"

PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - \
  "$VERSIONS_FILE" "$TOOLS_LOCK" "$GOBIN" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile

versions = pathlib.Path(sys.argv[1])
lock_path = pathlib.Path(sys.argv[2])
gobin = pathlib.Path(sys.argv[3])
tools = {}
for name in ("benchstat", "gitleaks", "golangci-lint", "govulncheck"):
    path = gobin / name
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"installed tool is not a regular file: {path}")
    if info.st_mode & 0o111 == 0:
        raise SystemExit(f"installed tool is not executable: {path}")
    tools[name] = hashlib.sha256(path.read_bytes()).hexdigest()
payload = {
    "manifest_sha256": hashlib.sha256(versions.read_bytes()).hexdigest(),
    "schema_version": 1,
    "tools": tools,
}
encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
descriptor, temporary_name = tempfile.mkstemp(
    dir=os.fspath(gobin), prefix=".harness-tools-lock.", suffix=".tmp"
)
try:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_name, lock_path)
except BaseException:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
    raise
PY
