#!/usr/bin/env python3
"""Actual PostgreSQL/Redis acceptance through only the public v1 interface."""
import json
import os
from pathlib import Path
import subprocess
import signal
import unittest
from workbench_test import WorkbenchContract

PAYLOAD = r'''
import json, os, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit
connection = urlsplit(os.environ['DATABASE_DSN'])
network = os.environ['HARNESS_RESOURCE_NETWORK']
service = network.removesuffix('-net')
mode = sys.argv[1]
env = {**os.environ, 'PGPASSWORD': connection.password, 'REDISCLI_AUTH': os.environ['REDIS_PASSWORD']}
def command(args):
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('database operation failed')
    return result.stdout.strip()
def sql(query):
    return command(['docker', 'exec', '-e', 'PGPASSWORD', service+'-postgres', 'psql', '-h', '127.0.0.1', '-X', '-v', 'ON_ERROR_STOP=1', '-U', connection.username, '-d', connection.path[1:], '-At', '-c', query])
def redis(*arguments):
    return command(['docker', 'exec', '-e', 'REDISCLI_AUTH', service+'-redis', 'redis-cli', '--no-auth-warning', '--user', os.environ['REDIS_USERNAME'], '-n', os.environ['REDIS_DB'], '--raw', *arguments])
if mode == 'write':
    sql('CREATE TABLE result(value integer); INSERT INTO result VALUES(42)')
    assert redis('SET', 'result', 'delivered') == 'OK'
elif mode == 'read':
    assert sql('SELECT value FROM result') == '42'
    assert redis('GET', 'result') == 'delivered'
elif mode == 'empty':
    assert sql("SELECT count(*) FROM information_schema.tables WHERE table_name='result'") == '0'
    assert redis('GET', 'result') == ''
elif mode == 'secret':
    print(os.environ['DATABASE_DSN'])
    print(os.environ['REDIS_PASSWORD'])
    Path(os.environ['HARNESS_ARTIFACT_DIR'], 'private-native.txt').write_text(os.environ['REDIS_PASSWORD'])
else:
    raise RuntimeError('unknown fixture mode')
print(json.dumps({'run':os.environ['HARNESS_RESOURCE_RUN_ID'], 'environment':os.environ['HARNESS_RESOURCE_ENVIRONMENT'], 'project':os.environ['HARNESS_RESOURCE_PROJECT'], 'mode':mode}))
'''


class DockerContract(WorkbenchContract):
    # Only this acceptance is inherited into the Docker suite.
    def test_live_session_data_isolation_and_cleanup(self):
        subprocess.run(['docker', 'info'], capture_output=True, check=True)
        (self.repo / 'app.py').write_text(PAYLOAD)
        self.descriptor['dependencies'] = {'enabled': True, 'limits': {'min_free_bytes': 0, 'wait_seconds': 0}}
        self.descriptor['profiles']['development'] = {'resources': 'shared', 'lease_seconds': 300}
        for operation in ('write', 'read', 'empty', 'secret'):
            self.descriptor['operations'][operation] = {'argv': ['python3', 'app.py', operation], 'timeout_seconds': 30}
        self.write('harness/project.json', self.descriptor)
        first, second = self.open(), self.open()
        written = self.run_operation(first, 'write')
        first_data = json.loads(Path(written['artifacts']['stdout']).read_text().splitlines()[-1])
        self.run_operation(first, 'read')
        empty = self.run_operation(second, 'empty')
        second_data = json.loads(Path(empty['artifacts']['stdout']).read_text().splitlines()[-1])
        self.assertEqual(first_data['environment'], second_data['environment'])
        self.assertNotEqual(first_data['run'], second_data['run'])
        redacted = self.run_operation(first, 'secret')
        text = Path(redacted['artifacts']['stdout']).read_text()
        self.assertIn('[REDACTED]', text)
        self.assertNotIn('postgres://', text)
        owner = self.call('observe', '--session', first)['supervisor']
        os.kill(owner['pid'], signal.SIGKILL)
        recovered = self.call('observe', '--session', first)
        self.assertEqual(recovered['state'], 'closed')
        self.assertEqual(recovered['cleanup'], 'passed')
        self.assertEqual(self.call('close', '--session', first)['cleanup'], 'passed')
        self.run_operation(second, 'empty')
        self.assertEqual(self.call('close', '--session', second)['cleanup'], 'passed')
        resource_files = list((Path(self.env['XDG_STATE_HOME']) / 'harnessctl/workbench-resources').glob('*/state.json'))
        self.assertEqual(len(resource_files), 1)
        state = json.loads(resource_files[0].read_text())
        self.assertEqual(state['runs'], {})
        self.assertEqual(state['environments'], {})
        self.descriptor['verification']['change'] = ['secret']
        self.write('harness/project.json', self.descriptor)
        destination = Path(self.temp.name).resolve() / 'docker-portable'
        exported = self.call('verify', 'change', '--compare-ref', 'HEAD', '--output', str(destination))
        self.assertEqual(exported['status'], 'passed')
        self.assertFalse(list(destination.rglob('private-native.txt')))
        self.assertFalse(list(destination.rglob('session.json')))
        for file in destination.rglob('*'):
            if file.is_file():
                self.assertNotIn('postgres://', file.read_text())
        state = json.loads(resource_files[0].read_text())
        self.assertEqual(state['runs'], {})
        self.assertEqual(state['environments'], {})
        for command in (['ps', '-aq'], ['network', 'ls', '-q'], ['volume', 'ls', '-q']):
            result = subprocess.run(['docker', *command, '--filter', 'label=io.harnessctl.project=' + first_data['project']], capture_output=True, text=True, check=True)
            self.assertFalse(result.stdout.strip(), result.stdout)


if __name__ == '__main__':
    suite = unittest.TestSuite([DockerContract('test_live_session_data_isolation_and_cleanup')])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
