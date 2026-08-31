#!/usr/bin/env python3
"""Run a command with owned test services; reclaim data before reporting success."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from lib.resource_docker import Docker, stop_process_group
from lib.resources import BudgetExceeded, CapacityWait, Manager, ResourceError, process_identity, project_identity, validate_config


def execute(manager, backend, args):
    parent = process_identity(int(os.environ.get('HARNESSCTL_OWNER_PID', os.getppid())))
    if parent is None:
        raise ResourceError('invoking Harness process is no longer alive')
    interrupted = [0]
    def on_signal(number, _frame):
        interrupted[0] = number
    previous = {number: signal.signal(number, on_signal) for number in (signal.SIGINT, signal.SIGTERM)}
    run, child, code, cleanup, pressure = None, None, 1, 'not_started', False
    started = time.monotonic()
    try:
        deadline = time.monotonic() + manager.config['limits']['wait_seconds']
        while True:
            if interrupted[0]:
                return 128 + interrupted[0]
            try:
                run = manager.acquire(args.mode)
                break
            except CapacityWait:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        environment = manager.environment(run['id'])
        injected = backend.bindings(environment, run)
        if interrupted[0] or process_identity(parent['pid']) != parent:
            code = 128 + (interrupted[0] or signal.SIGTERM)
            raise ResourceError('invocation ended before workload launch')
        command_env = {**os.environ, **injected}
        read_fd, write_fd = os.pipe()
        # The child cannot execute user code until its process identity is durable.
        bootstrap = 'import os,sys; fd=int(sys.argv[1]); ok=os.read(fd,1); os.close(fd); ' \
                    'sys.exit(125) if ok != b"1" else os.execvpe(sys.argv[2],sys.argv[2:],os.environ)'
        try:
            child = subprocess.Popen([sys.executable, '-c', bootstrap, str(read_fd), *args.command],
                                     env=command_env, start_new_session=True, pass_fds=(read_fd,))
            identity = process_identity(child.pid)
            if identity is None:
                raise ResourceError('workload exited before ownership registration')
            manager.attach(run['id'], identity)
            os.write(write_fd, b'1')
        finally:
            os.close(read_fd)
            os.close(write_fd)
        next_sample = time.monotonic() + manager.config['limits']['sample_seconds']
        while child.poll() is None:
            if parent is not None and process_identity(parent['pid']) != parent:
                interrupted[0] = signal.SIGTERM
            if interrupted[0] or time.monotonic() - started > manager.config['limits']['max_run_seconds']:
                code = 128 + interrupted[0] if interrupted[0] else 124
                stop_process_group(identity)
                child.wait(timeout=5)
                break
            if time.monotonic() >= next_sample:
                try:
                    manager.check_budget()
                except BudgetExceeded as error:
                    # Project totals cannot identify which concurrent task wrote
                    # the data. Block admission, but do not kill arbitrary peers.
                    if not pressure:
                        print('resources: admission blocked: ' + str(error), file=sys.stderr)
                    pressure = True
                next_sample = time.monotonic() + manager.config['limits']['sample_seconds']
            time.sleep(0.1)
        else:
            code = child.returncode if child.returncode >= 0 else 128 - child.returncode
        if interrupted[0]:
            code = 128 + interrupted[0]
    except (ResourceError, OSError, subprocess.TimeoutExpired) as error:
        print('resources: ' + str(error), file=sys.stderr)
    finally:
        if run:
            command_code = code
            try:
                manager.release(run['id'], args.retain_on_failure if code else 0)
                cleanup = 'retained' if code and args.retain_on_failure else 'passed'
            except (ResourceError, OSError) as error:
                cleanup = 'failed'
                print('resources: ' + str(error), file=sys.stderr)
                code = code or 1
            result = {'schema_version': 1, 'run_id': run['id'], 'command_exit_code': command_code,
                      'exit_code': code, 'cleanup': cleanup, 'budget_pressure': pressure,
                      'elapsed_seconds': round(time.monotonic() - started, 3)}
            # One bounded summary; no credentials or unbounded per-run history.
            with manager.locked():
                path = manager.root / 'last-result.json'
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(result, output, sort_keys=True)
                    output.write('\n')
            print('resources: ' + json.dumps(result, sort_keys=True), file=sys.stderr)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for number, handler in previous.items():
            signal.signal(number, handler)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('run', 'status', 'gc'))
    parser.add_argument('--config', default='harness/dependencies.json')
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--mode', choices=('shared', 'fresh'), default='shared')
    parser.add_argument('--retain-on-failure', type=int, default=0)
    parser.add_argument('--all-idle', action='store_true')
    argv = sys.argv[1:]
    command = []
    if '--' in argv:
        separator = argv.index('--')
        argv, command = argv[:separator], argv[separator + 1:]
    args = parser.parse_args(argv)
    args.command = command
    if (args.operation == 'run') != bool(command):
        parser.error('run requires -- COMMAND; other operations accept no command')
    if args.retain_on_failure < 0 or (args.operation != 'run' and args.retain_on_failure) or (args.operation == 'run' and args.all_idle):
        parser.error('invalid retention or cleanup options')
    try:
        config_path = Path(args.config)
        if config_path.is_symlink() or not config_path.is_file():
            raise ResourceError('a regular harness/dependencies.json isolation contract is required')
        config = validate_config(json.loads(config_path.read_text()))
        if not config['enabled'] and args.operation == 'run':
            os.execvpe(command[0], command, os.environ)
        base = args.state_root or Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'harnessctl/resources'
        backend = Docker(base)
        project = project_identity(Path.cwd(), backend.identity())
        root = base / project
        backend.root = root
        manager = Manager(root, project, backend, config)
        if args.operation == 'run':
            return execute(manager, backend, args)
        if args.operation == 'gc':
            manager.gc(all_idle=args.all_idle)
        result = manager.status()
        disk = Path.home() / 'Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw'
        result['docker_disk_allocated_bytes'] = disk.stat().st_blocks * 512 if disk.is_file() else None
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ResourceError, OSError, ValueError, KeyError) as error:
        print('resources: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
