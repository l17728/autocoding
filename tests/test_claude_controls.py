import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.claude import ClaudeSession
from autocoding.core import digest
from autocoding.transport import TransportError


class Transport:
    def __init__(self):
        self.messages=[]
        self.incoming=asyncio.Queue()

    async def send(self,message):
        self.messages.append(message)

    async def receive(self):
        return await self.incoming.get()

    def poison(self,code):
        pass

    async def close(self):
        pass


class ClaudeControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport=Transport()
        self.adapter=ClaudeSession(self.transport,session_id='fixture',controls=True,tools_enabled=True)
        self.addAsyncCleanup(self.adapter.close)
        self.operation={'subtype':'can_use_tool','tool_name':'Write','tool_use_id':'tool_1',
                        'input':{'file_path':'fixture.py','content':'value=1'}}

    def permission(self):
        self.adapter._handle({'type':'control_request','request_id':'permission_1','request':self.operation})

    async def test_control_response_is_correlated_and_interrupt_is_only_requested(self):
        self.adapter.turn_id='turn_1'
        task=asyncio.create_task(self.adapter.interrupt())
        await asyncio.sleep(0)
        request=self.transport.messages[-1]
        self.assertEqual('interrupt',request['request']['subtype'])
        self.adapter._handle({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{}}})
        self.assertEqual('interrupt_requested',(await task)['state'])
        self.assertFalse(self.adapter._result_received)

    async def test_permission_hash_and_single_delivery(self):
        self.permission()
        with self.assertRaisesRegex(TransportError,'OPERATION_MISMATCH'):
            await self.adapter.respond_approval('permission_1','bad-hash',allow=True)
        self.assertEqual([],self.transport.messages)
        sent=await self.adapter.respond_approval('permission_1',digest(self.operation),allow=True)
        self.assertEqual('sent_unconfirmed',sent['state'])
        reply=self.transport.messages[-1]['response']['response']
        self.assertEqual(self.operation['input'],reply['updatedInput'])
        with self.assertRaisesRegex(TransportError,'OPERATION_MISMATCH'):
            await self.adapter.respond_approval('permission_1',digest(self.operation),allow=True)
        self.adapter._handle({'type':'control_response','response':{'subtype':'success','request_id':'permission_1'}})
        await self.adapter.next_event()
        self.assertEqual('approval/delivered',(await self.adapter.next_event())['method'])

    async def test_cancelled_permission_cannot_be_approved(self):
        self.permission()
        self.adapter._handle({'type':'control_cancel_request','request_id':'permission_1'})
        with self.assertRaisesRegex(TransportError,'OPERATION_MISMATCH'):
            await self.adapter.respond_approval('permission_1',digest(self.operation),allow=True)

    async def test_progress_without_uuid_never_relaxes_final_correlation(self):
        self.adapter.turn_id='turn_1'
        self.adapter._ack=asyncio.get_running_loop().create_future()
        self.adapter._ack.set_result(True)
        self.adapter._handle({'type':'assistant','session_id':'fixture','message':{'role':'assistant',
            'content':[{'type':'text','text':'progress'}]}})
        self.assertEqual('progress',(await self.adapter.next_event())['params']['delta'])
        with self.assertRaisesRegex(TransportError,'TURN_MISMATCH'):
            self.adapter._handle({'type':'result','session_id':'fixture','subtype':'success','is_error':False})
        self.assertFalse(self.adapter._result_received)

    async def test_tool_child_assistant_frame_is_progress_not_protocol_failure(self):
        self.adapter.turn_id='turn_1'
        self.adapter._ack=asyncio.get_running_loop().create_future()
        self.adapter._ack.set_result(True)
        self.adapter._handle({'type':'assistant','session_id':'fixture','parent_tool_use_id':'tool_1',
            'message':{'role':'assistant','content':[{'type':'text','text':'private tool progress'}]}})
        self.assertFalse(self.adapter._result_received)
        self.assertTrue(self.adapter._events.empty())

    async def test_success_subtype_with_error_is_failure(self):
        self.adapter.turn_id='turn_1'
        self.adapter._handle({'type':'result','session_id':'fixture','subtype':'success','is_error':True,
            'user_message_uuid':'turn_1','user_message_uuids':['turn_1'],'result':'PRIVATE_CANARY'})
        event=await self.adapter.next_event()
        self.assertEqual('failed',event['params']['turn']['status'])
        self.assertNotIn('PRIVATE_CANARY',str(event))

    async def test_control_cancellation_preserves_cancellation(self):
        task=asyncio.create_task(self.adapter.control({'subtype':'initialize'}))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual('CLAUDE_CONTROL_DELIVERY_UNKNOWN',self.adapter._error)

    async def test_question_reply_preserves_questions_and_custom_answer(self):
        self.operation={'subtype':'can_use_tool','tool_name':'AskUserQuestion','tool_use_id':'tool_1',
                        'input':{'questions':[{'question':'Format?','options':[{'label':'JSON'}]},
                                              {'question':'Prefix?','options':[{'label':'app_'}]}]}}
        self.permission()
        event=await self.adapter.next_event()
        self.assertEqual('agent/question',event['method'])
        await self.adapter.respond_question('permission_1',digest(self.operation),[['JSON'],['team_']])
        value=self.transport.messages[-1]['response']['response']['updatedInput']
        self.assertEqual(self.operation['input']['questions'],value['questions'])
        self.assertEqual({'Format?':'JSON','Prefix?':'team_'},value['answers'])
