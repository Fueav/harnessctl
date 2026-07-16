#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODULE=""
SERVICE=""
OWNER=""

usage() {
  cat <<'USAGE'
Usage: scripts/init_project.sh --module <go-module> --service <service-name> --owner <github-owner>
USAGE
}

while (($#)); do
  case "$1" in
    --module) shift; MODULE="${1:-}" ;;
    --service) shift; SERVICE="${1:-}" ;;
    --owner) shift; OWNER="${1:-}" ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

if [[ -z "$MODULE" || -z "$SERVICE" || -z "$OWNER" ]]; then
  usage >&2
  exit 1
fi

replace_in_files() {
  local old="$1"
  local new="$2"
  shift 2
  for file in "$@"; do
    [[ -f "$file" ]] || continue
    sed -i.bak "s|$old|$new|g" "$file"
    rm -f "$file.bak"
  done
}

files=()
while IFS= read -r file; do
  files+=("$file")
done < <(find "$ROOT_DIR" -type f \
  -not -path "$ROOT_DIR/.git/*" \
  -not -path "$ROOT_DIR/.artifacts/*" \
  -not -path "$ROOT_DIR/.tools/*")

replace_in_files "github.com/your-org/ai-first-go-template" "$MODULE" "${files[@]}"
replace_in_files "ai-first-go-template" "$SERVICE" "${files[@]}"
replace_in_files "@platform-team" "$OWNER" "${files[@]}"

gofmt -w "$ROOT_DIR"/cmd "$ROOT_DIR"/internal "$ROOT_DIR"/pkg

printf 'initialized project: module=%s service=%s owner=%s\n' "$MODULE" "$SERVICE" "$OWNER"
