import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.runtime import SessionRuntime
from autocoding.core import DomainError


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.host=Host(self.root,self.root/'state')
        self.addAsyncCleanup(self.host.close)
        self.host.core.create_session('s','claude',owner='human',key='s')
        self.host.metadata['s']={'agent':'claude'}
        self.host.contexts['s']='Use project conventions'
        self.adapter=type('Adapter',(),{'respond_question':AsyncMock(return_value={'state':'sent_unconfirmed'}),
                                      'close':AsyncMock()})()
        self.host.runtimes['s']=SessionRuntime(self.host.core,'s',self.adapter)

    def question(self, request='r1', text='Which output format?', secret=False):
        return self.host.questions.register('s',{'id':request,'params':{'operation':{'input':{'questions':[
            {'question':text,'options':[{'label':'JSON'},{'label':'YAML'}],'isSecret':secret}]}}}})

    async def resolve(self, record, answer='JSON'):
        self.host.questions.save(record,'awaiting_owner',reason='Convention not yet known')
        await self.host.questions.answer(record['id'],[[answer]])
        self.host.questions.delivered('s',record['provider_request_id'])

    async def test_answer_retrieval_does_not_rebuild_permission_dashboard(self):
        await self.resolve(self.question('r1'))
        current=self.question('r2')
        with patch.object(self.host.interactions,'rows',side_effect=AssertionError('Unbounded approval history scan')):
            history=self.host.interactions.relevant(current['questions'],self.host.contexts['s'])
        self.assertEqual(1,len(history))
        self.assertTrue(history[0]['same_context'])

    async def test_dashboard_aggregates_full_history_but_decodes_only_requested_page(self):
        with self.host.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for i in range(150):
                self.host.interactions.record(conn,'history_'+str(i),kind='question',content='Repeated question',
                                              state='awaiting_owner',requested=True)
            conn.commit()
        with patch.object(self.host.interactions,'rows',side_effect=AssertionError('Unbounded projection')):
            result=self.host.interactions.snapshot(140,10)
        self.assertEqual(150,result['total'])
        self.assertEqual(150,result['requests'])
        self.assertEqual(149,result['repeat_requests'])
        self.assertEqual(10,len(result['items']))
        self.assertTrue(all(r['same_question_requests']==150 for r in result['items']))

    async def test_counts_transitions_replays_pagination_and_restart(self):
        first=self.question()
        self.assertEqual(first['id'],self.question()['id'])
        await self.resolve(first)
        self.host.questions.delivered('s','r1')
        second=self.question('r2')
        self.host.questions.save(second,'awaiting_owner',reason='Different constraints')
        page=self.host.interactions.snapshot(limit=1)
        self.assertEqual((2,2,1,1,1),(page['total'],page['requests'],page['owner_answers'],page['repeat_requests'],page['pending']))
        self.assertEqual(1,len(page['items']))
        again=Host(self.root,self.root/'state')
        try:
            self.assertEqual(2,again.interactions.snapshot()['requests'])
            again.questions.recover()
            self.assertEqual(0,again.interactions.snapshot()['pending'])
            saved=next(r for r in again.interactions.rows() if r['id']==second['id'])
            self.assertEqual('Use project conventions',saved['context'])
        finally:
            await again.close()
        with self.assertRaises(DomainError):
            self.host.interactions.snapshot(-1)

    async def test_history_is_used_before_asking_and_conflicting_answers_remain_visible(self):
        await self.resolve(self.question('r1'),'JSON')
        await self.resolve(self.question('r2'),'YAML')
        third=self.question('r3')
        self.host.contexts['s']='New scope; do not assume old authorization'
        self.host.pm_text=AsyncMock(return_value='{"action":"escalate","reason":"Conflicting prior answers; which convention applies here?"}')
        await self.host.questions.choose(third)
        prompt=self.host.pm_text.await_args.args[0]
        self.assertIn('previous_owner_answers',prompt)
        self.assertIn('JSON',prompt)
        self.assertIn('YAML',prompt)
        history=self.host.interactions.relevant(third['questions'],self.host.contexts['s'])
        self.assertEqual(2,len(history))
        self.assertTrue(all(not r['same_context'] for r in history))
        self.assertTrue(any(e['type']=='PMInteractionHistoryUsed' for e in self.host.core.events()))
        self.assertEqual(2,self.adapter.respond_question.await_count)

    async def test_unconfirmed_expired_and_secret_answers_are_not_retrieved(self):
        first=self.question()
        await self.host.questions.answer(first['id'],[['JSON']])
        self.assertEqual([],self.host.interactions.relevant(first['questions'],''))
        self.host.questions.delivered('s','r1')
        now=self.host.core.clock()
        self.host.core.clock=lambda:now+91*86400
        self.assertEqual([],self.host.interactions.relevant(first['questions'],''))
        secret=self.question('secret','PRIVATE_SECRET_QUESTION',secret=True)
        self.host.pm_text=AsyncMock()
        await self.host.questions.choose(secret)
        self.host.pm_text.assert_not_awaited()
        item=next(r for r in self.host.interactions.rows() if r['id']==secret['id'])
        self.assertNotIn('PRIVATE_SECRET_QUESTION',json.dumps(item))
        self.assertTrue(item['requested'])

    async def test_owner_only_question_activity_is_review_evidence(self):
        await self.resolve(self.question())
        evidence,useful=self.host.reflection.evidence(0)
        self.assertTrue(useful)
        self.assertEqual(1,evidence['interaction_counts']['owner_answers'])
        self.assertEqual([['JSON']],evidence['interactions'][0]['answers'])

    async def test_truncated_context_display_does_not_hide_changed_constraints(self):
        self.host.contexts['s']='x'*4100+' only local'
        question=self.question()
        await self.resolve(question)
        same=self.host.interactions.relevant(question['questions'],self.host.contexts['s'])
        changed=self.host.interactions.relevant(question['questions'],'x'*4100+' publish externally')
        self.assertTrue(same[0]['same_context'])
        self.assertFalse(changed[0]['same_context'])

    async def test_permission_escalation_is_counted_once_and_owner_response_is_visible(self):
        operation={'tool_name':'Write','input':{'path':'example.txt'}}
        with self.host.core.connection() as conn:
            generation=self.host.core._get(conn,'sessions','s')['generation']
        approval=self.host.approvals.register('s',generation,'a1',operation,key='approval1')
        self.host.operations[approval['id']]=operation
        self.host.pm_text=AsyncMock(return_value='{"decision":"escalate","reason":"Need scope clarification"}')
        await self.host._review_approval(approval)
        await self.host._review_approval(approval)
        self.assertEqual(1,self.host.interactions.snapshot()['requests'])
        self.host.approvals.decide(approval['id'],'denied',approval['operation_hash'],approval['revision'],key='deny')
        page=self.host.interactions.snapshot()
        self.assertEqual(1,page['owner_answers'])
        self.assertEqual(0,page['pending'])
        self.assertEqual('denied',page['items'][0]['answers'])
        await self.host._review_approval(approval)
        self.assertEqual(1,self.host.interactions.snapshot()['requests'])
