#!/usr/bin/env python3
"""Shared fail-closed primitives for Harness evidence producers and consumers."""

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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GATE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class EvidenceError(RuntimeError):
    """Raised when evidence or a trusted runtime path is unsafe or inconsistent."""


def normalize_relative(value: str, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise EvidenceError(f"{label} must be a relative POSIX path")
    if "\\" in value or "\x00" in value:
        raise EvidenceError(f"{label} must be a relative POSIX path")
    path = pathlib.PurePosixPath(value)
    if path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts):
        raise EvidenceError(f"{label} must be normalized")
    return path


def checked_repo(argument: pathlib.Path) -> pathlib.Path:
    repo = pathlib.Path(os.path.abspath(os.fspath(argument)))
    try:
        info = repo.lstat()
    except OSError as error:
        raise EvidenceError("repository root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvidenceError("repository root must be a non-symlink directory")
    return repo


def repo_relative(
    repo: pathlib.Path, argument: pathlib.Path, label: str
) -> pathlib.PurePosixPath:
    candidate = pathlib.Path(
        os.path.abspath(os.fspath(argument if argument.is_absolute() else repo / argument))
    )
    try:
        relative = candidate.relative_to(repo)
    except ValueError as error:
        raise EvidenceError(f"{label} must stay inside the repository") from error
    if not relative.parts:
        raise EvidenceError(f"{label} cannot be the repository root")
    return normalize_relative(relative.as_posix(), label)


def checked_directory(
    root: pathlib.Path, relative: pathlib.PurePosixPath, label: str
) -> pathlib.Path:
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise EvidenceError(f"{label} is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError(f"{label} has a symlink path component")
        if not stat.S_ISDIR(info.st_mode):
            raise EvidenceError(f"{label} must be a directory")
    return current


def regular_under(root: pathlib.Path, relative_value: str, label: str) -> pathlib.Path:
    relative = normalize_relative(relative_value, label)
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise EvidenceError(f"{label} is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError(f"{label} has a symlink path component")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise EvidenceError(f"{label} has a non-directory path component")
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError(f"{label} must be a regular file")
    return current


def read_bytes(path: pathlib.Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise EvidenceError(f"{label} is unreadable: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_json_bytes(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{label} is not valid UTF-8 JSON") from error


def load_json(path: pathlib.Path, label: str) -> Any:
    return load_json_bytes(read_bytes(path, label), label)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: pathlib.Path, label: Optional[str] = None) -> str:
    return sha256_bytes(read_bytes(path, label or os.fspath(path)))


def atomic_json(path: pathlib.Path, payload: Dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        dir=os.fspath(path.parent), prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return encoded


def atomic_json_under(
    root: pathlib.Path, relative: pathlib.PurePosixPath, payload: Dict[str, Any]
) -> bytes:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o755)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise EvidenceError("output has an unsafe path component")
    destination = current / relative.name
    if destination.is_symlink():
        raise EvidenceError("output must not be a symlink")
    return atomic_json(destination, payload)


def validated_roots(
    repo_argument: pathlib.Path, artifact_argument: pathlib.Path
) -> Tuple[pathlib.Path, pathlib.Path]:
    repo = checked_repo(repo_argument)
    relative = repo_relative(repo, artifact_argument, "artifact root")
    return repo, checked_directory(repo, relative, "artifact root")


def validated_argument_file(
    artifact_dir: pathlib.Path,
    argument: Optional[pathlib.Path],
    expected_relative: str,
    label: str,
) -> Optional[pathlib.Path]:
    if argument is None:
        return None
    expected = artifact_dir / expected_relative
    if pathlib.Path(os.path.abspath(os.fspath(argument))) != expected:
        raise EvidenceError(f"{label} must be {expected}")
    return regular_under(artifact_dir, expected_relative, label)


def artifact_records(payload: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, list):
        raise EvidenceError(f"{label} must be a list")
    records: List[Dict[str, Any]] = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise EvidenceError(f"{label} has an invalid record")
        path, digest, size = item.get("path"), item.get("sha256"), item.get("size_bytes")
        normalize_relative(path, f"{label} path")
        if path in seen or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise EvidenceError(f"{label} has a duplicate path or invalid digest")
        if type(size) is not int or size < 0:
            raise EvidenceError(f"{label} has an invalid size")
        records.append({"path": path, "sha256": digest, "size_bytes": size})
        seen.add(path)
    if records != sorted(records, key=lambda item: item["path"]):
        raise EvidenceError(f"{label} must use deterministic path ordering")
    return records


def collect_artifacts(
    root: pathlib.Path, candidates: Iterable[str]
) -> List[Dict[str, Any]]:
    records = []
    for relative in sorted(set(candidates)):
        candidate = root.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        path = regular_under(root, relative, f"artifact {relative!r}")
        records.append(
            {"path": relative, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        )
    return records


def load_manifest_bundle(
    root: pathlib.Path, summary: Dict[str, Any]
) -> Tuple[str, Dict[str, bytes], Dict[str, str]]:
    manifest_path = regular_under(root, "artifact_manifest.json", "artifact manifest")
    manifest_raw = read_bytes(manifest_path, "artifact manifest")
    manifest_digest = sha256_bytes(manifest_raw)
    if summary.get("artifact_manifest_sha256") != manifest_digest:
        raise EvidenceError("artifact manifest digest does not match summary")
    manifest = load_json_bytes(manifest_raw, "artifact manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise EvidenceError("artifact manifest has invalid schema")
    records = artifact_records(manifest.get("artifacts"), "manifest artifacts")
    if records != artifact_records(summary.get("artifacts"), "summary artifacts"):
        raise EvidenceError("summary and manifest artifact sets disagree")
    contents, observed = {}, {}
    for record in records:
        raw = read_bytes(regular_under(root, record["path"], f"artifact {record['path']!r}"), f"artifact {record['path']!r}")
        digest = sha256_bytes(raw)
        if len(raw) != record["size_bytes"] or digest != record["sha256"]:
            raise EvidenceError(f"artifact {record['path']!r} was modified")
        contents[record["path"]] = raw
        observed[record["path"]] = digest
    return manifest_digest, contents, observed


def validate_summary_seals(summary: Dict[str, Any], observed: Dict[str, str]) -> Dict[str, str]:
    records = summary.get("sealed_artifacts")
    if not isinstance(records, list):
        raise EvidenceError("summary has invalid sealed artifacts")
    sealed: Dict[str, str] = {}
    for item in records:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise EvidenceError("summary has an invalid artifact seal")
        path, digest = item.get("path"), item.get("sha256")
        normalize_relative(path, "sealed artifact path")
        if path in sealed or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise EvidenceError("summary has a duplicate or invalid artifact seal")
        sealed[path] = digest
    invalid = sorted(path for path, digest in sealed.items() if observed.get(path) != digest)
    if invalid:
        raise EvidenceError("artifact seals do not match the manifest: " + ", ".join(invalid))
    return sealed


def validate_sealed_arguments(
    root: pathlib.Path, values: Sequence[str]
) -> List[Dict[str, str]]:
    records, seen = [], set()
    for value in values:
        relative, separator, recorded = value.rpartition("=")
        if not separator:
            raise EvidenceError("sealed artifact record is malformed")
        normalize_relative(relative, "sealed artifact path")
        if relative in seen or not SHA256_RE.fullmatch(recorded):
            raise EvidenceError(f"sealed artifact {relative!r} is duplicated or invalid")
        observed = sha256_file(
            regular_under(root, relative, f"sealed artifact {relative!r}")
        )
        if observed != recorded:
            raise EvidenceError(f"sealed artifact {relative!r} changed after gate completion")
        records.append({"path": relative, "sha256": observed})
        seen.add(relative)
    return sorted(records, key=lambda item: item["path"])


def parse_gate_ledger(
    artifact_dir: pathlib.Path, gates_file: Optional[pathlib.Path]
) -> Tuple[List[Dict[str, Any]], List[str], Optional[bytes]]:
    if gates_file is None:
        return [], [], None
    raw = read_bytes(gates_file, "gate ledger")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise EvidenceError("gate ledger is not UTF-8") from error
    gates, logs, seen = [], [], set()
    for line_number, line in enumerate(lines, 1):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 5:
            raise EvidenceError(f"gate ledger line {line_number} is malformed")
        name, status_value, duration_value, log_relative, recorded_digest = fields
        if not GATE_NAME_RE.fullmatch(name) or name in seen:
            raise EvidenceError(f"gate ledger has invalid or duplicate gate {name!r}")
        if status_value not in ("passed", "failed", "skipped"):
            raise EvidenceError(f"gate {name!r} has invalid status")
        try:
            duration = int(duration_value)
        except ValueError as error:
            raise EvidenceError(f"gate {name!r} has invalid duration") from error
        if duration < 0 or log_relative != f"logs/{name}.log":
            raise EvidenceError(f"gate {name!r} has invalid duration or log path")
        if not SHA256_RE.fullmatch(recorded_digest):
            raise EvidenceError(f"gate {name!r} has invalid log digest")
        log = regular_under(artifact_dir, log_relative, f"gate {name!r} log")
        log_raw = read_bytes(log, f"gate {name!r} log")
        if sha256_bytes(log_raw) != recorded_digest:
            raise EvidenceError(f"gate {name!r} log changed after execution")
        if status_value == "skipped" and not log_raw.strip():
            raise EvidenceError(f"skipped gate {name!r} requires a reason")
        gates.append({"duration_seconds": duration, "log_path": log_relative, "log_sha256": recorded_digest, "name": name, "status": status_value})
        logs.append(log_relative)
        seen.add(name)
    return gates, logs, raw


def validate_summary_gates(
    summary: Dict[str, Any],
    contents: Dict[str, bytes],
    required: Sequence[str],
    skippable: Sequence[str],
) -> Dict[str, str]:
    gates = summary.get("gates")
    if not isinstance(gates, list) or tuple(g.get("name") for g in gates if isinstance(g, dict)) != tuple(required):
        raise EvidenceError("summary does not contain the required gate sequence")
    statuses, lines = {}, []
    for gate in gates:
        if set(gate) != {"duration_seconds", "log_path", "log_sha256", "name", "status"}:
            raise EvidenceError("summary has an invalid gate record")
        name, status_value = gate["name"], gate["status"]
        if status_value not in ("passed", "skipped") or (status_value == "skipped" and name not in skippable):
            raise EvidenceError(f"gate {name!r} did not pass or cannot be skipped")
        duration, log_relative, digest = gate["duration_seconds"], gate["log_path"], gate["log_sha256"]
        if type(duration) is not int or duration < 0 or log_relative != f"logs/{name}.log":
            raise EvidenceError(f"gate {name!r} has invalid metadata")
        raw = contents.get(log_relative)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or raw is None or sha256_bytes(raw) != digest:
            raise EvidenceError(f"gate {name!r} log is not in the manifest")
        if status_value == "skipped" and not raw.strip():
            raise EvidenceError(f"gate {name!r} has no skip reason")
        lines.append(f"{name}\t{status_value}\t{duration}\t{log_relative}\t{digest}\n")
        statuses[name] = status_value
    ledger = contents.get("gates.tsv")
    if ledger != "".join(lines).encode() or summary.get("gate_ledger_sha256") != sha256_bytes(ledger or b""):
        raise EvidenceError("gate ledger disagrees with the summary")
    return statuses


def git_bytes(repo: pathlib.Path, *arguments: str, required: bool = True) -> Optional[bytes]:
    result = subprocess.run(["git", "-C", os.fspath(repo), *arguments], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        return result.stdout
    if not required:
        return None
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    raise EvidenceError(detail or f"git exited with status {result.returncode}")


def git(repo: pathlib.Path, *arguments: str, required: bool = False) -> Optional[str]:
    raw = git_bytes(repo, *arguments, required=required)
    return None if raw is None else raw.decode("utf-8", errors="replace").strip()


def git_clean(repo: pathlib.Path, untracked: bool = True) -> Optional[bool]:
    raw = git_bytes(repo, "status", "--porcelain=v1", "-z", f"--untracked-files={'all' if untracked else 'no'}", required=False)
    return None if raw is None else not raw


def load_snapshot(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    payload = load_json(path, "change snapshot")
    if not isinstance(payload, dict):
        raise EvidenceError("change snapshot must be a JSON object")
    for key in ("head_sha", "head_tree_sha", "base_sha", "merge_base_sha"):
        if not isinstance(payload.get(key), str) or not FULL_SHA_RE.fullmatch(payload[key]):
            raise EvidenceError(f"change snapshot has invalid {key}")
    if type(payload.get("clean")) is not bool or payload.get("mode") not in ("change", "release"):
        raise EvidenceError("change snapshot has invalid mode or clean state")
    if not isinstance(payload.get("changes"), list):
        raise EvidenceError("change snapshot has invalid changes")
    return payload


def mutable_root(repo: pathlib.Path, argument: pathlib.Path, prefix: str) -> pathlib.Path:
    relative = repo_relative(repo, argument, "mutable root")
    if relative.parts[0] != prefix or ".git" in relative.parts:
        raise EvidenceError(f"mutable root must stay under {prefix}/")
    current = repo
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError(f"mutable root has a symlink path component: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise EvidenceError(f"mutable root has a non-directory path component: {current}")
    return repo.joinpath(*relative.parts)


def invalidate_outputs(repo: pathlib.Path, argument: pathlib.Path, prefix: str) -> None:
    relative = repo_relative(repo, argument, "artifact root")
    if relative.parts[0] != prefix:
        raise EvidenceError(f"artifact root must stay under {prefix}/")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.fspath(repo), flags | nofollow)
    try:
        for part in relative.parts:
            try:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode):
                os.unlink(part, dir_fd=descriptor)
                raise EvidenceError("removed unsafe artifact symlink without following it")
            if not stat.S_ISDIR(info.st_mode):
                raise EvidenceError(f"artifact path component is not a directory: {part}")
            next_descriptor = os.open(part, flags | nofollow, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        for name in ("summary.json", "artifact_manifest.json"):
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
    finally:
        os.close(descriptor)


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    root_parser = subparsers.add_parser("mutable-root")
    root_parser.add_argument("--repo", required=True, type=pathlib.Path)
    root_parser.add_argument("--path", required=True, type=pathlib.Path)
    root_parser.add_argument("--prefix", required=True)
    root_parser.add_argument("--invalidate", action="store_true")
    hash_parser = subparsers.add_parser("sha256")
    hash_parser.add_argument("path", type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "sha256":
            print(sha256_file(args.path))
        else:
            repo = checked_repo(args.repo)
            if args.invalidate:
                invalidate_outputs(repo, args.path, args.prefix)
            print(mutable_root(repo, args.path, args.prefix))
    except EvidenceError as error:
        print(f"evidence: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
