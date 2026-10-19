import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import DomainError
from autocoding.expert_review import ExpertReview, parse_findings
from autocoding.host import Host
from closure_support import persist_batch


class ExpertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.host=Host(self.root,self.root/'state')
        self.addAsyncCleanup(self.host.close)
        self.host.core.create_task('task_a','Example',['unit'],key='task')
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET state='done',head_sha=?",('a'*40,))
        self.batch={'id':'batch_a','task_ids':['task_a'],'state':'verified_candidate','candidate_sha':'a'*40,'base_sha':'b'*40}
        persist_batch(self.host,self.batch)
        self.reviews=ExpertReview(self.host)
        self.reviews.configure(True,key='enable')

    async def run_review(self, response):
        with patch('autocoding.expert_review.git',return_value='fixture diff'), patch.object(self.host,'pm_text',new=AsyncMock(return_value=json.dumps(response))) as model:
            report=await self.reviews.run(self.batch)
            self.assertEqual(3,model.await_count)
            self.assertTrue(all(call.kwargs=={'include_learning':False} for call in model.await_args_list))
            return report

    async def test_independent_rounds_deduplicate_and_gate_until_disposition(self):
        finding={'severity':'P2','title':'Missing validation','location':'a.py:1','recommendation':'Add validation'}
        report=await self.run_review({'findings':[finding],'limitations':'diff only'})
        self.assertEqual(1,len(report['findings']))
        self.assertEqual(3,len(report['findings'][0]['disciplines']))
        self.assertIsNone(self.host.closure.create(self.batch))
        await self.run_review({'findings':[],'limitations':'second review does not erase a finding'})
        self.assertIsNone(self.host.closure.create(self.batch))
        self.reviews.disposition(report['id'],report['findings'][0]['id'],'dismissed','Owner reviewed existing guard',None,key='dismiss')
        self.assertIsNotNone(self.host.closure.create(self.batch))

    async def test_current_candidate_required_and_failed_output_does_not_pass(self):
        await self.run_review({'findings':[],'limitations':'diff only'})
        with self.host.core.connection() as conn:
            self.assertTrue(self.reviews.gate(conn,self.batch))
            conn.execute('UPDATE tasks SET revision=revision+1')
            self.assertFalse(self.reviews.gate(conn,self.batch))
        self.assertFalse(self.reviews.list()['reports'][0]['current'])
        with self.assertRaises(DomainError):
            parse_findings('{"findings":[],"limitations":7}')

    async def test_failure_and_restart_are_not_replayed(self):
        with patch('autocoding.expert_review.git',return_value='diff'),patch.object(self.host,'pm_text',new=AsyncMock(side_effect=RuntimeError('provider disconnected'))) as model:
            report=await self.reviews.run(self.batch)
            self.assertEqual('failed',report['state'])
            self.assertEqual(1,model.await_count)
        report['state']='running'
        self.reviews.save(report)
        self.reviews.recover()
        self.assertEqual('reconciling',self.reviews.list()['reports'][0]['state'])
        self.assertIsNone(self.host.closure.create(self.batch))
