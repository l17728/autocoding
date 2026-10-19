import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import DomainError
from autocoding.host import Host
from autocoding.runtime import SessionRuntime
from autocoding.workspaces import git


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_recovery_starts_independent_attempt_after_timeout(self):
        from autocoding.recovery import Recovery
        self.provider('timeout')
        first = await self.host.workflow.start('task_a', max_seconds=1)
        await self.host.workflow.active['task_a']
        old_workspace = self.workspace
        recovery = Recovery(self.host)
        evidence = await asyncio.to_thread(recovery.inspect, first['id'])
        await asyncio.to_thread(recovery.resolve, first['id'], evidence['fingerprint'],
                                'Owner confirmed stop and checked effects', True, key='recover')
        self.provider('pass', prefix='replacement')
        second = await self.host.workflow.start('task_a')
        await self.host.workflow.active['task_a']
        current = next(r for r in self.host.workflow.list() if r['id']==second['id'])
        self.assertEqual('done', current['state'])
        self.assertNotEqual(old_workspace, self.workspace)

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'repo'
        self.root.mkdir()
        git(self.root,'init')
        git(self.root,'config','user.name','Fixture')
        git(self.root,'config','user.email','fixture@example.invalid')
        (self.root/'module.py').write_text('value=0\n')
        (self.root/'test_module.py').write_text('import module\nassert module.value==42\n')
        git(self.root,'add','.')
        git(self.root,'commit','-m','baseline')
        self.base=git(self.root,'rev-parse','HEAD')
        self.host=Host(self.root,self.root/'.local/workbench')
        self.addAsyncCleanup(self.host.close)
        (self.root/'.local/acceptance.json').write_text(json.dumps({'checks':{'unit':[sys.executable,'test_module.py']},
                                                                  'protected_paths':['test_module.py']}))
        self.host.core.create_task('task_a','Set value to 42',['unit'],key='task')

    def provider(self,mode='pass',prefix='s'):
        count=0
        async def start(agent,*,workspace_id,tools,task_scope=None):
            self.selected_agent=agent
            nonlocal count
            count+=1
            sid=prefix if count==1 else prefix+str(count)
            self.workspace=Path(self.host.workspaces.get(workspace_id)['path'])
            self.host.core.create_session(sid,agent,owner='human',key=sid)
            adapter=type('Adapter',(),{'close':AsyncMock(),'interrupt':AsyncMock()})()
            self.host.runtimes[sid]=SessionRuntime(self.host.core,sid,adapter)
            return {'session_id':sid}
        async def send(sid,epoch,text,*,key,sender):
            command=self.host.core.enqueue(sid,sender,epoch,text,key=key)
            cid=command['command_id']
            self.host.core.begin_dispatch(cid,key='dispatch:'+cid)
            self.host.core.record_command(cid,'acknowledged',provider_turn_id='t',key='ack:'+cid)
            if mode=='takeover':
                await self.host.runtimes[sid].takeover('human',epoch,key='takeover')
            elif mode=='timeout':
                pass
            else:
                if mode=='protected':
                    (self.workspace/'test_module.py').write_text('pass\n')
                else:
                    (self.workspace/'module.py').write_text('value=42\n' if mode=='pass' else 'value=1\n')
                self.host.core.record_command(cid,'completed',provider_turn_id='t',key='complete:'+cid)
            return command
        self.host.start=start
        self.host.send=send

    async def run_task(self,mode='pass'):
        self.provider(mode)
        initial=await self.host.workflow.start('task_a',max_seconds=1)
        work=self.host.workflow.active['task_a']
        await work
        return next(r for r in self.host.workflow.list() if r['id']==initial['id'])

    async def test_selected_provider_reaches_managed_execution(self):
        self.provider()
        for agent in ['codex','opencode']:
            tid='task_'+agent
            self.host.core.create_task(tid,'Set value to 42',['unit'],key=tid)
            initial=await self.host.workflow.start(tid,agent=agent)
            await self.host.workflow.active[tid]
            result=next(r for r in self.host.workflow.list() if r['id']==initial['id'])
            self.assertEqual(agent,self.selected_agent)
            self.assertEqual(agent,result['provider'])
            self.assertEqual('done',result['state'])

    async def test_invalid_provider_does_not_reserve_task(self):
        with self.assertRaisesRegex(DomainError,'UNSUPPORTED_AGENT'):
            await self.host.workflow.start('task_a',agent='unknown')
        self.assertEqual({},self.host.workflow.active)

    async def test_real_commit_and_independent_verification_finish_task(self):
        result=await self.run_task()
        self.assertEqual('done',result['state'])
        self.assertEqual('done',self.host.core.snapshot()['tasks'][0]['state'])
        self.assertEqual(self.base,git(self.root,'rev-parse','HEAD'))
        self.assertEqual('value=0\n',(self.root/'module.py').read_text())
        self.assertTrue(Path(result['artifact']).is_file())
        self.assertEqual(['inventory','expert_review'],[c['name'] for c in result['completion_checks']])
        self.assertEqual(3,len(result['command_ids']))
        self.assertTrue(all(self.host.core.command(cid)['state']=='completed' for cid in result['command_ids']))
        with self.assertRaisesRegex(DomainError,'SESSION_CLOSED'):
            await self.host.runtimes['s'].takeover('human',1,key='late')

    async def test_failed_checks_return_task_to_rework(self):
        result=await self.run_task('fail')
        self.assertEqual('rework_required',result['state'])
        self.assertEqual(['unit'],result['failed_checks'])
        self.assertEqual('todo',self.host.core.snapshot()['tasks'][0]['state'])

    async def test_takeover_never_commits_or_finishes(self):
        result=await self.run_task('takeover')
        self.assertEqual('reconciling',result['state'])
        self.assertEqual('blocked',self.host.core.snapshot()['tasks'][0]['state'])
        self.assertEqual(self.base,git(self.workspace,'rev-parse','HEAD'))

    async def test_timeout_is_not_automatic_rework(self):
        result=await self.run_task('timeout')
        self.assertEqual('reconciling',result['state'])
        self.assertEqual('RUN_TIMEOUT_RECONCILIATION_REQUIRED',result['reason'])

    async def test_modified_acceptance_file_is_rejected_before_commit(self):
        result=await self.run_task('protected')
        self.assertEqual('PROTECTED_ACCEPTANCE_FILE',result['reason'])
        self.assertEqual(self.base,git(self.workspace,'rev-parse','HEAD'))

    async def test_missing_owner_configuration_never_claims_task(self):
        (self.root/'.local/acceptance.json').unlink()
        with self.assertRaisesRegex(DomainError,'OWNER_CHECK_CONFIGURATION_REQUIRED'):
            await self.host.workflow.start('task_a')
        self.assertEqual('todo',self.host.core.snapshot()['tasks'][0]['state'])

    async def test_restart_blocks_active_lease_and_does_not_respawn(self):
        self.host.core.claim('task_a','agent:run_restart',key='lease')
        record={'id':'run_restart','task_id':'task_a','agent_id':'agent:run_restart'}
        self.host.workflow.save(record,'running')
        self.host.recover()
        self.assertEqual('reconciling',self.host.workflow.list()[0]['state'])
        self.assertEqual('blocked',self.host.core.snapshot()['tasks'][0]['state'])
        self.assertEqual({},self.host.runtimes)

    async def test_approval_guard_rejects_escape_and_protected_writes(self):
        self.host.workflow.guards['s']=(self.root,['test_module.py'])
        for file,code in [('..\\outside.py','WORKSPACE_OPERATION_REJECTED'),
                          ('test_module.py','PROTECTED_ACCEPTANCE_FILE'),('.git/config','WORKSPACE_OPERATION_REJECTED')]:
            with self.assertRaisesRegex(DomainError,code):
                self.host.workflow.guard('s',{'tool_name':'Write','input':{'file_path':file,'content':'anything'}})
        self.host.workflow.guard('s',{'tool_name':'Read','input':{'file_path':'test_module.py'}})

    async def test_batch_obeys_dependencies_and_verifies_integration(self):
        self.provider()
        self.host.core.create_task('task_b','Confirm behavior',['unit'],['task_a'],key='task_b')
        batch=await self.host.coordinator.start(['task_b','task_a'])
        async with asyncio.timeout(120):
            while self.host.coordinator.current:
                await asyncio.sleep(.1)
        final=next(b for b in self.host.coordinator.list() if b['id']==batch['id'])
        self.assertEqual('verified_candidate',final['state'])
        self.assertFalse(final['published'])
        runs=self.host.workflow.list()
        self.assertEqual(['task_a','task_b'],[r['task_id'] for r in runs])
        self.assertTrue(all(r['state']=='done' for r in runs))
        self.assertEqual(self.base,git(self.root,'rev-parse','HEAD'))

    async def test_batch_rework_is_bounded(self):
        self.provider('fail')
        await self.host.coordinator.start(['task_a'],max_attempts=1)
        async with asyncio.timeout(60):
            while self.host.coordinator.current:
                await asyncio.sleep(.1)
        final=self.host.coordinator.list()[0]
        self.assertEqual('rework_required',final['state'])
        self.assertEqual(1,final['attempts']['task_a'])

    async def test_followup_failure_blocks_commit_instead_of_accepting_first_claim(self):
        self.provider()
        send=self.host.send
        count=0
        async def fail_review(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:
                raise DomainError('REVIEW_TRANSPORT_FAILED')
            return await send(*args,**kwargs)
        self.host.send=fail_review
        initial=await self.host.workflow.start('task_a')
        await self.host.workflow.active['task_a']
        result=next(r for r in self.host.workflow.list() if r['id']==initial['id'])
        self.assertEqual('reconciling',result['state'])
        self.assertEqual('REVIEW_TRANSPORT_FAILED',result['reason'])
        self.assertEqual(self.base,git(self.workspace,'rev-parse','HEAD'))

    async def test_changed_policy_before_commit_blocks_old_acceptance(self):
        self.provider()
        original=self.host.send
        async def change_policy(*args,**kwargs):
            command=await original(*args,**kwargs)
            (self.root/'.local/acceptance.json').write_text(json.dumps({
                'checks':{'unit':[sys.executable,'-c','raise SystemExit(1)']},'protected_paths':['test_module.py']}))
            return command
        self.host.send=change_policy
        initial=await self.host.workflow.start('task_a')
        await self.host.workflow.active['task_a']
        result=next(r for r in self.host.workflow.list() if r['id']==initial['id'])
        self.assertEqual('OWNER_CHECK_CONFIGURATION_CHANGED',result['reason'])
        self.assertEqual('blocked',self.host.core.snapshot()['tasks'][0]['state'])
        self.assertEqual(self.base,git(self.workspace,'rev-parse','HEAD'))

    async def test_policy_changed_during_verifier_cannot_mark_done(self):
        self.provider()
        def check(*args,**kwargs):
            (self.root/'.local/acceptance.json').write_text(json.dumps({
                'checks':{'unit':[sys.executable,'test_module.py']},'protected_paths':['test_module.py','module.py']}))
            return {'passed':True,'status':'finished'}
        with patch('autocoding.verifier.execute',side_effect=check):
            initial=await self.host.workflow.start('task_a')
            await self.host.workflow.active['task_a']
        result=next(r for r in self.host.workflow.list() if r['id']==initial['id'])
        self.assertEqual('OWNER_CHECK_CONFIGURATION_CHANGED',result['reason'])
        self.assertEqual('blocked',self.host.core.snapshot()['tasks'][0]['state'])

    async def test_pending_approval_blocks_completion_checkpoint(self):
        self.provider()
        original=self.host.send
        async def unresolved(sid,*args,**kwargs):
            command=await original(sid,*args,**kwargs)
            with self.host.core.connection() as conn:
                generation=self.host.core._get(conn,'sessions',sid)['generation']
            self.host.approvals.register(sid,generation,'unanswered',{'tool':'write'},key='unanswered')
            return command
        self.host.send=unresolved
        initial=await self.host.workflow.start('task_a')
        await self.host.workflow.active['task_a']
        result=next(r for r in self.host.workflow.list() if r['id']==initial['id'])
        self.assertEqual('RUN_INTERACTION_RECONCILIATION_REQUIRED',result['reason'])
        self.assertEqual(self.base,git(self.workspace,'rev-parse','HEAD'))

    async def test_buffered_question_waiting_for_seal_lock_blocks_commit(self):
        await self.buffered_request('agent/question')

    async def test_buffered_unknown_request_also_blocks_commit(self):
        await self.buffered_request('provider/unknown')

    async def buffered_request(self, method):
        from autocoding.transport import TransportError
        self.provider()
        send=self.host.send
        count=0
        async def prepare_late_event(sid,*args,**kwargs):
            nonlocal count
            count+=1
            command=await send(sid,*args,**kwargs)
            if count==3:
                runtime=self.host.runtimes[sid]
                closing=asyncio.Event()
                emitted=False
                async def next_event(**unused):
                    nonlocal emitted
                    await closing.wait()
                    if not emitted:
                        emitted=True
                        return {'id':'late_fixture','method':method,'params':{'operation':{'questions':[]}}}
                    raise TransportError('TRANSPORT_CLOSED')
                async def close():
                    closing.set()
                    await asyncio.sleep(0)  # Let next_event wait on the seal lock.
                runtime.adapter.next_event=next_event
                runtime.adapter.close=close
                self.host.consumers[sid]=self.host._background(self.host._consume(sid,runtime))
            return command
        self.host.send=prepare_late_event
        initial=await self.host.workflow.start('task_a')
        await self.host.workflow.active['task_a']
        result=next(r for r in self.host.workflow.list() if r['id']==initial['id'])
        self.assertEqual('RUN_INTERACTION_RECONCILIATION_REQUIRED',result['reason'])
        self.assertEqual(self.base,git(self.workspace,'rev-parse','HEAD'))
        self.assertTrue(any(e['type']=='LateAgentInteraction' for e in self.host.core.events()))
