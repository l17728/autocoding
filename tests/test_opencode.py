import asyncio
import pathlib
import sys
import unittest
import httpx
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from autocoding.opencode import OpenCodeSession
from autocoding.transport import TransportError


class OpenCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_tool_requires_correlated_terminal_evidence(self):
        from autocoding.core import digest
        operation={'id':'per_denied','sessionID':'ses_test','tool':{'callID':'tool_denied'}}
        mode='unrelated';replies=[]
        def handler(request):
            if request.url.path.endswith('/reply'):
                replies.append(request.content);return httpx.Response(200,json=True)
            if request.url.path=='/permission':return httpx.Response(200,json=[operation] if mode=='pending' else [])
            if request.url.path=='/question':return httpx.Response(200,json=[])
            if request.url.path=='/session/status':return httpx.Response(200,json={'ses_test':{'type':'busy' if mode=='busy' else 'idle'}})
            info={'role':'assistant','parentID':'msg_test','sessionID':'ses_test','time':{'completed':1},'finish':'tool-calls'}
            part={'type':'tool','callID':'other' if mode=='unrelated' else 'tool_denied','state':{'status':'running' if mode=='running' else 'error'}}
            return httpx.Response(200,json=[{'info':info,'parts':[part]}])
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client);adapter.thread_id='ses_test';adapter.turn_id='msg_test'
        self.addAsyncCleanup(adapter.close)
        adapter._approvals[operation['id']]=operation
        await adapter.respond_approval(operation['id'],digest(operation),allow=False)
        adapter._poller=asyncio.create_task(adapter._poll('msg_test'))
        for mode in ('unrelated','busy','pending','running'):
            with self.subTest(mode=mode),self.assertRaises(TimeoutError):await adapter.next_event(timeout=.6)
        mode='terminal'
        event=await adapter.next_event(timeout=2)
        self.assertEqual('interrupted',event['params']['turn']['status'])
        self.assertIsNone(adapter.turn_id)
        self.assertEqual(1,len(replies))

    async def test_write_timeout_is_distinct_and_never_retried(self):
        count=0
        def handler(request):
            nonlocal count
            count+=1
            raise httpx.ReadTimeout('PRIVATE_CANARY',request=request)
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client)
        self.addAsyncCleanup(adapter.close)
        for _ in range(2):
            with self.assertRaisesRegex(TransportError,'^OPENCODE_HTTP_TIMEOUT$'):
                await adapter._request('POST','/session')
        self.assertEqual(1,count)

    async def test_permission_poll_and_once_reply_are_session_bound(self):
        from autocoding.core import digest
        import json
        operation={'id':'per_test','sessionID':'ses_test','permission':'edit',
                   'patterns':['calculator.py'],'metadata':{},'always':['*'],'tool':{'callID':'tool_1'}}
        replies=[]
        def handler(request):
            if request.url.path=='/session/status':
                return httpx.Response(200,json={})
            if request.url.path=='/permission':
                return httpx.Response(200,json=[dict(operation,id='per_other',sessionID='ses_other'),operation])
            if request.url.path.endswith('/reply'):
                replies.append(json.loads(request.content))
                return httpx.Response(200,json=True)
            return httpx.Response(200,json=[])
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client)
        adapter.thread_id='ses_test'
        self.addAsyncCleanup(adapter.close)
        adapter._poller=asyncio.create_task(adapter._poll('msg_test'))
        event=await adapter.next_event(timeout=2)
        self.assertEqual('tool/requestApproval',event['method'])
        self.assertEqual('per_test',event['id'])
        with self.assertRaisesRegex(TransportError,'APPROVAL_OPERATION_MISMATCH'):
            await adapter.respond_approval('per_test','wrong',allow=True)
        result=await adapter.respond_approval('per_test',digest(operation),allow=True)
        self.assertEqual('delivered',result['state'])
        self.assertEqual([{'reply':'once'}],replies)
        with self.assertRaisesRegex(TransportError,'APPROVAL_OPERATION_MISMATCH'):
            await adapter.respond_approval('per_test',digest(operation),allow=True)

    async def adapter(self,mode='normal'):
        self.mid=None
        self.posts=0
        def handler(request):
            if request.url.path.endswith('/prompt_async'):
                import json
                self.mid=json.loads(request.content)['messageID']
                self.posts+=1
                return httpx.Response(204)
            if request.url.path.endswith('/abort'):
                return httpx.Response(200,json=True)
            if mode=='http_error': return httpx.Response(401,text='PRIVATE_CANARY')
            if request.url.path=='/session/status':
                return httpx.Response(200,json={})
            message={'info':{'role':'assistant','parentID':self.mid,'sessionID':'ses_test',
                            'time':{'completed':1}},'parts':[{'type':'text','text':'公开回复'}]}
            if mode=='provider_error': message['info']['error']={'message':'PRIVATE_CANARY'}
            if mode=='wrong_session': message['info']['sessionID']='ses_other'
            if mode=='pending': message['info']['time']={}
            return httpx.Response(200,json=[{'info':{'role':'assistant','parentID':'old'}},message])
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client)
        adapter.thread_id='ses_test'
        self.addAsyncCleanup(adapter.close)
        return adapter

    async def test_receipt_correlated_result_and_multiple_turns(self):
        adapter=await self.adapter()
        for n in range(2):
            receipt=await adapter.send_text('中文\n$(ordinary text)',command_id=str(n))
            self.assertEqual('acknowledged',receipt['state'])
            text=await adapter.next_event(timeout=2)
            self.assertEqual('公开回复',text['params']['delta'])
            complete=await adapter.next_event(timeout=2)
            self.assertEqual(receipt['turn_id'],complete['params']['turn']['id'])
            self.assertEqual('completed',complete['params']['turn']['status'])
        self.assertEqual(2,self.posts)

    async def test_provider_error_emits_failure_without_private_text(self):
        adapter=await self.adapter('provider_error')
        await adapter.send_text('hello',command_id='1')
        event=await adapter.next_event(timeout=2)
        self.assertEqual('failed',event['params']['turn']['status'])
        self.assertNotIn('PRIVATE_CANARY',str(event))

    async def test_tool_step_and_busy_session_do_not_finish_turn(self):
        ready=False
        def handler(request):
            if request.url.path in {'/permission','/question'}:
                return httpx.Response(200,json=[])
            if request.url.path=='/session/status':
                return httpx.Response(200,json={'ses_test':{'type':'idle' if ready else 'busy'}})
            message={'info':{'role':'assistant','parentID':'msg_test','sessionID':'ses_test',
                            'time':{'completed':1},'finish':'stop' if ready else 'tool-calls'},
                     'parts':[{'type':'text','text':'final' if ready else 'working'}]}
            return httpx.Response(200,json=[message])
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client)
        adapter.thread_id='ses_test'
        self.addAsyncCleanup(adapter.close)
        adapter._poller=asyncio.create_task(adapter._poll('msg_test'))
        with self.assertRaises(TimeoutError):
            await adapter.next_event(timeout=.1)
        ready=True
        self.assertEqual('final',(await adapter.next_event(timeout=2))['params']['delta'])
        self.assertEqual('turn/completed',(await adapter.next_event(timeout=2))['method'])

    async def test_http_error_and_wrong_session_fail_closed(self):
        for mode in ['http_error','wrong_session']:
            adapter=await self.adapter(mode)
            await adapter.send_text('hello',command_id='1')
            with self.assertRaises(TransportError) as exc:
                await adapter.next_event(timeout=2)
            self.assertNotIn('PRIVATE_CANARY',str(exc.exception))

    async def test_interrupt_ack_is_not_completion(self):
        adapter=await self.adapter('pending')
        await adapter.send_text('hello',command_id='1')
        result=await adapter.interrupt()
        self.assertEqual('interrupt_requested',result['state'])
        self.assertIsNotNone(adapter.turn_id)

    async def test_oversized_stream_stops_reading_at_limit(self):
        class Stream(httpx.AsyncByteStream):
            count=0
            closed=False
            async def __aiter__(self):
                for _ in range(1000):
                    self.count+=1
                    yield b'x'*65536
            async def aclose(self):
                self.closed=True
        stream=Stream()
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=stream)),
                                 base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client)
        self.addAsyncCleanup(adapter.close)
        with self.assertRaisesRegex(TransportError,'FRAME_TOO_LARGE'):
            await adapter._request('GET','/large')
        self.assertEqual(65,stream.count)
        self.assertTrue(stream.closed)

    async def test_question_reply_supports_custom_answer(self):
        from autocoding.core import digest
        operation={'id':'que_test','sessionID':'ses_test','questions':[{'question':'Prefix?',
            'options':[{'label':'app_','description':'default'}],'custom':True}]}
        received=[]
        def handler(request):
            import json
            received.append((request.url.path,json.loads(request.content)))
            return httpx.Response(200,json=True)
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:9999')
        adapter=OpenCodeSession(client)
        adapter._questions['que_test']=operation
        self.addAsyncCleanup(adapter.close)
        response=await adapter.respond_question('que_test',digest(operation),[['team_']])
        self.assertEqual('delivered',response['state'])
        self.assertEqual([('/question/que_test/reply',{'answers':[['team_']]})],received)
        with self.assertRaisesRegex(TransportError,'QUESTION_ANSWER_MISMATCH'):
            await adapter.respond_question('que_test',digest(operation),[['team_']])
