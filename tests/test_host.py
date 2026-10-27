import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.claude import ClaudeSession
from autocoding.core import DomainError
from autocoding.host import Host
from autocoding.runtime import SessionRuntime
from autocoding.transport import JsonLineProcess


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def test_pm_response_timeout_preserves_only_safe_activity_diagnostic(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        adapter=SimpleNamespace(send_text=AsyncMock(return_value={'state':'acknowledged'}),
            next_event=AsyncMock(side_effect=TimeoutError()),close=AsyncMock(),
            last_error_diagnostic=None,
            activity_diagnostic=lambda phase:{'failure_phase':phase,'frames_seen':12,
                                                'last_frame':{'event_type':'system','event_subtype':'thinking_tokens'}})
        config=SimpleNamespace(api_key='PRIVATE_TEST_KEY',model='glm-5.3')
        with patch('autocoding.host.native_executable',return_value='claude.exe'), \
             patch('autocoding.host.resolve_zhipu',return_value=config), \
             patch('autocoding.host.claude_environment',return_value={}), \
             patch('autocoding.host.ClaudeSession.connect',new=AsyncMock(return_value=adapter)):
            with self.assertRaises(DomainError) as caught:
                await self.host.pm_text('review probe',include_learning=False)
        self.assertEqual('PM_RESPONSE_TIMEOUT',caught.exception.code)
        self.assertEqual('response_wait',caught.exception.diagnostic['failure_phase'])
        adapter.close.assert_awaited_once()

    async def test_managed_mcp_is_injected_for_all_providers_and_preserved_on_resume(self):
        import json
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        self.host.core.create_task('task_mcp','Implement',['unit'],key='task')
        lease=self.host.core.claim('task_mcp','agent:mcp',key='claim')
        scope={'task_id':'task_mcp','agent_id':'agent:mcp','attempt':lease['attempt']}
        config=SimpleNamespace(api_key='PRIVATE_TEST_KEY',model='fixture')
        for provider in ['claude','codex','opencode']:
            with self.subTest(provider=provider):
                adapter=SimpleNamespace(thread_id='thread_fixture',start_thread=AsyncMock(),close=AsyncMock())
                target={'claude':'autocoding.host.ClaudeSession.connect','codex':'autocoding.host.CodexSession.connect',
                        'opencode':'autocoding.opencode.OpenCodeSession.connect'}[provider]
                with patch('autocoding.host.native_executable',return_value=provider+'.exe'), \
                     patch.object(self.host.workspaces,'get',return_value={'path':str(self.host.root)}), \
                     patch('autocoding.host.resolve_zhipu',return_value=config), \
                     patch('autocoding.host.claude_environment',return_value={}), \
                     patch(target,new=AsyncMock(return_value=adapter)) as connect, \
                     patch.object(self.host,'_consume',new=AsyncMock()):
                    original=await self.host.start(provider,workspace_id='workspace_fixture',tools=True,task_scope=scope)
                    resumed=await self.host.start(provider,resume_from=original['session_id'])
                self.assertEqual(scope,resumed['task_scope'])
                if provider=='claude':
                    self.assertTrue(connect.call_args.kwargs['skip_permissions'])
                    server=json.loads(connect.call_args.kwargs['mcp_config'].read_text())['mcpServers']['autocoding']
                    args=server['args']
                elif provider=='codex':
                    self.assertEqual('never',adapter.start_thread.call_args.kwargs['approval_policy'])
                    args=adapter.start_thread.call_args.kwargs['mcp_servers']['autocoding']['args']
                else:
                    child=json.loads(connect.call_args.kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                    self.assertFalse(connect.call_args.kwargs['glob_compat'])
                    self.assertEqual('@ai-sdk/openai-compatible',child['provider']['zhipuai-coding-plan']['npm'])
                    self.assertEqual('https://open.bigmodel.cn/api/coding/paas/v4',
                                     child['provider']['zhipuai-coding-plan']['options']['baseURL'])
                    self.assertEqual('PRIVATE_TEST_KEY',child['provider']['zhipuai-coding-plan']['options']['apiKey'])
                    self.assertEqual('true',connect.call_args.kwargs['env']['OPENCODE_DISABLE_MODELS_FETCH'])
                    args=child['mcp']['autocoding']['command'][1:]
                    self.assertEqual('allow',child['permission']['autocoding_*'])
                    self.assertEqual('allow',child['permission']['*'])
                    self.assertEqual('bypass',connect.call_args.kwargs['permission_mode'])
                self.assertEqual('task_mcp',args[args.index('--task')+1])
                self.assertNotIn(lease['lease_token'],str(args))
                self.assertNotIn('PRIVATE_TEST_KEY',str(args)+str(resumed))

    async def test_pm_review_receives_workspace_and_escalation_is_audited(self):
        import json
        from unittest.mock import AsyncMock
        session=self.host.core.create_session('s','opencode',owner='human',key='s')
        approval=self.host.approvals.register('s',session['generation'],'per_test',{'permission':'edit'},key='a')
        self.host.operations[approval['id']]={'permission':'edit'}
        self.host.metadata['s']={'cwd':str(self.host.root)}
        self.host.workflow.guards['s']=(self.host.root,['test.py'])
        self.host.pm_text=AsyncMock(return_value=json.dumps({'decision':'escalate','reason':'Need scope clarification'}))
        await self.host._review_approval(approval)
        prompt=self.host.pm_text.call_args.args[0]
        self.assertIn('protected_paths',prompt)
        self.assertIn('test.py',prompt)
        self.assertEqual('pending',self.host.approvals.list()[0]['state'])
        self.assertEqual('approval_owner_required',self.host.output[-1]['type'])
        self.assertTrue(any(e['type']=='PMApprovalEscalated' for e in self.host.core.events()))

    async def test_pm_permission_receipt_is_recorded_and_never_resent(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        session=self.host.core.create_session('s','opencode',owner='human',key='s')
        operation={'permission':'edit','patterns':['calculator.py']}
        approval=self.host.approvals.register('s',session['generation'],'per_test',operation,key='permission')
        self.host.operations[approval['id']]=operation
        adapter=SimpleNamespace(respond_approval=AsyncMock(return_value={'state':'delivered','tool_use_id':'tool_1'}))
        self.host.runtimes['s']=SimpleNamespace(adapter=adapter,close=AsyncMock())
        for _ in range(2):
            await self.host.decide(approval['id'],'approved',approval['operation_hash'],approval['revision'],key='decision',actor='pm')
        self.assertEqual(1,adapter.respond_approval.await_count)
        self.assertEqual({},self.host.deliveries)
        self.assertEqual('pm',self.host.approvals.list()[0]['decided_by'])
        with self.host.core.connection() as conn:
            self.assertEqual('delivered',conn.execute('SELECT state FROM approval_delivery').fetchone()['state'])

    async def test_resume_uses_exact_provider_and_original_storage(self):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        state=self.host.storage/'session_original'
        state.mkdir()
        self.host.metadata['old']={'agent':'codex','provider_session_id':'thread_exact',
                                  'state_directory':str(state),'restart_count':0}
        adapter=SimpleNamespace(thread_id='thread_exact',start_thread=AsyncMock(),close=AsyncMock())
        with patch('autocoding.host.native_executable',return_value='codex.exe'), \
             patch('autocoding.host.CodexSession.connect',new=AsyncMock(return_value=adapter)), \
             patch.object(self.host,'_consume',new=AsyncMock()):
            result=await self.host.start('codex',resume_from='old')
        adapter.start_thread.assert_awaited_once_with(self.host.root,sandbox='danger-full-access',resume='thread_exact',approval_policy='never')
        self.assertEqual(str(state),result['state_directory'])
        self.assertEqual('old',result['resumed_from'])
        self.assertEqual(1,result['restart_count'])

    async def test_process_failure_restarts_boundedly_without_resending_input(self):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        from autocoding.transport import TransportError
        for attempts,closed,expected,code in [(0,False,1,'PROCESS_STREAM_CLOSED'),(0,False,1,'CLAUDE_ACK_TIMEOUT_DELIVERY_UNKNOWN'),
                                              (2,False,0,'PROCESS_STREAM_CLOSED'),(0,True,0,'PROCESS_STREAM_CLOSED')]:
            self.host.metadata['s']={'restart_count':attempts}
            runtime=SimpleNamespace(closed=closed,close=AsyncMock(),
                next_event=AsyncMock(side_effect=TransportError(code)))
            with patch.object(self.host,'_restart',new=AsyncMock()) as restart:
                await self.host._consume('s',runtime)
                await asyncio.gather(*list(self.host.tasks))
                self.assertEqual(expected,restart.await_count)

    async def test_unexpected_event_failure_logs_type_without_exception_text(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        runtime=SimpleNamespace(closed=False,
            next_event=AsyncMock(side_effect=ValueError('PRIVATE_PROVIDER_FRAME')),
            close=AsyncMock())
        self.host.metadata['s']={}

        await self.host._consume('s',runtime)

        event=self.host.output[-1]
        self.assertEqual('SESSION_EVENT_FAILED',event['code'])
        self.assertEqual('ValueError',event['error_type'])
        self.assertNotIn('PRIVATE_PROVIDER_FRAME',str(event))

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host=Host(self.temp.name,Path(self.temp.name)/'storage')
        self.addAsyncCleanup(self.host.close)

    async def connect(self,mode='normal'):
        transport=await JsonLineProcess.start([sys.executable,'-u',str(ROOT/'tests/fixtures/claude_agent.py'),mode],cwd=ROOT)
        adapter=ClaudeSession(transport,session_id='session_fixture',timeout=2 if mode=='silent' else 10)
        self.host.core.create_session('s','claude',owner='human',key='s')
        runtime=SessionRuntime(self.host.core,'s',adapter)
        self.host.runtimes['s']=runtime
        self.host.pending['s']={}
        self.host.pumps['s']=asyncio.Lock()
        self.host.streams['s']=self.host.redactor.stream()
        self.host._background(self.host._consume('s',runtime))

    async def wait_state(self,command,state):
        async with asyncio.timeout(30):
            while self.host.core.command(command)['state']!=state:
                await asyncio.sleep(.02)

    async def test_queued_turns_dispatch_serially_and_replay_never_resends(self):
        await self.connect()
        first=await self.host.send('s',0,'first',key='first')
        second=await self.host.send('s',0,'second',key='second')
        await self.wait_state(second['command_id'],'completed')
        self.assertEqual('completed',self.host.core.command(first['command_id'])['state'])
        replay=await self.host.send('s',0,'second',key='second')
        self.assertEqual(second['command_id'],replay['command_id'])
        self.assertEqual('completed',replay['state'])
        self.assertEqual(2,len([e for e in self.host.core.events() if e['type']=='CommandDispatching']))

    async def test_unknown_first_delivery_prevents_second_dispatch(self):
        await self.connect('silent')
        first=await self.host.send('s',0,'first',key='first')
        second=await self.host.send('s',0,'second',key='second')
        await self.wait_state(first['command_id'],'delivery_unknown')
        await self.host._drain('s')
        self.assertEqual('queued',self.host.core.command(second['command_id'])['state'])
        self.assertEqual(1,len([e for e in self.host.core.events() if e['type']=='CommandDispatching']))

    async def test_unsupported_claude_frame_shape_is_visible_without_values(self):
        await self.connect('unsupported-event')
        receipt=await self.host.send('s',0,'probe unsupported event',key='unsupported-event')
        await self.wait_state(receipt['command_id'],'delivery_unknown')
        await asyncio.gather(*list(self.host.tasks))
        event=next(item for item in self.host.output if item['type']=='error'
                   and item.get('protocol_diagnostic'))
        self.assertEqual('CLAUDE_UNSUPPORTED_EVENT',event['code'])
        self.assertEqual('rate_limit_event',event['protocol_diagnostic']['event_type'])
        self.assertNotIn('PRIVATE_CANARY',str(event))

    async def test_claude_tool_progress_is_visible_in_public_output(self):
        await self.connect('tool-progress')
        receipt=await self.host.send('s',0,'probe tool progress',key='tool-progress')
        await self.wait_state(receipt['command_id'],'completed')
        event=next(item for item in self.host.output if item['type']=='tool_progress')
        self.assertEqual('Bash',event['activity'])
        self.assertEqual(12.5,event['elapsed_seconds'])

    async def test_queue_limit_keeps_idempotent_retry_available(self):
        self.host.core.create_session('s','fixture',owner='human',key='s')
        first=None
        for n in range(128):
            result=self.host.core.enqueue('s','human',0,'text',key=str(n))
            first=first or result
        with self.assertRaisesRegex(DomainError,'INPUT_QUEUE_FULL'):
            self.host.core.enqueue('s','human',0,'text',key='overflow')
        self.assertEqual(first,self.host.core.enqueue('s','human',0,'text',key='0'))
