#!/usr/bin/env python3
"""Exercise prompt verification through actual change and candidate runners."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parent


class PromptEvalTests(unittest.TestCase):
    def fixture(self, directory, version=5):
        root = Path(directory)
        (root / "harness").mkdir()
        policy = json.loads((ENGINE / "harness_profiles.json").read_text())
        policy["schema_version"] = version
        policy["gate_sets"] = {
            "change": ["change_scope", "ai_boundaries", "prompt_evals"],
            "release": ["change_scope", "release_context_before", "ai_boundaries", "prompt_evals", "release_context_after"],
        }
        for profile in policy["profiles"].values():
            profile["gate_set"] = "change" if profile["evidence_modes"] == ["change"] else "release"
            profile["skippable_gates"] = []
        policy["conditional_gates"] = {key: value for key, value in policy["conditional_gates"].items() if key in {"test_race", "benchmarks"}}
        for rule in policy["conditional_gates"].values(): rule["always_profiles"] = []
        policy["conditional_gates"]["prompt_evals"] = {
            "always_profiles": ["release"], "path_prefixes": ["AGENTS.md", ".agents/", "docs/harness-workflows.md", "prompts/", "evals/"],
            "path_suffixes": ["/AGENTS.md"] if version >= 5 else [],
            "skip_reason": "no prompt inputs changed",
        }
        for evidence in policy["evidence_sets"].values():
            evidence["artifacts"] = ["gates.tsv", "change_scope.json", "ai_boundaries.json"]
        policy["machine_status_artifacts"] = ["ai_boundaries.json"]
        policy["trusted_approval_runtime"] = ["harness/harness_profiles.json"]
        (root / "harness/harness_profiles.json").write_text(json.dumps(policy))
        (root / "AGENTS.md").write_text("Original contract.\n")
        (root / "README.md").write_text("Project description.\n")
        (root / ".gitignore").write_text(".artifacts/\n.tools/\n")
        (root / ".ai-boundaries.yml").write_text("allowed:\n  - AGENTS.md\n  - README.md\n  - evals/\n  - prompts/\n  - .agents/\n  - docs/\n  - internal/\napproval_required:\n  - harness/\nforbidden:\n  - secrets/\n")
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "Fixture")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        return root

    def git(self, root, *arguments):
        return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()

    def commit(self, root):
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "fixture")
        return self.git(root, "rev-parse", "HEAD")

    def verify(self, root, base, mode):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("HARNESS_", "VERIFY_", "AI_BOUNDARY_"))}
        env.update(HARNESS_PROJECT_ROOT=str(root), HARNESS_ENGINE_DIR=str(ENGINE),
                   HARNESS_PROFILE_CONFIG=str(root / "harness/harness_profiles.json"),
                   HARNESS_EXTERNAL_ENGINE="1", HARNESSCTL_VERSION="v0.8.0", VERIFY_COMPARE_REF=base)
        return subprocess.run([str(ENGINE / ("verify_" + mode + ".sh"))], env=env, text=True, capture_output=True)

    def test_changed_agents_requires_evaluator_in_change_and_candidate(self):
        for mode in ("change", "candidate", "release"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                base = self.commit(root)
                (root / "AGENTS.md").write_text("Updated contract.\n")
                if mode != "change": self.commit(root)
                result = self.verify(root, base, mode)
                self.assertNotEqual(result.returncode, 0)
                log = root / ".artifacts" / mode / "logs/prompt_evals.log"
                self.assertTrue(log.exists(), result.stdout + result.stderr)
                self.assertIn("evals/run.sh", log.read_text())

    def test_existing_evaluator_runs_without_editing_eval_files(self):
        for mode in ("change", "candidate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                runner = root / "evals/run.sh"
                runner.parent.mkdir()
                runner.write_text("#!/bin/sh\nprintf executed > .artifacts/eval-invoked\n")
                runner.chmod(0o755)
                base = self.commit(root)
                (root / "AGENTS.md").write_text("Updated contract.\n")
                if mode == "candidate": self.commit(root)
                result = self.verify(root, base, mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((root / ".artifacts/eval-invoked").read_text(), "executed")

    def test_unrelated_change_skips_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            base = self.commit(root)
            (root / "README.md").write_text("Updated description.\n")
            self.commit(root)
            result = self.verify(root, base, "candidate")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads((root / ".artifacts/candidate/summary.json").read_text())
            self.assertEqual(next(g["status"] for g in summary["gates"] if g["name"] == "prompt_evals"), "skipped")

    def test_evaluator_failure_propagates_and_symlink_is_rejected(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                runner = root / "evals/run.sh"
                runner.parent.mkdir()
                if symlink:
                    runner.symlink_to("/bin/true")
                else:
                    runner.write_text("#!/bin/sh\nprintf stale-evidence >&2\nexit 19\n")
                    runner.chmod(0o755)
                base = self.commit(root)
                (root / "AGENTS.md").write_text("Updated contract.\n")
                self.commit(root)
                result = self.verify(root, base, "candidate")
                self.assertNotEqual(result.returncode, 0)
                log = root / ".artifacts/candidate/logs/prompt_evals.log"
                self.assertTrue(log.exists(), result.stdout + result.stderr)
                self.assertIn("symlink" if symlink else "stale-evidence", log.read_text())

    def test_skill_workflow_nested_contract_and_eval_only_select_gate(self):
        for relative in (".agents/skills/demo/SKILL.md", "docs/harness-workflows.md", "internal/risk/AGENTS.md", "evals/cases.json"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                base = self.commit(root)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text("changed\n")
                self.commit(root)
                result = self.verify(root, base, "candidate")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("evals/run.sh", (root / ".artifacts/candidate/logs/prompt_evals.log").read_text())

    def test_legacy_schema_keeps_existing_prompt_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory, version=4)
            base = self.commit(root)
            (root / "prompts").mkdir(); (root / "prompts/task.md").write_text("prompt\n")
            self.commit(root)
            self.assertNotEqual(self.verify(root, base, "candidate").returncode, 0)
            (root / "evals").mkdir(); (root / "evals/case.json").write_text("{}\n")
            self.commit(root)
            result = self.verify(root, base, "candidate")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_basename_suffix_requires_schema_five_and_cannot_traverse(self):
        for version, suffix in ((4, "/AGENTS.md"), (5, "/../AGENTS.md")):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory, version=version)
                path = root / "harness/harness_profiles.json"
                policy = json.loads(path.read_text())
                policy["conditional_gates"]["prompt_evals"]["path_suffixes"] = [suffix]
                path.write_text(json.dumps(policy))
                base = self.commit(root)
                result = self.verify(root, base, "candidate")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid suffixes", result.stderr)


if __name__ == "__main__":
    unittest.main()
