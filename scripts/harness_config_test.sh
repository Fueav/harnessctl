#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

PYTHONDONTWRITEBYTECODE=1 python3 -I -B -S - "$ROOT_DIR" <<'PY'
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1])
tool = root / "scripts/lib/harness_config.py"
source = json.loads((root / "scripts/harness_profiles.json").read_text(encoding="utf-8"))


def as_v1():
    policy = copy.deepcopy(source)
    policy["schema_version"] = 1
    policy["profiles"]["nightly"] = copy.deepcopy(policy["profiles"]["release"])
    for rule in policy["conditional_gates"].values():
        if "nightly" not in rule["always_profiles"]:
            rule["always_profiles"].insert(0, "nightly")
    policy.pop("custom_gates", None)
    policy.pop("symlinks", None)
    return policy


def as_v2():
    policy = as_v1()
    policy["schema_version"] = 2
    policy["custom_gates"] = {
        "project_check": {"run": "scripts/gates/project_check.sh"},
    }
    policy["symlinks"] = [
        {"link": "CLAUDE.md", "target": "AGENTS.md"},
        {"link": ".claude/skills", "target": ".agents/skills"},
    ]
    policy["gate_sets"]["change"].append("project_check")
    policy["gate_sets"]["release"].insert(-1, "project_check")
    policy["gate_artifacts"]["project_check"] = {
        "artifacts": ["custom/project-check.json"],
    }
    return policy


def as_v3():
    policy = as_v2()
    policy["schema_version"] = 3
    policy["profiles"].pop("nightly")
    for rule in policy["conditional_gates"].values():
        rule["always_profiles"] = [
            profile for profile in rule["always_profiles"] if profile != "nightly"
        ]
    policy["conditional_gates"]["project_check"] = {
        "always_profiles": [],
        "path_prefixes": ["docs/"],
        "skip_reason": "project check is unrelated to this change",
    }
    return policy


def run(policy, *arguments):
    with tempfile.TemporaryDirectory() as temporary:
        config = pathlib.Path(temporary) / "profiles.json"
        config.write_text(json.dumps(policy), encoding="utf-8")
        env = os.environ.copy()
        env["HARNESS_PROFILE_CONFIG"] = os.fspath(config)
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", os.fspath(tool), *arguments],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def accepted(policy, *arguments):
    result = run(policy, *arguments)
    assert result.returncode == 0, (arguments, result.stdout, result.stderr)
    return result.stdout


def rejected_policy(label, policy, mutate):
    mutate(policy)
    result = run(policy, "validate", "--profile", "change", "--mode", "change")
    assert result.returncode == 2, (label, result.returncode, result.stdout, result.stderr)
    assert "harness config:" in result.stderr, (label, result.stderr)
    assert "Traceback" not in result.stderr, (label, result.stderr)


def rejected(label, mutate):
    rejected_policy(label, as_v2(), mutate)


def rejected_v3(label, mutate):
    rejected_policy(label, as_v3(), mutate)


v1 = as_v1()
accepted(v1, "validate", "--profile", "change", "--mode", "change")
assert accepted(v1, "custom-gates", "--profile", "change") == ""
assert accepted(v1, "profile-gates", "--profile", "change").splitlines() == (
    v1["gate_sets"]["change"]
)
legacy_links = accepted(v1, "symlinks").splitlines()
assert legacy_links == [
    "CLAUDE.md\tAGENTS.md",
    ".claude/skills\t.agents/skills",
    "internal/risk/CLAUDE.md\tinternal/risk/AGENTS.md",
    "internal/ledger/CLAUDE.md\tinternal/ledger/AGENTS.md",
], legacy_links

v2 = as_v2()
accepted(v2, "validate", "--profile", "change", "--mode", "change")
assert accepted(v2, "custom-gates", "--profile", "change") == (
    "project_check\tscripts/gates/project_check.sh\n"
)
assert accepted(v2, "profile-gates", "--profile", "change").splitlines() == (
    v2["gate_sets"]["change"]
)
assert accepted(v2, "gate-artifacts", "--gate", "project_check") == (
    "custom/project-check.json\n"
)
assert accepted(v2, "gate-artifacts", "--gate", "build") == ""
assert accepted(v2, "symlinks") == (
    "CLAUDE.md\tAGENTS.md\n.claude/skills\t.agents/skills\n"
)
assert "spec_registry.json" in accepted(v2, "artifacts").splitlines()

v3 = as_v3()
accepted(v3, "validate", "--profile", "change", "--mode", "change")
assert accepted(v3, "conditional-gates", "--profile", "change").splitlines() == [
    "project_check"
]

trimmed = as_v2()
trimmed["gate_sets"]["change"].remove("toolchain")
trimmed["gate_sets"]["change"].remove("spec_registry")
trimmed["profiles"]["change"]["skippable_gates"].remove("toolchain")
accepted(trimmed, "validate", "--profile", "change", "--mode", "change")
assert accepted(trimmed, "profile-gates", "--profile", "change").splitlines() == (
    trimmed["gate_sets"]["change"]
)

rejected("builtin collision", lambda p: (
    p["custom_gates"].__setitem__("build", {"run": "scripts/gates/build.sh"}),
    p["gate_sets"]["change"].append("build"),
))
rejected("unknown gate-set member", lambda p: p["gate_sets"]["change"].append("unknown_gate"))
rejected("dead custom gate", lambda p: (
    p["gate_sets"]["change"].remove("project_check"),
    p["gate_sets"]["release"].remove("project_check"),
))
rejected("custom gate is skippable", lambda p: p["profiles"]["change"]["skippable_gates"].append("project_check"))
rejected("custom conditional gate", lambda p: p["conditional_gates"].__setitem__("project_check", {}))
rejected("absolute run", lambda p: p["custom_gates"]["project_check"].__setitem__("run", "/tmp/check.sh"))
rejected("parent run", lambda p: p["custom_gates"]["project_check"].__setitem__("run", "scripts/../check.sh"))
rejected("run outside allowed roots", lambda p: p["custom_gates"]["project_check"].__setitem__("run", "tools/check.sh"))
rejected("custom segment is not a tail", lambda p: (
    p["gate_sets"]["change"].remove("project_check"),
    p["gate_sets"]["change"].insert(-1, "project_check"),
))
rejected("custom after release context", lambda p: (
    p["gate_sets"]["release"].remove("project_check"),
    p["gate_sets"]["release"].append("project_check"),
))
rejected_v3("conditional unknown gate", lambda p: p["conditional_gates"].__setitem__(
    "unknown_gate", {
        "always_profiles": [], "path_prefixes": ["docs/"], "skip_reason": "not selected"
    },
))
rejected_v3("conditional custom gate references unrelated profile", lambda p: (
    p["gate_sets"]["release"].remove("project_check"),
    p["conditional_gates"]["project_check"]["always_profiles"].append("release"),
))
rejected_v3("invalid benchmark request flag", lambda p: p["conditional_gates"][
    "benchmarks"
].__setitem__("explicit_request", "yes"))
rejected("missing v2 key", lambda p: p.pop("symlinks"))
rejected("extra v2 key", lambda p: p.__setitem__("extra", True))
rejected("invalid custom name", lambda p: (
    p["custom_gates"].__setitem__("X", p["custom_gates"].pop("project_check")),
    p["gate_sets"]["change"].__setitem__(-1, "X"),
    p["gate_sets"]["release"].__setitem__(-2, "X"),
))
rejected("open custom object", lambda p: p["custom_gates"]["project_check"].__setitem__("extra", True))
rejected("invalid symlink path", lambda p: p["symlinks"][0].__setitem__("target", "../AGENTS.md"))
rejected("unhashable gate-set member", lambda p: p["gate_sets"]["change"].append({"invalid": True}))
rejected("unhashable skippable gate", lambda p: p["profiles"]["change"]["skippable_gates"].append({"invalid": True}))
rejected("unhashable artifact path", lambda p: p["gate_artifacts"]["project_check"]["artifacts"].append({"invalid": True}))
rejected("unhashable profile gate set", lambda p: p["profiles"]["change"].__setitem__("gate_set", {"invalid": True}))
rejected("unhashable evidence set reference", lambda p: p["evidence"].__setitem__("change", {"invalid": True}))
rejected("missing mandatory change scope", lambda p: p["gate_sets"]["change"].remove("change_scope"))
rejected("missing mandatory AI boundaries", lambda p: p["gate_sets"]["change"].remove("ai_boundaries"))
rejected("missing candidate release context before", lambda p: p["gate_sets"]["release"].remove("release_context_before"))
rejected("missing release context after", lambda p: p["gate_sets"]["release"].remove("release_context_after"))
rejected("builtin order is not a subsequence", lambda p: (
    p["gate_sets"]["change"].remove("vet"),
    p["gate_sets"]["change"].insert(p["gate_sets"]["change"].index("gofmt"), "vet"),
))
rejected("coverage threshold without unit coverage", lambda p: p["gate_sets"]["release"].remove("test_unit_coverage"))
for delimiter_name, delimiter in (("tab", "\t"), ("carriage return", "\r"), ("newline", "\n")):
    rejected(
        f"{delimiter_name} in custom command",
        lambda p, delimiter=delimiter: p["custom_gates"]["project_check"].__setitem__(
            "run", f"scripts/gates/a{delimiter}b.sh"
        ),
    )
    rejected(
        f"{delimiter_name} in symlink path",
        lambda p, delimiter=delimiter: p["symlinks"][0].__setitem__(
            "target", f"docs/a{delimiter}b.md"
        ),
    )
    rejected(
        f"{delimiter_name} in artifact path",
        lambda p, delimiter=delimiter: p["gate_artifacts"]["project_check"]["artifacts"].__setitem__(
            0, f"custom/a{delimiter}b.json"
        ),
    )

result = run(v2, "gate-artifacts", "--gate", "unknown_gate")
assert result.returncode == 2, (result.stdout, result.stderr)

print("harness config tests passed")
PY
