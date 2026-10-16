"""One strict project execution contract; no v0 translation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from .resources import ResourceError, validate_config

NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,79}')
TOOLS = {
    'golangci-lint': 'github.com/golangci/golangci-lint/cmd/golangci-lint',
    'govulncheck': 'golang.org/x/vuln/cmd/govulncheck',
    'gitleaks': 'github.com/zricethezav/gitleaks/v8',
    'benchstat': 'golang.org/x/perf/cmd/benchstat',
}
LEGACY = ('harness/harness_profiles.json', 'harness/dependencies.json', 'harness/tool_versions.env',
          'scripts/harness_profiles.json', 'scripts/tool_versions.env', 'harness/scaffold.lock')


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResourceError('duplicate JSON field: ' + key)
        result[key] = value
    return result


def regular(path):
    path = Path(path)
    # The path and every ancestor must be real directories/files, not redirected.
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ResourceError('symlink input is not allowed: ' + str(path))
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024**2:
        raise ResourceError('expected bounded regular file: ' + str(path))
    return path.read_bytes()


def document(path):
    raw = regular(path)
    return json.loads(raw, object_pairs_hook=strict_object), hashlib.sha256(raw).hexdigest()


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ResourceError('invalid or unknown fields; expected ' + ', '.join(required))


def bounded(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ResourceError(f'{label} must be an integer from {low} to {high}')


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ResourceError('invalid operation, project or criterion name')


def text(value, label):
    if not isinstance(value, str) or not value.strip() or '\0' in value or len(value) > 8192:
        raise ResourceError('invalid ' + label)


def load(repo):
    for legacy in LEGACY:
        if (repo / legacy).exists() or (repo / legacy).is_symlink():
            raise ResourceError('legacy descriptor must be removed before v1: ' + legacy)
    value, digest = document(repo / 'harness/project.json')
    fields(value, ('schema_version', 'project', 'operations', 'profiles', 'verification', 'dependencies', 'tools'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ResourceError('unsupported project schema')
    name(value['project'])
    if not isinstance(value['operations'], dict) or not value['operations']:
        raise ResourceError('operations must be a nonempty object')
    for key, operation in value['operations'].items():
        name(key)
        fields(operation, ('argv', 'timeout_seconds'), ('description', 'env'))
        argv = operation['argv']
        if not isinstance(argv, list) or not argv or len(argv) > 256:
            raise ResourceError('operation argv must be a nonempty array')
        for arg in argv:
            text(arg, 'argv string')
        executable = Path(argv[0])
        if executable.is_absolute() or '..' in executable.parts:
            raise ResourceError('operation executable must be a PATH name or safe relative path')
        if '/' in argv[0]:
            regular(repo / executable)
        bounded(operation['timeout_seconds'], 1, 86400, 'operation timeout_seconds')
        if 'description' in operation:
            text(operation['description'], 'operation description')
        env = operation.get('env', {})
        if not isinstance(env, dict):
            raise ResourceError('operation env must be an object')
        for key, item in env.items():
            if not re.fullmatch('[A-Z][A-Z0-9_]*', key) or key.startswith(('HARNESS', 'AI_BOUNDARY', 'VERIFY_')) or key in ('PATH', 'HOME', 'PYTHONPATH', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES'):
                raise ResourceError('reserved or invalid operation environment name')
            if not isinstance(item, str) or '\0' in item or len(item) > 8192:
                raise ResourceError('operation env values must be strings')
    if not isinstance(value['profiles'], dict) or 'development' not in value['profiles']:
        raise ResourceError('profiles must include development')
    for key, profile in value['profiles'].items():
        name(key)
        fields(profile, ('resources', 'lease_seconds'))
        if not isinstance(profile['resources'], str) or profile['resources'] not in ('none', 'shared', 'fresh'):
            raise ResourceError('invalid profile resources')
        bounded(profile['lease_seconds'], 1, 86400, 'profile lease_seconds')
    fields(value['verification'], ('change', 'candidate', 'release'))
    for operations in value['verification'].values():
        if not isinstance(operations, list) or not operations or any(not isinstance(op, str) or op not in value['operations'] for op in operations) or len(set(operations)) != len(operations):
            raise ResourceError('verification must reference unique known operations')
    dependencies = validate_config(value['dependencies'])
    if not dependencies['enabled'] and any(profile['resources'] != 'none' for profile in value['profiles'].values()):
        raise ResourceError('resource profile requires enabled dependencies')
    if not isinstance(value['tools'], dict):
        raise ResourceError('tools must be an object')
    for tool, version in value['tools'].items():
        if tool not in TOOLS or not isinstance(version, str) or not re.fullmatch(r'v[0-9][A-Za-z0-9.+-]{1,100}', version):
            raise ResourceError('tool requires an installer-supported name and exact v version')
    return value, digest


def goal(repo, argument, project):
    if not argument:
        return None, None, None
    path = Path(argument)
    path = path if path.is_absolute() else repo / path
    value, digest = document(path)
    fields(value, ('schema_version', 'id', 'objective', 'criteria'), ('scope',))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ResourceError('unsupported goal schema')
    name(value['id'])
    text(value['objective'], 'goal objective')
    if 'scope' in value:
        text(value['scope'], 'goal scope')
    if not isinstance(value['criteria'], list) or not value['criteria']:
        raise ResourceError('goal criteria must be nonempty')
    seen = set()
    for criterion in value['criteria']:
        fields(criterion, ('id', 'description', 'operation'))
        name(criterion['id'])
        text(criterion['description'], 'criterion description')
        if criterion['id'] in seen or not isinstance(criterion['operation'], str) or criterion['operation'] not in project['operations']:
            raise ResourceError('duplicate criterion or unknown operation')
        seen.add(criterion['id'])
    return value, digest, str(path)


def git(repo, *arguments):
    result = subprocess.run(['git', '-C', str(repo), *arguments], capture_output=True)
    if result.returncode:
        raise ResourceError('Git identity unavailable: ' + ' '.join(arguments[:2]))
    return result.stdout


def candidate(repo):
    if Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve() != repo:
        raise ResourceError('--repo must name the Git worktree root')
    head = git(repo, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    digest = hashlib.sha256()
    # Include contents, type, mode, path, staged delta and deletions, not only status.
    for raw in sorted(set(git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''}):
        relative = os.fsdecode(raw)
        path = repo / relative
        digest.update(raw + b'\0')
        try:
            info = path.lstat()
            digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b'\0')
            if stat.S_ISLNK(info.st_mode):
                digest.update(b'link:' + os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(info.st_mode):
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
            elif stat.S_ISDIR(info.st_mode):
                raise ResourceError('submodules require an explicit native verification adapter')
            else:
                raise ResourceError('unsupported candidate file type')
        except FileNotFoundError:
            digest.update(b'deleted')
        digest.update(b'\0')
    digest.update(git(repo, 'diff', '--cached', '--binary', 'HEAD', '--'))
    return {'head': head, 'working_state_sha256': digest.hexdigest(), 'clean': not bool(git(repo, 'status', '--porcelain', '--untracked-files=all'))}


def availability(repo, project):
    operations = {}
    for key, operation in project['operations'].items():
        executable = operation['argv'][0]
        path = repo / executable if '/' in executable else shutil.which(executable, path=str(repo / '.tools/bin') + os.pathsep + os.environ.get('PATH', ''))
        operations[key] = {'executable_available': bool(path and os.access(path, os.X_OK)), 'readiness': 'not_probed', **operation}
    docker = shutil.which('docker')
    if any(profile['resources'] != 'none' for profile in project['profiles'].values()):
        try:
            ready = bool(docker) and subprocess.run(['docker', 'info', '--format', '{{.ID}}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
        except subprocess.TimeoutExpired:
            ready = False
    else:
        ready = None
    return {'operations': operations, 'requirements': {'tools': project['tools'], 'docker_daemon_available': ready}}
