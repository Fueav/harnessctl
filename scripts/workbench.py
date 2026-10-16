#!/usr/bin/env python3
"""Versioned project workbench: one descriptor, session owner and execution path."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import project
from lib.evidence import atomic_json
from lib.resource_docker import Docker, stop_process_group
from lib.resources import BudgetExceeded, CapacityWait, Manager, ResourceError, alive, process_identity, project_identity

VERSION = os.environ.get('HARNESSCTL_VERSION', 'dev')
ENGINE_DIGEST = os.environ.get('HARNESS_ENGINE_SHA256', '')
MAX_LOG_BYTES = 8 * 1024**2
MAX_EXECUTIONS = 256


class SessionError(ResourceError):
    def __init__(self, message, state):
        super().__init__(message)
        self.details = {'session_id': state['session_id'], 'session_state': state['state'], 'cleanup': state['cleanup']}


def private(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ResourceError('state directory must be private and owned by this user')
    return path


def read(path):
    value, _ = project.document(path)
    return value


@contextmanager
def lock(path, blocking=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ResourceError('invalid state lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise ResourceError('session busy; observe the active execution') from error
        yield
    finally:
        os.close(fd)


def state_root():
    base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))).expanduser().resolve()
    return private(base / 'harnessctl/workbench')


def repository():
    root = Path(os.environ['HARNESS_PROJECT_ROOT']).resolve()
    project.candidate(root)
    return root


def project_root(repo):
    return private(state_root() / hashlib.sha256(str(repo).encode()).hexdigest()[:24])


def session_dir(repo, identifier, cleanup_only=False):
    if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
        raise ResourceError('invalid session ID')
    path = project_root(repo) / identifier
    if not path.exists():
        raise ResourceError('session does not belong to this worktree')
    private(path)
    state = read(path / 'session.json')
    if state['repo'] != str(repo) or state['session_id'] != identifier or state['engine_version'] != VERSION or (not cleanup_only and state.get('engine_sha256') != ENGINE_DIGEST):
        raise ResourceError('foreign session or engine version')
    return path


def public_session(state):
    keys = ('schema_version', 'session_id', 'state', 'repo', 'project', 'profile', 'created_at', 'expires_at',
            'descriptor_sha256', 'goal_sha256', 'engine_version', 'engine_sha256', 'cleanup', 'error', 'resource_availability', 'supervisor')
    return {key: state[key] for key in keys if key in state}


def manager_for(state):
    config = state['descriptor']['dependencies']
    base = private(state_root().parent / 'workbench-resources')
    backend = Docker(base)
    identity = project_identity(Path(state['repo']), backend.identity())
    backend.root = base / identity
    return Manager(backend.root, identity, backend, config), backend


def current_contract(state):
    descriptor, digest = project.load(Path(state['repo']))
    if digest != state['descriptor_sha256']:
        raise ResourceError('project descriptor changed; close and open a new session')
    if state['goal_path']:
        _, digest, _ = project.goal(Path(state['repo']), state['goal_path'], descriptor)
        if digest != state['goal_sha256']:
            raise ResourceError('goal changed; close and open a new session')
    return descriptor


def finish_interrupted(path, reason):
    for receipt_path in path.glob('executions/*/receipt.json'):
        receipt = read(receipt_path)
        if receipt['state'] == 'running':
            stop_process_group(receipt.get('child'))
            receipt.update(state='interrupted', exit_code=137, ended_at=time.time(), interruption=reason)
            atomic_json(receipt_path, receipt)


def recover(path):
    with lock(path / 'owner.lock', blocking=False):
        state = read(path / 'session.json')
        if state['state'] in ('closed', 'expired') and state['cleanup'] == 'passed':
            return state
        owner = state.get('supervisor')
        if owner and alive(owner):
            return state
        try:
            finish_interrupted(path, 'supervisor stopped')
            if state['descriptor']['profiles'][state['profile']]['resources'] != 'none':
                manager, _ = manager_for(state)
                # Allocation becomes durable in the resource registry before its
                # ID can be copied to the session. Recover that interrupted edge
                # by recorded owner identity, including incomplete environments.
                manager.gc()
                if state.get('resource_run'):
                    manager.release(state['resource_run'])
            state.update(state='closed', cleanup='passed', closed_at=time.time())
        except (ResourceError, OSError) as error:
            state.update(state='cleanup_pending', cleanup='failed', error=str(error))
        if state['cleanup'] == 'passed':
            discard_runtime(path)
        atomic_json(path / 'session.json', state)
        return state


def discard_runtime(path):
    runtime = path / 'runtime'
    if runtime.exists():
        if runtime.is_symlink():
            raise ResourceError('refusing redirected runtime cleanup')
        shutil.rmtree(runtime)


def storage_admission(root, descriptor, reserve=0):
    limits = project.validate_config(descriptor['dependencies'])['limits']
    if shutil.disk_usage(root).free < limits['min_free_bytes'] + reserve:
        raise ResourceError(f'workbench host reserve: {shutil.disk_usage(root).free} free bytes < {limits["min_free_bytes"] + reserve} required bytes; free space on filesystem containing {root}')
    total = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass  # A completed owner may concurrently discard its runtime.
    if total + reserve >= limits['max_storage_bytes']:
        raise ResourceError(f'workbench evidence budget: {total} bytes + {reserve} reserved >= {limits["max_storage_bytes"]} byte limit; archive closed session evidence from {root} before new work')


def create(repo, profile_name, goal_argument, extra_env=None):
    descriptor, digest = project.load(repo)
    if profile_name not in descriptor['profiles']:
        raise ResourceError('unknown profile: ' + profile_name)
    goal, goal_digest, goal_path = project.goal(repo, goal_argument, descriptor)
    root = project_root(repo)
    with lock(root / 'admission.lock'):
        active = 0
        for entry in root.iterdir():
            if not re.fullmatch('[a-f0-9]{32}', entry.name) or not entry.is_dir():
                continue
            prior = read(entry / 'session.json')
            if prior['state'] not in ('closed', 'expired'):
                if not prior.get('supervisor') or not alive(prior['supervisor']):
                    prior = recover(entry)
                if prior['state'] not in ('closed', 'expired'):
                    active += 1
        limits = project.validate_config(descriptor['dependencies'])['limits']
        storage_admission(root, descriptor, 1024**2)
        if active >= limits['max_runs']:
            raise ResourceError('session capacity reached; close an existing session')
        identifier = uuid.uuid4().hex
        path = private(root / identifier)
        private(path / 'executions')
        # The private copy outlives the invoking Go CLI extraction.
        shutil.copytree(Path(__file__).resolve().parent, path / 'runtime')
        now = time.time()
        state = {'schema_version': 1, 'session_id': identifier, 'repo': str(repo), 'project': descriptor['project'],
                 'engine_version': VERSION, 'engine_sha256': ENGINE_DIGEST, 'profile': profile_name, 'descriptor': descriptor,
                 'descriptor_sha256': digest, 'goal': goal, 'goal_sha256': goal_digest, 'goal_path': goal_path,
                 'created_at': now, 'expires_at': now + min(descriptor['profiles'][profile_name]['lease_seconds'], limits['max_run_seconds']),
                 'state': 'opening', 'cleanup': 'not_started', 'resource_availability': 'pending', 'extra_env': extra_env or {}}
        atomic_json(path / 'session.json', state)
        process = None
        try:
            with open(os.devnull, 'rb') as input_stream, open(os.devnull, 'ab') as output_stream:
                process = subprocess.Popen([sys.executable, '-I', '-B', str(path / 'runtime/workbench.py'), '_supervise', str(path)],
                                           stdin=input_stream, stdout=output_stream, stderr=output_stream, start_new_session=True,
                                           env={**os.environ, 'HARNESSCTL_VERSION': VERSION})
            deadline = time.monotonic() + min(180, descriptor['profiles'][profile_name]['lease_seconds'] + 10)
            while time.monotonic() < deadline:
                state = read(path / 'session.json')
                if state['state'] != 'opening':
                    if state['state'] == 'ready':
                        return path, state
                    raise ResourceError(state.get('error', 'session failed to open'))
                if process.poll() is not None:
                    recovered = recover(path)
                    raise ResourceError(recovered.get('error', 'session supervisor exited during open'))
                time.sleep(0.05)
            atomic_json(path / 'close.json', {'reason': 'opening timeout'})
            raise ResourceError('session opening timed out; cleanup requested, inspect session ' + identifier)
        except BaseException as error:
            # The ephemeral caller may be canceled before create returns its
            # handle. Persist cleanup intent before releasing that ownership.
            atomic_json(path / 'close.json', {'reason': 'initialization interrupted'})
            closed = close(path) if process is not None else recover(path)
            raise SessionError(str(error) or 'session initialization interrupted', closed) from error


def drain(source, destination, secrets):
    # Match on original byte positions. Replacing an early credential must not
    # move the safety boundary into a later, partially received credential.
    secrets = sorted(set(secrets), key=len, reverse=True)
    written, pending, truncated = 0, b'', False
    with destination.open('wb') as output:
        os.chmod(destination, 0o600)
        while True:
            chunk = source.read1(4096)
            pending += chunk
            cursor, safe = 0, bytearray()
            while cursor < len(pending):
                available = len(pending) - cursor
                if chunk and any(available < len(secret) and secret.startswith(pending[cursor:]) for secret in secrets):
                    break
                match = next((secret for secret in secrets if pending.startswith(secret, cursor)), None)
                if match:
                    safe.extend(b'[REDACTED]')
                    cursor += len(match)
                else:
                    safe.append(pending[cursor])
                    cursor += 1
            pending = pending[cursor:]
            data = bytes(safe)
            remaining = max(0, MAX_LOG_BYTES - written)
            output.write(data[:remaining])
            written += min(remaining, len(data))
            if len(data) > remaining and not truncated:
                output.write(b'\n[harness: output truncated at 8 MiB]\n')
                truncated = True
            output.flush()
            if not chunk:
                break


def execute(path, state, request, manager, backend, bindings):
    identifier = request['execution_id']
    directory = private(path / 'executions' / identifier)
    operation = state['descriptor']['operations'][request['operation']]
    repo = Path(state['repo'])
    receipt = {'schema_version': 1, 'session_id': state['session_id'], 'execution_id': identifier,
               'engine_version': VERSION, 'engine_sha256': ENGINE_DIGEST, 'descriptor_sha256': state['descriptor_sha256'], 'goal_sha256': state['goal_sha256'],
               'operation': request['operation'], 'argv': operation['argv'] + request['arguments'],
               'started_at': time.time(), 'ended_at': None, 'state': 'running', 'exit_code': None,
               'candidate': project.candidate(repo), 'artifacts': {name: str(directory / name) for name in ('stdout', 'stderr')}}
    atomic_json(directory / 'receipt.json', receipt)
    environment = {**os.environ, **operation.get('env', {}), **bindings, **state['extra_env'],
                   'HARNESS_SESSION_ID': state['session_id'], 'HARNESS_EXECUTION_ID': identifier,
                   'HARNESS_ARTIFACT_DIR': str(directory), 'HARNESS_PROJECT_ROOT': str(repo),
                   'PATH': str(repo / '.tools/bin') + os.pathsep + os.environ.get('PATH', '')}
    secrets = sorted({value.encode() for key, value in bindings.items() if value and ('PASSWORD' in key or 'DSN' in key or 'TOKEN' in key)}, key=len, reverse=True)
    child, threads = None, []
    code, interruption, error = 1, None, None
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.Popen([sys.executable, '-I', '-B', str(path / 'runtime/operation_guardian.py'), str(read_fd), str(directory / 'result.json'), *receipt['argv']],
                                 cwd=repo, env=environment, start_new_session=True, pass_fds=(read_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        identity = process_identity(child.pid)
        if identity is None:
            raise ResourceError('operation exited before ownership registration')
        receipt['child'] = identity
        atomic_json(directory / 'receipt.json', receipt)
        if manager:
            manager.attach(state['resource_run'], identity)
        for source, name in ((child.stdout, 'stdout'), (child.stderr, 'stderr')):
            thread = threading.Thread(target=drain, args=(source, directory / name, secrets), daemon=True)
            thread.start()
            threads.append(thread)
        os.write(write_fd, b'1')
        deadline = min(state['expires_at'], receipt['started_at'] + operation['timeout_seconds'])
        next_sample = time.time() + 1
        while True:
            if (directory / 'result.json').exists():
                code = read(directory / 'result.json')['exit_code']
                break
            if (path / 'close.json').exists():
                code, interruption = 143, 'session closed'
                break
            if time.time() >= deadline:
                code, interruption = 124, 'operation timeout or session lease expired'
                break
            if child.poll() is not None:
                code, interruption = 137, 'operation guardian stopped unexpectedly'
                break
            if manager and time.time() >= next_sample:
                try:
                    manager.check_budget()
                except BudgetExceeded:
                    receipt['budget_pressure'] = True
                next_sample = time.time() + manager.config['limits']['sample_seconds']
            time.sleep(0.05)
    except (ResourceError, OSError) as exception:
        error = str(exception)
    finally:
        os.close(read_fd)
        os.close(write_fd)
        try:
            stop_process_group(receipt.get('child'))
            if child:
                child.wait(timeout=5)
            if manager:
                manager.attach(state['resource_run'], None)
        except (ResourceError, OSError, subprocess.TimeoutExpired) as exception:
            error, code = str(exception), code or 1
            # Block subsequent commands until recovery has proved writers stopped.
            state.update(state='cleanup_pending', cleanup='failed', error=error)
            atomic_json(path / 'session.json', state)
        for thread in threads:
            thread.join(timeout=5)
        receipt.update(state='interrupted' if interruption else ('completed' if code == 0 and not error else 'failed'),
                       exit_code=code, ended_at=time.time())
        if interruption:
            receipt['interruption'] = interruption
        if error:
            receipt['error'] = error
        try:
            receipt['candidate_after'] = project.candidate(repo)
        except (ResourceError, OSError) as exception:
            receipt['candidate_error'] = str(exception)
        atomic_json(directory / 'receipt.json', receipt)
    return receipt


def supervise(path):
    # This lock prevents simultaneous recovery while the owner is initializing.
    with lock(path / 'owner.lock'):
        state = read(path / 'session.json')
        state['supervisor'] = process_identity(os.getpid())
        atomic_json(path / 'session.json', state)
        manager, backend, bindings = None, None, {}
        def stop(number, _frame):
            atomic_json(path / 'close.json', {'reason': 'signal ' + str(number)})
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, stop)
        try:
            profile = state['descriptor']['profiles'][state['profile']]
            if profile['resources'] != 'none':
                manager, backend = manager_for(state)
                deadline = min(state['expires_at'], time.time() + manager.config['limits']['wait_seconds'])
                while True:
                    if (path / 'close.json').exists() or time.time() >= state['expires_at']:
                        raise ResourceError('session closed before allocation')
                    try:
                        resource = manager.acquire(profile['resources'])
                        state['resource_run'] = resource['id']
                        atomic_json(path / 'session.json', state)
                        bindings = backend.bindings(manager.environment(resource['id']), resource)
                        break
                    except CapacityWait:
                        if time.time() >= deadline:
                            raise
                        time.sleep(0.1)
            state.update(state='ready', resource_availability='allocated' if manager else 'not_required')
            atomic_json(path / 'session.json', state)
            while time.time() < state['expires_at'] and not (path / 'close.json').exists():
                if (path / 'request.json').exists():
                    request = read(path / 'request.json')
                    execute(path, state, request, manager, backend, bindings)
                    (path / 'request.json').unlink()
                    if state['state'] == 'cleanup_pending':
                        break
                time.sleep(0.05)
        except Exception as error:
            state['error'] = str(error)
        finally:
            try:
                finish_interrupted(path, 'session owner stopping')
                if manager and state.get('resource_run'):
                    manager.release(state['resource_run'])
                state.update(state='expired' if time.time() >= state['expires_at'] else 'closed', cleanup='passed', closed_at=time.time())
            except Exception as error:
                state.update(state='cleanup_pending', cleanup='failed', error=str(error))
            if state['cleanup'] == 'passed':
                discard_runtime(path)
            atomic_json(path / 'session.json', state)


def close(path):
    state = read(path / 'session.json')
    if state['cleanup'] == 'passed' and state['state'] in ('closed', 'expired'):
        return state
    atomic_json(path / 'close.json', {'reason': 'explicit close'})
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        state = read(path / 'session.json')
        if state['state'] in ('closed', 'expired'):
            return state
        if state.get('supervisor') and not alive(state['supervisor']):
            return recover(path)
        if state['state'] == 'cleanup_pending':
            return state
        time.sleep(0.05)
    state = read(path / 'session.json')
    return {**state, 'cleanup': 'pending', 'error': 'cleanup is still running; observe or close again'}


def run(path, operation, arguments):
    with lock(path / 'request.lock', blocking=False):
        state = read(path / 'session.json')
        current_contract(state)
        if operation not in state['descriptor']['operations']:
            raise ResourceError('unknown operation: ' + operation)
        if state['state'] != 'ready' or time.time() >= state['expires_at'] or not alive(state['supervisor']):
            raise ResourceError('session is not ready; observe or close it')
        if (path / 'request.json').exists():
            raise ResourceError('session busy; observe the active execution')
        with lock(path.parent / 'admission.lock'):
            storage_admission(path.parent, state['descriptor'], 2 * MAX_LOG_BYTES + 65536)
        if len(list((path / 'executions').iterdir())) >= MAX_EXECUTIONS:
            raise ResourceError('session execution limit reached; close and open a new session')
        if len(arguments) > 256 or any('\0' in arg or len(arg) > 8192 for arg in arguments):
            raise ResourceError('invalid operation arguments')
        identifier = uuid.uuid4().hex
        atomic_json(path / 'request.json', {'execution_id': identifier, 'operation': operation, 'arguments': arguments})
        receipt_path = path / 'executions' / identifier / 'receipt.json'
        while True:
            if receipt_path.exists():
                receipt = read(receipt_path)
                if receipt['state'] != 'running' and not (path / 'request.json').exists():
                    return receipt
            latest = read(path / 'session.json')
            if not alive(state['supervisor']):
                recover(path)
                if receipt_path.exists():
                    return read(receipt_path)
                raise ResourceError('session stopped before operation began')
            if latest['state'] in ('closed', 'expired') and not receipt_path.exists():
                raise ResourceError(latest.get('error', 'session closed before operation began'))
            time.sleep(0.05)


def observe(path, execution):
    state = read(path / 'session.json')
    if state['state'] not in ('closed', 'expired') and state.get('supervisor') and not alive(state['supervisor']):
        state = recover(path)
    if execution:
        if not re.fullmatch('[a-f0-9]{32}', execution):
            raise ResourceError('invalid execution ID')
        receipt_path = path / 'executions' / execution / 'receipt.json'
        if not receipt_path.exists():
            raise ResourceError('execution does not belong to session')
        result = read(receipt_path)
        result['log_tail'] = {}
        for name, filename in result['artifacts'].items():
            try:
                with open(filename, 'rb') as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 4096))
                    result['log_tail'][name] = stream.read().decode(errors='replace')
            except FileNotFoundError:
                result['log_tail'][name] = ''
        return result
    return {**public_session(state), 'executions': [read(item) for item in sorted(path.glob('executions/*/receipt.json'))]}


def verify(repo, args):
    descriptor, digest = project.load(repo)
    initial = project.candidate(repo)
    compare = args.compare_ref or os.environ.get('VERIFY_COMPARE_REF')
    if args.mode in ('candidate', 'release') and not initial['clean']:
        raise ResourceError('candidate/release verification requires a clean checkout')
    if not compare:
        if args.mode != 'change':
            raise ResourceError('candidate/release requires a resolvable --compare-ref')
        compare = 'HEAD'
    from lib.change_scope import resolve_commit
    try:
        compare_sha = resolve_commit(repo, compare)
    except RuntimeError as error:
        raise ResourceError(str(error)) from error
    bound = {'engine_version': VERSION, 'engine_sha256': ENGINE_DIGEST, 'candidate': initial, 'compare_sha': compare_sha, 'descriptor_sha256': digest}
    artifact_dir = private(project_root(repo) / 'checks' / uuid.uuid4().hex)
    boundary_env = dict(os.environ, HARNESS_EXTERNAL_ENGINE='1')
    if args.mode == 'release':
        boundary_env['AI_BOUNDARY_APPROVAL_MODE'] = 'required'
    boundary = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).parent / 'check_ai_boundaries.py'),
                               '--repo', str(repo), '--base', compare_sha, '--mode', 'change' if args.mode == 'change' else 'release',
                               '--artifact-dir', str(artifact_dir)], env=boundary_env, capture_output=True, text=True)
    report = read(artifact_dir / 'ai_boundaries.json')
    if boundary.returncode:
        return {'schema_version': 1, **bound, 'status': 'failed', 'engineering_status': 'not_started',
                'approval_status': 'satisfied' if report['approval_satisfied'] else 'pending',
                'cleanup': 'not_started', 'boundaries': str(artifact_dir / 'ai_boundaries.json'),
                'error': '; '.join(report['errors']) or 'boundary authorization failed'}
    if project.candidate(repo) != initial or project.load(repo)[1] != digest:
        raise ResourceError('candidate or descriptor changed during boundary verification')
    path, state = create(repo, args.profile, args.goal, {'HARNESS_COMPARE_SHA': compare_sha, 'VERIFY_COMPARE_REF': compare_sha})
    result = {'schema_version': 1, 'session_id': state['session_id'], **bound, 'goal_sha256': state['goal_sha256'],
              'mode': args.mode, 'status': 'failed', 'engineering_status': 'failed', 'approval_status': 'unknown',
              'executions': [], 'criteria': [], 'scope': (state['goal'] or {}).get('scope', 'named project checks')}
    try:
        result['boundaries'] = str(artifact_dir / 'ai_boundaries.json')
        result['approval_status'] = 'satisfied' if report['approval_satisfied'] else 'pending'
        current_contract(state)
        if project.candidate(repo) != initial:
            raise ResourceError('candidate changed during boundary verification')
        criteria = (state['goal'] or {}).get('criteria', [])
        operations = list(dict.fromkeys(descriptor['verification'][args.mode] + [criterion['operation'] for criterion in criteria]))
        receipts = {}
        for operation in operations:
            receipts[operation] = run(path, operation, [])
            result['executions'].append(receipts[operation])
            if receipts[operation]['candidate'] != initial or receipts[operation].get('candidate_after') != initial:
                raise ResourceError('candidate changed during operation ' + operation)
            current_contract(state)
        for criterion in criteria:
            receipt = receipts[criterion['operation']]
            result['criteria'].append({'id': criterion['id'], 'description': criterion['description'],
                                       'operation': criterion['operation'], 'execution_id': receipt['execution_id'],
                                       'status': 'passed' if receipt['state'] == 'completed' and receipt['exit_code'] == 0 else 'failed'})
        current_contract(state)
        if project.candidate(repo) != initial:
            raise ResourceError('candidate changed during verification')
        if all(item['state'] == 'completed' and item['exit_code'] == 0 for item in receipts.values()):
            result.update(engineering_status='passed', status='passed' if result['approval_status'] == 'satisfied' else 'pending_approval')
        else:
            result['error'] = 'one or more required operations failed'
    except (ResourceError, OSError, ValueError) as error:
        result['error'] = str(error)
    finally:
        closed = close(path)
        result['cleanup'] = closed['cleanup']
        if result['cleanup'] != 'passed':
            result['status'] = 'failed'
            result['error'] = closed.get('error', 'cleanup incomplete')
        atomic_json(path / 'verification.json', result)
        result['receipt'] = str(path / 'verification.json')
    return result


def verification_with_export(repo, args):
    destination = None
    if args.output:
        requested = Path(args.output)
        requested = requested if requested.is_absolute() else repo / requested
        if requested.is_symlink() or requested.exists() or '..' in requested.parts:
            raise ResourceError('verification output must be a fresh non-symlink directory')
        destination = requested.parent.resolve() / requested.name
        if destination.is_relative_to(repo):
            ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '--quiet', str(destination)], capture_output=True)
            if ignored.returncode != 0:
                raise ResourceError('verification output inside the worktree must be Git-ignored; use an ignored directory or an external path')
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ResourceError('verification output already exists; choose a new directory') from error
    try:
        result = verify(repo, args)
    except (ResourceError, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        result = {'schema_version': 1, 'status': 'failed', 'error': str(error), 'engine_version': VERSION,
                  'engine_sha256': ENGINE_DIGEST, 'engineering_status': 'not_started', 'cleanup': 'not_started',
                  **getattr(error, 'details', {})}
    if destination is None:
        return result
    portable = json.loads(json.dumps(result))
    portable['receipt'] = 'verification.json'
    if 'boundaries' in portable:
        boundary = read(Path(portable['boundaries']))
        atomic_json(destination / 'boundaries.json', boundary)
        portable['boundaries'] = 'boundaries.json'
    for receipt in portable.get('executions', []):
        execution_root = private(destination / 'executions' / receipt['execution_id'])
        relative_root = Path('executions') / receipt['execution_id']
        for name in ('stdout', 'stderr'):
            # Only these two already-redacted engine outputs are public. Native
            # artifacts may contain secrets and require their own export policy.
            source = Path(receipt['artifacts'][name])
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LOG_BYTES + 128:
                raise ResourceError('public execution output is not a bounded regular log')
            raw = source.read_bytes()
            with (execution_root / name).open('xb') as output:
                os.chmod(execution_root / name, 0o600)
                output.write(raw)
            receipt['artifacts'][name] = str(relative_root / name)
        stored = json.loads(json.dumps(receipt))
        stored['artifacts'] = {'stdout': 'stdout', 'stderr': 'stderr'}
        atomic_json(execution_root / 'receipt.json', stored)
    # Written last: a partial copy is never mistaken for a completed bundle.
    atomic_json(destination / 'verification.json', portable)
    return {**portable, 'receipt': str(destination / 'verification.json'), 'bundle': str(destination)}


def install(repo, dry_run):
    descriptor, digest = project.load(repo)
    selected = descriptor['tools']
    if not dry_run:
        target = repo / '.tools/bin'
        if any(parent.is_symlink() for parent in (target, target.parent)):
            raise ResourceError('tool directory must not be a symlink')
        target.mkdir(parents=True, exist_ok=True)
        for name, version in selected.items():
            if (target / name).is_symlink():
                raise ResourceError('tool must not be a symlink')
            result = subprocess.run(['go', 'install', project.TOOLS[name] + '@' + version], env={**os.environ, 'GOBIN': str(target)}, capture_output=True, timeout=600)
            if result.returncode:
                raise ResourceError('tool installation failed: ' + name)
    return {'schema_version': 1, 'tools': selected, 'descriptor_sha256': digest, 'dry_run': dry_run}


def inspect_boundary_or_scope(repo, args):
    filename = 'check_ai_boundaries.py' if args.command == 'check' else 'collect_changes.py'
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).parent / filename), '--repo', str(repo), *args.arguments],
                            capture_output=True, text=True, env={**os.environ, 'HARNESS_EXTERNAL_ENGINE': '1'})
    payload = {'schema_version': 1, 'exit_code': result.returncode, 'status': 'passed' if result.returncode == 0 else 'failed'}
    if args.command == 'collect' and result.returncode == 0:
        try:
            payload['snapshot'] = json.loads(result.stdout)
        except ValueError:
            payload['output'] = result.stdout
    else:
        payload['output'] = result.stdout
    if result.returncode:
        payload['error'] = result.stderr.strip() or result.stdout.strip() or 'inspection failed'
    return payload


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ResourceError(message)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '_supervise':
        supervise(Path(sys.argv[2]))
        return 0
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    commands.add_parser('describe')
    for name, operation in (('check', 'boundaries'), ('collect', 'changes')):
        inspection = commands.add_parser(name)
        inspection.add_argument('operation', choices=(operation,))
        inspection.add_argument('arguments', nargs=argparse.REMAINDER)
    opening = commands.add_parser('open')
    opening.add_argument('--profile', default='development')
    opening.add_argument('--goal')
    running = commands.add_parser('run')
    running.add_argument('--session', required=True)
    running.add_argument('--operation', required=True)
    running.add_argument('arguments', nargs=argparse.REMAINDER)
    observation = commands.add_parser('observe')
    observation.add_argument('--session', required=True)
    observation.add_argument('--execution')
    closing = commands.add_parser('close')
    closing.add_argument('--session', required=True)
    verification = commands.add_parser('verify')
    verification.add_argument('mode', choices=('change', 'candidate', 'release'))
    verification.add_argument('--profile', default='development')
    verification.add_argument('--goal')
    verification.add_argument('--compare-ref')
    verification.add_argument('--output')
    installation = commands.add_parser('install-tools')
    installation.add_argument('--dry-run', action='store_true')
    def cancelled(number, _frame):
        raise ResourceError('invocation interrupted by signal ' + str(number))
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, cancelled)
    try:
        args = parser.parse_args()
        repo = repository()
        if args.command == 'describe':
            descriptor, digest = project.load(repo)
            result = {'schema_version': 1, 'project': descriptor['project'], 'descriptor_sha256': digest,
                      'profiles': descriptor['profiles'], **project.availability(repo, descriptor)}
        elif args.command in ('check', 'collect'):
            result = inspect_boundary_or_scope(repo, args)
        elif args.command == 'open':
            _, state = create(repo, args.profile, args.goal)
            result = public_session(state)
        elif args.command == 'verify':
            result = verification_with_export(repo, args)
        elif args.command == 'install-tools':
            result = install(repo, args.dry_run)
        else:
            path = session_dir(repo, args.session, cleanup_only=args.command in ('close', 'observe'))
            if args.command == 'run':
                arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
                result = run(path, args.operation, arguments)
            elif args.command == 'observe':
                result = observe(path, args.execution)
            else:
                result = public_session(close(path))
        print(json.dumps(result, sort_keys=True))
        if args.command in ('check', 'collect'):
            return result['exit_code']
        if args.command == 'run':
            return 0 if result['state'] == 'completed' and result['exit_code'] == 0 else (result['exit_code'] or 1)
        if args.command == 'verify':
            return 0 if result['status'] in ('passed', 'pending_approval') else 1
        if args.command == 'close':
            return 0 if result['cleanup'] == 'passed' else 1
        return 0
    except (ResourceError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'schema_version': 1, 'error': str(error), **getattr(error, 'details', {})}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
