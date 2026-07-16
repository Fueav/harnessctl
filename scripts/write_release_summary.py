#!/usr/bin/env python3
"""Write deterministic, commit-bound change or release evidence."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.fspath(pathlib.Path(__file__).resolve().parent))

from lib.evidence import (
    EvidenceError,
    FULL_SHA_RE,
    SHA256_RE,
    atomic_json,
    collect_artifacts,
    git,
    git_bytes,
    git_clean,
    load_json,
    load_snapshot,
    parse_gate_ledger,
    read_bytes,
    regular_under,
    sha256_bytes,
    sha256_file,
    validate_sealed_arguments,
    validated_argument_file,
    validated_roots,
)
from lib.harness_config import (
    ConfigError,
    POLICY,
    known_artifacts,
    profile_gates,
    required_evidence,
    skippable_gates,
    validate_mode_profile,
)


UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--artifact-dir", required=True, type=pathlib.Path)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--overall", required=True, choices=("passed", "failed"))
    parser.add_argument("--compare-ref", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--clean-before", required=True, choices=("true", "false", "unknown"))
    parser.add_argument("--clean-after", required=True, choices=("true", "false", "unknown"))
    parser.add_argument("--snapshot-file", type=pathlib.Path)
    parser.add_argument("--gates-file", type=pathlib.Path)
    parser.add_argument("--expected-snapshot-sha256", default="")
    parser.add_argument("--expected-gates-sha256", default="")
    parser.add_argument("--expected-head-sha", default="")
    parser.add_argument("--expected-head-tree-sha", default="")
    parser.add_argument("--expected-compare-sha", default="")
    parser.add_argument("--expected-merge-base-sha", default="")
    parser.add_argument("--coverage-threshold", default="")
    parser.add_argument("--sealed-artifact", action="append", default=[])
    parser.add_argument("--failure-reason", action="append", default=[])
    return parser.parse_args()


def _bool_or_none(value: str) -> Optional[bool]:
    return True if value == "true" else False if value == "false" else None


def _load_active_specs(artifact_dir: pathlib.Path) -> List[Dict[str, Any]]:
    candidate = artifact_dir / "spec_registry.json"
    if not candidate.exists() and not candidate.is_symlink():
        return []
    payload = load_json(
        regular_under(artifact_dir, "spec_registry.json", "Specification registry artifact"),
        "Specification registry artifact",
    )
    active = payload.get("active_specs") if isinstance(payload, dict) else None
    if not isinstance(active, list) or not all(isinstance(item, dict) for item in active):
        raise EvidenceError("Specification registry artifact has invalid active_specs")
    return sorted(active, key=lambda item: (str(item.get("spec_id", "")), str(item.get("module", ""))))


def _validate_machine_artifacts(artifact_dir: pathlib.Path) -> None:
    for relative in POLICY["machine_status_artifacts"]:
        candidate = artifact_dir / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        payload = load_json(
            regular_under(artifact_dir, relative, f"artifact {relative!r}"),
            f"artifact {relative!r}",
        )
        if not isinstance(payload, dict) or payload.get("status") != "passed":
            raise EvidenceError(f"artifact {relative!r} does not report passed")


def _load_approval(artifact_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    candidate = artifact_dir / "ai_boundaries.json"
    if not candidate.exists() and not candidate.is_symlink():
        return None
    payload = load_json(
        regular_under(artifact_dir, "ai_boundaries.json", "AI boundary artifact"),
        "AI boundary artifact",
    )
    if not isinstance(payload, dict):
        raise EvidenceError("AI boundary artifact must be a JSON object")
    mode, approved, satisfied = (
        payload.get("approval_mode"),
        payload.get("approved"),
        payload.get("approval_satisfied"),
    )
    if mode not in ("required", "deferred") or type(approved) is not bool or type(satisfied) is not bool:
        raise EvidenceError("AI boundary artifact has invalid approval metadata")
    return {
        "approval_evidence": payload.get("approval_evidence") if approved else None,
        "approval_mode": mode,
        "approval_satisfied": satisfied,
        "approved": approved,
    }


def _load_coverage(
    artifact_dir: pathlib.Path, threshold_value: str
) -> Tuple[Optional[float], Optional[float]]:
    if not threshold_value:
        return None, None
    try:
        threshold = float(threshold_value)
        percentage = float(
            read_bytes(
                regular_under(artifact_dir, "coverage_percent.txt", "coverage percentage artifact"),
                "coverage percentage artifact",
            ).decode("ascii").strip()
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise EvidenceError("coverage evidence is not numeric") from error
    if not 0.0 <= threshold <= 100.0 or not 0.0 <= percentage <= 100.0:
        raise EvidenceError("coverage evidence is outside 0..100")
    return threshold, percentage


def _verifier_inputs(repo: pathlib.Path) -> List[Dict[str, str]]:
    records = []
    raw = git_bytes(
        repo, "ls-files", "-z", "--", ".ai-boundaries.yml", ".github/actions",
        ".github/workflows/ci.yml", ".golangci.yml", "CODEOWNERS", "Makefile", "harness", "scripts",
        required=True,
    )
    for relative in sorted(item.decode() for item in (raw or b"").split(b"\0") if item):
        candidate = repo.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        path = regular_under(repo, relative, f"verifier input {relative!r}")
        records.append({"path": relative, "sha256": sha256_file(path)})
    return records


def main() -> int:
    args = _parse_args()
    try:
        repo, artifact_dir = validated_roots(args.repo, args.artifact_dir)
    except EvidenceError as error:
        print(f"release evidence error: {error}", file=sys.stderr)
        return 2

    errors: List[str] = []
    profile_name = args.profile or ("change" if args.mode == "change" else "release")
    try:
        validate_mode_profile(args.mode, profile_name)
        required_gates = profile_gates(profile_name)
        allowed_skips = skippable_gates(profile_name)
    except ConfigError as error:
        required_gates, allowed_skips = (), ()
        errors.append(str(error))
    for label, value in (("started_at", args.started_at), ("finished_at", args.finished_at)):
        if not UTC_RE.fullmatch(value):
            errors.append(f"{label} is not canonical UTC")

    snapshot: Dict[str, Any] = {}
    snapshot_sha256 = None
    try:
        snapshot_path = validated_argument_file(
            artifact_dir, args.snapshot_file, "change_scope.json", "change snapshot"
        )
        snapshot = load_snapshot(snapshot_path)
        snapshot_sha256 = sha256_file(snapshot_path) if snapshot_path else None
    except EvidenceError as error:
        errors.append(str(error))

    gates: List[Dict[str, Any]] = []
    log_paths: List[str] = []
    gates_sha256 = None
    try:
        gates_path = validated_argument_file(
            artifact_dir, args.gates_file, "gates.tsv", "gate ledger"
        )
        gates, log_paths, gates_raw = parse_gate_ledger(artifact_dir, gates_path)
        gates_sha256 = sha256_bytes(gates_raw) if gates_raw is not None else None
    except EvidenceError as error:
        errors.append(str(error))

    try:
        sealed_artifacts = validate_sealed_arguments(artifact_dir, args.sealed_artifact)
    except EvidenceError as error:
        sealed_artifacts = []
        errors.append(str(error))
    try:
        active_specs = _load_active_specs(artifact_dir) if "spec_registry" in required_gates else []
    except EvidenceError as error:
        active_specs = []
        errors.append(str(error))
    try:
        artifacts = collect_artifacts(artifact_dir, tuple(known_artifacts()) + tuple(log_paths))
    except (EvidenceError, ConfigError) as error:
        artifacts = []
        errors.append(str(error))
    try:
        verifier_inputs = _verifier_inputs(repo)
    except EvidenceError as error:
        verifier_inputs = []
        errors.append(str(error))
    try:
        _validate_machine_artifacts(artifact_dir)
    except EvidenceError as error:
        errors.append(str(error))
    try:
        approval = _load_approval(artifact_dir)
    except EvidenceError as error:
        approval = None
        errors.append(str(error))
    try:
        coverage_threshold, coverage_percentage = _load_coverage(
            artifact_dir, args.coverage_threshold
        )
    except EvidenceError as error:
        coverage_threshold, coverage_percentage = None, None
        errors.append(str(error))

    manifest_bytes = atomic_json(
        artifact_dir / "artifact_manifest.json",
        {"artifacts": artifacts, "schema_version": 1},
    )
    manifest_sha256 = sha256_bytes(manifest_bytes)

    observed_head = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    observed_tree = git(repo, "rev-parse", "--verify", "HEAD^{tree}")
    observed_branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD") or "DETACHED"
    observed_clean = git_clean(repo)
    resolved_compare = (
        git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.compare_ref}^{{commit}}")
        if args.compare_ref else None
    )
    computed_merge_base = (
        git(repo, "merge-base", resolved_compare, observed_head)
        if resolved_compare and observed_head else None
    )

    requested_overall = args.overall
    overall = "failed" if errors else requested_overall
    if overall == "passed" and not snapshot:
        overall = "failed"
        errors.append("passed evidence requires a complete change snapshot")
    if overall == "passed":
        expected = (
            ("HEAD", args.expected_head_sha),
            ("HEAD tree", args.expected_head_tree_sha),
            ("compare", args.expected_compare_sha),
            ("merge base", args.expected_merge_base_sha),
        )
        for label, value in expected:
            if not FULL_SHA_RE.fullmatch(value):
                overall = "failed"
                errors.append(f"passed evidence requires the resolved {label} SHA")

        gate_names = tuple(gate["name"] for gate in gates)
        statuses = {gate["name"]: gate["status"] for gate in gates}
        if gate_names != tuple(required_gates):
            overall = "failed"
            errors.append("passed evidence does not contain the exact required gate sequence")
        failed = [name for name, status in statuses.items() if status == "failed"]
        invalid_skips = [name for name, status in statuses.items() if status == "skipped" and name not in allowed_skips]
        if failed:
            overall = "failed"
            errors.append("passed evidence contains failed gates: " + ", ".join(failed))
        if invalid_skips:
            overall = "failed"
            errors.append("passed evidence contains forbidden skipped gates: " + ", ".join(invalid_skips))

        try:
            required_artifacts, required_seals = required_evidence(args.mode, statuses)
        except ConfigError as error:
            required_artifacts, required_seals = (), ()
            overall = "failed"
            errors.append(str(error))
        artifact_paths = {record["path"] for record in artifacts}
        sealed_paths = {record["path"] for record in sealed_artifacts}
        missing_artifacts = sorted(set(required_artifacts) - artifact_paths)
        missing_seals = sorted(set(required_seals) - sealed_paths)
        if missing_artifacts:
            overall = "failed"
            errors.append("passed evidence is missing required artifacts: " + ", ".join(missing_artifacts))
        if missing_seals:
            overall = "failed"
            errors.append("passed evidence is missing gate-completion seals: " + ", ".join(missing_seals))

        expected_snapshot_mode = "change" if args.mode == "change" else "release"
        identity_checks = (
            (snapshot.get("mode") == expected_snapshot_mode, "change snapshot mode does not match evidence mode"),
            (SHA256_RE.fullmatch(args.expected_snapshot_sha256) is not None and snapshot_sha256 == args.expected_snapshot_sha256, "change snapshot changed after collection"),
            (SHA256_RE.fullmatch(args.expected_gates_sha256) is not None and gates_sha256 == args.expected_gates_sha256, "gate ledger changed before summary generation"),
            (snapshot.get("head_sha") == args.expected_head_sha, "change snapshot HEAD does not match the initial context"),
            (snapshot.get("head_tree_sha") == args.expected_head_tree_sha, "change snapshot tree does not match the initial context"),
            (snapshot.get("base_sha") == args.expected_compare_sha, "change snapshot base does not match the initial context"),
            (snapshot.get("merge_base_sha") == args.expected_merge_base_sha, "change snapshot merge base does not match the initial context"),
            (resolved_compare == args.expected_compare_sha, "change snapshot base does not match the resolved compare ref"),
            (computed_merge_base == args.expected_merge_base_sha, "change snapshot merge base does not match Git"),
            (observed_head == args.expected_head_sha, "observed HEAD does not match the change snapshot"),
            (observed_tree == args.expected_head_tree_sha, "observed HEAD tree does not match the change snapshot"),
            (not args.branch or observed_branch == args.branch, "observed branch does not match the change snapshot"),
        )
        for valid, message in identity_checks:
            if not valid:
                overall = "failed"
                errors.append(message)

    if overall == "passed" and args.mode in ("candidate", "release"):
        coverage_error = None
        if "coverage_threshold" in required_gates:
            if coverage_threshold is None or coverage_percentage is None:
                coverage_error = "passed release evidence requires coverage proof"
            elif coverage_percentage < coverage_threshold:
                coverage_error = "coverage percentage is below the release threshold"
            elif coverage_threshold < float(POLICY["coverage_threshold"]):
                coverage_error = "coverage threshold is below the Harness policy minimum"
        elif coverage_threshold is not None or coverage_percentage is not None:
            coverage_error = "evidence without coverage_threshold must not contain coverage proof"
        if coverage_error: overall = "failed"; errors.append(coverage_error)
        if _bool_or_none(args.clean_before) is not True:
            overall = "failed"
            errors.append("passed release evidence requires clean-before state")
        if _bool_or_none(args.clean_after) is not True or observed_clean is not True:
            overall = "failed"
            errors.append("passed release evidence requires clean-after state")
        if approval is None:
            overall = "failed"
            errors.append("passed evidence requires AI approval metadata")
        elif args.mode == "candidate" and (
            approval["approval_mode"] != "deferred" or approval["approved"]
        ):
            overall = "failed"
            errors.append("candidate evidence requires deferred approval metadata")
        elif args.mode == "release" and (
            approval["approval_mode"] != "required" or not approval["approval_satisfied"]
        ):
            overall = "failed"
            errors.append("release evidence requires satisfied approval metadata")

    failure_reasons = list(args.failure_reason) + errors
    if overall == "failed" and not failure_reasons:
        failure_reasons.append("verification did not complete")
    summary: Dict[str, Any] = {
        "active_specs": active_specs,
        "approval": approval,
        "artifact_manifest_sha256": manifest_sha256,
        "artifacts": artifacts,
        "change_snapshot_sha256": snapshot_sha256,
        "coverage": {"percentage": coverage_percentage, "threshold": coverage_threshold},
        "gate_ledger_sha256": gates_sha256,
        "failure_reasons": failure_reasons,
        "finished_at": args.finished_at,
        "engine": {
            "distribution": "embedded_go_cli",
            "module": "github.com/Fueav/harnessctl",
            "version": os.environ.get("HARNESSCTL_VERSION", "legacy"),
        },
        "gates": gates,
        "git": {
            "branch": args.branch or observed_branch,
            "compare_ref": args.compare_ref or None,
            "compare_sha": args.expected_compare_sha or resolved_compare or snapshot.get("base_sha"),
            "head_sha": args.expected_head_sha or snapshot.get("head_sha") or observed_head,
            "head_tree_sha": args.expected_head_tree_sha or snapshot.get("head_tree_sha") or observed_tree,
            "merge_base_sha": args.expected_merge_base_sha or computed_merge_base or snapshot.get("merge_base_sha"),
            "observed_head_sha_after": observed_head,
            "observed_head_tree_sha_after": observed_tree,
            "observed_working_tree_clean_after": observed_clean,
            "working_tree_clean_after": _bool_or_none(args.clean_after),
            "working_tree_clean_before": _bool_or_none(args.clean_before),
        },
        "mode": args.mode,
        "overall": overall,
        "profile": profile_name,
        "release_ready": args.mode == "release" and profile_name == "release" and overall == "passed",
        "schema_version": 1,
        "sealed_artifacts": sealed_artifacts,
        "started_at": args.started_at,
        "verifier_inputs": verifier_inputs,
    }
    atomic_json(artifact_dir / "summary.json", summary)
    return 0 if overall == requested_overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
