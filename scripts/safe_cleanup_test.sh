#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/safe_cleanup.sh"
TMP_PARENT="$(mktemp -d)"
TMP_NAME="${TMP_PARENT##*/}"
OUTSIDE="$(mktemp -d)"
OUTSIDE_NAME="${OUTSIDE##*/}"

cleanup() {
  safe_remove_tree "$TMP_PARENT" "$(dirname "$TMP_PARENT")" "$TMP_NAME"
  safe_remove_tree "$OUTSIDE" "$(dirname "$OUTSIDE")" "$OUTSIDE_NAME"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

expect_rejected() {
  if safe_remove_tree "$@" >"$TMP_PARENT/out" 2>"$TMP_PARENT/err"; then
    fail "unsafe cleanup was accepted: $*"
  fi
}

mkdir -p "$TMP_PARENT/generated/nested"
printf 'owned\n' >"$TMP_PARENT/generated/nested/file"
safe_remove_tree "$TMP_PARENT/generated" "$TMP_PARENT" generated
[[ ! -e "$TMP_PARENT/generated" ]] || fail "generated directory was not removed"

mkdir -p "$TMP_PARENT/first" "$TMP_PARENT/second"
safe_remove_children "$TMP_PARENT" first second
[[ ! -e "$TMP_PARENT/first" && ! -e "$TMP_PARENT/second" ]] || \
  fail "generated child directories were not removed"

safe_remove_tree "$TMP_PARENT/missing" "$TMP_PARENT" missing
expect_rejected "" "$TMP_PARENT" generated
expect_rejected / / root
expect_rejected "$TMP_PARENT" "$TMP_PARENT" "$TMP_NAME"
mkdir -p "$TMP_PARENT/.git"
expect_rejected "$TMP_PARENT/.git" "$TMP_PARENT" .git
ln -s "$OUTSIDE" "$TMP_PARENT/link"
expect_rejected "$TMP_PARENT/link" "$TMP_PARENT" link
mkdir -p "$TMP_PARENT/other"
expect_rejected "$TMP_PARENT/other" "$TMP_PARENT" expected

printf 'safe cleanup tests passed\n'
