import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.acceptance import Acceptance
from autocoding.delegation import delegate,records,recover
from autocoding.planner import save_proposal
from autocoding.core import DomainError

class DelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.host=Host(self.temp.name,Path(self.temp.name)/'state')
        acceptance=Acceptance(self.host)
        acceptance.save(acceptance.snapshot()['fingerprint'],{'checks':{'unit':[sys.executable,'-V'],'edge':[sys.executable,'-V']}},key='checks')
        async def plan(goal,criteria):
            return save_proposal(self.host.core,[{'id':'task_delegated','description':goal,'acceptance':criteria,'dependencies':[]}],
                                 self.host.core.snapshot()['tasks'],self.host.redactor)
        self.host.propose=AsyncMock(side_effect=plan)
        self.host.coordinator.start=AsyncMock(return_value={'id':'batch_test','state':'queued'})
    async def asyncTearDown(self):
        await self.host.close();self.temp.cleanup()

    async def test_goal_is_adopted_and_executed_without_second_confirmation(self):
        result=await delegate(self.host,'实现中文界面',['unit'],agent='codex')
        self.assertEqual('executing',result['state'])
        self.assertEqual(['task_delegated'],self.host.coordinator.start.call_args.args[0])
        self.assertEqual(2,self.host.coordinator.start.call_args.kwargs['max_attempts'])
        self.assertIn('实现中文界面',self.host.core.snapshot()['tasks'][0]['description'])
        self.assertIn('业务验收标准',self.host.core.snapshot()['tasks'][0]['description'])
        self.assertEqual(result['id'],records(self.host)[0]['id'])

    async def test_pm_creates_its_own_check_instead_of_requiring_owner_configuration(self):
        result=await delegate(self.host,'目标',['not-configured'])
        self.assertEqual('executing',result['state'])
        self.host.propose.assert_awaited_once()
        policy=Acceptance(self.host).load()
        self.assertIn('pm_auto',policy['checks'])

    async def test_incomplete_plan_cannot_drop_an_acceptance_requirement(self):
        original=self.host.propose.side_effect
        async def incomplete(goal,criteria):return await original(goal,['unit'])
        self.host.propose.side_effect=incomplete
        with self.assertRaisesRegex(DomainError,'DELEGATION_ACCEPTANCE_GAP'):
            await delegate(self.host,'目标',['unit','edge'])
        self.assertEqual([],self.host.core.snapshot()['tasks'])
        self.host.coordinator.start.assert_not_awaited()
        self.assertEqual('stopped',records(self.host)[0]['state'])

    async def test_pause_during_planning_and_restart_do_not_start_work(self):
        original=self.host.propose.side_effect
        async def pause(goal,criteria):
            result=await original(goal,criteria);self.host.paused=True;return result
        self.host.propose.side_effect=pause
        with self.assertRaisesRegex(DomainError,'PROJECT_PAUSED'):
            await delegate(self.host,'目标',['unit'])
        self.assertEqual([],self.host.core.snapshot()['tasks'])
        with self.host.core.connection() as conn:
            conn.execute('INSERT INTO metadata VALUES (?,?)',('delegation:old',json.dumps({'id':'old','state':'planned'})))
        recover(self.host)
        self.assertEqual('reconciling',next(r for r in records(self.host) if r['id']=='old')['state'])
        self.host.coordinator.start.assert_not_awaited()
