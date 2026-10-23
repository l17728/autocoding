import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError, digest, encoded
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

    async def test_batch_failure_persists_redacted_diagnostic(self):
        import json
        record={'id':'batch_diagnostic','task_ids':[],'attempts':{},'run_ids':[]}
        diagnostic={'error_type':'RuntimeError',
                    'message':self.host.redactor.text('COORDINATOR_TEST_FAILURE')[:500]}
        self.host.coordinator.save(record,'failed',reason='BATCH_FAILED',failure_diagnostic=diagnostic)
        with self.host.core.connection() as conn:
            stored=conn.execute("SELECT value FROM metadata WHERE key='batch:batch_diagnostic'").fetchone()
            event=conn.execute("SELECT payload FROM events WHERE type='TaskBatchChanged' ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(diagnostic,json.loads(stored['value'])['failure_diagnostic'])
        self.assertEqual(diagnostic,json.loads(event['payload'])['failure_diagnostic'])

    async def test_concurrency_limit_and_failure_does_not_stop_new_dispatch(self):
        for tid in ['task_a','task_b','task_c']:
            self.task(tid)
        self.fail='task_a'
        await self.host.coordinator.start(['task_a','task_b','task_c'],max_concurrency=2,max_attempts=1)
        await self.until(lambda:self.current==2)
        self.release.set()
        await self.until(lambda:self.host.coordinator.current is None)
        self.assertEqual(2,self.peak)
        self.assertEqual(['task_a','task_b','task_c'],[x[0] for x in self.started])
        with self.host.core.connection() as conn:
            self.assertEqual('todo',self.host.core._get(conn,'tasks','task_a')['state'])
            self.assertEqual('done',self.host.core._get(conn,'tasks','task_b')['state'])
            self.assertEqual('done',self.host.core._get(conn,'tasks','task_c')['state'])
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

    async def test_exhausted_task_does_not_stop_independent_ready_work(self):
        self.task('task_a')
        self.task('task_b')
        self.fail='task_a'
        self.release.set()
        await self.host.coordinator.start(['task_a','task_b'],max_concurrency=1,max_attempts=2)
        await self.until(lambda:self.host.coordinator.current is None)
        self.assertEqual(['task_a','task_a','task_b'],[tid for tid,_,_ in self.started])
        with self.host.core.connection() as conn:
            self.assertEqual('todo',self.host.core._get(conn,'tasks','task_a')['state'])
            self.assertEqual('done',self.host.core._get(conn,'tasks','task_b')['state'])
        batch=self.host.coordinator.list()[0]
        self.assertEqual('rework_required',batch['state'])
        self.assertEqual('task_a',batch['stop_evidence'][0]['task_id'])
        self.assertEqual('BATCH_ATTEMPT_LIMIT',batch['stop_evidence'][0]['reason'])

    async def test_exhausted_dependency_frontier_is_rework_not_reconciliation(self):
        self.task('task_a')
        self.task('task_b',['task_a'])
        self.fail='task_a'
        self.release.set()
        # Selection order is not necessarily topological; test downstream first.
        await self.host.coordinator.start(['task_b','task_a'],max_concurrency=1,max_attempts=2)
        await self.until(lambda:self.host.coordinator.current is None)

        batch=self.host.coordinator.list()[0]
        self.assertEqual('rework_required',batch['state'])
        self.assertEqual('BATCH_ATTEMPT_LIMIT',batch['reason'])
        blocked=next(item for item in batch['stop_evidence'] if item['task_id']=='task_b')
        self.assertEqual('rework_required',blocked['state'])
        self.assertEqual('BATCH_TASKS_BLOCKED',blocked['reason'])
        self.assertEqual(['task_a'],blocked['blocked_by'])

    async def test_restart_recovery_never_dispatches_past_batch_attempt_limit(self):
        self.task('task_a')
        _,checks,protected=self.configuration('task_a')
        record={'id':'batch_recovered','task_ids':['task_a'],'created_at':1,'base_sha':'a'*40,
                'provider':'codex','max_attempts':2,'run_ids':[],
                'attempts':{'task_a':2},'agents':['codex'],'max_concurrency':1,
                'assignments':{'task_a':'codex'}}
        self.host.coordinator.save(record,'queued')
        await self.host.coordinator._perform(record,
            {'task_a':digest({'checks':checks,'protected_paths':protected})},checks)
        self.assertEqual([],self.started)
        self.assertEqual('rework_required',record['state'])
        self.assertEqual('BATCH_ATTEMPT_LIMIT',record['reason'])

    async def test_interrupted_batch_recovery_applies_serial_backpressure(self):
        self.task('task_a')
        self.task('task_b')
        record={'id':'batch_backpressure','task_ids':['task_a','task_b'],'created_at':1,
                'base_sha':'a'*40,'provider':'codex','max_attempts':3,'run_ids':[],
                'attempts':{'task_a':1,'task_b':1},'agents':['codex','opencode','claude'],
                'max_concurrency':3,'assignments':{'task_a':'codex','task_b':'opencode'}}
        self.host.coordinator.save(record,'reconciling',reason='RUN_DELIVERY_RECONCILIATION_REQUIRED',
                                   active_tasks=['task_a','task_b'])
        scheduled=[]
        original_background=self.host._background
        self.host._background=lambda coroutine: scheduled.append(coroutine)
        try:
            self.host.coordinator.recover()
            recovered=self.host.coordinator.list()[0]
            self.assertEqual(1,recovered['max_concurrency'])
            self.assertTrue(recovered['recovery_backpressure'])
            self.assertEqual('queued',recovered['state'])
            self.assertEqual(1,len(scheduled))
        finally:
            self.host._background=original_background
            for coroutine in scheduled:
                coroutine.close()

    async def test_persisted_match_detects_retry_with_unchanged_active_task_set(self):
        record={'id':'batch_retry','task_ids':['task_a'],'attempts':{'task_a':1},
                'run_ids':['run_first'],'max_attempts':3}
        self.host.coordinator.save(record,'running',active_tasks=['task_a'])
        self.assertTrue(self.host.coordinator.persisted_matches(record,'running',['task_a']))
        record['attempts']['task_a']=2
        record['run_ids'].append('run_second')
        self.assertFalse(self.host.coordinator.persisted_matches(record,'running',['task_a']))

    async def test_git_setup_failures_count_only_before_workspace_and_agent_handoff(self):
        record={'run_ids':['run_a','run_b','run_c']}
        self.rows.extend([
            {'id':'run_a','task_id':'task_a','state':'failed','reason':'GIT_UNAVAILABLE',
             'error_phase':'preparing'},
            {'id':'run_b','task_id':'task_a','state':'failed','reason':'GIT_UNAVAILABLE',
             'error_phase':'preparing','workspace_id':'workspace_created'},
            {'id':'run_c','task_id':'task_a','state':'failed','reason':'GIT_UNAVAILABLE',
             'error_phase':'preparing','session_id':'session_started'},
        ])
        self.assertEqual(1,self.host.coordinator.git_preparation_failures(record,'task_a'))
        record['attempts']={'task_a':3}
        self.assertEqual(2,self.host.coordinator.effective_attempts(record,'task_a'))

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

    async def test_reconciliation_batch_blocks_competing_filtered_batch(self):
        self.task('task_a')
        with self.host.core.connection() as conn:
            conn.execute('INSERT INTO metadata VALUES (?,?)',('batch:existing',encoded({
                'id':'existing','state':'reconciling','created_at':0,'task_ids':['other']})))
        with self.assertRaisesRegex(DomainError,'BATCH_ACTIVE'):
            await self.host.coordinator.start(['task_a'])

    async def test_generic_runner_timeout_uses_active_provider_for_fallback(self):
        self.rows.append({'id':'run_old','task_id':'task_a','state':'reconciling',
                          'provider':'claude','reason':'RUN_TIMEOUT_RECONCILIATION_REQUIRED'})
        record={'assignments':{'task_a':'claude'},'agents':['codex','opencode','claude'],
                'attempts':{'task_a':2}}
        self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual('codex',record['assignments']['task_a'])

    async def test_generic_rpc_timeout_without_provider_name_falls_back(self):
        self.rows.append({'id':'run_old','task_id':'task_a','state':'reconciling',
                          'provider':'claude','reason':'RPC_TIMEOUT_DELIVERY_UNKNOWN'})
        record={'assignments':{'task_a':'claude'},'agents':['claude','codex'],
                'attempts':{'task_a':2}}
        self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual('codex',record['assignments']['task_a'])

    async def test_delivery_unknown_falls_back_to_another_configured_provider(self):
        self.rows.append({'id':'run_delivery','task_id':'task_a','state':'reconciled',
                          'provider':'claude','reason':'RESTART_NEW_ATTEMPT',
                          'reconciliation_reason':'RUN_DELIVERY_RECONCILIATION_REQUIRED'})
        record={'assignments':{'task_a':'claude'},'agents':['claude','codex','opencode'],
                'attempts':{'task_a':1}}
        self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual('codex',record['assignments']['task_a'])

    async def test_provider_fallback_visits_each_configured_provider_only_once(self):
        record={'assignments':{'task_a':'codex'},'agents':['codex','opencode','claude'],
                'attempts':{'task_a':1}}
        for provider, replacement in [('codex','opencode'),('opencode','claude')]:
            self.rows.append({'id':'run_'+provider,'task_id':'task_a','state':'reconciling',
                              'provider':provider,'reason':provider.upper()+'_HTTP_TIMEOUT'})
            self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
            self.assertEqual(replacement,record['assignments']['task_a'])
        self.rows.append({'id':'run_claude','task_id':'task_a','state':'reconciling',
                          'provider':'claude','reason':'CLAUDE_HTTP_TIMEOUT'})
        self.assertFalse(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual(['codex','opencode','claude'],record['provider_attempts']['task_a'])

    async def test_pm_owned_input_takeover_falls_back_but_human_owned_does_not(self):
        self.host.core.create_session('session_pm','claude',owner='human',key='session-pm')
        self.host.core.takeover('session_pm','pm',0,key='takeover-pm')
        self.rows.append({'id':'run_pm','task_id':'task_a','state':'reconciled',
                          'provider':'claude','session_id':'session_pm',
                          'reconciliation_reason':'RUN_INPUT_TAKEN_OVER'})
        record={'assignments':{'task_a':'claude'},'agents':['claude','codex'],
                'attempts':{'task_a':2}}
        self.assertTrue(self.host.coordinator.fallback_provider(record,'task_a'))
        self.assertEqual('codex',record['assignments']['task_a'])

        self.host.core.create_session('session_human','claude',owner='human',key='session-human')
        self.host.core.takeover('session_human','pm',0,key='takeover-to-pm')
        self.host.core.takeover('session_human','human',1,key='takeover-to-human')
        self.rows.append({'id':'run_human','task_id':'task_b','state':'reconciled',
                          'provider':'claude','session_id':'session_human',
                          'reconciliation_reason':'RUN_INPUT_TAKEN_OVER'})
        record={'assignments':{'task_b':'claude'},'agents':['claude','codex'],
                'attempts':{'task_b':2}}
        self.assertFalse(self.host.coordinator.fallback_provider(record,'task_b'))

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
