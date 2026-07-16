#!/usr/bin/env python3
"""Strict loader and conditional-gate selector for the Harness profile policy."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import subprocess
import sys
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

if __package__:
    from .evidence import EvidenceError, load_json, normalize_relative, read_bytes, sha256_bytes
else:
    sys.path.insert(0, os.fspath(pathlib.Path(__file__).resolve().parent))
    from evidence import EvidenceError, load_json, normalize_relative, read_bytes, sha256_bytes


CONFIG_PATH = pathlib.Path(
    os.environ.get(
        "HARNESS_PROFILE_CONFIG",
        pathlib.Path(__file__).resolve().parent.parent / "harness_profiles.json",
    )
)


class ConfigError(RuntimeError):
    """Raised when the trusted Harness policy is malformed."""


def load_policy() -> Dict[str, Any]:
    try:
        info = CONFIG_PATH.lstat()
    except OSError as error:
        raise ConfigError("Harness profile policy is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError("Harness profile policy must be a regular non-symlink file")
    try:
        policy = load_json(CONFIG_PATH, "Harness profile policy")
    except EvidenceError as error:
        raise ConfigError(str(error)) from error
    required = {
        "schema_version", "coverage_threshold", "unsealed_artifacts", "gate_sets", "profiles",
        "conditional_gates", "evidence_sets", "evidence", "gate_artifacts",
        "machine_status_artifacts", "trusted_approval_runtime",
    }
    if not isinstance(policy, dict) or set(policy) != required or policy.get("schema_version") != 1:
        raise ConfigError("Harness profile policy has an invalid schema")
    if set(policy["profiles"]) != {"change", "pull_request", "nightly", "release"}:
        raise ConfigError("Harness profile policy has an invalid profile set")
    for name, profile in policy["profiles"].items():
        if not isinstance(profile, dict) or profile.get("gate_set") not in policy["gate_sets"]:
            raise ConfigError(f"Harness profile {name!r} has an invalid gate set")
        gates = policy["gate_sets"][profile["gate_set"]]
        skips = profile.get("skippable_gates")
        if not isinstance(gates, list) or len(gates) != len(set(gates)) or not isinstance(skips, list):
            raise ConfigError(f"Harness profile {name!r} has invalid gates")
        if not set(skips).issubset(gates):
            raise ConfigError(f"Harness profile {name!r} skips unknown gates")
    if set(policy["evidence"]) != {"change", "candidate", "release"} or any(
        value not in policy["evidence_sets"] for value in policy["evidence"].values()
    ):
        raise ConfigError("Harness profile policy has invalid evidence sets")
    if set(policy["conditional_gates"]) != {"test_race", "benchmarks"}:
        raise ConfigError("Harness profile policy has invalid conditional gates")
    artifact_rules = list(policy["evidence_sets"].values()) + list(policy["gate_artifacts"].values())
    for rules in artifact_rules:
        paths = rules.get("artifacts") if isinstance(rules, dict) else None
        if not isinstance(paths, list) or len(paths) != len(set(paths)):
            raise ConfigError("Harness profile policy has invalid artifact rules")
        for path in paths:
            normalize_relative(path, "Harness policy artifact")
    return policy


POLICY = load_policy()


def profile(name: str) -> Dict[str, Any]:
    try:
        return POLICY["profiles"][name]
    except KeyError as error:
        raise ConfigError(f"unknown verification profile: {name}") from error


def profile_gates(name: str) -> Tuple[str, ...]:
    return tuple(POLICY["gate_sets"][profile(name)["gate_set"]])


def skippable_gates(name: str) -> Tuple[str, ...]:
    return tuple(profile(name)["skippable_gates"])


def validate_mode_profile(mode: str, profile_name: str) -> None:
    if mode not in profile(profile_name)["evidence_modes"]:
        raise ConfigError(f"{mode} evidence cannot use the {profile_name} profile")


def required_evidence(
    mode: str, statuses: Mapping[str, str]
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    try:
        base = POLICY["evidence_sets"][POLICY["evidence"][mode]]
    except KeyError as error:
        raise ConfigError(f"unknown evidence mode: {mode}") from error
    artifacts = set(base["artifacts"])
    for gate, rules in POLICY["gate_artifacts"].items():
        if statuses.get(gate) == "passed":
            artifacts.update(rules["artifacts"])
    seals = artifacts - set(POLICY["unsealed_artifacts"])
    return tuple(sorted(artifacts)), tuple(sorted(seals))


def known_artifacts() -> Tuple[str, ...]:
    values = set()
    for rules in POLICY["evidence_sets"].values():
        values.update(rules["artifacts"])
    for rules in POLICY["gate_artifacts"].values():
        values.update(rules["artifacts"])
    return tuple(sorted(values))


def _git_blob(repo: pathlib.Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), "show", f"{revision}:{path}"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return result.stdout if result.returncode == 0 else b""


def gate_decision(
    profile_name: str,
    gate: str,
    changes: Sequence[Mapping[str, Any]],
    repo: pathlib.Path,
    base_sha: str,
    head_sha: str,
    performance_requested: bool = False,
) -> Tuple[bool, str]:
    try:
        rule = POLICY["conditional_gates"][gate]
    except KeyError as error:
        raise ConfigError(f"gate {gate!r} has no conditional policy") from error
    if profile_name in rule["always_profiles"]:
        return True, f"mandatory for {profile_name} profile"
    paths = [item.get("path") for item in changes if isinstance(item, Mapping)]
    matching = sorted(
        path for path in paths
        if isinstance(path, str) and path.startswith(tuple(rule["path_prefixes"]))
    )
    if matching:
        label = "protected financial paths changed" if gate == "test_race" else "performance-sensitive paths changed"
        return True, f"{label}: {', '.join(matching)}"
    if gate == "benchmarks" and performance_requested:
        return True, "explicit performance request"
    suffix = rule.get("benchmark_file_suffix")
    declaration = rule.get("benchmark_declaration", "").encode()
    if suffix:
        for path in paths:
            if isinstance(path, str) and path.endswith(suffix) and (
                declaration in _git_blob(repo, base_sha, path)
                or declaration in _git_blob(repo, head_sha, path)
            ):
                return True, f"benchmark file changed: {path}"
    return False, rule["skip_reason"]


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("coverage")
    subparsers.add_parser("artifacts")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--profile", required=True)
    validate.add_argument("--mode", required=True)
    decision = subparsers.add_parser("decision")
    decision.add_argument("--profile", required=True)
    decision.add_argument("--gate", required=True)
    decision.add_argument("--snapshot", required=True, type=pathlib.Path)
    decision.add_argument("--snapshot-sha256", required=True)
    decision.add_argument("--repo", required=True, type=pathlib.Path)
    decision.add_argument("--base", required=True)
    decision.add_argument("--head", required=True)
    decision.add_argument("--performance-requested", choices=("0", "1"), default="0")
    args = parser.parse_args()
    try:
        if args.command == "coverage":
            print(POLICY["coverage_threshold"])
            return 0
        if args.command == "artifacts":
            print("\n".join(known_artifacts()))
            return 0
        if args.command == "validate":
            validate_mode_profile(args.mode, args.profile)
            return 0
        raw = read_bytes(args.snapshot, "sealed change snapshot")
        if sha256_bytes(raw) != args.snapshot_sha256:
            raise ConfigError("sealed change snapshot digest mismatch")
        snapshot = json.loads(raw)
        run, reason = gate_decision(
            args.profile, args.gate, snapshot["changes"], args.repo,
            args.base, args.head, args.performance_requested == "1",
        )
        print(reason)
        return 0 if run else 3
    except (ConfigError, EvidenceError, KeyError, json.JSONDecodeError) as error:
        print(f"harness config: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
