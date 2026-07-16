#!/usr/bin/env python3
"""Command-line interface for the canonical Harness change scope."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import stat
import sys


def _load_change_scope_module():
    module_path = pathlib.Path(__file__).resolve().parent / "lib" / "change_scope.py"
    try:
        module_info = module_path.lstat()
    except OSError as error:
        raise RuntimeError("canonical change-scope library is unreadable") from error
    if stat.S_ISLNK(module_info.st_mode) or not stat.S_ISREG(module_info.st_mode):
        raise RuntimeError(
            "canonical change-scope library must be a regular non-symlink file"
        )
    specification = importlib.util.spec_from_file_location(
        "harness_change_scope", module_path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load canonical change-scope library")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_CHANGE_SCOPE = _load_change_scope_module()
ChangeRecord = _CHANGE_SCOPE.ChangeRecord
ChangeScopeError = _CHANGE_SCOPE.ChangeScopeError
snapshot = _CHANGE_SCOPE.snapshot


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--mode", required=True, choices=("change", "release"))
    parser.add_argument("--format", choices=("json", "nul"), default="json")
    return parser.parse_args()


def _json_snapshot(mode: str, git_snapshot: dict[str, object]) -> dict[str, object]:
    records = git_snapshot["changes"]
    if not isinstance(records, list) or not all(
        isinstance(record, ChangeRecord) for record in records
    ):
        raise ChangeScopeError("snapshot returned invalid change records")

    return {
        "mode": mode,
        "head_sha": git_snapshot["head_sha"],
        "head_tree_sha": git_snapshot["head_tree_sha"],
        "base_sha": git_snapshot["base_sha"],
        "merge_base_sha": git_snapshot["merge_base_sha"],
        "clean": git_snapshot["clean"],
        "changes": [
            {"path": record.path, "sources": list(record.sources)}
            for record in records
        ],
    }


def main() -> int:
    args = _parse_args()
    try:
        git_snapshot = snapshot(args.repo, args.base)
    except (ChangeScopeError, OSError) as error:
        print(f"collect_changes: {error}", file=sys.stderr)
        return 2

    records = git_snapshot["changes"]
    if args.format == "nul":
        if not isinstance(records, list) or not all(
            isinstance(record, ChangeRecord) for record in records
        ):
            print("collect_changes: snapshot returned invalid change records", file=sys.stderr)
            return 2
        sys.stdout.buffer.write(
            b"".join(os.fsencode(record.path) + b"\0" for record in records)
        )
        return 0

    json.dump(
        _json_snapshot(args.mode, git_snapshot),
        sys.stdout,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
