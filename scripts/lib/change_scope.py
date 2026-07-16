"""Canonical Git change-scope collection for Harness gates."""

from __future__ import annotations

import os
import pathlib
import subprocess
from dataclasses import dataclass


class ChangeScopeError(RuntimeError):
    """Raised when a canonical Git snapshot cannot be collected."""


@dataclass(frozen=True)
class ChangeRecord:
    """One changed path and the Git scopes that reported it."""

    path: str
    sources: tuple[str, ...]


def _git(repo: pathlib.Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        if not detail:
            detail = f"git exited with status {result.returncode}"
        raise ChangeScopeError(detail)
    return result.stdout


def _decode_paths(output: bytes) -> tuple[str, ...]:
    return tuple(os.fsdecode(item) for item in output.split(b"\0") if item)


def resolve_commit(repo: pathlib.Path, revision: str) -> str:
    """Resolve *revision* to a commit object, or fail without fallback."""

    if not revision:
        raise ChangeScopeError("Git commit revision must not be empty")

    try:
        output = _git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        )
    except ChangeScopeError as error:
        raise ChangeScopeError(
            f"invalid Git commit revision {revision!r}: {error}"
        ) from error

    resolved = os.fsdecode(output).strip()
    if not resolved:
        raise ChangeScopeError(f"invalid Git commit revision {revision!r}")
    return resolved


def collect_changes(repo: pathlib.Path, base_sha: str) -> list[ChangeRecord]:
    """Collect the deterministic union of committed and working-tree changes."""

    resolved_base = resolve_commit(repo, base_sha)
    scopes = (
        (
            "committed",
            (
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                f"{resolved_base}...HEAD",
                "--",
            ),
        ),
        (
            "staged",
            ("diff", "--no-renames", "--name-only", "-z", "--cached", "--"),
        ),
        (
            "unstaged",
            ("diff", "--no-renames", "--name-only", "-z", "--"),
        ),
        (
            "untracked",
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ),
    )

    sources_by_path: dict[str, list[str]] = {}
    for source, command in scopes:
        for path in _decode_paths(_git(repo, *command)):
            sources = sources_by_path.setdefault(path, [])
            if source not in sources:
                sources.append(source)

    return [
        ChangeRecord(path=path, sources=tuple(sources_by_path[path]))
        for path in sorted(sources_by_path)
    ]


def snapshot(repo: pathlib.Path, base_revision: str) -> dict[str, object]:
    """Resolve Git identities and collect their complete candidate change set."""

    base_sha = resolve_commit(repo, base_revision)
    head_sha = resolve_commit(repo, "HEAD")
    head_tree_sha = os.fsdecode(
        _git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{head_sha}^{{tree}}",
        )
    ).strip()
    merge_base_sha = os.fsdecode(_git(repo, "merge-base", base_sha, head_sha)).strip()
    if not merge_base_sha:
        raise ChangeScopeError(
            f"no merge base between {base_sha!r} and {head_sha!r}"
        )

    return {
        "head_sha": head_sha,
        "head_tree_sha": head_tree_sha,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "clean": is_clean(repo),
        "changes": collect_changes(repo, base_sha),
    }


def is_clean(repo: pathlib.Path) -> bool:
    """Return whether tracked and non-ignored untracked state is clean."""

    return not _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
