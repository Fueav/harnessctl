#!/usr/bin/env python3
"""Lifecycle contracts: no Docker or application source required by the consumer."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.resources import Manager, ResourceError, default_config, fingerprint, process_identity, validate_config
from lib.resource_docker import Docker, LABEL, stop_process_group


class FakeDocker:
    def __init__(self):
        self.environments = {}
        self.runs = {}
        self.created = 0
        self.fail_cleanup = False
        self.bytes = 0
        self.stopped = set()

    def resolve(self, config):
        return {name: 'sha256:' + config[name]['image'] for name in ('postgres', 'redis')}

    def create(self, env):
        self.created += 1
        self.environments[env['id']] = copy.deepcopy(env)

    def health(self, env):
        if env['id'] not in self.environments or self.unavailable(env):
            raise ResourceError('missing managed environment')

    def unavailable(self, env):
        return env['id'] in self.stopped

    def allocate(self, env, run):
        self.runs[run['id']] = (env['id'], run['database'], tuple(run['redis_dbs']))

    def cleanup(self, env, run):
        if self.fail_cleanup or self.unavailable(env):
            raise ResourceError('injected cleanup failure')
        self.stop_writers(env, run)
        self.runs.pop(run['id'], None)

    def stop_writers(self, env, run):
        stop_process_group(run.get('child'))

    def bindings(self, env, run):
        return {}

    def discard_run(self, env, run):
        self.stop_writers(env, run)
        self.runs.pop(run['id'], None)

    def destroy(self, env):
        if any(item[0] == env['id'] for item in self.runs.values()):
            raise AssertionError('destroyed environment in use')
        self.environments.pop(env['id'], None)

    def usage(self, env):
        return {'storage_bytes': self.bytes, 'postgres_bytes': self.bytes, 'redis_bytes': 0}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = FakeDocker()
        self.config = default_config()
        self.config['limits']['min_free_bytes'] = 0
        self.config['limits']['max_runs'] = 10
        self.config['limits']['max_environments'] = 3
        self.manager = Manager(self.root, 'a' * 24, self.backend, self.config)

    def test_hundred_serial_runs_reuse_services_without_retaining_databases(self):
        for _ in range(100):
            run = self.manager.acquire('shared')
            self.manager.release(run['id'])
            self.assertEqual(self.backend.runs, {})
            self.assertEqual(self.manager.status()['runs'], [])
        self.assertEqual(self.backend.created, 1)
        self.assertEqual(len(self.backend.environments), 1)

    def test_ten_active_runs_have_separate_data_and_cannot_delete_each_other(self):
        runs = [self.manager.acquire('shared') for _ in range(10)]
        self.assertEqual(self.backend.created, 1)
        self.assertEqual(len({r['database'] for r in runs}), 10)
        self.assertEqual(len({db for r in runs for db in r['redis_dbs']}), 20)
        with self.assertRaisesRegex(ResourceError, 'capacity'):
            self.manager.acquire('shared')
        self.manager.release(runs[0]['id'])
        self.assertEqual(len(self.backend.runs), 9)
        self.assertEqual(len(self.backend.environments), 1)

    def test_concurrent_allocations_are_serialized_without_duplicate_services(self):
        with ThreadPoolExecutor(max_workers=10) as executor:
            runs = list(executor.map(lambda _: self.manager.acquire('shared'), range(10)))
        self.assertEqual(self.backend.created, 1)
        self.assertEqual(len({r['database'] for r in runs}), 10)
        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(lambda run: self.manager.release(run['id']), runs))
        self.assertEqual(self.backend.runs, {})

    def test_corrupt_redis_slot_cannot_clear_another_database(self):
        run = self.manager.acquire('shared')
        state = json.loads((self.root / 'state.json').read_text())
        state['runs'][run['id']]['redis_dbs'] = [0, 1]
        (self.root / 'state.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(ResourceError, 'state'):
            self.manager.release(run['id'])
        self.assertIn(run['id'], self.backend.runs)

    def test_dependency_changes_split_environments_but_do_not_replace_active_ones(self):
        first = self.manager.acquire('shared')
        changed = copy.deepcopy(self.config)
        changed['postgres']['image'] = 'postgres:17-alpine'
        second_manager = Manager(self.root, 'a' * 24, self.backend, changed)
        second = second_manager.acquire('shared')
        self.assertNotEqual(first['environment'], second['environment'])
        self.assertEqual(len(self.backend.environments), 2)
        self.manager.release(first['id'])
        second_manager.release(second['id'])
        self.assertEqual(len(self.backend.environments), 1)

    def test_cleanup_failure_is_recorded_and_blocks_new_allocations(self):
        run = self.manager.acquire('shared')
        self.backend.fail_cleanup = True
        with self.assertRaisesRegex(ResourceError, 'cleanup'):
            self.manager.release(run['id'])
        self.assertEqual(self.manager.status()['runs'][0]['state'], 'cleanup_pending')
        with self.assertRaisesRegex(ResourceError, 'cleanup'):
            self.manager.acquire('shared')
        self.assertEqual(self.backend.created, 1)
        self.backend.fail_cleanup = False
        self.manager.gc()
        self.assertEqual(self.backend.runs, {})

    def test_dead_owner_is_reaped_before_allocation_but_live_owner_is_preserved(self):
        live = self.manager.acquire('shared')
        dead = self.manager.acquire('shared')
        state = json.loads((self.root / 'state.json').read_text())
        state['runs'][dead['id']]['owner'] = {'pid': 2147483647, 'started': 'gone', 'boot': 'gone'}
        (self.root / 'state.json').write_text(json.dumps(state))
        next_run = self.manager.acquire('shared')
        self.assertNotIn(dead['id'], self.backend.runs)
        self.assertIn(live['id'], self.backend.runs)
        self.assertIn(next_run['id'], self.backend.runs)

    def test_fresh_mode_leaves_no_environment_or_volume(self):
        run = self.manager.acquire('fresh')
        self.manager.release(run['id'])
        self.assertEqual(self.backend.runs, {})
        self.assertEqual(self.backend.environments, {})

    def test_storage_pressure_blocks_new_work_without_deleting_live_data(self):
        run = self.manager.acquire('shared')
        self.backend.bytes = self.config['limits']['max_storage_bytes'] + 1
        with self.assertRaisesRegex(ResourceError, 'storage'):
            self.manager.acquire('shared')
        self.assertIn(run['id'], self.backend.runs)
        self.assertEqual(len(self.backend.environments), 1)

    def test_gc_requires_recorded_ownership_and_rejects_corrupt_state(self):
        (self.root / 'state.json').write_text('{broken')
        with self.assertRaises(ResourceError):
            self.manager.gc()
        self.assertEqual(self.backend.created, 0)

    def test_gc_all_only_removes_idle_owned_resources(self):
        run = self.manager.acquire('shared')
        self.manager.gc(all_idle=True)
        self.assertIn(run['id'], self.backend.runs)
        self.manager.release(run['id'])
        self.manager.gc(all_idle=True)
        self.assertEqual(self.backend.environments, {})

    def test_reboot_recovery_discards_stopped_environment_only_after_all_owners_are_dead(self):
        run = self.manager.acquire('shared')
        state = json.loads(self.manager.path.read_text())
        state['runs'][run['id']]['owner'] = {'pid': 2147483647, 'started': 'gone', 'boot': 'gone'}
        self.manager.path.write_text(json.dumps(state))
        self.backend.stopped.add(run['environment'])
        new_run = self.manager.acquire('shared')
        self.assertNotEqual(new_run['environment'], run['environment'])
        self.assertNotIn(run['environment'], self.backend.environments)
        self.assertNotIn(run['id'], self.backend.runs)

    def test_service_failure_does_not_discard_another_live_run(self):
        live = self.manager.acquire('shared')
        dead = self.manager.acquire('shared')
        state = json.loads(self.manager.path.read_text())
        state['runs'][dead['id']]['owner'] = {'pid': 2147483647, 'started': 'gone', 'boot': 'gone'}
        self.manager.path.write_text(json.dumps(state))
        self.backend.stopped.add(dead['environment'])
        with self.assertRaisesRegex(ResourceError, 'cleanup'):
            self.manager.gc()
        self.assertIn(live['id'], self.backend.runs)
        self.assertIn(live['environment'], self.backend.environments)

    def test_storage_pressure_recycles_only_idle_environments_before_admission(self):
        run = self.manager.acquire('shared')
        self.manager.release(run['id'])
        with patch.object(self.backend, 'usage', side_effect=lambda env: {
                'storage_bytes': self.config['limits']['max_storage_bytes'] if env['id'] == run['environment'] else 0}):
            replacement = self.manager.acquire('shared')
        self.assertNotEqual(replacement['environment'], run['environment'])
        self.assertNotIn(run['environment'], self.backend.environments)

    def test_initial_environment_cost_is_checked_before_allocating_task_data(self):
        self.backend.bytes = self.config['limits']['max_storage_bytes']
        with self.assertRaisesRegex(ResourceError, 'storage'):
            self.manager.acquire('shared')
        self.assertEqual(self.backend.runs, {})
        self.assertEqual(self.backend.environments, {})

    def test_debug_retention_has_a_count_limit_and_expires_without_reusing_dirty_slots(self):
        retained = self.manager.acquire('shared')
        self.manager.release(retained['id'], retain_seconds=10)
        active = self.manager.acquire('shared')
        self.assertTrue(set(active['redis_dbs']).isdisjoint(retained['redis_dbs']))
        with self.assertRaisesRegex(ResourceError, 'retention capacity'):
            self.manager.release(active['id'], retain_seconds=10)
        self.manager.release(active['id'])
        self.manager.gc()
        self.assertIn(retained['id'], self.backend.runs)
        with patch('lib.resources.time.time', return_value=time.time() + 11):
            self.manager.gc()
        self.assertEqual(self.backend.runs, {})

    def test_debug_retention_cannot_preserve_data_above_the_storage_budget(self):
        run = self.manager.acquire('shared')
        self.backend.bytes = self.config['limits']['max_storage_bytes']
        with self.assertRaisesRegex(ResourceError, 'storage'):
            self.manager.release(run['id'], retain_seconds=10)
        self.assertNotEqual(self.manager.status()['runs'][0]['state'], 'retained')

    def test_config_schema_requires_an_integer_version(self):
        for version in (True, 1.0, '1'):
            with self.subTest(version=version), self.assertRaises(ResourceError):
                validate_config({'schema_version': version})

    def test_fingerprint_includes_effective_images_and_configuration(self):
        images = self.backend.resolve(self.config)
        first = fingerprint(self.config, images)
        changed = copy.deepcopy(self.config)
        changed['postgres']['memory_mb'] += 1
        self.assertNotEqual(first, fingerprint(changed, images))
        self.assertNotEqual(first, fingerprint(self.config, {**images, 'redis': 'different'}))

    def test_process_identity_distinguishes_pid_reuse(self):
        current = process_identity(os.getpid())
        self.assertEqual(current['pid'], os.getpid())
        self.assertTrue(current['started'])
        self.assertTrue(current['boot'])


class DockerOwnershipTests(unittest.TestCase):
    def test_exited_group_is_not_reported_as_permission_failure_on_macos(self):
        child = {'pid': 1234, 'started': 'same', 'boot': 'same'}
        live = type('Result', (), {'returncode': 0, 'stdout': '1234 1234 S\n'})()
        exited = type('Result', (), {'returncode': 0, 'stdout': '1234 1234 Z\n'})()
        with patch('lib.resource_docker.process_identity', return_value=child), \
             patch('lib.resource_docker.subprocess.run', side_effect=[live, exited, exited]), \
             patch('lib.resource_docker.os.killpg', side_effect=[None, PermissionError(1, 'Operation not permitted')]):
            stop_process_group(child)

    def test_docker_missing_volume_is_an_idempotent_success(self):
        result = type('Result', (), {'returncode': 1, 'stdout': '',
                                    'stderr': 'Error response from daemon: get fixture: no such volume'})()
        with patch('lib.resource_docker.subprocess.run', return_value=result):
            self.assertIsNone(Docker(Path('/unused')).call(['volume', 'inspect', 'fixture'], missing=True))

    def test_docker_connectivity_failure_is_never_treated_as_missing_resource(self):
        result = type('Result', (), {'returncode': 1, 'stdout': '',
                                    'stderr': 'Cannot connect to the Docker daemon'})()
        with patch('lib.resource_docker.subprocess.run', return_value=result):
            with self.assertRaises(ResourceError):
                Docker(Path('/unused')).call(['volume', 'inspect', 'fixture'], missing=True)

    def test_foreign_volume_cannot_be_adopted_or_removed(self):
        backend = Docker(Path('/unused'))
        with patch.object(backend, 'call', return_value=json.dumps({'Labels': {LABEL + 'project': 'foreign'}})) as call:
            with self.assertRaisesRegex(ResourceError, 'ownership'):
                backend.inspect('volume', 'fixture', {'project': 'ours', 'id': 'env'}, 'postgres')
        self.assertEqual(call.call_count, 1)


class SupervisorTests(unittest.TestCase):
    def test_dead_invoking_harness_process_cannot_start_new_work(self):
        import argparse
        import resources
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeDocker()
            manager = Manager(directory, 'a' * 24, backend, default_config())
            args = argparse.Namespace(mode='shared', retain_on_failure=0, command=[sys.executable, '-c', 'pass'])
            with patch.dict(os.environ, {'HARNESSCTL_OWNER_PID': '2147483647'}), self.assertRaises(ResourceError):
                resources.execute(manager, backend, args)
            self.assertEqual(backend.created, 0)

    def test_cleanup_failure_does_not_relabel_a_successful_command_as_failed(self):
        import argparse
        import resources
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeDocker()
            backend.fail_cleanup = True
            manager = Manager(directory, 'a' * 24, backend, default_config())
            args = argparse.Namespace(mode='shared', retain_on_failure=0, command=[sys.executable, '-c', 'pass'])
            self.assertNotEqual(resources.execute(manager, backend, args), 0)
            result = json.loads((Path(directory) / 'last-result.json').read_text())
            self.assertEqual(result['command_exit_code'], 0)
            self.assertEqual(result['cleanup'], 'failed')

    def test_unattributed_project_storage_pressure_does_not_kill_an_active_command(self):
        import argparse
        import resources
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeDocker()
            config = default_config()
            config['limits']['sample_seconds'] = 1
            manager = Manager(directory, 'a' * 24, backend, config)
            args = argparse.Namespace(mode='shared', retain_on_failure=0,
                                      command=[sys.executable, '-c', 'import time; time.sleep(1.3)'])
            with patch.object(manager, 'check_budget', side_effect=resources.BudgetExceeded('project test storage capacity exceeded')):
                self.assertEqual(resources.execute(manager, backend, args), 0)
            result = json.loads((Path(directory) / 'last-result.json').read_text())
            self.assertTrue(result['budget_pressure'])

    def test_signal_stops_child_and_preserves_exit_code_after_cleanup(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as directory:
                script = '''import argparse, pathlib, sys
from resources_test import FakeDocker
from lib.resources import Manager, default_config
from resources import execute
backend=FakeDocker()
config=default_config(); config['limits']['min_free_bytes']=0
manager=Manager(pathlib.Path(sys.argv[1]), 'a'*24, backend, config)
args=argparse.Namespace(mode='shared', retain_on_failure=0, command=[sys.executable,'-c','import time; time.sleep(30)'])
sys.exit(execute(manager, backend, args))
'''
                process = subprocess.Popen([sys.executable, '-c', script, directory],
                                           cwd=Path(__file__).parent, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        path = Path(directory) / 'state.json'
                        if path.exists() and any(run.get('child') for run in json.loads(path.read_text())['runs'].values()):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail('supervisor did not register its child')
                    process.send_signal(sig)
                    _, error = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + sig, error)
                    self.assertEqual(json.loads(path.read_text())['runs'], {})
                    self.assertEqual(json.loads((Path(directory) / 'last-result.json').read_text())['cleanup'], 'passed')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()


if __name__ == '__main__':
    unittest.main()
