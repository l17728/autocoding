from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import Core, DomainError
from autocoding.integration import Integration
from autocoding.verifier import Verifier
from autocoding.workspaces import Workspaces, git


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=self.root/'repo'
        self.repo.mkdir()
        git(self.repo,'init')
        git(self.repo,'config','user.name','Fixture')
        git(self.repo,'config','user.email','fixture@example.invalid')
        (self.repo/'value.txt').write_text('base')
        git(self.repo,'add','value.txt')
        git(self.repo,'commit','-m','base')
        self.base=git(self.repo,'rev-parse','HEAD')
        self.core=Core(self.root/'core.db')

    def task(self,name,file,content):
        work=Workspaces(self.root/'agents').create(self.repo,self.base,branch=True)
        path=Path(work['path'])
        (path/file).write_text(content)
        git(path,'add',file)
        git(path,'commit','-m',name)
        sha=git(path,'rev-parse','HEAD')
        tid='task_'+name
        self.core.create_task(tid,name,['unit'],key=tid)
        lease=self.core.claim(tid,'agent:'+name,key=tid)
        job=self.core.report(tid,'agent:'+name,lease['lease_token'],lease['attempt'],sha,['commit'],key=tid)
        code=f'from pathlib import Path; assert Path({file!r}).read_text()=={content!r}'
        result=Verifier(self.core,self.repo,self.root/('verify-'+name),{'unit':[sys.executable,'-c',code]}).run(job)
        self.assertEqual('done',result['state'])
        return tid

    def prepare(self,tasks,code):
        return Integration(self.core,self.repo,self.root/'integration',{'project':[sys.executable,'-c',code]}).prepare(self.base,tasks)

    def test_verified_tasks_merge_and_project_checks_without_moving_source(self):
        a=self.task('a','a.txt','A')
        b=self.task('b','b.txt','B')
        result=self.prepare([a,b],"from pathlib import Path; assert Path('a.txt').read_text()+Path('b.txt').read_text()=='AB'")
        self.assertEqual('verified_candidate',result['state'])
        self.assertFalse(result['published'])
        self.assertEqual(self.base,git(self.repo,'rev-parse','HEAD'))
        self.assertFalse((self.repo/'a.txt').exists())

    def test_merge_conflict_preserves_reviewable_workspace(self):
        a=self.task('a','value.txt','A')
        b=self.task('b','value.txt','B')
        result=self.prepare([a,b],'raise AssertionError')
        self.assertEqual('conflict',result['state'])
        self.assertTrue(git(result['workspace']['path'],'diff','--name-only','--diff-filter=U'))
        self.assertEqual({},result['checks'])
        self.assertEqual(self.base,git(self.repo,'rev-parse','HEAD'))

    def test_project_failure_is_not_accepted(self):
        a=self.task('a','a.txt','A')
        result=self.prepare([a],'raise SystemExit(1)')
        self.assertEqual('checks_failed',result['state'])
        self.assertFalse(result['checks']['project']['passed'])

    def test_unverified_task_cannot_enter_integration(self):
        self.core.create_task('task_a','A',['unit'],key='a')
        with self.assertRaisesRegex(DomainError,'TASK_NOT_VERIFIED'):
            self.prepare(['task_a'],'pass')

    def test_done_dependency_missing_from_candidate_is_rejected(self):
        a=self.task('a','a.txt','A')
        b=self.task('b','b.txt','B')
        with self.core.connection() as conn:
            conn.execute('INSERT INTO dependencies VALUES (?,?)',(b,a))
        with self.assertRaisesRegex(DomainError,'DEPENDENCY_NOT_IN_CANDIDATE'):
            self.prepare([b],'pass')
