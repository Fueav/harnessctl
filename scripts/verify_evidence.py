#!/usr/bin/env python3
"""Verify that sealed Harness evidence is reusable for the current checkout."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import subprocess
import sys
from typing import Any, Dict

sys.path.insert(0, os.fspath(pathlib.Path(__file__).resolve().parent))

from lib.evidence import (
    EvidenceError,
    FULL_SHA_RE,
    checked_repo,
    git,
    git_clean,
    load_json_bytes,
    load_manifest_bundle,
    read_bytes,
    regular_under,
    sha256_file,
    validate_summary_gates,
    validate_summary_seals,
)
from lib.harness_config import (
    ConfigError,
    POLICY,
    conditional_gates,
    gate_decision,
    profile_gates,
    required_evidence,
    skippable_gates,
    validate_mode_profile,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("HARNESS_PROJECT_ROOT", ".")),
    )
    parser.add_argument("--evidence-dir", required=True, type=pathlib.Path)
    parser.add_argument("--compare-ref")
    parser.add_argument("--mode", choices=("change", "candidate", "release"))
    parser.add_argument("--profile")
    return parser.parse_args()


def checked_evidence_dir(argument: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(os.path.abspath(os.fspath(argument)))
    try:
        info = path.lstat()
    except OSError as error:
        raise EvidenceError("evidence directory is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvidenceError("evidence directory must be a non-symlink directory")
    return path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def current_verifier_inputs(repo: pathlib.Path, expected: Any) -> None:
    require(isinstance(expected, list), "summary has invalid verifier inputs")
    observed = []
    seen = set()
    for item in expected:
        require(
            isinstance(item, dict) and set(item) == {"path", "sha256"},
            "summary has an invalid verifier input record",
        )
        relative, digest = item.get("path"), item.get("sha256")
        require(isinstance(relative, str) and relative not in seen, "summary has duplicate verifier inputs")
        path = regular_under(repo, relative, f"verifier input {relative!r}")
        require(sha256_file(path) == digest, f"verifier input changed: {relative}")
        observed.append(item)
        seen.add(relative)
    require(observed == sorted(observed, key=lambda item: item["path"]), "verifier inputs are not sorted")


def recompute_scope(
    repo: pathlib.Path, mode: str, base_sha: str, expected: Dict[str, Any]
) -> None:
    collector = pathlib.Path(__file__).resolve().parent / "collect_changes.py"
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
            base_sha,
            "--mode",
            "change" if mode == "change" else "release",
            "--format",
            "json",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise EvidenceError("cannot recompute evidence change scope")
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise EvidenceError("recomputed change scope is invalid") from error
    require(observed == expected, "evidence change scope differs from current Git state")


def verify(args: argparse.Namespace) -> Dict[str, Any]:
    repo = checked_repo(args.repo)
    evidence_dir = checked_evidence_dir(args.evidence_dir)
    summary_raw = read_bytes(
        regular_under(evidence_dir, "summary.json", "evidence summary"),
        "evidence summary",
    )
    summary = load_json_bytes(summary_raw, "evidence summary")
    require(isinstance(summary, dict), "evidence summary must be an object")
    require(summary.get("schema_version") == 1, "evidence summary has an unsupported schema")
    require(summary.get("overall") == "passed", "evidence summary did not pass")
    mode, profile = summary.get("mode"), summary.get("profile")
    require(mode in ("change", "candidate", "release"), "evidence summary has an invalid mode")
    require(isinstance(profile, str), "evidence summary has an invalid profile")
    if args.mode:
        require(args.mode == mode, "evidence mode does not match the requested mode")
    if args.profile:
        require(args.profile == profile, "evidence profile does not match the requested profile")
    validate_mode_profile(mode, profile)

    expected_engine = {
        "distribution": "embedded_go_cli",
        "module": "github.com/Fueav/harnessctl",
        "version": os.environ.get("HARNESSCTL_VERSION", ""),
    }
    require(summary.get("engine") == expected_engine, "evidence uses a different harnessctl engine")
    require(git_clean(repo) is True, "current working tree must be clean for evidence reuse")

    identity = summary.get("git")
    require(isinstance(identity, dict), "evidence summary has invalid Git identity")
    for key in ("head_sha", "head_tree_sha", "compare_sha", "merge_base_sha"):
        require(FULL_SHA_RE.fullmatch(str(identity.get(key, ""))) is not None, f"evidence has invalid {key}")
    head = git(repo, "rev-parse", "--verify", "HEAD^{commit}", required=True)
    tree = git(repo, "rev-parse", "--verify", "HEAD^{tree}", required=True)
    require(head == identity["head_sha"], "evidence is stale for the current HEAD")
    require(tree == identity["head_tree_sha"], "evidence is stale for the current HEAD tree")
    if args.compare_ref:
        compare = git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{args.compare_ref}^{{commit}}",
            required=True,
        )
        require(compare == identity["compare_sha"], "evidence compare SHA differs from the requested ref")
    else:
        compare = git(repo, "rev-parse", "--verify", f"{identity['compare_sha']}^{{commit}}", required=True)
    merge_base = git(repo, "merge-base", compare, head, required=True)
    require(merge_base == identity["merge_base_sha"], "evidence merge base differs from current Git state")

    _, contents, observed = load_manifest_bundle(evidence_dir, summary)
    sealed = validate_summary_seals(summary, observed)
    statuses = validate_summary_gates(
        summary, contents, profile_gates(profile), skippable_gates(profile)
    )
    required_artifacts, required_seals = required_evidence(mode, statuses)
    missing_artifacts = sorted(set(required_artifacts) - set(observed))
    missing_seals = sorted(set(required_seals) - set(sealed))
    require(not missing_artifacts, "evidence is missing artifacts: " + ", ".join(missing_artifacts))
    require(not missing_seals, "evidence is missing seals: " + ", ".join(missing_seals))
    require(
        summary.get("change_snapshot_sha256") == observed.get("change_scope.json"),
        "evidence change scope digest disagrees with the summary",
    )

    scope = load_json_bytes(contents["change_scope.json"], "evidence change scope")
    require(isinstance(scope, dict), "evidence change scope must be an object")
    recompute_scope(repo, mode, identity["compare_sha"], scope)
    for gate in conditional_gates(profile):
        selected, _ = gate_decision(
            profile,
            gate,
            scope["changes"],
            repo,
            identity["compare_sha"],
            identity["head_sha"],
        )
        if selected:
            require(statuses.get(gate) == "passed", f"current scope requires gate {gate!r}")

    current_verifier_inputs(repo, summary.get("verifier_inputs"))
    for relative in POLICY["machine_status_artifacts"]:
        payload = load_json_bytes(contents[relative], f"machine artifact {relative!r}")
        require(isinstance(payload, dict) and payload.get("status") == "passed", f"machine artifact {relative!r} did not pass")
    approval = summary.get("approval")
    require(isinstance(approval, dict), "evidence summary has invalid approval metadata")
    if mode == "candidate":
        require(
            approval.get("approval_mode") == "deferred" and approval.get("approved") is False,
            "candidate evidence is not approval-deferred",
        )
    if mode == "release":
        require(
            approval.get("approval_mode") == "required" and approval.get("approval_satisfied") is True,
            "release evidence lacks satisfied approval metadata",
        )
    return {
        "schema_version": 1,
        "status": "passed",
        "reused": True,
        "mode": mode,
        "profile": profile,
        "head_sha": head,
        "evidence_dir": os.fspath(evidence_dir),
    }


def main() -> int:
    args = parse_args()
    try:
        report = verify(args)
    except (ConfigError, EvidenceError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"evidence verify: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
