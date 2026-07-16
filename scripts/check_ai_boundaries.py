#!/usr/bin/env python3
"""Enforce fail-closed AI edit boundaries against one Git snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple


SECTIONS = ("allowed", "approval_required", "forbidden")
CLASSIFICATION_RANK = {
    "unclassified": 0,
    "allowed": 1,
    "approval_required": 2,
    "forbidden": 3,
}
BOOTSTRAP_PROTECTED = (
    ".ai-boundaries.yml",
    "CODEOWNERS",
    ".github/",
    ".codex/",
    ".claude/",
    "scripts/check_ai_boundaries.sh",
    "scripts/check_ai_boundaries.py",
    "scripts/collect_changes.py",
    "scripts/verify_release.sh",
)
COLLECTOR_RUNTIME_PATHS = (
    "scripts/collect_changes.py",
    "scripts/lib/",
)
CHANGE_SOURCES = ("committed", "staged", "unstaged", "untracked")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_SECTION_RE = re.compile(r"^([a-z_]+):[ \t]*(?:#.*)?$")
_ITEM_RE = re.compile(r"^[ \t]+-[ \t]+(.+?)[ \t]*$")


class BoundaryError(RuntimeError):
    """Raised when boundary inputs cannot be trusted."""


Policy = Dict[str, Tuple[str, ...]]


def _parse_args() -> argparse.Namespace:
    root = pathlib.Path(
        os.environ.get(
            "HARNESS_PROJECT_ROOT", pathlib.Path(__file__).resolve().parent.parent
        )
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=root)
    parser.add_argument("--base")
    parser.add_argument("--mode", choices=("change", "release"))
    parser.add_argument("--artifact-dir", type=pathlib.Path)
    parser.add_argument("--snapshot-file", type=pathlib.Path)
    parser.add_argument("--snapshot-sha256")
    return parser.parse_args()


def _parse_scalar(raw_value: str, source: str, line_number: int) -> str:
    value = raw_value.strip()
    if not value:
        raise BoundaryError(f"{source}:{line_number}: policy entry must not be empty")

    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise BoundaryError(
                f"{source}:{line_number}: malformed quoted policy entry: {error.msg}"
            ) from error
        if not isinstance(decoded, str):
            raise BoundaryError(
                f"{source}:{line_number}: policy entry must be a path string"
            )
        value = decoded
    elif value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise BoundaryError(
                f"{source}:{line_number}: malformed quoted policy entry"
            )
        value = value[1:-1].replace("''", "'")
    elif "#" in value:
        raise BoundaryError(
            f"{source}:{line_number}: inline comments are not allowed on policy entries"
        )

    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise BoundaryError(f"{source}:{line_number}: invalid empty or control path")
    if value.startswith("/"):
        raise BoundaryError(f"{source}:{line_number}: policy paths must be relative")
    if any(part == ".." for part in value.rstrip("/").split("/")):
        raise BoundaryError(f"{source}:{line_number}: policy paths must not traverse upward")
    if any(character in value for character in "*?[]"):
        raise BoundaryError(
            f"{source}:{line_number}: glob patterns are not supported; use a directory suffix '/'"
        )
    return value


def parse_policy(raw: bytes, source: str) -> Policy:
    """Parse the repository's intentionally flat three-section YAML contract."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BoundaryError(f"{source}: policy must be valid UTF-8") from error

    parsed: Dict[str, List[str]] = {}
    current: Optional[str] = None
    seen_entries: Dict[str, str] = {}

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not raw_line[:1].isspace():
            match = _SECTION_RE.fullmatch(raw_line)
            if match is None:
                raise BoundaryError(
                    f"{source}:{line_number}: malformed policy section"
                )
            section = match.group(1)
            if section not in SECTIONS:
                raise BoundaryError(
                    f"{source}:{line_number}: unknown policy section {section!r}"
                )
            if section in parsed:
                raise BoundaryError(
                    f"{source}:{line_number}: duplicate policy section {section!r}"
                )
            parsed[section] = []
            current = section
            continue

        match = _ITEM_RE.fullmatch(raw_line)
        if match is None or current is None:
            raise BoundaryError(f"{source}:{line_number}: malformed policy entry")
        entry = _parse_scalar(match.group(1), source, line_number)
        if entry in seen_entries:
            raise BoundaryError(
                f"{source}:{line_number}: duplicate policy entry {entry!r} "
                f"(already in {seen_entries[entry]!r})"
            )
        seen_entries[entry] = current
        parsed[current].append(entry)

    missing = [section for section in SECTIONS if section not in parsed]
    if missing:
        raise BoundaryError(
            f"{source}: missing required policy section(s): {', '.join(missing)}"
        )
    extra = sorted(set(parsed) - set(SECTIONS))
    if extra:
        raise BoundaryError(f"{source}: unexpected policy section(s): {', '.join(extra)}")

    return {section: tuple(parsed[section]) for section in SECTIONS}


def _matches(path: str, entry: str) -> bool:
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry


def classify_policy(path: str, policy: Policy) -> str:
    """Return the strictest matching classification inside one policy."""

    result = "unclassified"
    for section in SECTIONS:
        if any(_matches(path, entry) for entry in policy[section]):
            if CLASSIFICATION_RANK[section] > CLASSIFICATION_RANK[result]:
                result = section
    return result


def _classify_bootstrap(path: str) -> str:
    if any(_matches(path, entry) for entry in BOOTSTRAP_PROTECTED):
        return "approval_required"
    return "unclassified"


def _strictest(classifications: Sequence[str]) -> str:
    return max(classifications, key=lambda item: CLASSIFICATION_RANK[item])


def _git(repo: pathlib.Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"git exited with status {result.returncode}"
        raise BoundaryError(detail)
    return result.stdout


def _decode_git_paths(output: bytes) -> Tuple[str, ...]:
    return tuple(os.fsdecode(item) for item in output.split(b"\0") if item)


def _resolve_commit(repo: pathlib.Path, revision: str) -> str:
    if not revision:
        raise BoundaryError("Git commit revision must not be empty")
    try:
        output = _git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        )
    except BoundaryError as error:
        raise BoundaryError(
            f"invalid Git commit revision {revision!r}: {error}"
        ) from error
    resolved = os.fsdecode(output).strip()
    if not resolved:
        raise BoundaryError(f"invalid Git commit revision {revision!r}")
    return resolved


def _collect_builtin_snapshot(
    repo: pathlib.Path, base_revision: str, mode: str
) -> Dict[str, Any]:
    """Collect scope without executing any candidate repository code."""

    base_sha = _resolve_commit(repo, base_revision)
    head_sha = _resolve_commit(repo, "HEAD")
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
        raise BoundaryError(
            f"no merge base between {base_sha!r} and {head_sha!r}"
        )

    scopes = (
        (
            "committed",
            (
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                f"{base_sha}...HEAD",
                "--",
            ),
        ),
        ("staged", ("diff", "--no-renames", "--name-only", "-z", "--cached", "--")),
        ("unstaged", ("diff", "--no-renames", "--name-only", "-z", "--")),
        ("untracked", ("ls-files", "--others", "--exclude-standard", "-z")),
    )
    sources_by_path: Dict[str, List[str]] = {}
    for source, command in scopes:
        for path in _decode_git_paths(_git(repo, *command)):
            sources = sources_by_path.setdefault(path, [])
            if source not in sources:
                sources.append(source)

    return {
        "mode": mode,
        "head_sha": head_sha,
        "head_tree_sha": head_tree_sha,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "clean": not _git(
            repo,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ),
        "changes": [
            {"path": path, "sources": sources_by_path[path]}
            for path in sorted(sources_by_path)
        ],
    }


def _snapshot_identity(snapshot: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple(
        snapshot[key]
        for key in (
            "mode",
            "head_sha",
            "head_tree_sha",
            "base_sha",
            "merge_base_sha",
            "clean",
        )
    )


def _change_map(snapshot: Dict[str, Any]) -> Dict[str, Tuple[str, ...]]:
    return {
        change["path"]: tuple(change["sources"])
        for change in snapshot["changes"]
    }


def _merge_scope_snapshots(
    trusted_builtin: Dict[str, Any],
    snapshots: Sequence[Tuple[str, Dict[str, Any]]],
) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    trusted_identity = _snapshot_identity(trusted_builtin)
    trusted_changes = _change_map(trusted_builtin)
    sources_by_path: Dict[str, List[str]] = {
        path: list(sources) for path, sources in trusted_changes.items()
    }

    for label, snapshot in snapshots:
        if _snapshot_identity(snapshot) != trusted_identity:
            errors.append(f"{label} snapshot identity disagrees with trusted scope")
        change_map = _change_map(snapshot)
        if change_map != trusted_changes:
            errors.append(f"{label} change scope disagrees with trusted scope")
        for path, sources in change_map.items():
            merged_sources = sources_by_path.setdefault(path, [])
            for source in sources:
                if source not in merged_sources:
                    merged_sources.append(source)

    source_rank = {source: index for index, source in enumerate(CHANGE_SOURCES)}
    merged = dict(trusted_builtin)
    merged["changes"] = [
        {
            "path": path,
            "sources": sorted(
                sources_by_path[path],
                key=lambda source: (source_rank.get(source, len(source_rank)), source),
            ),
        }
        for path in sorted(sources_by_path)
    ]
    return merged, errors


def _run_collector(
    repo: pathlib.Path,
    base: str,
    mode: str,
    collector: pathlib.Path,
    label: str,
) -> Dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            os.fspath(collector),
            "--repo",
            os.fspath(repo),
            "--base",
            base,
            "--mode",
            mode,
            "--format",
            "json",
        ],
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"collector exited with status {result.returncode}"
        raise BoundaryError(f"{label} change collector failed: {detail}")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BoundaryError(
            f"{label} change collector returned invalid JSON: {error}"
        ) from error
    return _validate_snapshot_payload(payload, mode, f"{label} change collector")


def _validate_snapshot_payload(
    payload: Any, mode: str, label: str
) -> Dict[str, Any]:
    required = {
        "mode",
        "head_sha",
        "head_tree_sha",
        "base_sha",
        "merge_base_sha",
        "clean",
        "changes",
    }
    if not isinstance(payload, dict):
        raise BoundaryError(f"{label} returned a non-object JSON payload")
    if set(payload) != required:
        raise BoundaryError(f"{label} returned an unexpected schema")
    if payload.get("mode") != mode:
        raise BoundaryError(f"{label} returned a mismatched mode")
    for key in ("head_sha", "head_tree_sha", "base_sha", "merge_base_sha"):
        value = payload.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise BoundaryError(f"{label} omitted snapshot identity {key!r}")
    if type(payload.get("clean")) is not bool:
        raise BoundaryError(f"{label} returned an invalid clean state")

    changes = payload.get("changes")
    if not isinstance(changes, list):
        raise BoundaryError(f"{label} returned invalid changes")
    seen_paths = set()
    previous_path: Optional[str] = None
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "sources"}:
            raise BoundaryError(f"{label} returned a non-object change")
        path = change.get("path")
        sources = change.get("sources")
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise BoundaryError(f"{label} returned an invalid path")
        normalized = pathlib.PurePosixPath(path)
        if (
            normalized.is_absolute()
            or normalized.as_posix() != path
            or any(part in ("", ".", "..") for part in normalized.parts)
            or path == ".git"
            or path.startswith(".git/")
        ):
            raise BoundaryError(f"{label} returned a non-canonical path {path!r}")
        if path in seen_paths:
            raise BoundaryError(f"{label} returned duplicate path {path!r}")
        if previous_path is not None and path < previous_path:
            raise BoundaryError(f"{label} returned non-deterministic path ordering")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(source, str) and source for source in sources
        ):
            raise BoundaryError(f"{label} returned invalid sources for path {path!r}")
        expected_sources = [
            source for source in CHANGE_SOURCES if source in set(sources)
        ]
        if sources != expected_sources:
            raise BoundaryError(
                f"{label} returned non-canonical sources for path {path!r}"
            )
        seen_paths.add(path)
        previous_path = path
    return payload


def _read_regular_file(path: pathlib.Path, label: str) -> bytes:
    requirement = (
        f"{label} must be a readable regular file and must not be a symlink"
    )
    try:
        before = os.lstat(os.fspath(path))
    except OSError as error:
        raise BoundaryError(f"{requirement}: {error}") from error
    if not stat.S_ISREG(before.st_mode):
        raise BoundaryError(requirement)

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        file_descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise BoundaryError(f"{requirement}: {error}") from error
    try:
        after = os.fstat(file_descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise BoundaryError(requirement)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BoundaryError(f"{label} changed while it was being opened")
        with os.fdopen(file_descriptor, "rb") as stream:
            file_descriptor = -1
            return stream.read()
    except OSError as error:
        raise BoundaryError(f"{requirement}: {error}") from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _load_sealed_snapshot(
    repo: pathlib.Path,
    snapshot_argument: pathlib.Path,
    expected_sha256: str,
    mode: str,
) -> Tuple[Dict[str, Any], str, str]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise BoundaryError("sealed change snapshot digest must be 64 lowercase hex characters")
    candidate = snapshot_argument
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = candidate.parent.resolve() / candidate.name
    try:
        relative = candidate.relative_to(repo).as_posix()
    except ValueError as error:
        raise BoundaryError("sealed change snapshot must stay inside the repository") from error
    raw = _read_regular_file(candidate, "sealed change snapshot")
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected_sha256:
        raise BoundaryError(
            "sealed change snapshot digest mismatch: expected {}, observed {}".format(
                expected_sha256, observed_sha256
            )
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoundaryError("sealed change snapshot is not valid UTF-8 JSON") from error
    return (
        _validate_snapshot_payload(payload, mode, "sealed change snapshot"),
        relative,
        observed_sha256,
    )


def _read_git_blob_optional(
    repo: pathlib.Path, commit_sha: str, path: str
) -> Optional[bytes]:
    paths = _decode_git_paths(
        _git(repo, "ls-tree", "-r", "--name-only", "-z", commit_sha, "--", path)
    )
    if not paths:
        return None
    if paths != (path,):
        raise BoundaryError(
            f"trusted base returned ambiguous tree entries for {path!r}: {paths!r}"
        )
    return _git(repo, "show", f"{commit_sha}:{path}")


def _run_bound_collector_snapshot(
    repo: pathlib.Path,
    base_sha: str,
    mode: str,
    collector_raw: bytes,
    library_raw: bytes,
    label: str,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"{label}-ai-boundary-") as directory:
        scripts_dir = pathlib.Path(directory) / "scripts"
        library_dir = scripts_dir / "lib"
        library_dir.mkdir(parents=True)
        collector = scripts_dir / "collect_changes.py"
        library = library_dir / "change_scope.py"
        collector.write_bytes(collector_raw)
        library.write_bytes(library_raw)
        return _run_collector(repo, base_sha, mode, collector, label)


def _trusted_collector_snapshot(
    repo: pathlib.Path,
    builtin_snapshot: Dict[str, Any],
    mode: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    base_sha = builtin_snapshot["base_sha"]
    collector_path = "scripts/collect_changes.py"
    library_path = "scripts/lib/change_scope.py"
    collector_raw = _read_git_blob_optional(repo, base_sha, collector_path)
    library_raw = _read_git_blob_optional(repo, base_sha, library_path)

    if collector_raw is None and library_raw is None:
        return builtin_snapshot, {
            "source": "builtin_git_fallback",
            "reason": "trusted base predates the canonical collector",
        }
    if collector_raw is None or library_raw is None:
        raise BoundaryError(
            "trusted base has an incomplete canonical collector; expected both "
            f"{collector_path} and {library_path}"
        )

    snapshot = _run_bound_collector_snapshot(
        repo,
        base_sha,
        mode,
        collector_raw,
        library_raw,
        "trusted-base",
    )

    return snapshot, {
        "source": f"{base_sha}:scripts/collect_changes.py",
        "collector_sha256": hashlib.sha256(collector_raw).hexdigest(),
        "library_sha256": hashlib.sha256(library_raw).hexdigest(),
    }


def _read_candidate_policy(repo: pathlib.Path) -> bytes:
    return _read_regular_file(
        repo / ".ai-boundaries.yml",
        "candidate policy .ai-boundaries.yml",
    )


def _read_trusted_policy(repo: pathlib.Path, base_sha: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), "show", f"{base_sha}:.ai-boundaries.yml"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"git show exited with status {result.returncode}"
        raise BoundaryError(
            "trusted policy .ai-boundaries.yml is missing or unreadable "
            f"at {base_sha}: {detail}"
        )
    return result.stdout


def _policy_evidence(source: str, raw: bytes) -> Dict[str, str]:
    return {
        "source": source,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _empty_artifact(
    mode: str,
    approved: bool,
    approval_evidence: Optional[str],
    approval_mode: str,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "failed",
        "mode": mode,
        "approval_mode": approval_mode,
        "approved": approved,
        "approval_evidence": approval_evidence if approved else None,
        "approval_satisfied": False,
        "snapshot": None,
        "change_scope": {
            "strategy": None,
            "trusted": None,
            "candidate": None,
            "sealed": None,
            "agreement": None,
        },
        "policy": {"trusted": None, "candidate": None},
        "changes": [],
        "classifications": {
            "allowed": [],
            "approval_required": [],
            "forbidden": [],
            "unclassified": [],
        },
        "errors": [],
    }


def _set_artifact_snapshot(
    artifact: Dict[str, Any], snapshot: Dict[str, Any]
) -> None:
    artifact["snapshot"] = {
        "head_sha": snapshot["head_sha"],
        "head_tree_sha": snapshot["head_tree_sha"],
        "base_sha": snapshot["base_sha"],
        "merge_base_sha": snapshot["merge_base_sha"],
        "clean": snapshot["clean"],
    }


def _set_artifact_changes(
    artifact: Dict[str, Any], changes: Sequence[Dict[str, Any]]
) -> None:
    artifact["changes"] = [
        {
            "path": change["path"],
            "sources": list(change["sources"]),
            "evaluation_status": "not_evaluated",
            "trusted_classification": None,
            "candidate_classification": None,
            "bootstrap_classification": _classify_bootstrap(change["path"]),
            "effective_classification": None,
        }
        for change in changes
    ]


def _write_json_atomic(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = -1
    temporary_name: Optional[str] = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=os.fspath(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            file_descriptor = -1
            json.dump(
                payload,
                stream,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _finish(
    artifact_path: pathlib.Path, artifact: Dict[str, Any], exit_code: int
) -> int:
    try:
        _write_json_atomic(artifact_path, artifact)
    except OSError as error:
        print(
            f"check_ai_boundaries: cannot write artifact {artifact_path}: {error}",
            file=sys.stderr,
        )
        return 2

    for error in artifact["errors"]:
        print(f"check_ai_boundaries: {error}", file=sys.stderr)
    if exit_code == 0:
        print(
            "AI boundary check passed for "
            f"{len(artifact['changes'])} changed path(s); evidence: {artifact_path}"
        )
    return exit_code


def main() -> int:
    args = _parse_args()
    repo = args.repo.resolve()
    base = args.base
    if base is None:
        base = os.environ.get("AI_BOUNDARY_COMPARE_REF") or os.environ.get(
            "VERIFY_COMPARE_REF", ""
        )
    mode = args.mode or os.environ.get("AI_BOUNDARY_MODE", "change")
    approved = os.environ.get("AI_BOUNDARY_APPROVED", "0") == "1"
    approval_evidence = os.environ.get("AI_BOUNDARY_APPROVAL_EVIDENCE", "").strip()
    approval_mode = os.environ.get("AI_BOUNDARY_APPROVAL_MODE", "required")
    external_engine = os.environ.get("HARNESS_EXTERNAL_ENGINE", "0") == "1"

    artifact_dir = args.artifact_dir
    if artifact_dir is None:
        configured_artifact_dir = os.environ.get("AI_BOUNDARY_ARTIFACT_DIR")
        if configured_artifact_dir:
            artifact_dir = pathlib.Path(configured_artifact_dir)
        else:
            default_mode = mode if mode in ("change", "release") else "change"
            artifact_dir = repo / ".artifacts" / default_mode
    if not artifact_dir.is_absolute():
        artifact_dir = repo / artifact_dir
    artifact_path = artifact_dir / "ai_boundaries.json"
    artifact = _empty_artifact(
        mode, approved, approval_evidence or None, approval_mode
    )

    if mode not in ("change", "release"):
        artifact["errors"].append(
            f"invalid AI_BOUNDARY_MODE {mode!r}; expected 'change' or 'release'"
        )
        return _finish(artifact_path, artifact, 2)
    if approval_mode not in ("required", "deferred"):
        artifact["errors"].append(
            "invalid AI_BOUNDARY_APPROVAL_MODE {!r}; expected 'required' or "
            "'deferred'".format(approval_mode)
        )
        return _finish(artifact_path, artifact, 2)
    if approval_mode == "deferred" and approved:
        artifact["errors"].append(
            "deferred approval mode must not claim AI_BOUNDARY_APPROVED=1"
        )
        return _finish(artifact_path, artifact, 1)
    if approved and (
        not approval_evidence
        or len(approval_evidence) > 512
        or any(character in approval_evidence for character in "\x00\r\n")
        or not approval_evidence.startswith(("github:", "owner-request:"))
    ):
        artifact["errors"].append(
            "AI_BOUNDARY_APPROVED=1 requires canonical "
            "AI_BOUNDARY_APPROVAL_EVIDENCE using github: or owner-request:"
        )
        return _finish(artifact_path, artifact, 1)
    if not base:
        artifact["errors"].append(
            "an explicit base is required via --base, AI_BOUNDARY_COMPARE_REF, "
            "or VERIFY_COMPARE_REF"
        )
        return _finish(artifact_path, artifact, 2)
    if (args.snapshot_file is None) != (args.snapshot_sha256 is None):
        artifact["errors"].append(
            "--snapshot-file and --snapshot-sha256 must be provided together"
        )
        return _finish(artifact_path, artifact, 2)

    try:
        builtin_before = _collect_builtin_snapshot(repo, base, mode)
    except (BoundaryError, OSError) as error:
        artifact["errors"].append(f"trusted builtin change scope failed: {error}")
        return _finish(artifact_path, artifact, 2)

    _set_artifact_snapshot(artifact, builtin_before)
    _set_artifact_changes(artifact, builtin_before["changes"])
    artifact["change_scope"]["strategy"] = "builtin_bootstrap_preflight"
    artifact["change_scope"]["trusted"] = {"source": "builtin_git_scope"}

    sealed_snapshot: Optional[Dict[str, Any]] = None
    if args.snapshot_file is not None and args.snapshot_sha256 is not None:
        try:
            sealed_snapshot, sealed_relative, sealed_sha256 = _load_sealed_snapshot(
                repo, args.snapshot_file, args.snapshot_sha256, mode
            )
        except (BoundaryError, OSError) as error:
            artifact["errors"].append(str(error))
            return _finish(artifact_path, artifact, 2)
        artifact["change_scope"]["sealed"] = {
            "source": sealed_relative,
            "sha256": sealed_sha256,
        }

    collector_runtime_changes = [
        change["path"]
        for change in builtin_before["changes"]
        if any(_matches(change["path"], entry) for entry in COLLECTOR_RUNTIME_PATHS)
    ]
    if (
        collector_runtime_changes
        and not external_engine
        and not approved
        and approval_mode == "required"
    ):
        artifact["errors"].append(
            "collector runtime changes require AI_BOUNDARY_APPROVED=1 before "
            "candidate collector code can execute: "
            + ", ".join(collector_runtime_changes)
        )
        return _finish(artifact_path, artifact, 1)

    scripts_dir = pathlib.Path(__file__).resolve().parent
    collector = scripts_dir / "collect_changes.py"
    collector_library = scripts_dir / "lib" / "change_scope.py"
    try:
        if external_engine:
            trusted_snapshot = None
            trusted_evidence = None
            collector_label = "harnessctl collector scripts/collect_changes.py"
            library_label = "harnessctl collector library scripts/lib/change_scope.py"
            candidate_source = "harnessctl external engine"
            runtime_label = "harnessctl"
        else:
            trusted_snapshot, trusted_evidence = _trusted_collector_snapshot(
                repo, builtin_before, mode
            )
            collector_label = "candidate collector scripts/collect_changes.py"
            library_label = "candidate collector library scripts/lib/change_scope.py"
            candidate_source = "scripts/collect_changes.py"
            runtime_label = "candidate"
        candidate_collector_raw = _read_regular_file(collector, collector_label)
        candidate_library_raw = _read_regular_file(
            collector_library,
            library_label,
        )
        artifact["change_scope"]["candidate"] = {
            "source": candidate_source,
            "collector_sha256": hashlib.sha256(candidate_collector_raw).hexdigest(),
            "library_sha256": hashlib.sha256(candidate_library_raw).hexdigest(),
        }
        candidate_snapshot = _run_bound_collector_snapshot(
            repo,
            builtin_before["base_sha"],
            mode,
            candidate_collector_raw,
            candidate_library_raw,
            runtime_label,
        )
        if external_engine:
            trusted_snapshot = candidate_snapshot
            trusted_evidence = {
                "source": "harnessctl_external_engine",
                "version": os.environ.get("HARNESSCTL_VERSION", "unknown"),
                "collector_sha256": hashlib.sha256(candidate_collector_raw).hexdigest(),
                "library_sha256": hashlib.sha256(candidate_library_raw).hexdigest(),
            }
        builtin_after = _collect_builtin_snapshot(
            repo, builtin_before["base_sha"], mode
        )
    except (BoundaryError, OSError) as error:
        artifact["errors"].append(str(error))
        return _finish(artifact_path, artifact, 2)

    comparison_snapshots: List[Tuple[str, Dict[str, Any]]] = [
        ("post-collector builtin", builtin_after),
        ("trusted-base collector", trusted_snapshot),
        ("candidate collector", candidate_snapshot),
    ]
    if sealed_snapshot is not None:
        comparison_snapshots.append(("sealed change snapshot", sealed_snapshot))
    snapshot, scope_errors = _merge_scope_snapshots(
        builtin_before,
        comparison_snapshots,
    )
    _set_artifact_snapshot(artifact, snapshot)
    _set_artifact_changes(artifact, snapshot["changes"])
    artifact["change_scope"]["strategy"] = (
        "builtin_fallback_candidate_union"
        if trusted_evidence["source"] == "builtin_git_fallback"
        else "builtin_trusted_candidate_union"
    )
    artifact["change_scope"]["trusted"] = trusted_evidence
    artifact["change_scope"]["agreement"] = not scope_errors
    if scope_errors:
        artifact["errors"].extend(scope_errors)
        return _finish(artifact_path, artifact, 2)

    candidate_raw: Optional[bytes] = None
    trusted_raw: Optional[bytes] = None
    candidate_policy: Optional[Policy] = None
    trusted_policy: Optional[Policy] = None

    try:
        candidate_raw = _read_candidate_policy(repo)
        artifact["policy"]["candidate"] = _policy_evidence(
            ".ai-boundaries.yml", candidate_raw
        )
        candidate_policy = parse_policy(candidate_raw, "candidate policy")
    except BoundaryError as error:
        artifact["errors"].append(str(error))

    try:
        trusted_raw = _read_trusted_policy(repo, snapshot["base_sha"])
        artifact["policy"]["trusted"] = _policy_evidence(
            f"{snapshot['base_sha']}:.ai-boundaries.yml", trusted_raw
        )
        trusted_policy = parse_policy(trusted_raw, "trusted policy")
    except BoundaryError as error:
        artifact["errors"].append(str(error))

    if candidate_policy is None or trusted_policy is None:
        return _finish(artifact_path, artifact, 2)

    classifications = artifact["classifications"]
    for change in artifact["changes"]:
        path = change["path"]
        trusted_classification = classify_policy(path, trusted_policy)
        candidate_classification = classify_policy(path, candidate_policy)
        bootstrap_classification = _classify_bootstrap(path)
        effective_classification = _strictest(
            (
                trusted_classification,
                candidate_classification,
                bootstrap_classification,
            )
        )
        change.update(
            {
                "evaluation_status": "evaluated",
                "trusted_classification": trusted_classification,
                "candidate_classification": candidate_classification,
                "bootstrap_classification": bootstrap_classification,
                "effective_classification": effective_classification,
            }
        )
        classifications[effective_classification].append(path)

    forbidden = classifications["forbidden"]
    unclassified = classifications["unclassified"]
    approval_required = classifications["approval_required"]
    if forbidden:
        artifact["errors"].append(
            "forbidden AI boundary paths changed: " + ", ".join(forbidden)
        )
    if unclassified:
        artifact["errors"].append(
            "unclassified AI boundary paths changed: " + ", ".join(unclassified)
        )
    artifact["approval_satisfied"] = not approval_required or approved
    if approval_required and not approved and approval_mode == "required":
        artifact["errors"].append(
            "approval-required paths changed; set AI_BOUNDARY_APPROVED=1 only "
            "after owner approval: " + ", ".join(approval_required)
        )

    if artifact["errors"]:
        return _finish(artifact_path, artifact, 1)

    artifact["status"] = "passed"
    return _finish(artifact_path, artifact, 0)


if __name__ == "__main__":
    raise SystemExit(main())
