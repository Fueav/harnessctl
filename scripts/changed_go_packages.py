#!/usr/bin/env python3
"""Select conservative Go package patterns from one sealed change snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from typing import Any, Dict, List


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCES = ("committed", "staged", "unstaged", "untracked")


class SelectionError(RuntimeError):
    """Raised when focused package selection cannot trust its inputs."""


def _parse_args() -> argparse.Namespace:
    root = pathlib.Path(
        os.environ.get(
            "HARNESS_PROJECT_ROOT", pathlib.Path(__file__).resolve().parent.parent
        )
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=root)
    parser.add_argument("--snapshot-file", required=True, type=pathlib.Path)
    parser.add_argument("--snapshot-sha256", required=True)
    return parser.parse_args()


def _checked_repo(argument: pathlib.Path) -> pathlib.Path:
    repo = pathlib.Path(os.path.abspath(os.fspath(argument)))
    try:
        info = repo.lstat()
    except OSError as error:
        raise SelectionError("repository root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SelectionError("repository root must be a non-symlink directory")
    return repo


def _normalize_path(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise SelectionError(f"{label} must be a non-empty relative path")
    if "\\" in value or "\x00" in value:
        raise SelectionError(f"{label} must be a POSIX path")
    path = pathlib.PurePosixPath(value)
    if path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts):
        raise SelectionError(f"{label} must be normalized")
    if value == ".git" or value.startswith(".git/"):
        raise SelectionError(f"{label} cannot enter .git")
    return path


def _read_snapshot(repo: pathlib.Path, argument: pathlib.Path, expected_digest: str) -> Dict[str, Any]:
    if SHA256_RE.fullmatch(expected_digest) is None:
        raise SelectionError("snapshot digest must be 64 lowercase hexadecimal characters")
    candidate = pathlib.Path(
        os.path.abspath(os.fspath(argument if argument.is_absolute() else repo / argument))
    )
    try:
        relative = candidate.relative_to(repo)
    except ValueError as error:
        raise SelectionError("snapshot must stay inside the repository") from error
    if not relative.parts:
        raise SelectionError("snapshot cannot be the repository root")
    current = repo
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise SelectionError("snapshot is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise SelectionError("snapshot has a symlink path component")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise SelectionError("snapshot has a non-directory path component")
    if not stat.S_ISREG(info.st_mode):
        raise SelectionError("snapshot must be a regular file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(current), flags)
    except OSError as error:
        raise SelectionError(f"snapshot is unreadable: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SelectionError("snapshot must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    observed = hashlib.sha256(raw).hexdigest()
    if observed != expected_digest:
        raise SelectionError(
            f"snapshot digest mismatch: expected {expected_digest}, observed {observed}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SelectionError("snapshot is not valid UTF-8 JSON") from error
    return _validate_snapshot(payload)


def _validate_snapshot(payload: Any) -> Dict[str, Any]:
    expected_keys = {
        "base_sha",
        "changes",
        "clean",
        "head_sha",
        "head_tree_sha",
        "merge_base_sha",
        "mode",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise SelectionError("snapshot has an unexpected schema")
    if payload.get("mode") != "change" or type(payload.get("clean")) is not bool:
        raise SelectionError("snapshot has an invalid mode or clean state")
    for key in ("base_sha", "head_sha", "head_tree_sha", "merge_base_sha"):
        if FULL_SHA_RE.fullmatch(str(payload.get(key, ""))) is None:
            raise SelectionError(f"snapshot has invalid {key}")
    changes = payload.get("changes")
    if not isinstance(changes, list):
        raise SelectionError("snapshot changes must be a list")
    previous = None
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "sources"}:
            raise SelectionError("snapshot has an invalid change record")
        path = change.get("path")
        _normalize_path(path, "changed path")
        if path in seen or (previous is not None and path < previous):
            raise SelectionError("snapshot paths must be unique and sorted")
        sources = change.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SelectionError("snapshot change sources must be a non-empty list")
        canonical = [source for source in SOURCES if source in set(sources)]
        if sources != canonical:
            raise SelectionError("snapshot change sources are not canonical")
        previous = path
        seen.add(path)
    return payload


def _is_regular_without_symlink(repo: pathlib.Path, path: pathlib.PurePosixPath) -> bool:
    current = repo
    for index, part in enumerate(path.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
        if index < len(path.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return False
    return stat.S_ISREG(info.st_mode)


def _select(repo: pathlib.Path, snapshot: Dict[str, Any]) -> List[str]:
    changed_paths = [change["path"] for change in snapshot["changes"]]
    if any(path in ("go.mod", "go.sum") for path in changed_paths):
        return ["./..."]

    packages = set()
    for value in changed_paths:
        if not value.endswith(".go"):
            continue
        path = _normalize_path(value, "changed Go path")
        if not _is_regular_without_symlink(repo, path):
            return ["./..."]
        parent = path.parent.as_posix()
        packages.add("." if parent == "." else f"./{parent}")
    return sorted(packages)


def main() -> int:
    args = _parse_args()
    try:
        repo = _checked_repo(args.repo)
        snapshot = _read_snapshot(
            repo, args.snapshot_file, args.snapshot_sha256
        )
        packages = _select(repo, snapshot)
    except (OSError, SelectionError) as error:
        print(f"changed-go-packages: {error}", file=sys.stderr)
        return 2
    for package in packages:
        sys.stdout.buffer.write(os.fsencode(package) + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
