#!/usr/bin/env python3
"""Exercise real Codex seatbelt permissions, including cross-call recovery."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import unittest

import workbench_test as contract


class SandboxLifecycle(unittest.TestCase):
    setUpClass = classmethod(contract.WorkbenchContract.setUpClass.__func__)
    tearDownClass = classmethod(contract.WorkbenchContract.tearDownClass.__func__)
    git = contract.WorkbenchContract.git
    write = contract.WorkbenchContract.write
    open = contract.WorkbenchContract.open
    run_operation = contract.WorkbenchContract.run_operation

    def setUp(self):
        contract.WorkbenchContract.setUp(self)
        root = Path(self.temp.name).resolve()
        temporary = root / 'tmp'
        temporary.mkdir()
        self.env['TMPDIR'] = str(temporary)
        permissions = ','.join(json.dumps(str(path)) + '="write"' for path in (root, self.repo.resolve() / '.git'))
        self.sandbox = ['codex', 'sandbox', '-P', 'harness_test', '-C', str(self.repo),
                        '-c', 'default_permissions="harness_test"',
                        '-c', 'permissions.harness_test={extends=":read-only",filesystem={' + permissions + '},network={enabled=false}}',
                        '-c', 'approval_policy="never"', '--']

    def command(self, *args):
        return [*self.sandbox, self.binary, *args, '--repo', str(self.repo)]

    def call(self, *args, ok=True):
        result = subprocess.run(self.command(*args), env=self.env, text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def cleanup_sessions(self):
        for session in self.sessions:
            subprocess.run(self.command('close', '--session', session), env=self.env, capture_output=True, timeout=40)

    def test_separate_invocations_success_failure_timeout_and_close(self):
        session = self.open()
        self.assertEqual(self.run_operation(session, 'test')['state'], 'completed')
        self.assertEqual(self.run_operation(session, 'fail', ok=False)['exit_code'], 3)
        self.assertEqual(self.run_operation(session, 'slow', ok=False)['exit_code'], 124)
        self.assertEqual(self.call('close', '--session', session)['cleanup'], 'passed')
        self.assertEqual(self.call('verify', 'change', '--compare-ref', 'HEAD')['status'], 'passed')

    def active_execution(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for path in Path(self.env['XDG_STATE_HOME']).glob('harnessctl/workbench/*/*/session.json'):
                for receipt in (path.parent / 'executions').glob('*/receipt.json'):
                    value = json.loads(receipt.read_text())
                    if value.get('child') and value['state'] == 'running':
                        return path, json.loads(path.read_text()), value
            time.sleep(0.02)
        self.fail('operation never registered')

    def test_cancel_verify_reclaims_operation(self):
        self.descriptor['verification']['change'] = ['slow']
        self.descriptor['operations']['slow']['timeout_seconds'] = 30
        self.write('harness/project.json', self.descriptor)
        controller = '''import json,os,signal,subprocess,sys,time
from pathlib import Path
p = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        receipts = Path(os.environ['XDG_STATE_HOME']).glob('harnessctl/workbench/*/*/executions/*/receipt.json')
        if any(json.loads(path.read_text()).get('child') for path in receipts):
            break
        time.sleep(.02)
    else:
        raise RuntimeError('operation never registered')
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=15)
    print(out, end='')
    print(err, end='', file=sys.stderr)
    sys.exit(p.returncode)
finally:
    if p.poll() is None:
        p.kill()
        p.communicate()
'''
        process = subprocess.run([*self.sandbox, 'python3', '-c', controller, self.binary, 'verify', 'change',
                                  '--compare-ref', 'HEAD', '--repo', str(self.repo)], env=self.env,
                                 capture_output=True, text=True, timeout=30)
        self.assertNotEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['cleanup'], 'passed')
        path = next(Path(self.env['XDG_STATE_HOME']).glob('harnessctl/workbench/*/*/session.json'))
        self.assertEqual(json.loads(path.read_text())['state'], 'closed')

    def test_crashed_supervisor_guardian_stops_descendant_writers(self):
        self.descriptor['operations']['slow'] = {'argv': ['python3', '-c',
            'import subprocess,time; subprocess.Popen(["python3","-c",'
            '"import time; from pathlib import Path; p=Path(\\\"scratch/heartbeat\\\"); "'
            '+ "\\nwhile True: p.write_text(str(time.time())); time.sleep(.03)"]); time.sleep(30)'], 'timeout_seconds': 30}
        (self.repo / 'scratch').mkdir()
        self.write('harness/project.json', self.descriptor)
        session = self.open()
        process = subprocess.Popen(self.command('run', '--session', session, '--operation', 'slow'), env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, state, _ = self.active_execution()
        heartbeat = self.repo / 'scratch/heartbeat'
        deadline = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(heartbeat.exists(), 'descendant writer never started')
        os.kill(state['supervisor']['pid'], signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=20)
        result = json.loads(stdout)
        self.assertEqual(result.get('state'), 'interrupted', stdout + stderr)
        self.assertEqual(self.call('observe', '--session', session)['cleanup'], 'passed')
        stopped = heartbeat.read_text()
        time.sleep(.2)
        self.assertEqual(heartbeat.read_text(), stopped)


if __name__ == '__main__':
    if sys.platform != 'darwin' or not shutil.which('codex'):
        raise SystemExit('sandbox-integration requires macOS and the Codex CLI; it was not run')
    unittest.main()
