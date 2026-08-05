#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FINALIZER="$ROOT_DIR/scripts/finalize_approval.py"
source "$ROOT_DIR/scripts/lib/safe_cleanup.sh"
TMP_DIR="$(mktemp -d)"
TMP_NAME="${TMP_DIR##*/}"
trap 'safe_remove_tree "$TMP_DIR" "$(dirname "$TMP_DIR")" "$TMP_NAME"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

setup_fixture() {
  local name="$1"
  local omit_spec_registry="${2:-0}"
  local use_glob="${3:-0}"
  local omit_coverage="${4:-0}"
  local omit_conditionals="${5:-0}"
  local changed_path="data.txt"
  REPO="$TMP_DIR/$name"
  CANDIDATE_DIR="$REPO/.artifacts/candidate"
  OUTPUT="$REPO/.artifacts/approval/finalization.json"
  git init -q -b main "$REPO"
  git -C "$REPO" config user.email approval-test@example.com
  git -C "$REPO" config user.name "Approval Test"
  mkdir -p "$REPO/scripts/lib"
  cp "$ROOT_DIR/scripts/finalize_approval.py" "$REPO/scripts/finalize_approval.py"
  cp "$ROOT_DIR/scripts/check_ai_boundaries.py" "$REPO/scripts/check_ai_boundaries.py"
  cp "$ROOT_DIR/scripts/collect_changes.py" "$REPO/scripts/collect_changes.py"
  cp "$ROOT_DIR/scripts/lib/change_scope.py" "$REPO/scripts/lib/change_scope.py"
  cp "$ROOT_DIR/scripts/lib/evidence.py" "$REPO/scripts/lib/evidence.py"
  cp "$ROOT_DIR/scripts/lib/harness_config.py" "$REPO/scripts/lib/harness_config.py"
  cp "$ROOT_DIR/scripts/harness_profiles.json" "$REPO/scripts/harness_profiles.json"
  chmod +x "$REPO/scripts/finalize_approval.py"
  FINALIZER="$REPO/scripts/finalize_approval.py"
  printf '.artifacts/\n' >"$REPO/.gitignore"
  if [[ "$use_glob" == "1" ]]; then
    changed_path="nested/service.key"
    printf 'allowed:\n  - docs/\napproval_required:\n  - "*.key"\nforbidden:\n  - "*.pem"\n' \
      >"$REPO/.ai-boundaries.yml"
  elif [[ "$omit_conditionals" == "1" ]]; then
    changed_path="internal/risk/change.go"
    printf 'allowed:\n  - docs/\napproval_required:\n  - internal/risk/\nforbidden:\n  - secrets/\n' \
      >"$REPO/.ai-boundaries.yml"
  else
    printf 'allowed:\n  - docs/\napproval_required:\n  - data.txt\nforbidden:\n  - secrets/\n' \
      >"$REPO/.ai-boundaries.yml"
  fi
  python3 - "$REPO/scripts/harness_profiles.json" \
    "$omit_spec_registry" "$omit_coverage" "$omit_conditionals" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))
omit_spec_registry, omit_coverage, omit_conditionals = sys.argv[2:]
if omit_spec_registry == "1":
    policy["gate_sets"]["release"].remove("spec_registry")
    policy["evidence_sets"]["release"]["artifacts"].remove("spec_registry.json")
    policy["machine_status_artifacts"].remove("spec_registry.json")
if omit_coverage == "1":
    for gate in ("test_unit_coverage", "coverage_threshold"):
        policy["gate_sets"]["release"].remove(gate)
    for artifact in ("coverage.out", "coverage_percent.txt"):
        policy["evidence_sets"]["release"]["artifacts"].remove(artifact)
if omit_conditionals == "1":
    for gate in ("test_race", "benchmarks"):
        policy["gate_sets"]["release"].remove(gate)
        policy["profiles"]["pull_request"]["skippable_gates"].remove(gate)
        policy["conditional_gates"][gate]["always_profiles"].remove("release")
path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
  printf 'base\n' >"$REPO/data.txt"
  git -C "$REPO" add -A
  git -C "$REPO" commit -q -m base
  BASE="$(git -C "$REPO" rev-parse HEAD)"
  mkdir -p "$REPO/$(dirname "$changed_path")"
  printf 'candidate\n' >>"$REPO/$changed_path"
  git -C "$REPO" add "$changed_path"
  git -C "$REPO" commit -q -m candidate
  HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
  HEAD_TREE="$(git -C "$REPO" rev-parse 'HEAD^{tree}')"
  mkdir -p "$CANDIDATE_DIR"
  python3 - "$CANDIDATE_DIR" "$HEAD_SHA" "$HEAD_TREE" "$BASE" \
    "$changed_path" "$omit_spec_registry" "$omit_coverage" "$omit_conditionals" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
head, tree, base, changed_path, omit_spec_registry, omit_coverage, omit_conditionals = sys.argv[2:]
required_gates = [
    "change_scope",
    "release_context_before",
    "toolchain",
    "symlinks",
    "gofmt",
    "build",
    "vet",
    "golangci",
    "test_unit_coverage",
    "govulncheck",
    "gitleaks",
    "ai_boundaries",
    "coverage_threshold",
    "test_race",
    "migration_safety",
    "prompt_evals",
    "spec_registry",
    "benchmarks",
    "release_context_after",
]
if omit_spec_registry == "1":
    required_gates.remove("spec_registry")
if omit_coverage == "1":
    required_gates.remove("test_unit_coverage")
    required_gates.remove("coverage_threshold")
if omit_conditionals == "1":
    required_gates.remove("test_race")
    required_gates.remove("benchmarks")
documents = {
    "ai_boundaries.json": {
        "approval_mode": "deferred",
        "approval_satisfied": False,
        "approved": False,
        "classifications": {
            "allowed": [],
            "approval_required": [changed_path],
            "forbidden": [],
            "unclassified": [],
        },
        "schema_version": 1,
        "snapshot": {
            "base_sha": base,
            "clean": True,
            "head_sha": head,
            "head_tree_sha": tree,
            "merge_base_sha": base,
        },
        "status": "passed",
    },
    "change_scope.json": {
        "base_sha": base,
        "changes": [{"path": changed_path, "sources": ["committed"]}],
        "clean": True,
        "head_sha": head,
        "head_tree_sha": tree,
        "merge_base_sha": base,
        "mode": "release",
    },
}
if omit_spec_registry != "1":
    documents["spec_registry.json"] = {
        "active_specs": [],
        "schema_version": 1,
        "specs": [],
        "status": "passed",
    }
for name, payload in documents.items():
    (root / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
if omit_coverage != "1":
    (root / "coverage.out").write_text("mode: atomic\n", encoding="utf-8")
    (root / "coverage_percent.txt").write_text("100.0\n", encoding="ascii")

gates = []
ledger_lines = []
for name in required_gates:
    status = "skipped" if name in {"test_race", "benchmarks"} else "passed"
    log = root / "logs" / f"{name}.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(
        "profile policy skip\n" if status == "skipped" else "passed\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(log.read_bytes()).hexdigest()
    relative = f"logs/{name}.log"
    gates.append(
        {
            "duration_seconds": 0,
            "log_path": relative,
            "log_sha256": digest,
            "name": name,
            "status": status,
        }
    )
    ledger_lines.append(f"{name}\t{status}\t0\t{relative}\t{digest}\n")
(root / "gates.tsv").write_text("".join(ledger_lines), encoding="utf-8")

artifacts = []
artifact_names = list(documents) + ["gates.tsv"] + [gate["log_path"] for gate in gates]
if omit_coverage != "1":
    artifact_names.extend(("coverage.out", "coverage_percent.txt"))
for name in sorted(artifact_names):
    path = root / name
    artifacts.append(
        {
            "path": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    )
manifest = {"artifacts": artifacts, "schema_version": 1}
manifest_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
(root / "artifact_manifest.json").write_bytes(manifest_raw)
summary = {
    "artifact_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
    "artifacts": artifacts,
    "change_snapshot_sha256": next(
        item["sha256"] for item in artifacts if item["path"] == "change_scope.json"
    ),
    "coverage": (
        {"percentage": None, "threshold": None}
        if omit_coverage == "1"
        else {"percentage": 100.0, "threshold": 70.0}
    ),
    "gate_ledger_sha256": next(
        item["sha256"] for item in artifacts if item["path"] == "gates.tsv"
    ),
    "gates": gates,
    "git": {
        "compare_sha": base,
        "head_sha": head,
        "head_tree_sha": tree,
        "merge_base_sha": base,
    },
    "mode": "candidate",
    "overall": "passed",
    "profile": "pull_request",
    "release_ready": False,
    "schema_version": 1,
    "sealed_artifacts": [
        {"path": item["path"], "sha256": item["sha256"]}
        for item in artifacts
        if item["path"] in {
            "ai_boundaries.json",
            "change_scope.json",
            "coverage.out",
            "coverage_percent.txt",
            "spec_registry.json",
        }
    ],
}
(root / "summary.json").write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
  git -C "$REPO" checkout -q --detach "$BASE"
}

reseal_fixture() {
  python3 - "$CANDIDATE_DIR" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
summary_path = root / "summary.json"
manifest_path = root / "artifact_manifest.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
paths = [item["path"] for item in manifest["artifacts"]]
artifacts = []
for relative in sorted(paths):
    path = root / relative
    artifacts.append(
        {
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    )
by_path = {item["path"]: item["sha256"] for item in artifacts}
manifest = {"artifacts": artifacts, "schema_version": 1}
manifest_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
manifest_path.write_bytes(manifest_raw)
summary["artifact_manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
summary["artifacts"] = artifacts
summary["change_snapshot_sha256"] = by_path["change_scope.json"]
for seal in summary["sealed_artifacts"]:
    seal["sha256"] = by_path[seal["path"]]
summary_path.write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
}

run_finalizer() {
  local decision="$1"
  local expected_head="${2:-$HEAD_SHA}"
  python3 "$FINALIZER" \
    --repo "$REPO" \
    --candidate-dir .artifacts/candidate \
    --expected-head-sha "$expected_head" \
    --expected-compare-sha "$BASE" \
    --review-decision "$decision" \
    --output .artifacts/approval/finalization.json
}

setup_fixture approved
run_finalizer APPROVED || fail "exact approved candidate evidence failed"
python3 - "$OUTPUT" "$HEAD_SHA" "$BASE" <<'PY'
import json
import pathlib
import re
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["status"] == "passed", payload
assert payload["approval_satisfied"] is True, payload
assert payload["candidate"]["head_sha"] == sys.argv[2], payload
assert payload["candidate"]["compare_sha"] == sys.argv[3], payload
assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["finished_at"]), payload
assert len(payload["candidate"]["head_tree_sha"]) == 40, payload
assert len(payload["candidate"]["merge_base_sha"]) == 40, payload
PY

setup_fixture glob-without-spec-registry 1 1
run_finalizer APPROVED || \
  fail "glob-classified candidate without spec_registry evidence failed"
python3 - "$CANDIDATE_DIR" "$OUTPUT" <<'PY'
import json
import pathlib
import sys

candidate = pathlib.Path(sys.argv[1])
boundary = json.loads((candidate / "ai_boundaries.json").read_text(encoding="utf-8"))
result = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
assert boundary["classifications"]["approval_required"] == [
    "nested/service.key"
], boundary
assert not (candidate / "spec_registry.json").exists(), boundary
assert result["status"] == "passed", result
assert result["errors"] == [], result
PY

setup_fixture candidate-without-coverage 0 0 1
run_finalizer APPROVED || fail "candidate without coverage gates failed finalization"
python3 - "$CANDIDATE_DIR/summary.json" <<'PY'
import json
import pathlib
import sys

summary = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert summary["coverage"] == {"percentage": None, "threshold": None}, summary
assert not {"test_unit_coverage", "coverage_threshold"} & {
    gate["name"] for gate in summary["gates"]
}, summary
PY

setup_fixture candidate-without-conditionals 0 0 0 1
run_finalizer APPROVED || \
  fail "candidate without triggered conditional gates failed finalization"

setup_fixture wrong-trusted-checkout
git -C "$REPO" checkout -q --detach "$HEAD_SHA"
if run_finalizer APPROVED; then
  fail "finalizer running from the candidate checkout passed"
fi

setup_fixture modified-trusted-runtime
printf '\n# local modification\n' >>"$REPO/scripts/check_ai_boundaries.py"
if run_finalizer APPROVED; then
  fail "modified trusted boundary runtime passed finalization"
fi

setup_fixture dismissed
if run_finalizer REVIEW_REQUIRED; then
  fail "non-approved review decision passed finalization"
fi

setup_fixture stale-head
if run_finalizer APPROVED 0000000000000000000000000000000000000000; then
  fail "stale candidate head passed finalization"
fi

setup_fixture stale-base
BASE=0000000000000000000000000000000000000000
if run_finalizer APPROVED; then
  fail "stale candidate base passed finalization"
fi

setup_fixture failed-summary
python3 - "$CANDIDATE_DIR/summary.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["overall"] = "failed"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
if run_finalizer APPROVED; then
  fail "failed candidate summary passed finalization"
fi

setup_fixture release-ready-candidate
python3 - "$CANDIDATE_DIR/summary.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["release_ready"] = True
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
if run_finalizer APPROVED; then
  fail "release-ready candidate summary passed finalization"
fi

setup_fixture missing-gate
python3 - "$CANDIDATE_DIR/summary.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["gates"] = payload["gates"][:-1]
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
if run_finalizer APPROVED; then
  fail "candidate evidence with a missing gate passed finalization"
fi

setup_fixture omitted-real-change
python3 - "$CANDIDATE_DIR" <<'PY'
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
scope_path = root / "change_scope.json"
scope = json.loads(scope_path.read_text(encoding="utf-8"))
scope["changes"] = []
scope_path.write_text(json.dumps(scope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
boundary_path = root / "ai_boundaries.json"
boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
boundary["classifications"]["approval_required"] = []
boundary_path.write_text(json.dumps(boundary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
reseal_fixture
if run_finalizer APPROVED; then
  fail "internally consistent evidence that omitted a real change passed finalization"
fi

setup_fixture misclassified-real-change
python3 - "$CANDIDATE_DIR/ai_boundaries.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["approval_satisfied"] = True
payload["classifications"]["allowed"] = ["data.txt"]
payload["classifications"]["approval_required"] = []
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
reseal_fixture
if run_finalizer APPROVED; then
  fail "internally consistent evidence with a false boundary classification passed"
fi

setup_fixture tampered-manifest
printf '\n' >>"$CANDIDATE_DIR/artifact_manifest.json"
if run_finalizer APPROVED; then
  fail "tampered candidate manifest passed finalization"
fi

setup_fixture tampered-artifact
printf '\n' >>"$CANDIDATE_DIR/ai_boundaries.json"
if run_finalizer APPROVED; then
  fail "tampered candidate artifact passed finalization"
fi

setup_fixture tampered-seal
python3 - "$CANDIDATE_DIR/summary.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["sealed_artifacts"][0]["sha256"] = "0" * 64
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
if run_finalizer APPROVED; then
  fail "tampered candidate seal passed finalization"
fi

setup_fixture missing-artifact
rm "$CANDIDATE_DIR/change_scope.json"
if run_finalizer APPROVED; then
  fail "missing candidate artifact passed finalization"
fi

setup_fixture symlink-artifact
mv "$CANDIDATE_DIR/ai_boundaries.json" "$TMP_DIR/outside-ai-boundaries.json"
ln -s "$TMP_DIR/outside-ai-boundaries.json" "$CANDIDATE_DIR/ai_boundaries.json"
if run_finalizer APPROVED; then
  fail "symlinked candidate artifact passed finalization"
fi

setup_fixture symlink-summary
mv "$CANDIDATE_DIR/summary.json" "$TMP_DIR/outside-summary.json"
ln -s "$TMP_DIR/outside-summary.json" "$CANDIDATE_DIR/summary.json"
if run_finalizer APPROVED; then
  fail "symlinked candidate summary passed finalization"
fi

printf 'finalize approval tests passed\n'
