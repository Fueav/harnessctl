#!/usr/bin/env python3
"""Black-box v1 contract; configuration-only consumers, no engine import."""
import json
import os
from pathlib import Path
import subprocess
import signal
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = str(Path(cls.build.name) / 'harnessctl')
        subprocess.run(['go', 'build', '-ldflags', '-X main.version=v1.0.0-rc.1', '-o', cls.binary, './cmd/harnessctl'], cwd=ROOT, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'project'
        self.repo.mkdir()
        self.env = {**os.environ, 'XDG_STATE_HOME': str(Path(self.temp.name) / 'state')}
        self.sessions = []
        self.addCleanup(self.cleanup_sessions)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Harness Test')
        self.git('config', 'user.email', 'harness@example.test')
        (self.repo / 'harness').mkdir()
        self.write('harness/harness.lock', {'schema_version': 1, 'module': 'github.com/Fueav/harnessctl', 'version': 'v1.0.0-rc.1'})
        (self.repo / '.gitignore').write_text('scratch/\n')
        (self.repo / '.ai-boundaries.yml').write_text('allowed:\n  - app.py\n  - note.txt\n  - goal.json\n  - harness/\n  - .gitignore\napproval_required:\n  - .ai-boundaries.yml\nforbidden:\n  - secrets/\n')
        (self.repo / 'app.py').write_text('print("working")\n')
        self.descriptor = {'schema_version': 1, 'project': 'fixture', 'operations': {
            'test': {'argv': ['python3', 'app.py'], 'timeout_seconds': 10},
            'fail': {'argv': ['python3', '-c', 'print("semantic failure"); raise SystemExit(3)'], 'timeout_seconds': 10},
            'slow': {'argv': ['python3', '-c', 'import time; time.sleep(30)'], 'timeout_seconds': 1},
            'mutate': {'argv': ['python3', '-c', 'from pathlib import Path; Path("app.py").write_text("print(123)\\n")'], 'timeout_seconds': 10}},
            'profiles': {'development': {'resources': 'none', 'lease_seconds': 60}},
            'verification': {'change': ['test'], 'candidate': ['test'], 'release': ['test']},
            'dependencies': {'enabled': False}, 'tools': {}}
        self.write('harness/project.json', self.descriptor)
        self.git('add', '.')
        self.git('commit', '-m', 'fixture baseline')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, value):
        (self.repo / name).write_text(json.dumps(value))

    def call(self, *args, ok=True):
        proc = subprocess.run([self.binary, *args, '--repo', str(self.repo)], env=self.env, capture_output=True, text=True, timeout=40)
        try:
            value = json.loads(proc.stdout)
        except ValueError:
            self.fail(f'protocol is not JSON: {proc.returncode}: {proc.stdout!r} {proc.stderr!r}')
        self.assertEqual(value['schema_version'], 1)
        if ok:
            self.assertEqual(proc.returncode, 0, value)
        else:
            self.assertNotEqual(proc.returncode, 0, value)
        return value

    def open(self):
        result = self.call('open', '--profile', 'development')
        self.sessions.append(result['session_id'])
        return result['session_id']

    def cleanup_sessions(self):
        for session in self.sessions:
            subprocess.run([self.binary, 'close', '--repo', str(self.repo), '--session', session], env=self.env, capture_output=True, timeout=40)

    def run_operation(self, session, name, ok=True):
        result = self.call('run', '--session', session, '--operation', name, ok=ok)
        while result['state'] == 'running':
            time.sleep(0.05)
            result = self.call('observe', '--session', session, '--execution', result['execution_id'])
        return result

    def test_describe_rejects_unknown_and_legacy(self):
        self.assertIn('test', self.call('describe')['operations'])
        self.descriptor['invented'] = True
        self.write('harness/project.json', self.descriptor)
        self.assertIn('error', self.call('describe', ok=False))
        del self.descriptor['invented']
        self.write('harness/project.json', self.descriptor)
        self.write('harness/dependencies.json', {'enabled': False})
        self.assertIn('error', self.call('describe', ok=False))

    def test_session_survives_parent_and_receipts_remain(self):
        session = self.open()
        first = self.run_operation(session, 'test')
        second = self.run_operation(session, 'test')
        self.assertEqual(first['state'], 'completed')
        self.assertNotEqual(first['execution_id'], second['execution_id'])
        self.assertEqual(first['exit_code'], 0)
        self.assertIn('working', Path(first['artifacts']['stdout']).read_text())
        self.assertEqual(first['candidate']['head'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.call('close', '--session', session)['cleanup'], 'passed')
        self.assertEqual(self.call('close', '--session', session)['cleanup'], 'passed')
        self.assertEqual(self.call('observe', '--session', session, '--execution', first['execution_id'])['state'], 'completed')

    def test_failed_and_timeout_attempts_recorded(self):
        session = self.open()
        failed = self.run_operation(session, 'fail', ok=False)
        self.assertEqual(failed['exit_code'], 3)
        self.assertEqual(failed['state'], 'failed')
        timeout = self.run_operation(session, 'slow', ok=False)
        self.assertEqual(timeout['exit_code'], 124)
        self.assertEqual(timeout['state'], 'interrupted')
        observed = self.call('observe', '--session', session)
        self.assertEqual(len(observed['executions']), 2)

    def test_contract_changes_and_foreign_sessions_rejected(self):
        session = self.open()
        self.descriptor['project'] = 'altered'
        self.write('harness/project.json', self.descriptor)
        self.assertIn('error', self.call('run', '--session', session, '--operation', 'test', ok=False))
        self.assertIn('error', self.call('observe', '--session', 'a' * 32, ok=False))

    def test_verify_binds_candidate_and_criteria(self):
        goal = {'schema_version': 1, 'id': 'result', 'objective': 'actual result', 'criteria': [{'id': 'semantic', 'description': 'product works', 'operation': 'fail'}]}
        self.write('goal.json', goal)
        result = self.call('verify', 'change', '--compare-ref', 'HEAD', '--goal', 'goal.json', ok=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['criteria'][0]['status'], 'failed')
        self.assertEqual(result['cleanup'], 'passed')
        (self.repo / 'goal.json').unlink()
        result = self.call('verify', 'candidate', '--compare-ref', 'HEAD')
        self.assertEqual(result['status'], 'passed')
        (self.repo / 'app.py').write_text('print("dirty")\n')
        self.assertIn('error', self.call('verify', 'candidate', '--compare-ref', 'HEAD', ok=False))

    def test_verification_rejects_candidate_mutation(self):
        self.descriptor['verification']['change'] = ['mutate']
        self.write('harness/project.json', self.descriptor)
        result = self.call('verify', 'change', '--compare-ref', 'HEAD', ok=False)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('candidate', result['error'])
        self.assertEqual(result['cleanup'], 'passed')

    def test_later_operation_cannot_restore_mutated_candidate_into_a_pass(self):
        self.descriptor['operations']['restore'] = {'argv': ['python3', '-c', 'from pathlib import Path; Path("app.py").write_text(\'print("working")\\n\')'], 'timeout_seconds': 10}
        self.descriptor['verification']['change'] = ['mutate', 'restore']
        self.write('harness/project.json', self.descriptor)
        result = self.call('verify', 'change', '--compare-ref', 'HEAD', ok=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['executions']), 1)
        self.assertIn('candidate', result['error'])


    def test_boundaries_cannot_be_omitted_by_descriptor(self):
        (self.repo / 'secrets').mkdir()
        (self.repo / 'secrets/key').write_text('fixture-only')
        result = self.call('verify', 'change', '--compare-ref', 'HEAD', ok=False)
        self.assertIn('error', result)

    def test_malformed_nested_contracts_never_emit_tracebacks(self):
        for value in ([], {}, True, None, 1):
            self.descriptor['operations']['test']['argv'] = value
            self.write('harness/project.json', self.descriptor)
            self.assertIn('error', self.call('describe', ok=False))
        self.descriptor['operations']['test']['argv'] = ['python3', 'app.py']
        self.write('harness/project.json', self.descriptor)
        self.write('goal.json', {'schema_version': 1, 'id': 'bad', 'objective': 'bad goal', 'criteria': [{'id': 'a', 'description': 'bad reference', 'operation': []}]})
        self.assertIn('error', self.call('open', '--goal', 'goal.json', ok=False))

    def test_expiry_and_close_reclaim_runtime_but_keep_receipts(self):
        self.descriptor['profiles']['development']['lease_seconds'] = 2
        self.write('harness/project.json', self.descriptor)
        session = self.open()
        time.sleep(2.5)
        result = self.call('observe', '--session', session)
        self.assertEqual(result['state'], 'expired')
        self.assertEqual(result['cleanup'], 'passed')
        paths = list((Path(self.env['XDG_STATE_HOME']) / 'harnessctl/workbench').glob('*/' + session))
        self.assertEqual(len(paths), 1)
        self.assertFalse((paths[0] / 'runtime').exists())

    def test_supervisor_crash_recovers_registered_child(self):
        self.descriptor['operations']['slow']['timeout_seconds'] = 30
        self.write('harness/project.json', self.descriptor)
        session = self.open()
        process = subprocess.Popen([self.binary, 'run', '--repo', str(self.repo), '--session', session, '--operation', 'slow'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                observation = self.call('observe', '--session', session)
                executions = observation['executions']
                if executions and executions[0].get('child'):
                    break
                time.sleep(0.05)
            else:
                self.fail('operation child was not durably registered')
            os.kill(observation['supervisor']['pid'], signal.SIGKILL)
            out, err = process.communicate(timeout=15)
            result = json.loads(out)
            self.assertEqual(result['state'], 'interrupted', err)
            observed = self.call('observe', '--session', session)
            self.assertEqual(observed['cleanup'], 'passed')
            self.assertEqual(observed['state'], 'closed')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_deferred_candidate_never_claims_full_verification(self):
        (self.repo / '.ai-boundaries.yml').write_text((self.repo / '.ai-boundaries.yml').read_text() + '\n')
        self.git('add', '.')
        self.git('commit', '-m', 'protected contract change')
        self.env['AI_BOUNDARY_APPROVAL_MODE'] = 'deferred'
        result = self.call('verify', 'candidate', '--compare-ref', 'HEAD^')
        self.assertEqual(result['status'], 'pending_approval')
        self.assertEqual(result['engineering_status'], 'passed')
        self.assertEqual(result['approval_status'], 'pending')
        self.assertEqual(self.call('verify', 'release', '--compare-ref', 'HEAD^', ok=False)['status'], 'failed')

    def test_same_version_other_build_cannot_execute_session(self):
        session = self.open()
        other = str(Path(self.temp.name) / 'other-build')
        subprocess.run(['go', 'build', '-trimpath', '-ldflags', '-X main.version=v1.0.0-rc.1', '-o', other, './cmd/harnessctl'], cwd=ROOT, check=True)
        result = subprocess.run([other, 'run', '--repo', str(self.repo), '--session', session, '--operation', 'test'], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('error', json.loads(result.stdout))
        closed = subprocess.run([other, 'close', '--repo', str(self.repo), '--session', session], env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(closed.stdout)['cleanup'], 'passed')


    def test_configuration_only_go_consumer(self):
        (self.repo / 'go.mod').write_text('module example.test/consumer\n\ngo 1.22\n')
        (self.repo / 'result.go').write_text('package result\nfunc Complete(items []bool) bool { for _, done := range items { if !done { return false } }; return len(items)>0 }\n')
        (self.repo / 'result_test.go').write_text('package result\nimport "testing"\nfunc TestComplete(t *testing.T) { if Complete(nil) || Complete([]bool{true,false}) || !Complete([]bool{true,true}) { t.Fatal("incomplete result reported complete") } }\n')
        self.descriptor['operations']['test']['argv'] = ['go', 'test', './...']
        self.write('harness/project.json', self.descriptor)
        session = self.open()
        result = self.run_operation(session, 'test')
        self.assertEqual(result['state'], 'completed')
        self.assertFalse((self.repo / 'scripts').exists())


    def test_old_public_paths_are_removed(self):
        for command in [('resources', 'status'), ('scaffold', 'audit'), ('evidence', 'verify'), ('workspace-preflight',)]:
            self.assertIn('error', self.call(*command, ok=False))


if __name__ == '__main__':
    unittest.main()
