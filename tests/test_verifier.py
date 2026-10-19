import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from autocoding.core import Core, DomainError
from autocoding.verifier import Verifier, execute
from autocoding.workspaces import Workspaces, git


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.name', 'Fixture')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'module.py').write_text('value = 42\n', encoding='utf-8')
        git(self.repo, 'add', 'module.py')
        git(self.repo, 'commit', '-m', 'fixture')
        self.sha = git(self.repo, 'rev-parse', 'HEAD')
        self.core = Core(self.root / 'core.db')
        self.core.create_task('task_check', 'Check pinned code', ['unit'], key='task')
        lease = self.core.claim('task_check', 'agent:a', key='claim')
        self.job = self.core.report('task_check', 'agent:a', lease['lease_token'], lease['attempt'],
                                    self.sha, ['commit:' + self.sha], key='report')

    def verifier(self, code):
        return Verifier(self.core, self.repo, self.root / 'verification', {'unit': [sys.executable, '-c', code]})

    def test_pinned_worktree_ignores_dirty_source_and_marks_done(self):
        (self.repo / 'module.py').write_text('value = 0\n', encoding='utf-8')
        report = self.verifier('import module; assert module.value == 42').run(self.job)
        self.assertEqual('done', report['state'])
        self.assertEqual('value = 0\n', (self.repo / 'module.py').read_text())
        artifact = json.loads(Path(report['artifact']).read_text())
        self.assertEqual(self.sha, artifact['job']['head_sha'])

    def test_failure_returns_task_for_rework(self):
        report = self.verifier('raise SystemExit(7)').run(self.job)
        self.assertEqual('todo', report['state'])
        self.assertEqual(7, report['checks']['unit']['exit_code'])

    def test_modified_tracked_files_cannot_pass(self):
        report = self.verifier("from pathlib import Path; Path('module.py').write_text('value = 0')").run(self.job)
        self.assertEqual('todo', report['state'])
        self.assertEqual('workspace_changed', report['checks']['unit']['status'])

    def test_cancelled_job_never_runs(self):
        self.core.cancel_task('task_check', expected_revision=self.job['task_revision'], key='cancel')
        with self.assertRaisesRegex(DomainError, 'STALE_VERIFICATION'):
            self.verifier('raise Exception()').run(self.job)

    def test_environment_and_output_do_not_expose_credentials(self):
        with patch.dict(os.environ, {'ZHIPU_API_KEY': 'PRIVATE_CANARY', 'ANTHROPIC_AUTH_TOKEN': 'PRIVATE_CANARY'}):
            report = execute([sys.executable, '-c',
                "import os; assert 'ZHIPU_API_KEY' not in os.environ; assert 'ANTHROPIC_AUTH_TOKEN' not in os.environ; print('PRIVATE_CANARY')"], self.repo)
        self.assertTrue(report['passed'])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(report))

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object behavior')
    def test_timeout_kills_descendant_before_it_can_write(self):
        marker = self.repo / 'late.txt'
        child = "import time,pathlib; time.sleep(2); pathlib.Path('late.txt').write_text('bad')"
        code = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(30)'
        report = execute([sys.executable, '-c', code], self.repo, timeout=1)
        self.assertEqual('timeout', report['status'])
        time.sleep(2.2)
        self.assertFalse(marker.exists())

    def test_worktree_cleanup_refuses_dirty_or_running(self):
        manager = Workspaces(self.root / 'worktrees')
        work = manager.create(self.repo, self.sha, branch=True)
        with self.assertRaisesRegex(DomainError, 'PROCESS_STOP_REQUIRED'):
            manager.remove(work['id'])
        path = manager.file(work['id'], 'keep.txt')
        path.write_text('user work')
        with self.assertRaisesRegex(DomainError, 'WORKSPACE_DIRTY'):
            manager.remove(work['id'], stopped=True)
        self.assertTrue(path.exists())
        with self.assertRaisesRegex(DomainError, 'PATH_ESCAPE'):
            manager.file(work['id'], '../escape')


if __name__ == '__main__':
    unittest.main()
