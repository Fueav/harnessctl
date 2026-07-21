#!/usr/bin/env python3
"""Validate the compact Harness manifest and one-file Specification registry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any


REQUIRED_WORKFLOWS = {
    "HARNESS-FOCUSED-CHANGE",
    "HARNESS-MAINTENANCE",
    "HARNESS-SPEC-FIRST-FEATURE",
    "HARNESS-VERIFICATION-INCIDENT",
}
WORKFLOW_FIELDS = {
    "id",
    "use_when",
    "artifact_policy",
    "verification",
    "stop_rule",
    "evidence",
}
SPEC_FIELDS = {"spec_id", "module", "status", "workflow_class"}
SPEC_ID_RE = re.compile(r"^SPEC-[A-Z0-9][A-Z0-9_-]*$")
MODULE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
STATUSES = {"draft", "approved", "implemented", "retired"}
FIXED_SIZE_BUDGETS = {
    "AGENTS.md": 120,
    "docs/harness-workflows.json": 200,
    "harness/harness_profiles.json": 200,
}
SKILL_SIZE_BUDGET = 60


def parse_args() -> argparse.Namespace:
    root = pathlib.Path(
        os.environ.get(
            "HARNESS_PROJECT_ROOT", pathlib.Path(__file__).resolve().parent.parent
        )
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=root)
    parser.add_argument("--compare-ref", default=os.environ.get("VERIFY_COMPARE_REF"))
    parser.add_argument("--artifact-dir", type=pathlib.Path)
    parser.add_argument("--snapshot-file", type=pathlib.Path)
    parser.add_argument("--snapshot-sha256")
    return parser.parse_args()


def load_json(path: pathlib.Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is not valid UTF-8 JSON: {exc}")
        return {}


def parse_workflow_manifest(manifest: Any, errors: list[str]) -> set[str]:
    if not isinstance(manifest, dict):
        errors.append("workflow manifest must be a JSON object")
        return set()

    version = manifest.get("version")
    classes = manifest.get("workflow_classes")
    if not isinstance(classes, list):
        errors.append("workflow manifest must define a workflow_classes array")
        return set()

    workflow_ids: set[str] = set()
    if version == 2:
        for index, item in enumerate(classes):
            if not isinstance(item, dict) or set(item) != WORKFLOW_FIELDS:
                errors.append(
                    f"workflow_classes[{index}] must use the legacy version 2 field set"
                )
                continue
            workflow_id = item.get("id")
            if not isinstance(workflow_id, str) or workflow_id in workflow_ids:
                errors.append(
                    f"workflow_classes[{index}] has an invalid or duplicate id"
                )
            else:
                workflow_ids.add(workflow_id)
            for field in WORKFLOW_FIELDS - {"id", "evidence"}:
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(
                        f"workflow_classes[{index}].{field} must be non-empty"
                    )
            if not isinstance(item.get("evidence"), list) or not item["evidence"]:
                errors.append(
                    f"workflow_classes[{index}].evidence must be non-empty"
                )
    elif version == 3:
        for index, workflow_id in enumerate(classes):
            if not isinstance(workflow_id, str) or not workflow_id.strip():
                errors.append(
                    f"workflow_classes[{index}] must be a non-empty workflow id"
                )
            elif workflow_id in workflow_ids:
                errors.append(
                    f"workflow_classes[{index}] has a duplicate workflow id"
                )
            else:
                workflow_ids.add(workflow_id)
    else:
        errors.append("workflow manifest version must be 2 or 3")

    if workflow_ids != REQUIRED_WORKFLOWS:
        errors.append("workflow manifest must define exactly the four core workflows")
    return workflow_ids


def parse_frontmatter(path: pathlib.Path, errors: list[str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path} is not readable UTF-8: {exc}")
        return {}
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        errors.append(f"{path} must start with YAML-style frontmatter")
        return {}
    end = lines[1:].index("---") + 1
    result: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("'\"")
    missing = sorted(SPEC_FIELDS - set(result))
    if missing:
        errors.append(f"{path} frontmatter missing fields: {', '.join(missing)}")
    return result


def git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def changed_paths(
    repo: pathlib.Path, compare_ref: str | None, snapshot: dict[str, Any]
) -> list[str]:
    if isinstance(snapshot.get("changes"), list):
        return sorted(
            item["path"]
            for item in snapshot["changes"]
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        )
    if not compare_ref:
        return []
    tracked = git(repo, "diff", "--name-only", compare_ref, "--").splitlines()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(tracked + untracked))


def is_harness_path(path: str) -> bool:
    return (
        path
        in {
            ".ai-boundaries.yml",
            "AGENTS.md",
            "BLUEPRINT.md",
            "MIGRATION.md",
            "README.md",
            "Makefile",
        }
        or path.startswith(".agents/skills/")
        or path.startswith(".claude/commands/")
        or path.startswith(".claude/hooks/")
        or path.startswith(".codex/")
        or path.startswith(".github/")
        or path.startswith("docs/ai-tools/")
        or path.startswith("docs/harness-")
        or path.startswith("harness/")
        or path == "docs/prd/_template.md"
        or path.startswith("specs/_template/")
        or path.startswith("scripts/")
    )


def size_budget_records(
    repo: pathlib.Path, errors: list[str]
) -> list[dict[str, int | str]]:
    budgets = dict(FIXED_SIZE_BUDGETS)
    skills_root = repo / ".agents" / "skills"
    if skills_root.is_dir():
        for path in skills_root.glob("**/SKILL.md"):
            budgets[path.relative_to(repo).as_posix()] = SKILL_SIZE_BUDGET

    records: list[dict[str, int | str]] = []
    for relative, limit in sorted(budgets.items()):
        try:
            actual = len((repo / relative).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"size-budget file {relative} is unreadable: {exc}")
            continue
        records.append({"actual": actual, "limit": limit, "path": relative})
        if actual > limit:
            errors.append(f"{relative} exceeds {limit} lines: {actual}")
    return records


def harness_line_delta(repo: pathlib.Path, compare_ref: str | None) -> tuple[int, int]:
    if not compare_ref:
        return 0, 0
    added = deleted = 0
    for line in git(repo, "diff", "--numstat", compare_ref, "--").splitlines():
        add, remove, path = line.split("\t", 2)
        if is_harness_path(path) and add.isdigit() and remove.isdigit():
            added += int(add)
            deleted += int(remove)
    for path in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        if is_harness_path(path):
            try:
                added += len((repo / path).read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeDecodeError):
                added += 1
    return added, deleted


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    artifact_dir = args.artifact_dir
    if artifact_dir is None:
        artifact_dir = pathlib.Path(
            os.environ.get("SPEC_REGISTRY_ARTIFACT_DIR", ".artifacts/change")
        )
    if not artifact_dir.is_absolute():
        artifact_dir = repo / artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    size_budgets = size_budget_records(repo, errors)

    snapshot: dict[str, Any] = {}
    if (args.snapshot_file is None) != (args.snapshot_sha256 is None):
        errors.append("--snapshot-file and --snapshot-sha256 must be provided together")
    elif args.snapshot_file is not None and args.snapshot_sha256 is not None:
        snapshot_path = args.snapshot_file
        if not snapshot_path.is_absolute():
            snapshot_path = repo / snapshot_path
        try:
            raw = snapshot_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != args.snapshot_sha256:
                errors.append("change snapshot digest mismatch")
            snapshot = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"change snapshot is invalid: {exc}")

    manifest = load_json(
        repo / "docs/harness-workflows.json", "workflow manifest", errors
    )
    workflow_ids = parse_workflow_manifest(manifest, errors)

    registry = load_json(repo / "specs/index.json", "spec registry", errors)
    registered = registry.get("specs") if isinstance(registry, dict) else None
    if registry.get("version") != 1 or not isinstance(registered, list):
        errors.append("spec registry must use version 1 and a specs array")
        registered = []
    registered_set = set(registered)
    specs_root = repo / "specs"
    discovered = {
        f"specs/{child.name}/spec.md"
        for child in specs_root.iterdir()
        if child.is_dir()
        and child.name != "_template"
        and (child / "spec.md").is_file()
    } if specs_root.is_dir() else set()
    if registered_set != discovered:
        errors.append("spec registry and filesystem differ")

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for relative in sorted(discovered):
        path = repo / relative
        metadata = parse_frontmatter(path, errors)
        module = path.parent.name
        spec_id = metadata.get("spec_id", "")
        workflow = metadata.get("workflow_class", "")
        status = metadata.get("status", "")
        if metadata.get("module") != module or not MODULE_RE.fullmatch(module):
            errors.append(f"{relative} module does not match its directory")
        if not SPEC_ID_RE.fullmatch(spec_id) or spec_id in seen_ids:
            errors.append(f"{relative} has an invalid or duplicate spec_id")
        seen_ids.add(spec_id)
        if workflow not in workflow_ids:
            errors.append(f"{relative} has unknown workflow_class {workflow!r}")
        if status not in STATUSES:
            errors.append(f"{relative} has invalid status {status!r}")
        records.append(
            {
                "module": module,
                "path": relative,
                "spec_id": spec_id,
                "status": status,
                "workflow_class": workflow,
            }
        )

    try:
        changes = changed_paths(repo, args.compare_ref, snapshot)
        changed_specs = {path for path in changes if path in discovered}
        added, deleted = harness_line_delta(repo, args.compare_ref)
        if added > deleted:
            errors.append(
                f"Harness line budget exceeded: +{added}/-{deleted}; refactor before adding"
            )
    except RuntimeError as exc:
        changes, changed_specs, added, deleted = [], set(), 0, 0
        errors.append(f"cannot inspect changes: {exc}")

    errors = sorted(set(errors))
    active_specs = [record for record in records if record["path"] in changed_specs]
    payload = {
        "active_specs": active_specs,
        "harness_line_budget": {"added": added, "deleted": deleted},
        "schema_version": 1,
        "size_budgets": size_budgets,
        "specs": records,
        "status": "failed" if errors else "passed",
        "violations": errors,
        "workflow_classes": sorted(workflow_ids),
    }
    (artifact_dir / "spec_registry.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for error in errors:
        print(f"spec_registry: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
