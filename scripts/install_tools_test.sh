#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

mkdir -p "$TMP_DIR/fake-bin" "$TMP_DIR/gobin"
cat >"$TMP_DIR/fake-bin/go" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_GO_LOG"
module="${2:-}"
case "$module" in
  github.com/golangci/golangci-lint/cmd/golangci-lint@*) name=golangci-lint ;;
  golang.org/x/vuln/cmd/govulncheck@*) name=govulncheck ;;
  github.com/zricethezav/gitleaks/v8@*) name=gitleaks ;;
  golang.org/x/perf/cmd/benchstat@*) name=benchstat ;;
  *) printf 'unexpected module: %s\n' "$module" >&2; exit 2 ;;
esac
printf '#!/usr/bin/env bash\nexit 0\n' >"$GOBIN/$name"
chmod +x "$GOBIN/$name"
SH
chmod +x "$TMP_DIR/fake-bin/go"

: >"$TMP_DIR/go.log"
env \
  PATH="$TMP_DIR/fake-bin:$PATH" \
  GOBIN="$TMP_DIR/gobin" \
  FAKE_GO_LOG="$TMP_DIR/go.log" \
  "$ROOT_DIR/scripts/install_tools.sh"

cat >"$TMP_DIR/expected.log" <<'EOF'
install github.com/golangci/golangci-lint/cmd/golangci-lint@v1.64.8
install golang.org/x/vuln/cmd/govulncheck@v1.1.4
install github.com/zricethezav/gitleaks/v8@v8.24.2
install golang.org/x/perf/cmd/benchstat@v0.0.0-20230717203022-1ba3a21238c9
EOF

if ! diff -u "$TMP_DIR/expected.log" "$TMP_DIR/go.log"; then
  fail "tool installer did not use the exact pinned module versions"
fi

env \
  PATH="$TMP_DIR/fake-bin:$PATH" \
  GOBIN="$TMP_DIR/gobin" \
  FAKE_GO_LOG="$TMP_DIR/go.log" \
  "$ROOT_DIR/scripts/install_tools.sh"
if ! diff -u "$TMP_DIR/expected.log" "$TMP_DIR/go.log"; then
  fail "valid cached harness tools were installed again"
fi

printf '# tampered\n' >>"$TMP_DIR/gobin/gitleaks"
env \
  PATH="$TMP_DIR/fake-bin:$PATH" \
  GOBIN="$TMP_DIR/gobin" \
  FAKE_GO_LOG="$TMP_DIR/go.log" \
  "$ROOT_DIR/scripts/install_tools.sh"
cat "$TMP_DIR/expected.log" "$TMP_DIR/expected.log" >"$TMP_DIR/reinstalled.log"
if ! diff -u "$TMP_DIR/reinstalled.log" "$TMP_DIR/go.log"; then
  fail "tampered cached harness tools were not reinstalled from pins"
fi

if grep -R -F '@latest' \
  "$ROOT_DIR/scripts/tool_versions.env" \
  "$ROOT_DIR/scripts/install_tools.sh" \
  "$ROOT_DIR/scripts/verify_release.sh" \
  "$ROOT_DIR/.github/workflows/ci.yml"; then
  fail "mandatory tool paths still contain @latest"
fi

printf 'install tools tests passed\n'
