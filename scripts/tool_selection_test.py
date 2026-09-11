#!/usr/bin/env python3
"""Exercise selective installation against real Git changes and a fake Go installer."""
import json, os, subprocess, tempfile
from pathlib import Path

engine = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory); repo = root / 'repo'; repo.mkdir()
    for args in [('init', '-q'), ('config', 'user.email', 'test@example.com'), ('config', 'user.name', 'Test')]:
        subprocess.run(['git', *args], cwd=repo, check=True)
    (repo / 'README.md').write_text('baseline\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=repo, check=True)
    policy = json.loads((engine / 'harness_profiles.json').read_text()); policy['schema_version'] = 4
    for gates in policy['gate_sets'].values():
        if 'toolchain' in gates: gates.remove('toolchain')
    for profile in policy['profiles'].values():
        if 'toolchain' in profile['skippable_gates']: profile['skippable_gates'].remove('toolchain')
    for gate in ['golangci', 'govulncheck']:
        policy['conditional_gates'][gate] = {'always_profiles':['release'], 'path_prefixes':['go.mod'], 'path_suffixes':['.go'], 'skip_reason':'no Go inputs'}
    policy_path = root / 'policy.json'; policy_path.write_text(json.dumps(policy))
    env = dict(os.environ, HARNESS_PROJECT_ROOT=str(repo), HARNESS_PROFILE_CONFIG=str(policy_path), GOBIN=str(root/'bin'), HARNESS_TOOL_VERSIONS=str(engine/'tool_versions.env'))
    (repo / 'README.md').write_text('docs change\n')
    command = [str(engine/'install_tools.sh'), '--profile', 'pull_request', '--compare-ref', 'HEAD', '--dry-run']
    def plan():
        result = subprocess.run(command, env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert plan()['tools'] == ['gitleaks'], plan()
    (repo / 'new.go').write_text('package example\n')
    assert set(plan()['tools']) == {'gitleaks','golangci-lint','govulncheck'}, plan()
    fake = root / 'fake'; fake.mkdir()
    go = fake/'go'
    go.write_text("#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\nm=sys.argv[2].split('@')[0]\nname='gitleaks' if '/gitleaks/' in m else m.rsplit('/',1)[-1]\np=Path(os.environ['GOBIN'])/name\np.write_text('#!/bin/sh\\nexit 0\\n');p.chmod(0o755)\nwith open(os.environ['INSTALL_LOG'],'a') as f:f.write(name+'\\n')\n")
    go.chmod(0o755)
    env.update(PATH=str(fake)+os.pathsep+os.environ['PATH'], INSTALL_LOG=str(root/'installed'))
    (repo/'new.go').unlink()
    subprocess.run(command[:-1], env=env, capture_output=True, check=True)
    assert (root/'installed').read_text().splitlines() == ['gitleaks']
    (repo/'new.go').write_text('package example\n')
    subprocess.run(command[:-1], env=env, capture_output=True, check=True)
    assert (root/'installed').read_text().splitlines() == ['gitleaks','golangci-lint','govulncheck']
    command[2] = 'release'
    assert set(plan()['tools']) == {'gitleaks','golangci-lint','govulncheck','benchstat'}
    command[4] = 'missing-ref'
    assert subprocess.run(command, env=env, capture_output=True).returncode != 0
print('tool selection passed')
