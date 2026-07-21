#!/usr/bin/env bash
# Checked cleanup for generated directories owned by Harness.

safe_remove_tree() {
  if (( $# != 3 )); then
    printf 'safe_remove_tree requires target, parent, and expected basename\n' >&2
    return 2
  fi

  local target="$1"
  local allowed_parent="$2"
  local expected_name="$3"
  local actual_name parent_physical target_parent target_physical

  [[ -n "$target" && -n "$allowed_parent" && -n "$expected_name" ]] || {
    printf 'safe_remove_tree rejects empty arguments\n' >&2
    return 2
  }
  [[ "$target" == /* && "$allowed_parent" == /* ]] || {
    printf 'safe_remove_tree requires absolute paths\n' >&2
    return 2
  }
  [[ "$allowed_parent" != "/" && -d "$allowed_parent" && ! -L "$allowed_parent" ]] || {
    printf 'safe_remove_tree rejects unsafe parent: %s\n' "$allowed_parent" >&2
    return 2
  }

  actual_name="${target##*/}"
  [[ "$actual_name" == "$expected_name" && "$actual_name" != ".git" ]] || {
    printf 'safe_remove_tree basename mismatch: %s\n' "$target" >&2
    return 2
  }
  [[ ! -L "$target" ]] || {
    printf 'safe_remove_tree rejects symlink target: %s\n' "$target" >&2
    return 2
  }
  [[ -e "$target" ]] || return 0
  [[ -d "$target" ]] || {
    printf 'safe_remove_tree target is not a directory: %s\n' "$target" >&2
    return 2
  }

  parent_physical="$(cd "$allowed_parent" && pwd -P)" || return 2
  target_parent="$(cd "$(dirname "$target")" && pwd -P)" || return 2
  [[ "$target_parent" == "$parent_physical" ]] || {
    printf 'safe_remove_tree target is not a direct child of %s: %s\n' \
      "$allowed_parent" "$target" >&2
    return 2
  }

  target_physical="$(cd "$target" && pwd -P)" || return 2
  [[ "$target_physical" == "$parent_physical/$actual_name" ]] || {
    printf 'safe_remove_tree target resolves outside parent: %s\n' "$target" >&2
    return 2
  }

  find "$target" -depth -delete
}

safe_remove_children() {
  if (( $# < 2 )); then
    printf 'safe_remove_children requires parent and child names\n' >&2
    return 2
  fi
  local parent="$1" name
  shift
  for name in "$@"; do
    safe_remove_tree "$parent/$name" "$parent" "$name" || return
  done
}
