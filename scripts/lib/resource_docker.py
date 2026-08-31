"""Docker adapter. Every destructive operation verifies recorded ownership."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from .resources import ResourceError, process_identity

LABEL = 'io.harnessctl.'


def stop_process_group(child):
    if not child:
        return
    current = process_identity(child['pid'])
    if current is not None and current != child:
        raise ResourceError('workload PID was reused; refusing to signal it')
    def members():
        group = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], text=True, capture_output=True)
        if group.returncode:
            raise ResourceError('cannot inspect workload process group')
        rows = [line.split() for line in group.stdout.splitlines() if len(line.split()) == 3]
        return [int(item[0]) for item in rows if int(item[1]) == child['pid'] and not item[2].startswith('Z')]
    if not members():
        return
    # A live original leader proves the group. An orphan group cannot be claimed by
    # PID alone: fail closed rather than killing a possibly unrelated process.
    if current is None:
        raise ResourceError('orphan workload group requires owner reconciliation')
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child['pid'], sig)
        except ProcessLookupError:
            return
        except PermissionError:
            if members():
                raise
            return
        for _ in range(20):
            # macOS may return EPERM for killpg(0) when only a zombie remains.
            # Observe live writers instead of treating that as cleanup failure.
            if not members():
                return
            time.sleep(0.05)
    raise ResourceError('workload process group did not stop')


class Docker:
    def __init__(self, root):
        self.root = Path(root)

    def call(self, args, *, data=None, env=None, timeout=40, missing=False):
        try:
            result = subprocess.run(['docker', *args], input=data, text=True, capture_output=True,
                                    env={**os.environ, **(env or {})}, timeout=timeout,
                                    pass_fds=(self.lock_fd,) if getattr(self, 'lock_fd', None) is not None else ())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ResourceError('Docker operation unavailable: ' + args[0]) from error
        if result.returncode:
            absent = re.search(r'no such (?:object|volume|container|image|network)\b|network .+ not found', result.stderr.lower())
            if missing and absent:
                return None
            # Never print CLI arguments, SQL or daemon output containing credentials.
            raise ResourceError('Docker operation failed: ' + ' '.join(args[:2]))
        return result.stdout.strip()

    def identity(self):
        host = os.environ.get('DOCKER_HOST', '')
        if host and not host.startswith('unix://'):
            raise ResourceError('run resources on the Docker host; remote daemon ports are not local')
        if not host:
            endpoint = self.call(['context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'])
            if not endpoint.startswith('unix://'):
                raise ResourceError('resource lifecycle requires a local Docker endpoint')
        return self.call(['info', '--format', '{{.ID}}'])

    def resolve(self, config):
        resolved = {}
        for service in ('postgres', 'redis'):
            info = self.call(['image', 'inspect', config[service]['image'], '--format', '{{.Id}}'], missing=True)
            if info is None:
                self.call(['pull', config[service]['image']], timeout=180)
                info = self.call(['image', 'inspect', config[service]['image'], '--format', '{{.Id}}'])
            if not info.startswith('sha256:'):
                raise ResourceError('dependency image has no immutable identity')
            resolved[service] = info
        return resolved

    def labels(self, env, role, run=None):
        value = {LABEL + 'managed': 'test-resources-v1', LABEL + 'project': env['project'],
                 LABEL + 'environment': env['id'], LABEL + 'role': role}
        if run:
            value[LABEL + 'run'] = run['id']
        return value

    def label_args(self, labels):
        return [item for pair in labels.items() for item in ('--label', '='.join(pair))]

    def inspect(self, kind, name, env, role, run=None, missing=False):
        raw = self.call([kind, 'inspect', name, '--format', '{{json .}}'], missing=missing)
        if raw is None:
            return None
        value = json.loads(raw)
        actual = value.get('Config', {}).get('Labels', {}) if kind in ('container', 'image') else value.get('Labels', {})
        if any((actual or {}).get(key) != item for key, item in self.labels(env, role, run).items()):
            raise ResourceError('resource ownership mismatch; refusing mutation')
        return value

    def create(self, env):
        network = env['name'] + '-net'
        self.call(['network', 'create', *self.label_args(self.labels(env, 'network')), network])
        for service in ('postgres', 'redis'):
            self.call(['volume', 'create', *self.label_args(self.labels(env, service)), env['name'] + '-' + service + '-data'])
            config = env['config'][service]
            path = config.get('data_path', '/data')
            args = ['run', '--detach', '--pull=never', '--name', env['name'] + '-' + service,
                    *self.label_args(self.labels(env, service)), '--network', network, '--network-alias', service,
                    '--memory', str(config['memory_mb']) + 'm', '--cpus', str(config['cpus']),
                    '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                    '--mount', 'type=volume,source=' + env['name'] + '-' + service + '-data,target=' + path]
            if service == 'postgres':
                args += ['--publish', '127.0.0.1::5432', '--env', 'POSTGRES_PASSWORD', '--env', 'POSTGRES_DB=postgres',
                         env['images'][service], 'postgres', '-c', 'max_connections=' + str(config['max_connections']),
                         '-c', 'max_wal_size=' + str(config['wal_mb']) + 'MB',
                         '-c', 'checkpoint_timeout=' + str(config['checkpoint_seconds']) + 's']
            else:
                args += ['--publish', '127.0.0.1::6379', '--env', 'HARNESS_REDIS_PASSWORD', env['images'][service],
                         'sh', '-c', 'exec redis-server --save "" --appendonly no --requirepass "$HARNESS_REDIS_PASSWORD" '
                         '--maxmemory ' + str(config['memory_mb'] * 3 // 4) + 'mb --maxmemory-policy noeviction '
                         '--databases ' + str(2 * env['config']['limits']['max_runs'] + 1)]
            self.call(args, env={'POSTGRES_PASSWORD': env['secret'], 'HARNESS_REDIS_PASSWORD': env['secret']})
            info = self.inspect('container', env['name'] + '-' + service, env, service)
            volumes = [item for item in info['Mounts'] if item['Type'] == 'volume']
            if len(volumes) != 1 or volumes[0]['Name'] != env['name'] + '-' + service + '-data':
                raise ResourceError('dependency image created an unexpected data volume')
        for attempt in range(60):
            try:
                self.health(env)
                break
            except ResourceError:
                if attempt == 59:
                    raise
                time.sleep(0.5)
        self.sql(env, 'REVOKE CONNECT ON DATABASE postgres FROM PUBLIC; REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;')

    def sql(self, env, statement):
        self.inspect('container', env['name'] + '-postgres', env, 'postgres')
        return self.call(['exec', '-i', env['name'] + '-postgres', 'psql', '-X', '-U', 'postgres', '-d', 'postgres',
                          '-v', 'ON_ERROR_STOP=1', '-At'], data=statement)

    def redis(self, env, *args, db=0):
        self.inspect('container', env['name'] + '-redis', env, 'redis')
        return self.call(['exec', '-e', 'REDISCLI_AUTH', env['name'] + '-redis', 'redis-cli', '--raw', '-e',
                          '-n', str(db), *args], env={'REDISCLI_AUTH': env['secret']})

    def health(self, env):
        for service in ('postgres', 'redis'):
            info = self.inspect('container', env['name'] + '-' + service, env, service)
            if not info['State']['Running'] or info['Image'] != env['images'][service]:
                raise ResourceError('dependency is unhealthy or its image identity changed')
        if self.sql(env, 'SELECT 1;') != '1' or self.redis(env, 'PING') != 'PONG':
            raise ResourceError('dependency health check failed')

    def unavailable(self, env):
        for service in ('postgres', 'redis'):
            info = self.inspect('container', env['name'] + '-' + service, env, service, missing=True)
            if info is None or not info['State']['Running']:
                return True
        return False

    def allocate(self, env, run):
        name = run['database']
        self.sql(env, f'CREATE ROLE "{name}" LOGIN PASSWORD \'{run["secret"]}\' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;')
        self.sql(env, f'CREATE DATABASE "{name}" OWNER "{name}" TEMPLATE template0;')
        self.sql(env, f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC;')
        self.redis(env, 'ACL', 'SETUSER', name, 'reset', 'on', '>' + run['secret'], '~*', '&*',
                   '+@all', '-@admin', '-@dangerous', '+client|setname', '+client|setinfo')
        for db in run['redis_dbs']:
            if self.redis(env, 'DBSIZE', db=db) != '0':
                raise ResourceError('Redis slot contains unowned data')

    def stop_workloads(self, env, run):
        stop_process_group(run.get('child'))
        filters = ['--filter', 'label=' + LABEL + 'project=' + env['project'],
                   '--filter', 'label=' + LABEL + 'run=' + run['id']]
        for identifier in self.call(['ps', '-aq', *filters]).splitlines():
            self.inspect('container', identifier, env, 'workload', run)
            self.call(['rm', '-f', '-v', '--', identifier])
            if self.inspect('container', identifier, env, 'workload', run, missing=True) is not None:
                raise ResourceError('workload container remains after cleanup')

    def stop_writers(self, env, run):
        self.stop_workloads(env, run)
        pg = self.inspect('container', env['name'] + '-postgres', env, 'postgres', missing=True)
        redis = self.inspect('container', env['name'] + '-redis', env, 'redis', missing=True)
        if pg:
            role = self.sql(env, f"SELECT rolname FROM pg_roles WHERE rolname='{run['database']}';")
            owner = self.sql(env, f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{run['database']}';")
            if owner and owner != run['database']:
                raise ResourceError('database ownership changed; refusing cleanup')
            if role:
                self.sql(env, f'ALTER ROLE "{run["database"]}" NOLOGIN;')
            if owner:
                self.sql(env, f'ALTER DATABASE "{run["database"]}" ALLOW_CONNECTIONS false;')
                self.sql(env, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{run['database']}';")
        if redis and self.redis(env, 'ACL', 'GETUSER', run['database']):
            self.redis(env, 'ACL', 'SETUSER', run['database'], 'off')
            self.redis(env, 'CLIENT', 'KILL', 'USER', run['database'], 'SKIPME', 'yes')

    def cleanup(self, env, run):
        self.stop_writers(env, run)
        if self.inspect('container', env['name'] + '-postgres', env, 'postgres', missing=True):
            self.sql(env, f'DROP DATABASE IF EXISTS "{run["database"]}" WITH (FORCE); DROP ROLE IF EXISTS "{run["database"]}";')
            if self.sql(env, f"SELECT datname FROM pg_database WHERE datname='{run['database']}';"):
                raise ResourceError('run database survived cleanup')
            if self.sql(env, f"SELECT rolname FROM pg_roles WHERE rolname='{run['database']}';"):
                raise ResourceError('run role survived cleanup')
        if self.inspect('container', env['name'] + '-redis', env, 'redis', missing=True):
            for db in run['redis_dbs']:
                self.redis(env, 'FLUSHDB', 'SYNC', db=db)
                if self.redis(env, 'DBSIZE', db=db) != '0':
                    raise ResourceError('run Redis data survived cleanup')
            self.redis(env, 'ACL', 'DELUSER', run['database'])
        self.cleanup_metadata(env, run)

    def discard_run(self, env, run):
        self.stop_workloads(env, run)
        self.cleanup_metadata(env, run)

    def cleanup_metadata(self, env, run):
        filters = ['--filter', 'label=' + LABEL + 'project=' + env['project'],
                   '--filter', 'label=' + LABEL + 'run=' + run['id']]
        for identifier in set(self.call(['image', 'ls', '-q', *filters]).splitlines()):
            self.inspect('image', identifier, env, 'workload', run)
            self.call(['image', 'rm', '--', identifier])
        for suffix in ('labels', 'container.env'):
            path = self.root / (run['id'] + '.' + suffix)
            if path.is_symlink():
                raise ResourceError('run metadata is a symlink')
            path.unlink(missing_ok=True)

    def destroy(self, env):
        for service in ('postgres', 'redis'):
            name = env['name'] + '-' + service
            if self.inspect('container', name, env, service, missing=True):
                self.call(['rm', '-f', '-v', '--', name])
            if self.inspect('volume', name + '-data', env, service, missing=True):
                self.call(['volume', 'rm', '--', name + '-data'])
            for kind, resource in (('container', name), ('volume', name + '-data')):
                if self.inspect(kind, resource, env, service, missing=True):
                    raise ResourceError('dependency resource survived cleanup')
        network = env['name'] + '-net'
        if self.inspect('network', network, env, 'network', missing=True):
            self.call(['network', 'rm', '--', network])
        if self.inspect('network', network, env, 'network', missing=True):
            raise ResourceError('dependency network survived cleanup')

    def disk_bytes(self, container, program, path):
        option, column = {'du': ('-sk', 0), 'df': ('-Pk', 3)}[program]
        # A read can fail while PostgreSQL removes files or Docker is briefly
        # busy. Require a complete successful measurement; never accept partial
        # output or retry mutations; each of three attempts has a ten-second limit.
        for attempt in range(3):
            try:
                output = self.call(['exec', container, program, option, path], timeout=10)
                value = int(output.splitlines()[-1].split()[column])
                if value < 0:
                    raise ValueError('negative disk measurement')
                return value * 1024
            except (ResourceError, ValueError, IndexError) as error:
                if attempt == 2:
                    raise ResourceError(program + ' storage sampling failed after 3 attempts') from error
                time.sleep(0.1)

    def usage(self, env):
        usage = {'storage_bytes': 0, 'service_log_budget_bytes': 60 * 1024**2}
        for service in ('postgres', 'redis'):
            self.inspect('container', env['name'] + '-' + service, env, service)
            path = env['config'][service].get('data_path', '/data')
            usage[service + '_bytes'] = self.disk_bytes(env['name'] + '-' + service, 'du', path)
            usage['storage_bytes'] += usage[service + '_bytes']
            usage['docker_free_bytes'] = self.disk_bytes(env['name'] + '-' + service, 'df', path)
        usage['storage_bytes'] += usage['service_log_budget_bytes']
        return usage

    def bindings(self, env, run):
        ports = {}
        for service, port in (('postgres', '5432/tcp'), ('redis', '6379/tcp')):
            info = self.inspect('container', env['name'] + '-' + service, env, service)
            bindings = info['NetworkSettings']['Ports'][port]
            if len(bindings) != 1 or bindings[0]['HostIp'] != '127.0.0.1':
                raise ResourceError('dependency binding is not loopback-only')
            ports[service] = bindings[0]['HostPort']
        name, secret = run['database'], run['secret']
        def variables(pg_host, redis_host):
            dsn = f'postgres://{name}:{secret}@{pg_host}/{name}?sslmode=disable'
            return {'DATABASE_DSN': dsn, 'TEST_DATABASE_DSN': dsn,
                    'REDIS_ADDR': redis_host, 'TEST_REDIS_ADDR': redis_host, 'CACHE_REDIS_ADDR': redis_host,
                    'REDIS_USERNAME': name, 'TEST_REDIS_USERNAME': name, 'CACHE_REDIS_USERNAME': name,
                    'REDIS_PASSWORD': secret, 'TEST_REDIS_PASSWORD': secret, 'CACHE_REDIS_PASSWORD': secret,
                    'REDIS_DB': str(run['redis_dbs'][0]), 'TEST_REDIS_DB': str(run['redis_dbs'][0]),
                    'CACHE_REDIS_DB': str(run['redis_dbs'][1]), 'CACHE_NAMESPACE': run['id'],
                    'HARNESS_RESOURCE_RUN_ID': run['id'], 'HARNESS_RESOURCE_PROJECT': env['project'],
                    'HARNESS_RESOURCE_MODE': env['mode'],
                    'HARNESS_RESOURCE_ENVIRONMENT': env['id'], 'HARNESS_RESOURCE_NETWORK': env['name'] + '-net'}
        labels_path = self.root / (run['id'] + '.labels')
        env_path = self.root / (run['id'] + '.container.env')
        for path, values in ((labels_path, self.labels(env, 'workload', run)),
                             (env_path, variables('postgres:5432', 'redis:6379'))):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
        result = variables('127.0.0.1:' + ports['postgres'], '127.0.0.1:' + ports['redis'])
        result.update(HARNESS_RESOURCE_LABEL_FILE=str(labels_path), HARNESS_RESOURCE_ENV_FILE=str(env_path))
        return result
