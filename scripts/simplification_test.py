#!/usr/bin/env python3
"""Exercise proportional selection and workflow-free specs through public scripts."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

engine = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    policy = json.loads((engine / "harness_profiles.json").read_text())
    policy["schema_version"] = 4
    policy["conditional_gates"]["build"] = {
        "always_profiles": ["release"], "path_prefixes": ["go.mod", "internal/"],
        "path_suffixes": [".go"], "skip_reason": "no Go inputs changed",
    }
    config = root / "policy.json"
    config.write_text(json.dumps(policy))
    env = dict(os.environ, HARNESS_PROFILE_CONFIG=str(config))
    def decision(path, profile="pull_request"):
        snapshot = root / "snapshot.json"
        raw = json.dumps({"changes": [{"path": path}]}).encode()
        snapshot.write_bytes(raw)
        return subprocess.run([sys.executable, "-I", "-B", "-S", str(engine / "lib/harness_config.py"),
            "decision", "--profile", profile, "--gate", "build", "--snapshot", str(snapshot),
            "--snapshot-sha256", hashlib.sha256(raw).hexdigest(), "--repo", str(root),
            "--base", "a" * 40, "--head", "b" * 40], env=env, capture_output=True, text=True)
    for path, profile, code in [("README.md", "pull_request", 3), ("feature.go", "pull_request", 0),
            ("internal/data/template.txt", "pull_request", 0), ("go.mod", "pull_request", 0),
            ("README.md", "release", 0)]:
        result = decision(path, profile)
        assert result.returncode == code, (path, profile, result.stderr)
    policy["conditional_gates"]["ai_boundaries"] = policy["conditional_gates"]["build"]
    config.write_text(json.dumps(policy))
    result = decision("README.md")
    assert result.returncode == 2, "mandatory boundary check became conditional"

    (root / "specs/alpha").mkdir(parents=True)
    (root / "specs/index.json").write_text(json.dumps({"version": 2, "specs": ["specs/alpha/spec.md"]}))
    spec = root / "specs/alpha/spec.md"
    spec.write_text("---\nspec_id: SPEC-ALPHA-001\nmodule: alpha\nstatus: approved\n---\n\nAn observable contract.\n")
    command = [sys.executable, "-I", "-B", "-S", str(engine / "check_spec_registry.py"), "--repo", str(root)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    spec.write_text(spec.read_text().replace("SPEC-ALPHA-001", "invalid"))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0 and "spec_id" in result.stderr, result.stderr

    spec.write_text(spec.read_text().replace("invalid", "SPEC-ALPHA-001"))
    del policy["conditional_gates"]["ai_boundaries"]
    for gate in ("test_unit_coverage", "coverage_threshold"):
        policy["conditional_gates"][gate] = policy["conditional_gates"]["build"].copy()
    policy["gate_sets"]["candidate"] = ["change_scope", "release_context_before", "build",
        "test_unit_coverage", "gitleaks", "ai_boundaries", "coverage_threshold", "spec_registry", "release_context_after"]
    policy["profiles"]["pull_request"].update(gate_set="candidate", skippable_gates=[])
    policy["evidence_sets"]["candidate"] = policy["evidence_sets"]["change"].copy()
    policy["evidence"]["candidate"] = "candidate"
    policy["gate_artifacts"].update(test_unit_coverage={"artifacts": ["coverage.out"]},
        coverage_threshold={"artifacts": ["coverage_percent.txt"]})
    policy["trusted_approval_runtime"] = ["harness/harness_profiles.json"]
    (root / "harness").mkdir()
    config = root / "harness/harness_profiles.json"
    config.write_text(json.dumps(policy))
    env.update(HARNESS_PROFILE_CONFIG=str(config), HARNESS_PROJECT_ROOT=str(root), HARNESS_ENGINE_DIR=str(engine), HARNESS_EXTERNAL_ENGINE="1", HARNESSCTL_VERSION="v0.6.0")
    (root / "AGENTS.md").write_text("Fixture contract.\n")
    (root / ".ai-boundaries.yml").write_text("allowed:\n  - README.md\napproval_required:\n  - harness/\nforbidden:\n  - secrets/\n")
    (root / "README.md").write_text("before\n")
    (root / ".gitignore").write_text(".artifacts/\n.tools/\n")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q"); git("config", "user.name", "Fixture"); git("config", "user.email", "fixture@example.test")
    git("add", "."); git("commit", "-qm", "baseline")
    env["VERIFY_COMPARE_REF"] = git("rev-parse", "HEAD")
    (root / "README.md").write_text("after\n")
    git("commit", "-qam", "docs change")
    tools = root / ".tools/bin"; tools.mkdir(parents=True)
    for name, body in {"go": "exit 99", "gitleaks": "exit 0"}.items():
        executable = tools / name; executable.write_text("#!/bin/sh\n" + body + "\n"); executable.chmod(0o755)
    result = subprocess.run([str(engine / "verify_candidate.sh")], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((root / ".artifacts/candidate/summary.json").read_text())
    assert summary["overall"] == "passed" and summary["release_ready"] is False, summary
    statuses = {gate["name"]: gate["status"] for gate in summary["gates"]}
    assert statuses["build"] == statuses["test_unit_coverage"] == statuses["coverage_threshold"] == "skipped", statuses
    assert statuses["gitleaks"] == statuses["ai_boundaries"] == "passed", statuses

    head = git("rev-parse", "HEAD")
    git("checkout", "-q", "--detach", env["VERIFY_COMPARE_REF"])
    env["HARNESS_EXTERNAL_ENGINE"] = "1"
    result = subprocess.run([sys.executable, str(engine / "finalize_approval.py"), "--repo", str(root),
        "--candidate-dir", str(root / ".artifacts/candidate"), "--expected-head-sha", head,
        "--expected-compare-sha", env["VERIFY_COMPARE_REF"], "--review-decision", "APPROVED",
        "--output", str(root / ".artifacts/approval/result.json")], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

print("simplification tests passed")
