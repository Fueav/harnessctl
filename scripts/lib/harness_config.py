#!/usr/bin/env python3
"""Strict loader and conditional-gate selector for the Harness profile policy."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
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

BUILTIN_GATES = frozenset({
    "change_scope", "toolchain", "symlinks", "gofmt", "build", "vet", "golangci",
    "changed_package_tests", "test_unit_coverage", "govulncheck", "gitleaks",
    "ai_boundaries", "coverage_threshold", "test_race", "migration_safety",
    "prompt_evals", "spec_registry", "benchmarks", "release_context_before",
    "release_context_after",
})
CUSTOM_GATE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
V1_KEYS = {
    "schema_version", "coverage_threshold", "unsealed_artifacts", "gate_sets", "profiles",
    "conditional_gates", "evidence_sets", "evidence", "gate_artifacts",
    "machine_status_artifacts", "trusted_approval_runtime",
}
LEGACY_SYMLINKS = [
    {"link": "CLAUDE.md", "target": "AGENTS.md"},
    {"link": ".claude/skills", "target": ".agents/skills"},
    {"link": "internal/risk/CLAUDE.md", "target": "internal/risk/AGENTS.md"},
    {"link": "internal/ledger/CLAUDE.md", "target": "internal/ledger/AGENTS.md"},
]


class ConfigError(RuntimeError):
    """Raised when the trusted Harness policy is malformed."""


def _relative(value: Any, label: str) -> pathlib.PurePosixPath:
    if isinstance(value, str) and any(character in value for character in "\t\r\n"):
        raise ConfigError(f"{label} must not contain TSV control characters")
    try:
        return normalize_relative(value, label)
    except EvidenceError as error:
        raise ConfigError(str(error)) from error


def _unique_strings(value: Any, label: str) -> Sequence[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) for item in value)
        or len(value) != len(set(value))
    ):
        raise ConfigError(f"{label} must be a list of unique strings")
    return value


def _validate_v2_extensions(policy: Dict[str, Any]) -> None:
    custom = policy["custom_gates"]
    if not isinstance(custom, dict):
        raise ConfigError("Harness profile policy has invalid custom gates")
    for name, rule in custom.items():
        if not isinstance(name, str) or not CUSTOM_GATE_RE.fullmatch(name) or name in BUILTIN_GATES:
            raise ConfigError(f"Harness profile policy has invalid custom gate {name!r}")
        if not isinstance(rule, dict) or set(rule) != {"run"}:
            raise ConfigError(f"Harness custom gate {name!r} has an invalid command")
        run = _relative(rule["run"], f"Harness custom gate {name!r} command")
        if len(run.parts) < 2 or run.parts[0] not in ("scripts", "harness"):
            raise ConfigError(f"Harness custom gate {name!r} command must be under scripts/ or harness/")

    links = policy["symlinks"]
    if not isinstance(links, list):
        raise ConfigError("Harness profile policy has invalid symlinks")
    for pair in links:
        if not isinstance(pair, dict) or set(pair) != {"link", "target"}:
            raise ConfigError("Harness profile policy has an invalid symlink pair")
        _relative(pair["link"], "Harness symlink link")
        _relative(pair["target"], "Harness symlink target")

    known = BUILTIN_GATES | set(custom)
    referenced = set()
    for set_name, gates in policy["gate_sets"].items():
        gates = _unique_strings(gates, f"Harness gate set {set_name!r}")
        unknown = set(gates) - known
        if unknown:
            raise ConfigError(f"Harness gate set {set_name!r} contains unknown gates")
        if "release_context_after" in gates and gates[-1] != "release_context_after":
            raise ConfigError(f"Harness gate set {set_name!r} has invalid custom gate ordering")
        ordered = gates[:-1] if gates and gates[-1] == "release_context_after" else gates
        positions = [index for index, gate in enumerate(ordered) if gate in custom]
        if positions and positions != list(range(positions[0], len(ordered))):
            raise ConfigError(f"Harness gate set {set_name!r} has invalid custom gate ordering")
        referenced.update(gate for gate in gates if gate in custom)
    if referenced != set(custom):
        raise ConfigError("Harness profile policy contains unreferenced custom gates")


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
    if not isinstance(policy, dict) or type(policy.get("schema_version")) is not int:
        raise ConfigError("Harness profile policy has an invalid schema")
    schema_version = policy["schema_version"]
    required = V1_KEYS if schema_version == 1 else V1_KEYS | {"custom_gates", "symlinks"}
    if schema_version not in (1, 2) or set(policy) != required:
        raise ConfigError("Harness profile policy has an invalid schema")
    mapping_keys = (
        "conditional_gates", "evidence", "evidence_sets", "gate_artifacts",
        "gate_sets", "profiles",
    )
    if any(not isinstance(policy.get(key), dict) for key in mapping_keys):
        raise ConfigError("Harness profile policy has invalid gates")
    if schema_version == 2:
        _validate_v2_extensions(policy)
    else:
        policy["custom_gates"] = {}
        policy["symlinks"] = [dict(pair) for pair in LEGACY_SYMLINKS]
    if set(policy["profiles"]) != {"change", "pull_request", "nightly", "release"}:
        raise ConfigError("Harness profile policy has an invalid profile set")
    for name, profile in policy["profiles"].items():
        gate_set = profile.get("gate_set") if isinstance(profile, dict) else None
        if not isinstance(gate_set, str) or gate_set not in policy["gate_sets"]:
            raise ConfigError(f"Harness profile {name!r} has an invalid gate set")
        gates = _unique_strings(
            policy["gate_sets"][gate_set],
            f"Harness profile {name!r} gates",
        )
        _unique_strings(
            profile.get("evidence_modes"),
            f"Harness profile {name!r} evidence modes",
        )
        skips = _unique_strings(
            profile.get("skippable_gates"),
            f"Harness profile {name!r} skippable gates",
        )
        if not set(skips).issubset(gates):
            raise ConfigError(f"Harness profile {name!r} skips unknown gates")
        if schema_version == 2 and set(skips) & set(policy["custom_gates"]):
            raise ConfigError(f"Harness profile {name!r} skips a custom gate")
    evidence_references = policy["evidence"].values()
    if (
        set(policy["evidence"]) != {"change", "candidate", "release"}
        or not all(isinstance(value, str) for value in evidence_references)
        or any(value not in policy["evidence_sets"] for value in policy["evidence"].values())
    ):
        raise ConfigError("Harness profile policy has invalid evidence sets")
    if set(policy["conditional_gates"]) != {"test_race", "benchmarks"}:
        raise ConfigError("Harness profile policy has invalid conditional gates")
    artifact_rules = list(policy["evidence_sets"].values()) + list(policy["gate_artifacts"].values())
    for rules in artifact_rules:
        paths = rules.get("artifacts") if isinstance(rules, dict) else None
        paths = _unique_strings(paths, "Harness policy artifact rules")
        for path in paths:
            _relative(path, "Harness policy artifact")
    return policy


try:
    POLICY = load_policy()
except ConfigError as error:
    if __name__ == "__main__":
        print(f"harness config: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    raise


def profile(name: str) -> Dict[str, Any]:
    try:
        return POLICY["profiles"][name]
    except KeyError as error:
        raise ConfigError(f"unknown verification profile: {name}") from error


def profile_gates(name: str) -> Tuple[str, ...]:
    return tuple(POLICY["gate_sets"][profile(name)["gate_set"]])


def skippable_gates(name: str) -> Tuple[str, ...]:
    return tuple(profile(name)["skippable_gates"])


def custom_gates(name: str) -> Tuple[Tuple[str, str], ...]:
    custom = POLICY["custom_gates"]
    return tuple((gate, custom[gate]["run"]) for gate in profile_gates(name) if gate in custom)


def gate_artifacts(name: str) -> Tuple[str, ...]:
    if name not in BUILTIN_GATES and name not in POLICY["custom_gates"]:
        raise ConfigError(f"unknown gate: {name}")
    return tuple(POLICY["gate_artifacts"].get(name, {}).get("artifacts", ()))


def symlinks() -> Tuple[Tuple[str, str], ...]:
    return tuple((pair["link"], pair["target"]) for pair in POLICY["symlinks"])


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
    custom = subparsers.add_parser("custom-gates")
    custom.add_argument("--profile", required=True)
    gate_artifact_parser = subparsers.add_parser("gate-artifacts")
    gate_artifact_parser.add_argument("--gate", required=True)
    subparsers.add_parser("symlinks")
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
        if args.command == "custom-gates":
            lines = (f"{name}\t{run}" for name, run in custom_gates(args.profile))
            sys.stdout.write("".join(f"{line}\n" for line in lines))
            return 0
        if args.command == "gate-artifacts":
            sys.stdout.write("".join(f"{path}\n" for path in gate_artifacts(args.gate)))
            return 0
        if args.command == "symlinks":
            sys.stdout.write("".join(f"{link}\t{target}\n" for link, target in symlinks()))
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
