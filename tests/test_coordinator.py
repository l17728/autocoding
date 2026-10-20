import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from closure_support import integration_evidence


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host=Host(self.temp.name,Path(self.temp.name)/'storage')
        self.addAsyncCleanup(self.host.close)
        self.rows=[]
        self.started=[]
        self.current=0
        self.peak=0
        self.release=asyncio.Event()
        self.fail=None
        self.reconcile=None
        self.recoveries=[]
        self.cancelled=[]
        self.host.workflow.configuration=self.configuration
        self.host.workflow.list=lambda:self.rows
        self.host.workflow.start=self.start_run
        self.host.workflow.recover=self.recover_runs
        self.git=patch('autocoding.coordinator.git',return_value='a'*40)
        self.git.start()
        self.addCleanup(self.git.stop)
        self.integration=patch('autocoding.coordinator.Integration')
        self.integrator=self.integration.start()
        self.addCleanup(self.integration.stop)
        self.integrator.return_value.prepare.side_effect=lambda base,tids:integration_evidence(self.host,tids)

    def task(self,tid,deps=None):
        self.host.core.create_task(tid,tid,['unit'],deps or [],key=tid)

    def configuration(self,tid):
        with self.host.core.connection() as conn:
            task=dict(self.host.core._get(conn,'tasks',tid))
        return task,{'unit':[sys.executable,'test.py']},[]

    def recover_runs(self):
        self.recoveries.append(True)
        for row in self.rows:
            if row['state']=='reconciling':
                row['state']='reconciled'
                with self.host.core.connection() as conn:
                    conn.execute('UPDATE tasks SET state=?,agent=NULL,lease_token=NULL,lease_until=NULL WHERE id=?',
                        ('todo',row['task_id']))

    async def start_run(self,tid,*,base_sha,batch_id,agent):
        self.started.append((tid,agent,base_sha))
        row={'id':'run_'+str(len(self.rows)),'task_id':tid,'state':'running','head_sha':str(len(self.rows)+1)*40}
        self.rows.append(row)
        async def work():
            self.current+=1
            self.peak=max(self.peak,self.current)
            try:
                await self.release.wait()
                await asyncio.sleep(.03)
                prior=sum(1 for item in self.rows if item['task_id']==tid)
                row['state']='rework_required' if tid==self.fail else ('reconciling' if tid==self.reconcile and prior==1 else 'done')
                with self.host.core.connection() as conn:
                    conn.execute('UPDATE tasks SET state=?,head_sha=? WHERE id=?',
                        ('blocked' if row['state']=='reconciling' else ('todo' if tid==self.fail else 'done'),row['head_sha'],tid))
            except asyncio.CancelledError:
                self.cancelled.append(tid)
                raise
            finally:
                self.current-=1
                self.host.workflow.active.pop(tid,None)
        self.host.workflow.active[tid]=self.host._background(work())
        return dict(row)

    async def until(self,predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(.01)

    async def test_three_providers_overlap_and_dependency_waits(self):
        for tid in ['task_a','task_b','task_c']:
            self.task(tid)
        self.task('task_d',['task_a','task_b'])
        await self.host.coordinator.start(['task_a','task_b','task_c','task_d'],
            agents=['codex','opencode','claude'],max_concurrency=3)
        await self.until(lambda:self.current==3)
        self.assertEqual(['codex','opencode','claude'],[x[1] for x in self.started])
        self.assertNotIn('task_d',[x[0] for x in self.started])
        self.release.set()
        await self.until(lambda:self.host.coordinator.current is None)
        self.assertEqual(3,self.peak)
        self.assertEqual('task_d',self.started[-1][0])
        self.assertTrue(all(x[2]=='a'*40 for x in self.started))
        self.assertEqual('verified_candidate',self.host.coordinator.list()[0]['state'])
        self.assertEqual(1,len(self.host.closure.list()))
        self.assertEqual(4,len(self.host.closure.list()[0]['tasks']))

    async def test_concurrency_limit_and_failure_stop_new_dispatch(self):
        for tid in ['task_a','task_b','task_c']:
            self.task(tid)
        self.fail='task_a'
        await self.host.coordinator.start(['task_a','task_b','task_c'],max_concurrency=2,max_attempts=1)
        await self.until(lambda:self.current==2)
        self.release.set()
        await self.until(lambda:self.host.coordinator.current is None)
        self.assertEqual(2,self.peak)
        self.assertEqual(['task_a','task_b'],[x[0] for x in self.started])
        self.assertEqual('rework_required',self.host.coordinator.list()[0]['state'])
        self.integrator.return_value.prepare.assert_not_called()

    async def test_final_incremental_batch_integrates_previously_done_tasks(self):
        self.task('task_a')
        self.release.set()
        await self.host.coordinator.start(['task_a'])
        await self.until(lambda:self.host.coordinator.current is None)
        self.task('task_b')
        await self.host.coordinator.start(['task_b'])
        await self.until(lambda:self.host.coordinator.current is None)
        batch=self.host.coordinator.list()[-1]
        self.assertEqual(['task_b'],batch['task_ids'])
        self.assertEqual(['task_a','task_b'],batch['integration_task_ids'])
        self.assertEqual(['task_a','task_b'],self.integrator.return_value.prepare.call_args.args[1])
        self.assertEqual(['task_a','task_b'],[row[0] for row in self.started])
        reports=self.host.closure.list()
        self.assertEqual(2,len(reports))
        self.assertTrue(reports[0]['task_snapshot_current'])
        self.assertFalse(reports[1]['task_snapshot_current'])
        self.assertEqual(reports[0]['lessons'][0]['content'],reports[1]['lessons'][0]['content'])

    async def test_partial_batch_does_not_execute_or_close_unselected_tasks(self):
        self.task('task_a')
        self.task('task_b')
        self.release.set()
        await self.host.coordinator.start(['task_a'])
        await self.until(lambda:self.host.coordinator.current is None)
        batch=self.host.coordinator.list()[0]
        self.assertEqual('verified_candidate',batch['state'])
        self.assertEqual(['task_a'],batch['integration_task_ids'])
        self.assertEqual(['task_a'],[row[0] for row in self.started])
        self.assertEqual([],self.host.closure.list())
        self.assertEqual('deferred',batch['closure_state'])

    async def test_rework_uses_own_head_and_bounded_attempts(self):
        self.task('task_a')
        self.task('task_b')
        self.fail='task_a'
        self.release.set()
        await self.host.coordinator.start(['task_a','task_b'],max_concurrency=2,max_attempts=2)
        await self.until(lambda:self.host.coordinator.current is None)
        a=[x for x in self.started if x[0]=='task_a']
        self.assertEqual(2,len(a))
        first=next(r for r in self.rows if r['task_id']=='task_a')
        self.assertEqual(first['head_sha'],a[1][2])

    async def test_reconciliation_creates_fresh_bounded_attempt(self):
        self.task('task_a')
        self.reconcile='task_a'
        self.release.set()
        await self.host.coordinator.start(['task_a'],max_attempts=2)
        await self.until(lambda:self.host.coordinator.current is None)
        self.assertEqual(2,len([row for row in self.started if row[0]=='task_a']))
        self.assertEqual(1,len(self.recoveries))
        self.assertEqual('verified_candidate',self.host.coordinator.list()[0]['state'])

    async def test_provider_timeout_falls_back_without_replaying_old_provider(self):
        self.rows.append({'id':'run_old','task_id':'task_a','state':'reconciling',
                          'provider':'opencode','reason':'OPENCODE_HTTP_TIMEOUT'})
        record={'assignments':{'task_a':'opencode'},'agents':['codex','opencode','claude'],
                'attempts':{'task_a':2}}
        self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual('codex',record['assignments']['task_a'])
        self.assertEqual(0,record['attempts']['task_a'])
        self.assertEqual('opencode',record['provider_fallbacks'][0]['from'])
        self.assertEqual('codex',record['provider_fallbacks'][0]['to'])

    async def test_generic_runner_timeout_uses_active_provider_for_fallback(self):
        self.rows.append({'id':'run_old','task_id':'task_a','state':'reconciling',
                          'provider':'claude','reason':'RUN_TIMEOUT_RECONCILIATION_REQUIRED'})
        record={'assignments':{'task_a':'claude'},'agents':['codex','opencode','claude'],
                'attempts':{'task_a':2}}
        self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual('codex',record['assignments']['task_a'])

    async def test_shutdown_cancels_children_before_releasing_reservations(self):
        self.task('task_a')
        self.task('task_b')
        await self.host.coordinator.start(['task_a','task_b'],max_concurrency=2)
        await self.until(lambda:self.current==2)
        await self.host.close()
        self.assertEqual({'task_a','task_b'},set(self.cancelled))
        self.assertEqual({},self.host.coordinator.reserved)
        self.assertEqual('reconciling',self.host.coordinator.list()[0]['state'])

    async def test_pause_drains_active_work_but_does_not_dispatch_next(self):
        for tid in ['task_a','task_b','task_c']:
            self.task(tid)
        await self.host.coordinator.start(['task_a','task_b','task_c'],max_concurrency=2)
        await self.until(lambda:self.current==2)
        self.host.paused=True
        self.release.set()
        await self.until(lambda:self.current==0)
        await asyncio.sleep(.3)
        self.assertEqual(2,len(self.started))
        self.host.paused=False
        await self.until(lambda:self.host.coordinator.current is None)
        self.assertEqual(3,len(self.started))
