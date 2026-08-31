#!/usr/bin/env python3
"""Real Docker acceptance using disposable, configuration-only Git worktrees."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid


def docker_proxy():
    args = sys.argv[2:]
    root = Path(os.environ['HARNESS_FIXTURE_FAULT_ROOT'])
    phase = os.environ['HARNESS_FIXTURE_FAULT_PHASE']
    data = sys.stdin.read() if args[:2] == ['exec', '-i'] and 'psql' in args else None
    matched = bool(data and data.startswith('CREATE DATABASE' if phase == 'allocation' else 'DROP DATABASE'))
    if matched and phase == 'cleanup-failure':
        (root / 'blocked').write_text(json.dumps({'owner': os.getppid()}))
        raise SystemExit(1)
    result = subprocess.run([os.environ['HARNESS_FIXTURE_DOCKER'], *args], input=data, text=True)
    if matched and result.returncode == 0:
        (root / 'blocked').write_text(json.dumps({'owner': os.getppid()}))
        deadline = time.monotonic() + 30
        while not (root / 'resume').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    raise SystemExit(result.returncode)


def command(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{argv[0]} failed ({result.returncode}): {result.stderr[-1800:]}')
    return result.stdout.strip()


def child(root, hold, workload):
    run = os.environ['HARNESS_RESOURCE_RUN_ID']
    prefix = os.environ['HARNESS_RESOURCE_NETWORK'].removesuffix('-net')
    dsn = urllib.parse.urlsplit(os.environ['TEST_DATABASE_DSN'])
    pg = ['docker', 'exec', '-i', '-e', 'PGPASSWORD', prefix + '-postgres', 'psql', '-X', '-h', '127.0.0.1',
          '-U', dsn.username, '-d', dsn.path[1:], '-v', 'ON_ERROR_STOP=1', '-At']
    pg_env = {**os.environ, 'PGPASSWORD': dsn.password}
    command(pg, input=Path('migration.sql').read_text(), env=pg_env)
    redis = ['docker', 'exec', '-e', 'REDISCLI_AUTH', prefix + '-redis', 'redis-cli', '--raw', '-e',
             '--user', os.environ['TEST_REDIS_USERNAME'], '-n', os.environ['TEST_REDIS_DB']]
    redis_env = {**os.environ, 'REDISCLI_AUTH': os.environ['TEST_REDIS_PASSWORD']}
    command(redis + ['SET', 'same-key', run], env=redis_env)
    denied = subprocess.run(redis + ['FLUSHALL'], text=True, capture_output=True, env=redis_env)
    if denied.returncode == 0:
        raise RuntimeError('test user can clear the entire Redis instance')
    if workload:
        name = 'harness-it-' + run
        command(['docker', 'create', '--name', name, '--label-file', os.environ['HARNESS_RESOURCE_LABEL_FILE'],
                 '--network', os.environ['HARNESS_RESOURCE_NETWORK'], 'postgres:16-alpine', 'true'])
        mounts = json.loads(command(['docker', 'inspect', name, '--format', '{{json .Mounts}}']))
        (root / (run + '.workload.json')).write_text(json.dumps({'container': name, 'volumes': [m['Name'] for m in mounts if m['Type'] == 'volume']}))
    (root / (run + '.ready')).write_text('ready')
    while hold and not (root / 'release').exists():
        time.sleep(0.1)
    if command(redis + ['GET', 'same-key'], env=redis_env) != run:
        raise RuntimeError('another task changed the Redis data')
    expected = Path('version.txt').read_text().strip()
    if command(pg, input='SELECT version FROM sample;', env=pg_env) != expected:
        raise RuntimeError('another worktree changed the database schema/data')


def main():
    if sys.argv[1:2] == ['--docker-proxy']:
        docker_proxy()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--rounds', type=int, default=100)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--child', type=Path)
    parser.add_argument('--hold', action='store_true')
    parser.add_argument('--workload', action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.child, args.hold, args.workload)
        return
    if not args.binary or not args.output or args.rounds < 1:
        parser.error('--binary, --output and a positive --rounds are required')
    binary = str(args.binary.resolve())
    root = Path(tempfile.mkdtemp(prefix='harness-resources-it-'))
    repo = root / 'repo'
    (repo / 'harness').mkdir(parents=True)
    config = {'schema_version': 1, 'enabled': True, 'isolation': 'database-per-run-v1',
              'limits': {'max_runs': 10, 'max_environments': 3, 'wait_seconds': 0}}
    version = command([binary, 'version']).split()[1]
    (repo / 'harness/harness.lock').write_text(json.dumps({'schema_version': 1, 'module': 'github.com/Fueav/harnessctl', 'version': version}))
    (repo / 'harness/dependencies.json').write_text(json.dumps(config))
    (repo / 'migration.sql').write_text("CREATE TABLE sample(version integer, payload text); INSERT INTO sample VALUES(1,repeat('x',32768));")
    (repo / 'version.txt').write_text('1')
    git_env = {**os.environ, 'GIT_AUTHOR_NAME': 'Harness fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Harness fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
    command(['git', 'init', '-q', '-b', 'main', str(repo)])
    command(['git', '-C', str(repo), 'add', '.'])
    command(['git', '-C', str(repo), 'commit', '-qm', 'configuration-only consumer'], env=git_env)
    worktree = root / 'other-worktree'
    command(['git', '-C', str(repo), 'worktree', 'add', '-qb', 'different-migration', str(worktree)])
    (worktree / 'migration.sql').write_text("CREATE TABLE sample(version integer, extra boolean); INSERT INTO sample VALUES(2,true);")
    (worktree / 'version.txt').write_text('2')
    command(['git', '-C', str(worktree), 'add', '.'])
    command(['git', '-C', str(worktree), 'commit', '-qm', 'different schema'], env=git_env)
    processes, observations, cases = [], [], []
    state_root = root / 'state'
    fault_root = root / 'fault'
    fault_root.mkdir()
    proxy = fault_root / 'docker'
    proxy.write_text(f'#!{sys.executable}\nimport os,sys\nos.execv(sys.executable,[sys.executable,{str(Path(__file__).resolve())!r},"--docker-proxy",*sys.argv[1:]])\n')
    proxy.chmod(0o700)
    def fault_env(phase):
        for name in ('blocked', 'resume'):
            (fault_root / name).unlink(missing_ok=True)
        return {**os.environ, 'PATH': str(fault_root) + os.pathsep + os.environ['PATH'],
                'HARNESS_FIXTURE_DOCKER': shutil.which('docker'),
                'HARNESS_FIXTURE_FAULT_ROOT': str(fault_root), 'HARNESS_FIXTURE_FAULT_PHASE': phase}
    def cli(operation, path=repo, extra=(), env=None):
        return subprocess.run([binary, 'resources', operation, '--repo', str(path), '--state-root', str(state_root), *extra],
                              text=True, capture_output=True, env=env)
    def state():
        files = list(state_root.glob('*/state.json'))
        if len(files) != 1:
            raise RuntimeError('worktrees did not use one project ownership registry')
        return json.loads(files[0].read_text())
    def require(result):
        if result.returncode:
            raise RuntimeError(result.stderr[-2400:])
        return result
    def payload(hold=False, workload=False):
        return ['--', sys.executable, str(Path(__file__).resolve()), '--child', str(root),
                *(['--hold'] if hold else []), *(['--workload'] if workload else [])]
    def start(path=repo):
        process = subprocess.Popen([binary, 'resources', 'run', '--repo', str(path), '--state-root', str(state_root), *payload(True)],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        return process
    def wait_active(count):
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            if list(state_root.glob('*/state.json')):
                current = state()
                active = [run for run in current['runs'].values() if run['state'] == 'active' and (root / (run['id'] + '.ready')).exists()]
                if len(active) == count:
                    return active
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('concurrent command exited prematurely: ' + ''.join(p.communicate()[1] for p in processes if p.poll() is not None))
            time.sleep(0.1)
        raise RuntimeError('concurrent commands did not become ready')
    foreign = 'harness-it-foreign-' + uuid.uuid4().hex[:16]
    command(['docker', 'volume', 'create', '--label', 'fixture=foreign-to-harness', foreign])
    outcome = {'status': 'failed', 'rounds': args.rounds, 'fixture': str(root), 'cases': cases, 'observations': observations,
               'engine_version': version, 'engine_binary_sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
               'worktree_commits': [command(['git', '-C', str(path), 'rev-parse', 'HEAD']) for path in (repo, worktree)]}
    try:
        for index in range(args.rounds):
            require(cli('run', path=repo if index % 2 == 0 else worktree, extra=payload(workload=index % 10 == 0)))
            if state()['runs']:
                raise RuntimeError('completed serial run retained a database lease')
            if index % 10 == 0 or index == args.rounds - 1:
                sample = json.loads(require(cli('status')).stdout)
                observations.append({'round': index + 1, **sample})
                environment = next(iter(state()['environments'].values()))
                count = command(['docker', 'exec', environment['name'] + '-postgres', 'psql', '-X', '-U', 'postgres', '-At',
                                 '-c', "SELECT count(*) FROM pg_database WHERE datname LIKE 'hc_%';"])
                if count != '0':
                    raise RuntimeError('completed run database still exists in PostgreSQL')
                print(json.dumps({'serial_round': index + 1, 'active_databases': 0, 'environments': len(sample['environments'])}), flush=True)
        cases.append('serial_different_worktrees_reuse_one_environment_and_drop_actual_databases')
        for record in root.glob('*.workload.json'):
            item = json.loads(record.read_text())
            for kind, names in (('container', [item['container']]), ('volume', item['volumes'])):
                for name in names:
                    if subprocess.run(['docker', kind, 'inspect', name], capture_output=True).returncode == 0:
                        raise RuntimeError('owned workload or its anonymous volume leaked')
        cases.append('owned_workload_anonymous_volumes_removed')
        for index in range(10):
            start(repo if index % 2 == 0 else worktree)
        active = wait_active(10)
        if len({run['environment'] for run in active}) != 1 or len({run['database'] for run in active}) != 10:
            raise RuntimeError('concurrent worktree isolation/reuse failed')
        overflow = cli('run', extra=payload())
        if overflow.returncode == 0 or 'capacity' not in overflow.stderr:
            raise RuntimeError('concurrent capacity limit was bypassed')
        victim = active[0]
        os.kill(victim['owner']['pid'], signal.SIGKILL)
        require(cli('gc'))
        if len(state()['runs']) != 9:
            raise RuntimeError('crash recovery affected another active task')
        (root / 'release').write_text('release')
        codes = [p.wait(timeout=100) for p in processes]
        if sum(code != 0 for code in codes) != 1:
            raise RuntimeError('crashed task affected healthy concurrent commands')
        processes.clear()
        (root / 'release').unlink()
        cases.append('ten_concurrent_worktrees_capacity_and_kill9_recovery_preserve_other_data')
        for sig, entry in ((signal.SIGINT, 'supervisor'), (signal.SIGTERM, 'supervisor'),
                           (signal.SIGINT, 'cli'), (signal.SIGTERM, 'cli')):
            process = start()
            run = wait_active(1)[0]
            os.kill(run['owner']['pid'] if entry == 'supervisor' else process.pid, sig)
            out, error = process.communicate(timeout=30)
            if process.returncode != 128 + sig:
                raise RuntimeError(f'{entry} signal exit code {process.returncode}, expected {128 + sig}: {error[-1200:]}')
            processes.clear()
            if state()['runs']:
                raise RuntimeError('signal cleanup left a run')
        cases.append('interrupt_and_termination_cleanup_and_exit_codes')
        controller = start()
        wait_active(1)
        controller.kill()
        controller.wait(timeout=10)
        deadline = time.monotonic() + 30
        while state()['runs'] and time.monotonic() < deadline:
            time.sleep(0.1)
        controller.communicate(timeout=5)
        processes.clear()
        if state()['runs']:
            raise RuntimeError('invoking CLI death left its supervised workload active')
        cases.append('invoking_cli_kill9_stops_supervisor_workload_and_reclaims_data')
        healthy = start()
        live = wait_active(1)[0]
        for phase in ('allocation', 'cleanup'):
            process = subprocess.Popen([binary, 'resources', 'run', '--repo', str(repo), '--state-root', str(state_root), *payload()],
                                       env=fault_env(phase), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(process)
            deadline = time.monotonic() + 40
            while not (fault_root / 'blocked').exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Docker fault fixture did not reach ' + phase)
                time.sleep(0.05)
            owner = json.loads((fault_root / 'blocked').read_text())['owner']
            if not any(run['owner']['pid'] == owner and run['id'] != live['id'] for run in state()['runs'].values()):
                raise RuntimeError('fault fixture did not identify its own supervisor')
            os.kill(owner, signal.SIGKILL)
            recovery = subprocess.Popen([binary, 'resources', 'gc', '--repo', str(repo), '--state-root', str(state_root)],
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(recovery)
            time.sleep(0.3)
            if recovery.poll() is not None:
                raise RuntimeError('recovery raced the in-flight Docker operation')
            (fault_root / 'resume').write_text('resume')
            out, error = recovery.communicate(timeout=30)
            if recovery.returncode or set(state()['runs']) != {live['id']}:
                raise RuntimeError('interrupted ' + phase + ' failed recovery: ' + error[-1200:])
            process.communicate(timeout=10)
            processes.remove(process)
            processes.remove(recovery)
        (root / 'release').write_text('release')
        if healthy.wait(timeout=30):
            raise RuntimeError('fault recovery changed the healthy task data')
        processes.clear()
        (root / 'release').unlink()
        cases.append('kill_during_allocation_and_cleanup_serializes_inflight_rpc_and_preserves_active_data')
        broken_env = fault_env('cleanup-failure')
        failed = cli('run', extra=payload(), env=broken_env)
        if failed.returncode == 0 or not any(run['state'] == 'cleanup_pending' for run in state()['runs'].values()):
            raise RuntimeError('cleanup failure was hidden')
        pending = set(state()['runs'])
        blocked = cli('run', extra=payload(), env=broken_env)
        if blocked.returncode == 0 or set(state()['runs']) != pending:
            raise RuntimeError('new work bypassed cleanup debt')
        require(cli('gc'))
        if state()['runs']:
            raise RuntimeError('cleanup failure did not recover')
        cases.append('docker_cleanup_failure_blocks_allocations_and_retries_idempotently')
        first = start()
        original = wait_active(1)[0]
        changed = dict(config, postgres={'memory_mb': 1025})
        (worktree / 'harness/dependencies.json').write_text(json.dumps(changed))
        require(cli('run', path=worktree, extra=payload()))
        if original['id'] not in state()['runs'] or len(state()['environments']) != 2:
            raise RuntimeError('changed dependency configuration replaced an active environment')
        (root / 'release').write_text('release')
        if first.wait(timeout=30):
            raise RuntimeError('environment splitting affected active data')
        processes.clear()
        (root / 'release').unlink()
        cases.append('configuration_changes_split_pools_and_evict_excess_idle_versions')
        (worktree / 'harness/dependencies.json').write_text(json.dumps(config))
        active_process = start()
        active_run = wait_active(1)[0]
        tiny = dict(config, limits={**config['limits'], 'max_storage_bytes': 1})
        (worktree / 'harness/dependencies.json').write_text(json.dumps(tiny))
        blocked = cli('run', path=worktree, extra=payload())
        if blocked.returncode == 0 or set(state()['runs']) != {active_run['id']}:
            raise RuntimeError('storage admission removed active data or admitted excess work')
        (root / 'release').write_text('release')
        if active_process.wait(timeout=30):
            raise RuntimeError('capacity rejection changed active data')
        processes.clear()
        (root / 'release').unlink()
        blocked = cli('run', path=worktree, extra=payload())
        if blocked.returncode == 0 or state()['runs'] or state()['environments']:
            raise RuntimeError('idle storage pressure was not reclaimed before admission')
        (worktree / 'harness/dependencies.json').write_text(json.dumps(config))
        cases.append('storage_budget_blocks_tasks_preserves_active_data_and_reclaims_idle_volumes')
        interrupted = start()
        run = wait_active(1)[0]
        environment = state()['environments'][run['environment']]
        os.kill(run['owner']['pid'], signal.SIGKILL)
        interrupted.wait(timeout=10)
        command(['docker', 'stop', environment['name'] + '-postgres', environment['name'] + '-redis'])
        require(cli('gc'))
        processes.clear()
        if state()['runs'] or state()['environments']:
            raise RuntimeError('stopped dependencies with dead owners did not recover')
        cases.append('reboot_simulation_reclaims_stopped_owned_services_without_sql')
        require(cli('gc', extra=['--all-idle']))
        require(cli('run', extra=['--mode', 'fresh', *payload()]))
        if state()['runs'] or state()['environments']:
            raise RuntimeError('fresh mode did not remove all its resources')
        cases.append('fresh_release_environment_zero_residue')
        command(['docker', 'volume', 'inspect', foreign])
        cases.append('unowned_volume_preserved')
        outcome['status'] = 'passed'
    except Exception as error:
        outcome['error'] = str(error)
        raise
    finally:
        (fault_root / 'resume').write_text('resume')
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
        cleanup = cli('gc', extra=['--all-idle'])
        outcome['final_cleanup_code'] = cleanup.returncode
        if cleanup.returncode:
            outcome['status'] = 'failed'
            outcome['cleanup_error'] = cleanup.stderr
        command(['docker', 'volume', 'rm', foreign])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(outcome, indent=2) + '\n')
        args.output.chmod(0o600)
        print(json.dumps({'status': outcome['status'], 'cases': cases, 'report': str(args.output)}), flush=True)
        if outcome['status'] == 'passed':
            shutil.rmtree(root)
    if outcome['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
