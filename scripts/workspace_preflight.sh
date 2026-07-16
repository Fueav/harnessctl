#!/usr/bin/env bash
set -uo pipefail

PREFIX="[workspace-preflight]"
export GIT_OPTIONAL_LOCKS=0

GIT_BIN="$(command -v git 2>/dev/null || true)"
if [[ -z "$GIT_BIN" ]]; then
  printf '%s git=unavailable\n' "$PREFIX"
  exit 0
fi

ROOT_DIR="$("$GIT_BIN" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT_DIR" ]]; then
  printf '%s repository=unavailable\n' "$PREFIX"
  exit 0
fi

HEAD_SHA="$("$GIT_BIN" -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)"
if [[ ! "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s repository=%s head=unavailable\n' "$PREFIX" "$ROOT_DIR"
  exit 0
fi

BRANCH="$("$GIT_BIN" -C "$ROOT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [[ -z "$BRANCH" ]]; then
  BRANCH="DETACHED"
fi

STATUS_OUTPUT="$("$GIT_BIN" -C "$ROOT_DIR" \
  -c core.fsmonitor=false \
  -c submodule.recurse=false \
  status --porcelain=v1 --untracked-files=all --ignore-submodules=all 2>/dev/null)"
STATUS_CODE=$?
if (( STATUS_CODE != 0 )); then
  WORKTREE_STATE="unknown"
elif [[ -n "$STATUS_OUTPUT" ]]; then
  WORKTREE_STATE="dirty"
else
  WORKTREE_STATE="clean"
fi
printf '%s branch=%s state=%s head=%s\n' \
  "$PREFIX" "$BRANCH" "$WORKTREE_STATE" "$HEAD_SHA"

UPSTREAM="$("$GIT_BIN" -C "$ROOT_DIR" rev-parse \
  --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
if [[ -z "$UPSTREAM" ]]; then
  printf '%s upstream=none local_refs_only=true\n' "$PREFIX"
else
  COUNTS="$("$GIT_BIN" -C "$ROOT_DIR" rev-list --left-right --count \
    "HEAD...$UPSTREAM" 2>/dev/null || true)"
  read -r AHEAD BEHIND <<<"$COUNTS"
  if [[ "${AHEAD:-}" =~ ^[0-9]+$ && "${BEHIND:-}" =~ ^[0-9]+$ ]]; then
    printf '%s upstream=%s ahead=%s behind=%s local_refs_only=true\n' \
      "$PREFIX" "$UPSTREAM" "$AHEAD" "$BEHIND"
  else
    printf '%s upstream=%s divergence=unknown local_refs_only=true\n' \
      "$PREFIX" "$UPSTREAM"
  fi
fi

SUMMARY_PATH="$ROOT_DIR/.artifacts/release/summary.json"
if [[ -L "$ROOT_DIR/.artifacts" || -L "$ROOT_DIR/.artifacts/release" || \
  -L "$SUMMARY_PATH" ]]; then
  printf '%s release_summary=invalid reason=symlink\n' "$PREFIX"
elif [[ ! -e "$SUMMARY_PATH" ]]; then
  printf '%s release_summary=missing\n' "$PREFIX"
elif [[ ! -f "$SUMMARY_PATH" ]]; then
  printf '%s release_summary=invalid reason=not_regular\n' "$PREFIX"
else
  SUMMARY_VALUES="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - \
    "$SUMMARY_PATH" 2>/dev/null <<'PY'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
if path.stat().st_size > 1024 * 1024:
    raise SystemExit(1)
payload = json.loads(path.read_text(encoding="utf-8"))
head = payload.get("git", {}).get("head_sha") if isinstance(payload, dict) else None
overall = payload.get("overall") if isinstance(payload, dict) else None
if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
    raise SystemExit(1)
if overall not in ("passed", "failed"):
    raise SystemExit(1)
print("{}\t{}".format(head, overall))
PY
)"
  SUMMARY_CODE=$?
  if (( SUMMARY_CODE != 0 )); then
    printf '%s release_summary=invalid\n' "$PREFIX"
  else
    IFS=$'\t' read -r SUMMARY_HEAD SUMMARY_OVERALL <<<"$SUMMARY_VALUES"
    if [[ "$SUMMARY_HEAD" == "$HEAD_SHA" ]]; then
      printf '%s release_summary=current summary_head=%s current_head=%s overall=%s\n' \
        "$PREFIX" "$SUMMARY_HEAD" "$HEAD_SHA" "$SUMMARY_OVERALL"
    else
      printf '%s release_summary=stale summary_head=%s current_head=%s overall=%s\n' \
        "$PREFIX" "$SUMMARY_HEAD" "$HEAD_SHA" "$SUMMARY_OVERALL"
    fi
  fi
fi

if ! PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - \
  "$GIT_BIN" "$ROOT_DIR" "$HEAD_SHA" "$PREFIX" <<'PY'
import json
import os
import subprocess
import sys
import time

git_bin, root, head, prefix = sys.argv[1:]
environment = os.environ.copy()
environment["GIT_OPTIONAL_LOCKS"] = "0"
deadline = time.monotonic() + 2.5


def run(arguments, timeout_cap=1.0):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return subprocess.run(
        [git_bin, *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        timeout=max(0.05, min(timeout_cap, remaining)),
    )


try:
    listed = run(["-C", root, "worktree", "list", "--porcelain", "-z"])
    if listed.returncode != 0:
        raise RuntimeError
    records = []
    record = {}
    for field in listed.stdout.split(b"\0"):
        if not field:
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = field.partition(b" ")
        record[key] = value if separator else b""
    if record:
        records.append(record)

    root_real = os.path.realpath(root)
    truncated = len(records) > 21
    candidates = []
    for record in records[:21]:
        path_raw = record.get(b"worktree")
        commit_raw = record.get(b"HEAD")
        if path_raw is None or commit_raw is None:
            continue
        path = os.fsdecode(path_raw)
        commit = commit_raw.decode("ascii", errors="strict")
        if os.path.realpath(path) == root_real:
            continue
        ancestor = run(
            ["-C", root, "merge-base", "--is-ancestor", commit, head],
            timeout_cap=0.5,
        )
        if ancestor.returncode != 0:
            continue
        status = run(
            [
                "-C",
                path,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "submodule.recurse=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ],
            timeout_cap=0.5,
        )
        state = "unknown"
        if status.returncode == 0:
            state = "dirty" if status.stdout else "clean"
        branch_raw = record.get(b"branch", b"")
        branch = os.fsdecode(branch_raw)
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        if not branch:
            branch = "DETACHED"
        candidates.append((os.fsencode(path), path, branch, commit, state))

    if candidates:
        for _, path, branch, commit, state in sorted(candidates):
            print(
                "{} merged_worktree_candidate path={} branch={} head={} state={}".format(
                    prefix,
                    json.dumps(path, ensure_ascii=True),
                    json.dumps(branch, ensure_ascii=True),
                    commit,
                    state,
                )
            )
    else:
        print("{} merged_worktree_candidates=none".format(prefix))
    if truncated:
        print(
            "{} worktree_scan_truncated=true total={} inspected=21".format(
                prefix, len(records)
            )
        )
except (OSError, RuntimeError, subprocess.TimeoutExpired, TimeoutError, UnicodeError):
    print("{} merged_worktree_candidates=unknown".format(prefix))
PY
then
  printf '%s merged_worktree_candidates=unknown\n' "$PREFIX"
fi

exit 0
