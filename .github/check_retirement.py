"""Check retirement and preservation without executing the historical engine."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = "a9f383444612a1606cec53fd54f7e6a2df59c2d4"
RETIREMENT_DOCUMENTS = {"AGENTS.md", "README.md"}
REMOVED = {".github/workflows/ci.yml"}
ADDED = {".github/check_retirement.py", ".github/workflows/retirement.yml"}


def main():
    for name in (".github/workflows/ci.yml", ".agents/plugins", ".claude-plugin", ".codex-plugin"):
        if (ROOT / name).exists():
            raise SystemExit(f"Retired distribution entrypoint exists: {name}")
    workflows = {path.name for path in (ROOT / ".github/workflows").iterdir()}
    if workflows != {"retirement.yml"}:
        raise SystemExit(f"Unexpected active workflows: {sorted(workflows)}")
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", HISTORICAL], cwd=ROOT
    )
    entries = [entry.split(b"\t", 1) for entry in tree.rstrip(b"\0").split(b"\0")]
    expected_paths = {path.decode() for _, path in entries} - REMOVED | ADDED
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    current_paths = {path.decode() for path in tracked.rstrip(b"\0").split(b"\0")}
    if current_paths != expected_paths:
        raise SystemExit(
            f"Frozen tree changed: added={sorted(current_paths - expected_paths)}, "
            f"missing={sorted(expected_paths - current_paths)}"
        )
    preserved = 0
    for metadata, raw_path in entries:
        mode, kind, blob = metadata.split()
        name = raw_path.decode()
        if name in RETIREMENT_DOCUMENTS | REMOVED:
            continue
        path = ROOT / name
        expected = subprocess.check_output(["git", "cat-file", "blob", blob.decode()], cwd=ROOT)
        if kind != b"blob" or path.is_symlink() or path.read_bytes() != expected:
            raise SystemExit(f"Historical source changed: {name}")
        executable = bool(path.stat().st_mode & 0o111)
        if executable != (mode == b"100755"):
            raise SystemExit(f"Historical source mode changed: {name}")
        preserved += 1
    print(f"PASS: retired CI/distribution absent; {preserved} historical files preserved")


if __name__ == "__main__":
    main()
