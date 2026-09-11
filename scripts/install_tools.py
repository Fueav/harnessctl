#!/usr/bin/env python3
"""Install pinned tools selected by the same change and gate policy as verification."""
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.evidence import atomic_json

TOOLS = {
    'golangci-lint': ('GOLANGCI_LINT_VERSION', 'github.com/golangci/golangci-lint/cmd/golangci-lint'),
    'govulncheck': ('GOVULNCHECK_VERSION', 'golang.org/x/vuln/cmd/govulncheck'),
    'gitleaks': ('GITLEAKS_VERSION', 'github.com/zricethezav/gitleaks/v8'),
    'benchstat': ('BENCHSTAT_VERSION', 'golang.org/x/perf/cmd/benchstat'),
}
GATE_TOOLS = {'golangci': ['golangci-lint'], 'govulncheck': ['govulncheck'], 'gitleaks': ['gitleaks'], 'benchmarks': ['benchstat'], 'toolchain': list(TOOLS)}

def selection(args):
    if args.profile is None: return list(TOOLS), []
    from lib import harness_config as config
    from lib.change_scope import snapshot
    if not args.compare_ref: raise ValueError('--compare-ref is required with --profile')
    state = snapshot(args.repo, args.compare_ref)
    changes = [{'path': item.path} for item in state['changes']]
    gates = [gate for gate in config.profile_gates(args.profile) if config.gate_decision(args.profile, gate, changes, args.repo, state['base_sha'], state['head_sha'])[0]]
    needed = set()
    for gate in gates:
        needed.update(GATE_TOOLS.get(gate, []))
        # Project-owned commands may call any pinned tool, including nested verifiers.
        if gate in config.POLICY['custom_gates']: needed.update(TOOLS)
    return [name for name in TOOLS if name in needed], gates

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['change','pull_request','release'])
    parser.add_argument('--compare-ref', default=os.environ.get('VERIFY_COMPARE_REF'))
    parser.add_argument('--repo', type=Path, default=Path(os.environ['HARNESS_PROJECT_ROOT']))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    tools, gates = selection(args)
    if args.dry_run:
        print(json.dumps({'tools':tools, 'gates':gates})); return
    versions = Path(os.environ['HARNESS_TOOL_VERSIONS'])
    gobin = Path(os.environ.get('GOBIN', args.repo/'.tools/bin'))
    if gobin.is_symlink(): raise ValueError('GOBIN must not be a symlink')
    gobin.mkdir(parents=True, exist_ok=True)
    lock = gobin/'.harness-tools-lock.json'
    if lock.is_symlink(): raise ValueError('tool lock must not be a symlink')
    manifest = hashlib.sha256(versions.read_bytes()).hexdigest()
    cached = {}
    try:
        data = json.loads(lock.read_text())
        if data.get('schema_version') == 1 and data.get('manifest_sha256') == manifest: cached = data.get('tools', {})
        if not isinstance(cached, dict): cached = {}
    except (OSError, ValueError): pass
    env = dict(os.environ, GOBIN=str(gobin.resolve()))
    for name in tools:
        path = gobin/name
        if path.is_symlink(): raise ValueError('tool must not be a symlink: ' + name)
        if path.is_file() and os.access(path, os.X_OK) and cached.get(name) == hashlib.sha256(path.read_bytes()).hexdigest(): continue
        version_key, module = TOOLS[name]
        subprocess.run(['go','install',module+'@'+os.environ[version_key]], env=env, check=True)
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK): raise ValueError('installed tool is invalid: '+name)
        cached[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        atomic_json(lock, {'schema_version':1, 'manifest_sha256':manifest, 'tools':cached})
    print('pinned harness tools ready: ' + (', '.join(tools) or 'none'))

if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('install-tools: '+str(error), file=sys.stderr); raise SystemExit(2)
