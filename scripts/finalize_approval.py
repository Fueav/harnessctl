#!/usr/bin/env python3
"""Finalize review approval against trusted Git scope and sealed candidate evidence."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.fspath(pathlib.Path(__file__).resolve().parent))

from lib.evidence import (
    EvidenceError,
    FULL_SHA_RE,
    atomic_json_under,
    checked_directory,
    checked_repo,
    git,
    git_bytes,
    git_clean,
    load_json,
    load_json_bytes,
    load_manifest_bundle,
    normalize_relative,
    read_bytes,
    regular_under,
    repo_relative,
    sha256_bytes,
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


FinalizationError = EvidenceError


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--candidate-dir", required=True, type=pathlib.Path)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--expected-compare-sha", required=True)
    parser.add_argument("--review-decision", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    return parser.parse_args()


def _verify_trusted_checkout(repo: pathlib.Path, expected_compare: str) -> None:
    if git(repo, "rev-parse", "--verify", "HEAD^{commit}", required=True) != expected_compare:
        raise FinalizationError("approval finalizer is not running from the expected base")
    if git_clean(repo, untracked=False) is not True:
        raise FinalizationError("trusted base checkout has tracked modifications")
    external_engine = os.environ.get("HARNESS_EXTERNAL_ENGINE", "0") == "1"
    if not external_engine:
        finalizer = regular_under(repo, "scripts/finalize_approval.py", "trusted approval finalizer")
        try:
            if not os.path.samefile(pathlib.Path(__file__).resolve(), finalizer):
                raise FinalizationError("approval finalizer is not executing from the trusted checkout")
        except OSError as error:
            raise FinalizationError("trusted approval finalizer is unreadable") from error
    for relative in POLICY["trusted_approval_runtime"]:
        local = read_bytes(regular_under(repo, relative, f"trusted runtime {relative!r}"), f"trusted runtime {relative!r}")
        committed = git_bytes(repo, "show", f"{expected_compare}:{relative}", required=True)
        if local != committed:
            raise FinalizationError(f"trusted runtime {relative!r} does not match the expected base")


def _recompute_boundaries(
    repo: pathlib.Path, expected_head: str, expected_compare: str
) -> Dict[str, Any]:
    if os.environ.get("HARNESS_EXTERNAL_ENGINE", "0") == "1":
        checker = pathlib.Path(__file__).resolve().parent / "check_ai_boundaries.py"
    else:
        checker = regular_under(
            repo, "scripts/check_ai_boundaries.py", "trusted AI boundary checker"
        )
    with tempfile.TemporaryDirectory(prefix="approval-boundary-") as directory:
        root = pathlib.Path(directory)
        worktree, artifact_dir = root / "candidate", root / "evidence"
        added = False
        try:
            git_bytes(
                repo, "worktree", "add", "--detach", os.fspath(worktree), expected_head,
                required=True,
            )
            added = True
            environment = os.environ.copy()
            for name in (
                "AI_BOUNDARY_APPROVAL_EVIDENCE",
                "VERIFY_ARTIFACT_DIR",
                "VERIFY_CANDIDATE_ARTIFACT_DIR",
                "VERIFY_EVIDENCE_MODE",
                "VERIFY_PROFILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "AI_BOUNDARY_APPROVAL_MODE": "deferred",
                    "AI_BOUNDARY_APPROVED": "0",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            if os.environ.get("HARNESS_EXTERNAL_ENGINE", "0") == "1":
                environment.update(
                    {
                        "HARNESS_EXTERNAL_ENGINE": "1",
                        "HARNESS_PROJECT_ROOT": os.fspath(worktree),
                        "HARNESS_PROFILE_CONFIG": os.fspath(
                            worktree / "harness" / "harness_profiles.json"
                        ),
                        "HARNESSCTL_VERSION": os.environ.get(
                            "HARNESSCTL_VERSION", "unknown"
                        ),
                    }
                )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-S",
                    os.fspath(checker),
                    "--repo",
                    os.fspath(worktree),
                    "--base",
                    expected_compare,
                    "--mode",
                    "release",
                    "--artifact-dir",
                    os.fspath(artifact_dir),
                ],
                check=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise FinalizationError(
                    "trusted AI boundary recomputation failed"
                    + (f": {detail}" if detail else "")
                )
            trusted = load_json(
                artifact_dir / "ai_boundaries.json", "trusted AI boundary artifact"
            )
            if not isinstance(trusted, dict) or trusted.get("status") != "passed":
                raise FinalizationError("trusted AI boundary recomputation did not pass")
            return trusted
        finally:
            if added:
                subprocess.run(
                    ["git", "-C", os.fspath(repo), "worktree", "remove", "--force", os.fspath(worktree)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(
                    ["git", "-C", os.fspath(repo), "worktree", "prune"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


def _trusted_scope(
    boundary: Dict[str, Any], expected_head: str, expected_compare: str
) -> Dict[str, Any]:
    snapshot = boundary.get("snapshot")
    if boundary.get("mode") != "release" or not isinstance(snapshot, dict):
        raise FinalizationError("trusted boundary recomputation has no release snapshot")
    if snapshot.get("head_sha") != expected_head or snapshot.get("base_sha") != expected_compare:
        raise FinalizationError("trusted boundary recomputation used the wrong Git identity")
    if snapshot.get("clean") is not True or any(
        not FULL_SHA_RE.fullmatch(str(snapshot.get(key, "")))
        for key in ("head_tree_sha", "merge_base_sha")
    ):
        raise FinalizationError("trusted boundary recomputation is not a clean snapshot")
    changes, previous = [], None
    for change in boundary.get("changes", []):
        if not isinstance(change, dict):
            raise FinalizationError("trusted boundary recomputation has invalid changes")
        path, sources = change.get("path"), change.get("sources")
        normalize_relative(path, "trusted changed path")
        if sources != ["committed"] or (previous is not None and path <= previous):
            raise FinalizationError("trusted boundary recomputation has non-canonical changes")
        changes.append({"path": path, "sources": sources})
        previous = path
    return {
        "base_sha": expected_compare,
        "changes": changes,
        "clean": True,
        "head_sha": expected_head,
        "head_tree_sha": snapshot["head_tree_sha"],
        "merge_base_sha": snapshot["merge_base_sha"],
        "mode": "release",
    }


def _validate_machine_evidence(
    repo: pathlib.Path,
    summary: Dict[str, Any],
    git_identity: Dict[str, Any],
    contents: Dict[str, bytes],
    observed: Dict[str, str],
    sealed: Dict[str, str],
    trusted_scope: Dict[str, Any],
) -> Dict[str, str]:
    required_gates = profile_gates("pull_request")
    statuses = validate_summary_gates(
        summary, contents, required_gates, skippable_gates("pull_request")
    )
    required_artifacts, required_seals = required_evidence("candidate", statuses)
    missing_artifacts = sorted(set(required_artifacts) - set(observed))
    missing_seals = sorted(set(required_seals) - set(sealed))
    if missing_artifacts:
        raise FinalizationError("candidate evidence is missing required artifacts: " + ", ".join(missing_artifacts))
    if missing_seals:
        raise FinalizationError("candidate evidence is missing required seals: " + ", ".join(missing_seals))
    if summary.get("change_snapshot_sha256") != observed.get("change_scope.json"):
        raise FinalizationError("candidate change snapshot digest disagrees with the summary")

    coverage = summary.get("coverage")
    if statuses.get("coverage_threshold") == "passed":
        try:
            coverage_percentage = float(contents["coverage_percent.txt"].decode("ascii").strip())
        except (KeyError, UnicodeDecodeError, ValueError) as error:
            raise FinalizationError("candidate coverage evidence is invalid") from error
        threshold = coverage.get("threshold") if isinstance(coverage, dict) else None
        percentage = coverage.get("percentage") if isinstance(coverage, dict) else None
        if type(threshold) not in (int, float) or type(percentage) not in (int, float):
            raise FinalizationError("candidate summary has invalid coverage values")
        if (
            threshold < float(POLICY["coverage_threshold"])
            or percentage != coverage_percentage or percentage < threshold
        ):
            raise FinalizationError("candidate coverage does not satisfy the trusted threshold")
    elif coverage != {"percentage": None, "threshold": None} or "coverage_percent.txt" in contents:
        raise FinalizationError("candidate without coverage_threshold has unexpected coverage evidence")
    if statuses.get("test_unit_coverage") != "passed" and "coverage.out" in contents:
        raise FinalizationError("candidate without test_unit_coverage has unexpected coverage output")

    scope = load_json_bytes(contents["change_scope.json"], "candidate change snapshot")
    if scope != trusted_scope:
        raise FinalizationError("candidate change snapshot disagrees with trusted Git recomputation")
    for scope_key, git_key in (
        ("head_sha", "head_sha"),
        ("head_tree_sha", "head_tree_sha"),
        ("base_sha", "compare_sha"),
        ("merge_base_sha", "merge_base_sha"),
    ):
        if scope.get(scope_key) != git_identity.get(git_key):
            raise FinalizationError(f"candidate change snapshot disagrees with summary identity {git_key}")

    for gate in conditional_gates("pull_request"):
        required, _ = gate_decision(
            "pull_request", gate, trusted_scope["changes"], repo,
            trusted_scope["base_sha"], trusted_scope["head_sha"],
        )
        if required and statuses.get(gate) != "passed":
            raise FinalizationError(f"trusted Git recomputation requires the {gate} gate")

    for relative in POLICY["machine_status_artifacts"]:
        payload = load_json_bytes(contents[relative], f"candidate artifact {relative!r}")
        if not isinstance(payload, dict) or payload.get("status") != "passed":
            raise FinalizationError(f"candidate artifact {relative!r} does not report passed")
    return statuses


def _validate_candidate(
    repo: pathlib.Path,
    candidate_dir: pathlib.Path,
    expected_head: str,
    expected_compare: str,
    trusted_scope: Dict[str, Any],
) -> Tuple[Dict[str, str], str, str, Dict[str, Any]]:
    summary_raw = read_bytes(
        regular_under(candidate_dir, "summary.json", "candidate summary"),
        "candidate summary",
    )
    summary = load_json_bytes(summary_raw, "candidate summary")
    expected = {
        "schema_version": 1,
        "mode": "candidate",
        "profile": "pull_request",
        "overall": "passed",
        "release_ready": False,
    }
    if not isinstance(summary, dict):
        raise FinalizationError("candidate summary must be a JSON object")
    for key, value in expected.items():
        if summary.get(key) != value:
            raise FinalizationError(f"candidate summary has invalid {key}")
    if os.environ.get("HARNESS_EXTERNAL_ENGINE", "0") == "1":
        engine = summary.get("engine")
        expected_version = os.environ.get("HARNESSCTL_VERSION", "")
        if not isinstance(engine, dict) or engine != {
            "distribution": "embedded_go_cli",
            "module": "github.com/Fueav/harnessctl",
            "version": expected_version,
        }:
            raise FinalizationError("candidate evidence uses a different harnessctl engine")
    validate_mode_profile("candidate", "pull_request")

    git_identity = summary.get("git")
    if not isinstance(git_identity, dict) or any(
        not FULL_SHA_RE.fullmatch(str(git_identity.get(key, "")))
        for key in ("head_sha", "head_tree_sha", "compare_sha", "merge_base_sha")
    ):
        raise FinalizationError("candidate summary has invalid Git identity")
    if git_identity["head_sha"] != expected_head:
        raise FinalizationError("candidate evidence is stale for the expected HEAD")
    if git_identity["compare_sha"] != expected_compare:
        raise FinalizationError("candidate evidence uses the wrong compare SHA")

    manifest_sha256, contents, observed = load_manifest_bundle(candidate_dir, summary)
    sealed = validate_summary_seals(summary, observed)
    _validate_machine_evidence(
        repo, summary, git_identity, contents, observed, sealed, trusted_scope
    )

    boundary = load_json_bytes(contents["ai_boundaries.json"], "AI boundary artifact")
    if not isinstance(boundary, dict) or boundary.get("status") != "passed":
        raise FinalizationError("AI boundary artifact does not report passed")
    if boundary.get("approval_mode") != "deferred" or boundary.get("approved") is not False:
        raise FinalizationError("candidate AI boundary artifact is not approval-deferred")
    classifications = boundary.get("classifications")
    if not isinstance(classifications, dict) or any(
        not isinstance(classifications.get(key), list)
        for key in ("allowed", "approval_required", "forbidden", "unclassified")
    ):
        raise FinalizationError("candidate AI boundary artifact has invalid classifications")
    if classifications["forbidden"] or classifications["unclassified"]:
        raise FinalizationError("candidate AI boundary artifact contains rejected paths")
    if type(boundary.get("approval_satisfied")) is not bool or (
        classifications["approval_required"] and boundary["approval_satisfied"]
    ):
        raise FinalizationError("candidate AI boundary artifact has invalid approval state")
    if boundary.get("snapshot") != {
        "base_sha": trusted_scope["base_sha"],
        "clean": True,
        "head_sha": trusted_scope["head_sha"],
        "head_tree_sha": trusted_scope["head_tree_sha"],
        "merge_base_sha": trusted_scope["merge_base_sha"],
    }:
        raise FinalizationError("candidate AI boundary artifact has the wrong snapshot")
    return (
        {key: git_identity[key] for key in ("compare_sha", "head_sha", "head_tree_sha", "merge_base_sha")},
        sha256_bytes(summary_raw),
        manifest_sha256,
        boundary,
    )


def main() -> int:
    args = _parse_args()
    errors: List[str] = []
    candidate: Dict[str, Any] = {
        "compare_sha": args.expected_compare_sha,
        "head_sha": args.expected_head_sha,
    }
    summary_sha256 = manifest_sha256 = trusted_recomputation = None
    repo = None
    output_relative = None
    try:
        repo = checked_repo(args.repo)
        candidate_relative = repo_relative(repo, args.candidate_dir, "candidate evidence root")
        output_relative = repo_relative(repo, args.output, "approval output")
        candidate_dir = checked_directory(repo, candidate_relative, "candidate evidence root")
        if not FULL_SHA_RE.fullmatch(args.expected_head_sha):
            raise FinalizationError("expected HEAD SHA is invalid")
        if not FULL_SHA_RE.fullmatch(args.expected_compare_sha):
            raise FinalizationError("expected compare SHA is invalid")
        _verify_trusted_checkout(repo, args.expected_compare_sha)
        if args.review_decision != "APPROVED":
            raise FinalizationError("current review decision is not APPROVED")
        trusted_boundary = _recompute_boundaries(
            repo, args.expected_head_sha, args.expected_compare_sha
        )
        trusted_scope = _trusted_scope(
            trusted_boundary, args.expected_head_sha, args.expected_compare_sha
        )
        candidate, summary_sha256, manifest_sha256, candidate_boundary = _validate_candidate(
            repo, candidate_dir, args.expected_head_sha, args.expected_compare_sha, trusted_scope
        )
        for key, label in (
            ("snapshot", "snapshot"),
            ("classifications", "classifications"),
            ("approval_satisfied", "approval state"),
        ):
            if candidate_boundary.get(key) != trusted_boundary.get(key):
                raise FinalizationError(
                    f"candidate AI boundary {label} disagrees with trusted recomputation"
                )
        trusted_recomputation = {
            "boundary_sha256": sha256_bytes(
                (json.dumps(trusted_boundary, sort_keys=True) + "\n").encode()
            ),
            "change_count": len(trusted_scope["changes"]),
            "head_tree_sha": trusted_scope["head_tree_sha"],
            "merge_base_sha": trusted_scope["merge_base_sha"],
        }
    except (EvidenceError, ConfigError) as error:
        errors.append(str(error))
        if repo is None or output_relative is None:
            print(f"approval finalization error: {error}")
            return 2

    passed = not errors
    payload: Dict[str, Any] = {
        "approval_satisfied": passed,
        "artifact_manifest_sha256": manifest_sha256,
        "candidate": candidate,
        "candidate_summary_sha256": summary_sha256,
        "errors": errors,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "review_decision": args.review_decision,
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "trusted_recomputation": trusted_recomputation,
    }
    try:
        atomic_json_under(repo, output_relative, payload)
    except EvidenceError as error:
        print(f"approval finalization error: {error}")
        return 2
    for error in errors:
        print(f"approval finalization error: {error}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
