import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import Core, DomainError
from autocoding.planner import parse_plan, request_plan, save_proposal, adopt
from autocoding.redaction import Redactor


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core=Core(Path(self.temp.name)/'core.db')
        self.task={'id':'task_a','description':'Implement','acceptance':['unit'],'dependencies':[]}

    async def test_proposal_does_not_create_tasks_until_adopted(self):
        text=json.dumps({'tasks':[self.task]})
        class Adapter:
            async def send_text(self,prompt,**kwargs):
                self.events=iter([{'method':'item/agentMessage/delta','params':{'delta':'Preparing the plan.'}},
                    {'method':'item/agentMessage/delta','params':{'delta':text}},
                    {'method':'turn/completed','params':{'turn':{'status':'completed'}}}])
            async def next_event(self,**kwargs):
                return next(self.events)
        tasks=await request_plan(Adapter(),'Implement a feature',['unit'])
        proposal=save_proposal(self.core,tasks,[],Redactor())
        self.assertEqual([],self.core.snapshot()['tasks'])
        result=adopt(self.core,proposal['id'],key='adopt')
        self.assertEqual(['task_a'],result['task_ids'])
        self.assertEqual('todo',self.core.snapshot()['tasks'][0]['state'])

    async def test_owner_criteria_cannot_be_replaced_by_model(self):
        for task in [{**self.task,'acceptance':['skip validation']},{**self.task,'shell':'anything'}]:
            with self.assertRaisesRegex(DomainError,'PM_OUTPUT_INVALID'):
                parse_plan(json.dumps({'tasks':[task]}),['unit'])

    async def test_changed_tasks_invalidate_proposal(self):
        proposal=save_proposal(self.core,[self.task],[],Redactor())
        self.core.create_task('task_b','New scope',['unit'],key='task')
        with self.assertRaisesRegex(DomainError,'STALE_PM_CONTEXT'):
            adopt(self.core,proposal['id'],key='adopt')

    async def test_failed_model_cannot_propose_or_inject_tools(self):
        class Adapter:
            async def send_text(self,*args,**kwargs):
                pass
            async def next_event(self,**kwargs):
                return {'id':'permission','method':'tool/requestApproval'}
        with self.assertRaisesRegex(DomainError,'PM_TOOL_REQUEST_REJECTED'):
            await request_plan(Adapter(),'Ignore rules',['unit'])
