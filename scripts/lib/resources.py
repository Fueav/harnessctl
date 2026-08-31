"""Owned, bounded test dependencies shared across Git worktrees (no daemon)."""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid


class ResourceError(RuntimeError):
    pass


class CapacityWait(ResourceError):
    pass


class BudgetExceeded(ResourceError):
    pass


def default_config():
    return {
        'schema_version': 1,
        'enabled': True,
        'isolation': 'database-per-run-v1',
        'postgres': {'image': 'postgres:16-alpine', 'data_path': '/var/lib/postgresql/data',
                     'memory_mb': 1024, 'cpus': 2, 'max_connections': 120,
                     'wal_mb': 256, 'checkpoint_seconds': 60},
        'redis': {'image': 'redis:7-alpine', 'memory_mb': 256, 'cpus': 1},
        'limits': {'max_runs': 4, 'max_environments': 2, 'max_idle_environments': 1,
                   'idle_seconds': 3600, 'max_storage_bytes': 8 * 1024**3,
                   'min_free_bytes': 5 * 1024**3, 'wait_seconds': 60,
                   'max_run_seconds': 3600, 'sample_seconds': 10,
                   'max_retained_runs': 1, 'max_retain_seconds': 3600},
    }


def validate_config(value):
    defaults = default_config()
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ResourceError('unknown resource configuration fields')
    result = copy.deepcopy(defaults)
    for key, item in value.items():
        if isinstance(defaults[key], dict):
            if not isinstance(item, dict) or set(item) - set(defaults[key]):
                raise ResourceError('unknown resource configuration fields in ' + key)
            result[key].update(item)
        else:
            result[key] = item
    if type(result['schema_version']) is not int or result['schema_version'] != 1 or result['isolation'] != 'database-per-run-v1':
        raise ResourceError('unsupported resource isolation contract; use an adapted consumer')
    if type(result['enabled']) is not bool:
        raise ResourceError('resource enabled must be a boolean')
    for service in ('postgres', 'redis'):
        image = result[service]['image']
        if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]{0,240}', image):
            raise ResourceError('invalid dependency image')
        for key in ('memory_mb', 'cpus'):
            if type(result[service][key]) is not int or result[service][key] <= 0:
                raise ResourceError('invalid resource limit: ' + key)
    if result['postgres']['data_path'] not in ('/var/lib/postgresql/data', '/var/lib/postgresql'):
        raise ResourceError('unsupported PostgreSQL data mount')
    if type(result['postgres']['max_connections']) is not int or result['postgres']['max_connections'] < 10:
        raise ResourceError('invalid PostgreSQL connection limit')
    for key, minimum in (('wal_mb', 80), ('checkpoint_seconds', 30)):
        if type(result['postgres'][key]) is not int or result['postgres'][key] < minimum:
            raise ResourceError('invalid PostgreSQL setting: ' + key)
    for key, number in result['limits'].items():
        if type(number) is not int or number < (0 if key in ('max_idle_environments', 'idle_seconds', 'min_free_bytes', 'wait_seconds', 'max_retained_runs') else 1):
            raise ResourceError('invalid resource limit: ' + key)
    if result['limits']['max_runs'] > 100 or result['limits']['max_environments'] > 20:
        raise ResourceError('resource concurrency exceeds supported bounds')
    return result


def fingerprint(config, images):
    payload = {key: config[key] for key in ('schema_version', 'isolation', 'postgres', 'redis')}
    payload.update(images=images, redis_databases=2 * config['limits']['max_runs'] + 1)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise ResourceError('invalid process identity')
    if Path('/proc/sys/kernel/random/boot_id').exists():
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                return None
            return {'pid': pid, 'started': fields[19],
                    'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        except FileNotFoundError:
            return None
    command = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=', '-o', 'lstart='],
                             text=True, capture_output=True, env={**os.environ, 'LC_ALL': 'C'})
    if command.returncode == 1 and not command.stdout.strip():
        return None
    if command.returncode or not command.stdout.strip():
        raise ResourceError('cannot establish process liveness')
    fields = command.stdout.strip().split(None, 1)
    if fields[0].startswith('Z'):
        return None
    boot = subprocess.run(['sysctl', '-n', 'kern.boottime'], text=True, capture_output=True)
    if boot.returncode or not boot.stdout.strip():
        raise ResourceError('cannot establish host boot identity')
    return {'pid': pid, 'started': fields[1], 'boot': boot.stdout.strip()}


def alive(identity):
    return process_identity(identity['pid']) == identity


def project_identity(repo, daemon):
    command = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                             text=True, capture_output=True)
    if command.returncode:
        raise ResourceError('resource ownership requires a Git repository')
    common = str(Path(command.stdout.strip()).resolve(strict=True))
    return hashlib.sha256((common + '\0' + daemon).encode()).hexdigest()[:24]


class Manager:
    def __init__(self, root, project, backend, config):
        self.root, self.project, self.backend = Path(root), project, backend
        self.config = validate_config(config)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ResourceError('resource state directory must be private and owned by this user')
        self.path = self.root / 'state.json'

    @contextmanager
    def locked(self):
        fd = os.open(self.root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ResourceError('invalid resource lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            # An in-flight Docker CLI retains this lock if the supervisor is
            # killed, so recovery cannot race a still-running create/delete RPC.
            self.backend.lock_fd = fd
            yield self.read()
        finally:
            self.backend.lock_fd = None
            os.close(fd)

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {'schema_version': 1, 'project': self.project, 'environments': {}, 'runs': {}}
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 8 * 1024**2:
            raise ResourceError('invalid resource state file')
        try:
            value = json.loads(self.path.read_text())
            if set(value) != {'schema_version', 'project', 'environments', 'runs'} or value['schema_version'] != 1 or value['project'] != self.project:
                raise ValueError('state contract')
            for kind in ('environments', 'runs'):
                if not isinstance(value[kind], dict):
                    raise ValueError('state objects')
                for key, item in value[kind].items():
                    if not re.fullmatch('[a-f0-9]{32}', key) or not isinstance(item, dict) or item.get('id') != key:
                        raise ValueError('state identity')
                    if kind == 'environments':
                        validate_config(item['config'])
                        if item['project'] != self.project or item['name'] != 'hc-' + self.project[:10] + '-' + key[:12]:
                            raise ValueError('environment ownership')
                        if not re.fullmatch('[a-f0-9]{64}', item['secret']) or item['state'] not in ('creating', 'ready', 'cleanup_pending'):
                            raise ValueError('environment state')
                    else:
                        if item['environment'] not in value['environments'] or item['database'] != 'hc_' + key:
                            raise ValueError('run ownership')
                        databases = item['redis_dbs']
                        maximum = value['environments'][item['environment']]['config']['limits']['max_runs'] * 2
                        if not isinstance(databases, list) or len(databases) != 2 or len(set(databases)) != 2 or any(type(db) is not int or not 1 <= db <= maximum for db in databases):
                            raise ValueError('run Redis ownership')
                        if not re.fullmatch('[a-f0-9]{32}', item['secret']) or item['state'] not in ('allocating', 'active', 'cleanup_pending', 'retained'):
                            raise ValueError('run state')
            occupied = [(item['environment'], db) for item in value['runs'].values() for db in item['redis_dbs']]
            if len(occupied) != len(set(occupied)):
                raise ValueError('overlapping Redis ownership')
            return value
        except (KeyError, TypeError, ValueError) as error:
            raise ResourceError('invalid resource state; preserving all resources') from error

    def save(self, state):
        fd, temporary = tempfile.mkstemp(prefix='.state-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(state, output, sort_keys=True)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def cleanup_run(self, state, run):
        run['state'] = 'cleanup_pending'
        self.save(state)
        try:
            self.backend.cleanup(state['environments'][run['environment']], run)
        except Exception as error:
            run['cleanup_error'] = type(error).__name__
            self.save(state)
            raise ResourceError('cleanup failed; new allocations are blocked') from error
        del state['runs'][run['id']]
        state['environments'][run['environment']]['last_used'] = time.time()
        self.save(state)

    def recover(self, state):
        def eligible(run):
            if run['state'] == 'retained':
                return run['retain_until'] <= time.time()
            return run['state'] == 'cleanup_pending' or not alive(run['owner'])
        for env in list(state['environments'].values()):
            runs = [run for run in state['runs'].values() if run['environment'] == env['id']]
            # After a reboot SQL is unavailable. Reclaim the entire owned test
            # environment only after every lease is recoverable and writers stop.
            if runs and all(eligible(run) for run in runs) and self.backend.unavailable(env):
                env['state'] = 'cleanup_pending'
                for run in runs:
                    run['state'] = 'cleanup_pending'
                self.save(state)
                try:
                    for run in runs:
                        self.backend.discard_run(env, run)
                    self.backend.destroy(env)
                except Exception as error:
                    raise ResourceError('environment recovery failed; new allocations are blocked') from error
                for run in runs:
                    del state['runs'][run['id']]
                del state['environments'][env['id']]
                self.save(state)
            else:
                for run in runs:
                    if eligible(run):
                        self.cleanup_run(state, run)
        for env in list(state['environments'].values()):
            if env['state'] != 'ready' and not any(r['environment'] == env['id'] for r in state['runs'].values()):
                self.destroy(state, env)

    def destroy(self, state, env):
        env['state'] = 'cleanup_pending'
        self.save(state)
        try:
            self.backend.destroy(env)
        except Exception as error:
            raise ResourceError('environment cleanup failed; preserving ownership record') from error
        del state['environments'][env['id']]
        self.save(state)

    def prune_idle(self, state, all_idle=False, keep=None):
        active = {run['environment'] for run in state['runs'].values()}
        idle = sorted((env for env in state['environments'].values() if env['id'] not in active),
                      key=lambda env: env['last_used'], reverse=True)
        retained = 0
        for env in idle:
            expired = time.time() - env['last_used'] >= self.config['limits']['idle_seconds']
            if env['id'] != keep and (all_idle or env['mode'] == 'fresh' or expired or retained >= self.config['limits']['max_idle_environments']):
                self.destroy(state, env)
            else:
                retained += 1

    def budget(self, state):
        limit = self.config['limits']
        if shutil.disk_usage(self.root).free < limit['min_free_bytes']:
            raise BudgetExceeded('host free storage is below the configured reserve')
        usage = [self.backend.usage(env) for env in state['environments'].values() if env['state'] == 'ready']
        if sum(item['storage_bytes'] for item in usage) >= limit['max_storage_bytes']:
            raise BudgetExceeded('project test storage capacity exceeded')
        if any(item.get('docker_free_bytes', limit['min_free_bytes']) < limit['min_free_bytes'] for item in usage):
            raise BudgetExceeded('Docker free storage is below the configured reserve')

    def acquire(self, mode):
        if mode not in ('shared', 'fresh'):
            raise ResourceError('unknown resource mode')
        with self.locked() as state:
            self.recover(state)
            self.prune_idle(state)
            try:
                self.budget(state)
            except ResourceError:
                self.prune_idle(state, all_idle=True)
                self.budget(state)
            if len(state['runs']) >= self.config['limits']['max_runs']:
                raise CapacityWait('run capacity reached')
            images = self.backend.resolve(self.config)
            key = fingerprint(self.config, images)
            env = next((item for item in state['environments'].values()
                        if mode == item['mode'] == 'shared' and item['fingerprint'] == key and item['state'] == 'ready'), None)
            if env is None:
                if len(state['environments']) >= self.config['limits']['max_environments']:
                    self.prune_idle(state, all_idle=True)
                if len(state['environments']) >= self.config['limits']['max_environments']:
                    raise CapacityWait('environment capacity reached')
                identifier = uuid.uuid4().hex
                env = {'id': identifier, 'project': self.project, 'fingerprint': key, 'mode': mode,
                       'name': 'hc-' + self.project[:10] + '-' + identifier[:12],
                       'config': copy.deepcopy(self.config), 'images': images,
                       'secret': uuid.uuid4().hex + uuid.uuid4().hex, 'state': 'creating', 'last_used': time.time()}
                state['environments'][identifier] = env
                self.save(state)
                try:
                    self.backend.create(env)
                    env['state'] = 'ready'
                    self.save(state)
                except Exception as error:
                    self.destroy(state, env)
                    raise ResourceError('dependency creation failed') from error
            self.backend.health(env)
            try:
                self.budget(state)
            except ResourceError:
                self.prune_idle(state, all_idle=True)
                raise
            taken = {db for run in state['runs'].values() if run['environment'] == env['id'] for db in run['redis_dbs']}
            available = [db for db in range(1, 2 * env['config']['limits']['max_runs'] + 1) if db not in taken]
            if len(available) < 2:
                raise CapacityWait('Redis database capacity reached')
            identifier = uuid.uuid4().hex
            run = {'id': identifier, 'environment': env['id'], 'owner': process_identity(os.getpid()),
                   'database': 'hc_' + identifier, 'secret': uuid.uuid4().hex,
                   'redis_dbs': available[:2], 'state': 'allocating', 'created': time.time(), 'child': None}
            state['runs'][identifier] = run
            self.save(state)
            try:
                self.backend.allocate(env, run)
            except Exception as error:
                self.cleanup_run(state, run)
                raise ResourceError('run data allocation failed') from error
            run['state'] = 'active'
            self.save(state)
            return copy.deepcopy(run)

    def attach(self, identifier, child):
        with self.locked() as state:
            run = state['runs'].get(identifier)
            if run is None or run['owner'] != process_identity(os.getpid()) or run['state'] != 'active':
                raise ResourceError('run ownership changed before command launch')
            run['child'] = child
            self.save(state)

    def environment(self, identifier):
        with self.locked() as state:
            run = state['runs'][identifier]
            return copy.deepcopy(state['environments'][run['environment']])

    def release(self, identifier, retain_seconds=0):
        with self.locked() as state:
            run = state['runs'].get(identifier)
            if run is None:
                return
            if retain_seconds:
                retained = sum(item['state'] == 'retained' for item in state['runs'].values())
                if retain_seconds > self.config['limits']['max_retain_seconds'] or retained >= self.config['limits']['max_retained_runs']:
                    raise ResourceError('debug retention capacity exceeded')
                self.budget(state)
                self.backend.stop_writers(state['environments'][run['environment']], run)
                run.update(state='retained', retain_until=time.time() + retain_seconds)
                self.save(state)
            else:
                self.cleanup_run(state, run)
            self.prune_idle(state)

    def gc(self, all_idle=False):
        with self.locked() as state:
            self.recover(state)
            self.prune_idle(state, all_idle=all_idle)

    def status(self):
        with self.locked() as state:
            return {'schema_version': 1, 'project': self.project,
                    'host_free_bytes': shutil.disk_usage(self.root).free,
                    'environments': [{key: env[key] for key in ('id', 'name', 'fingerprint', 'mode', 'state', 'last_used')}
                                     | {'usage': self.backend.usage(env) if env['state'] == 'ready' else None}
                                     for env in state['environments'].values()],
                    'runs': [{key: run[key] for key in ('id', 'environment', 'database', 'redis_dbs', 'state', 'created')}
                             for run in state['runs'].values()]}

    def check_budget(self):
        with self.locked() as state:
            self.budget(state)
