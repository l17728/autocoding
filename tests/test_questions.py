import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import DomainError
from autocoding.host import Host
from autocoding.runtime import SessionRuntime
from autocoding.questions import normalize, validate_answers


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host=Host(self.temp.name,Path(self.temp.name)/'storage')
        self.addAsyncCleanup(self.host.close)
        self.host.core.create_session('s','claude',owner='human',key='s')
        self.adapter=type('Adapter',(),{'respond_question':AsyncMock(return_value={'state':'sent_unconfirmed'}),
                                      'close':AsyncMock()})()
        self.host.runtimes['s']=SessionRuntime(self.host.core,'s',self.adapter)
        self.host.metadata['s']={'agent':'claude'}
        self.host.contexts['s']='Use JSON and team_ prefix'
        self.operation={'tool_name':'AskUserQuestion','input':{'questions':[
            {'question':'Format?','options':[{'label':'JSON'},{'label':'YAML'}],'multiSelect':False},
            {'question':'Prefix?','options':[{'label':'app_'},{'label':'lib_'}],'multiSelect':False}]}}

    def question(self):
        return self.host.questions.register('s',{'id':'request_1','params':{'operation':self.operation}})

    async def test_pm_selects_and_supplies_custom_text_once(self):
        record=self.question()
        self.host.pm_text=AsyncMock(return_value=json.dumps({'action':'answer','answers':[['JSON'],['team_']],
                                                            'reason':'Follow the task requirements'}))
        await self.host.questions.choose(record)
        final=self.host.questions.list()[0]
        self.assertEqual('pm',final['answered_by'])
        self.assertEqual('sent_unconfirmed',final['state'])
        self.assertEqual([['JSON'],['team_']],final['answers'])
        await self.host.questions.answer(record['id'],[['JSON'],['team_']])
        self.assertEqual(1,self.adapter.respond_question.await_count)
        self.host.questions.delivered('s','request_1')
        self.assertEqual('delivered',self.host.questions.list()[0]['state'])

    async def test_pm_escalation_preserves_question_for_owner(self):
        record=self.question()
        self.host.pm_text=AsyncMock(return_value='{"action":"escalate","reason":"Scope choice"}')
        await self.host.questions.choose(record)
        self.assertEqual('awaiting_owner',self.host.questions.list()[0]['state'])
        self.adapter.respond_question.assert_not_awaited()
        await self.host.questions.answer(record['id'],[['YAML'],['custom_']])
        self.assertEqual('owner',self.host.questions.list()[0]['answered_by'])

    async def test_late_pm_escalation_does_not_overwrite_owner_answer(self):
        record=self.question()
        async def decision(prompt):
            await self.host.questions.answer(record['id'],[['JSON'],['team_']])
            return '{"action":"escalate","reason":"late"}'
        self.host.pm_text=decision
        await self.host.questions.choose(record)
        self.assertEqual('sent_unconfirmed',self.host.questions.list()[0]['state'])

    async def test_takeover_invalidates_pending_pm_choice(self):
        record=self.question()
        self.host.core.takeover('s','pm',0,key='takeover')
        with self.assertRaisesRegex(DomainError,'STALE_AGENT_QUESTION'):
            await self.host.questions.answer(record['id'],[['JSON'],['team_']])
        self.adapter.respond_question.assert_not_awaited()

    async def test_invalid_selection_or_multiselect_cannot_be_sent(self):
        questions=normalize('opencode',{'questions':[{'question':'Pick','options':[{'label':'A'}],'custom':False}]})
        for answers in [[['B']],[['A','A']],[]]:
            with self.assertRaisesRegex(DomainError,'INVALID_QUESTION_ANSWERS'):
                validate_answers(questions,answers)

    async def test_secret_question_never_calls_pm(self):
        self.host.metadata['s']['agent']='codex'
        record=self.host.questions.register('s',{'id':1,'params':{'operation':{'questions':[
            {'id':'secret','question':'API credential?','isSecret':True,'options':None}]}}})
        self.host.pm_text=AsyncMock()
        await self.host.questions.choose(record)
        self.host.pm_text.assert_not_awaited()
        self.assertEqual('awaiting_owner',self.host.questions.list()[0]['state'])

    async def test_failed_delivery_is_never_resent(self):
        record=self.question()
        self.adapter.respond_question.side_effect=RuntimeError('PRIVATE_CANARY')
        with self.assertRaises(RuntimeError):
            await self.host.questions.answer(record['id'],[['JSON'],['team_']])
        self.assertEqual('delivery_unknown',self.host.questions.list()[0]['state'])
        with self.assertRaisesRegex(DomainError,'QUESTION_ALREADY_ANSWERED'):
            await self.host.questions.answer(record['id'],[['JSON'],['team_']])
        self.assertNotIn('PRIVATE_CANARY',str(self.host.core.events()))
