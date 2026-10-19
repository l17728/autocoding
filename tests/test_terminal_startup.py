import asyncio
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError
from autocoding.transport import TransportError
from autocoding.terminal_hub import startup_visible_text


class TerminalStartupTests(unittest.IsolatedAsyncioTestCase):
    def test_readiness_ignores_formatting_but_not_control_payloads(self):
        self.assertIn('Ask anything',startup_visible_text('Ask \x1b[38;2;1;2;3manything\x1b[0m'))
        for payload in ('\x1b]0;Ask anything\x07','\x1bPAsk anything\x1b\\','\x1b]0;Ask anything'):
            self.assertNotIn('Ask anything',startup_visible_text(payload))

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.host=Host(self.temp.name,Path(self.temp.name)/'state')
        self.stack=ExitStack()
        self.stack.enter_context(patch('autocoding.terminal_hub.native_executable',return_value='fixture.exe'))
        self.stack.enter_context(patch('autocoding.terminal_hub.git',return_value='a'*40))
        self.stack.enter_context(patch.object(self.host.workspaces,'create',return_value={'id':'fixture','path':self.temp.name}))
        self.stack.enter_context(patch.object(self.host.workspaces,'get',return_value={'id':'fixture','path':self.temp.name}))
        self.stack.enter_context(patch('autocoding.terminal_hub.STARTUP_RETRY_DELAY',0))
        self.children=[]

    async def asyncTearDown(self):
        await self.host.close()
        self.stack.close()
        self.temp.cleanup()

    def spawn(self,failures,*,entered=False,unconfirmed=False,silent=False,failure_text=None):
        async def launch(*args,**kwargs):
            child=SimpleNamespace(_closed=False,_failure=None,user_input_seen=entered)
            failed=len(self.children)<failures
            emitted=False
            async def read(**kwargs):
                nonlocal emitted
                if silent:
                    await asyncio.sleep(.005)
                    raise TimeoutError()
                if failed:
                    if failure_text:return failure_text
                    raise TransportError('TERMINAL_CLOSED')
                if not emitted:
                    emitted=True
                    return 'permissions: YOLO mode'
                await asyncio.Future()
            async def close():
                child._closed=True
                if unconfirmed:child._failure='TERMINAL_STOP_UNCONFIRMED'
            child.read=read;child.close=close
            self.children.append(child)
            return child
        return self.stack.enter_context(patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=launch)))

    async def wait(self,sid,state):
        async with asyncio.timeout(5):
            while self.host.terminals.records[sid]['startup_state']!=state:
                await asyncio.sleep(.005)

    async def test_transient_startup_exit_retries_only_after_close(self):
        spawn=self.spawn(1)
        record=await self.host.terminals.start('codex',startup_attempts=3)
        await self.wait(record['id'],'ready')
        self.assertEqual(2,spawn.await_count)
        self.assertTrue(self.children[0]._closed)
        self.assertFalse(self.children[1]._closed)
        self.assertEqual([],self.host.core.snapshot()['commands'])
        self.assertEqual(1,self.host.core.snapshot()['sessions'][0]['epoch'])
        self.assertEqual(spawn.call_args_list[0],spawn.call_args_list[1])
        timings=self.host.terminals.records[record['id']]['startup_timings']
        self.assertGreaterEqual(timings['prepare_ms'],0)
        self.assertEqual(2,len(timings['attempts']))
        self.assertEqual('TERMINAL_CLOSED',timings['attempts'][0]['error'])
        self.assertGreaterEqual(timings['attempts'][1]['ready_ms'],timings['attempts'][1]['first_output_ms'])
        self.assertEqual(1,timings['attempts'][1]['output_chunks'])
        with patch.object(self.host.core,'snapshot',side_effect=AssertionError('whole project scan')):
            self.assertTrue(self.host.terminals.snapshot()[0]['live'])

    async def test_exhaustion_stops_until_a_new_owner_start(self):
        spawn=self.spawn(99)
        record=await self.host.terminals.start('codex',startup_attempts=3)
        await self.wait(record['id'],'failed')
        await asyncio.sleep(.03)
        self.assertEqual(3,spawn.await_count)
        self.assertTrue(all(child._closed for child in self.children))
        again=await self.host.terminals.start('codex',resume_from=record['id'],startup_attempts=1)
        await self.wait(again['id'],'failed')
        self.assertNotEqual(record['id'],again['id'])
        self.assertEqual(4,spawn.await_count)
        self.assertNotIn('resume',spawn.call_args.args[2])
        self.assertEqual(record['cwd'],again['cwd'])

    async def test_native_thread_startup_error_uses_bounded_retry_without_replay(self):
        spawn=self.spawn(1,failure_text='No active \x1b[31mthread is available.\x1b[0m')
        record=await self.host.terminals.start('codex',startup_attempts=3)
        await self.wait(record['id'],'ready')
        self.assertEqual(2,spawn.await_count)
        self.assertTrue(self.children[0]._closed)
        timing=self.host.terminals.records[record['id']]['startup_timings']['attempts'][0]
        self.assertEqual('TERMINAL_THREAD_NOT_READY',timing['error'])
        self.assertEqual([],self.host.core.snapshot()['commands'])

    async def test_creation_failure_is_distinct_from_retryable_startup_output(self):
        # Existing retry coverage starts AFTER creation. Unknown creation/stop
        # outcomes must not be reported as a recovered thread-readiness error.
        with patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=TransportError('CONPTY_START_FAILED'))) as spawn:
            with self.assertRaisesRegex(TransportError,'CONPTY_START_FAILED'):
                await self.host.terminals.start('codex',startup_attempts=3)
        self.assertEqual(1,spawn.await_count)
        record=next(iter(self.host.terminals.records.values()))
        self.assertEqual('reconciling',record['startup_state'])
        self.assertEqual([],self.host.core.snapshot()['commands'])

    async def test_confirmed_creation_failure_retries_with_sanitized_diagnostics(self):
        from autocoding.terminal_errors import TerminalStartError
        good=self.spawn(0)
        original=good.side_effect;calls=0
        async def create(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise TerminalStartError('input_connect',TimeoutError('secret-key-must-not-be-recorded'),True)
            return await original(*args,**kwargs)
        good.side_effect=create
        record=await self.host.terminals.start('codex',startup_attempts=3)
        await self.wait(record['id'],'ready')
        self.assertEqual(2,calls)
        diagnostic=record['startup_timings']['attempts'][0]['diagnostic']
        self.assertEqual({'stage':'input_connect','kind':'TimeoutError','stopped_confirmed':True},diagnostic)
        self.assertEqual([],self.host.core.snapshot()['commands'])

    async def test_creation_failure_exhaustion_allows_explicit_restart_only(self):
        from autocoding.terminal_errors import TerminalStartError
        failure=TerminalStartError('input_auth',OSError('private'),True)
        with patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=failure)) as spawn:
            with self.assertRaises(TransportError):await self.host.terminals.start('codex',startup_attempts=3)
            self.assertEqual(3,spawn.await_count)
            record=next(iter(self.host.terminals.records.values()))
            self.assertEqual('failed',record['startup_state'])
            self.assertTrue(record['stopped_confirmed'])
            self.assertTrue(self.host.terminals.snapshot()[0]['can_resume'])
            with self.assertRaises(TransportError):await self.host.terminals.start('codex',resume_from=record['id'],startup_attempts=1)
            self.assertEqual(4,spawn.await_count)

    async def test_unconfirmed_creation_failure_never_retries(self):
        from autocoding.terminal_errors import TerminalStartError
        with patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=TerminalStartError('process_create',OSError(),False))) as spawn:
            with self.assertRaises(TransportError):await self.host.terminals.start('codex',startup_attempts=3)
            self.assertEqual(1,spawn.await_count)
            self.assertFalse(self.host.terminals.snapshot()[0]['can_resume'])

    async def test_creation_backoff_can_be_stopped(self):
        from autocoding.terminal_errors import TerminalStartError
        with patch('autocoding.terminal_hub.STARTUP_RETRY_DELAY',.2),patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=TerminalStartError('input_connect',TimeoutError(),True))) as spawn:
            pending=asyncio.create_task(self.host.terminals.start('codex'))
            async with asyncio.timeout(5):
                while not self.host.terminals.records or next(iter(self.host.terminals.records.values()))['startup_state']!='retrying':await asyncio.sleep(.005)
            sid=next(iter(self.host.terminals.records))
            snapshot=self.host.terminals.snapshot()[0]
            self.assertTrue(snapshot['can_stop'])
            self.assertFalse(snapshot['can_resume'])
            await self.host.terminals.stop(sid)
            with self.assertRaises(TransportError):await pending
            self.assertEqual(1,spawn.await_count)
            self.assertEqual('failed',self.host.terminals.records[sid]['startup_state'])

    async def test_creation_and_readiness_share_one_attempt_budget(self):
        from autocoding.terminal_errors import TerminalStartError
        spawn=self.spawn(1);original=spawn.side_effect;calls=0
        async def create(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise TerminalStartError('input_connect',TimeoutError(),True)
            return await original(*args,**kwargs)
        spawn.side_effect=create
        record=await self.host.terminals.start('codex',startup_attempts=3)
        await self.wait(record['id'],'ready')
        self.assertEqual(3,calls)
        self.assertEqual(3,self.host.terminals.records[record['id']]['startup_attempt'])
        self.assertEqual(3,len(record['startup_timings']['attempts']))

    async def test_unknown_stop_or_user_input_prevents_retry(self):
        for entered,unconfirmed,state in [(True,False,'stopped'),(False,True,'reconciling')]:
            with self.subTest(entered=entered,unconfirmed=unconfirmed):
                spawn=self.spawn(99,entered=entered,unconfirmed=unconfirmed)
                record=await self.host.terminals.start('codex')
                await self.wait(record['id'],state)
                self.assertEqual(1,spawn.await_count)

    async def test_project_pause_prevents_next_startup_attempt(self):
        spawn=self.spawn(99)
        record=await self.host.terminals.start('codex')
        self.host.paused=True
        await self.wait(record['id'],'failed')
        self.assertEqual(1,spawn.await_count)

    async def test_retry_backoff_can_be_stopped_but_not_resumed(self):
        spawn=self.spawn(99)
        with patch('autocoding.terminal_hub.STARTUP_RETRY_DELAY',30):
            record=await self.host.terminals.start('codex')
            await self.wait(record['id'],'retrying')
            snapshot=self.host.terminals.snapshot()[0]
            self.assertTrue(snapshot['can_stop'])
            self.assertFalse(snapshot['can_resume'])
            with self.assertRaisesRegex(DomainError,'TERMINAL_RESUME_UNAVAILABLE'):
                await self.host.terminals.start('codex',resume_from=record['id'])
            await self.host.terminals.stop(record['id'])
        self.assertEqual(1,spawn.await_count)
        self.assertTrue(self.host.terminals.snapshot()[0]['can_resume'])

    async def test_silent_startup_times_out_and_limit_is_validated(self):
        spawn=self.spawn(99,silent=True)
        with patch('autocoding.terminal_hub.STARTUP_TIMEOUT',.01):
            record=await self.host.terminals.start('codex',startup_attempts=2)
            await self.wait(record['id'],'failed')
        self.assertEqual(2,spawn.await_count)
        self.assertEqual('TERMINAL_START_TIMEOUT',self.host.terminals.records[record['id']]['startup_error'])
        for limit in (0,6,True):
            with self.assertRaisesRegex(DomainError,'INVALID_STARTUP_ATTEMPTS'):
                await self.host.terminals.start('codex',startup_attempts=limit)

    async def test_real_conpty_failed_startup_exhausts_without_orphan_process(self):
        from autocoding.terminal import TerminalSession
        real_start=TerminalSession.start
        children=[]
        async def launch(core,sid,argv,**kwargs):
            child=await real_start(core,sid,[sys.executable,'-u','-c','import os; os._exit(1)'],
                                   cwd=kwargs['cwd'],env=kwargs['env'])
            children.append(child)
            return child
        with patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=launch)) as spawn, \
             patch('autocoding.terminal_hub.STARTUP_TIMEOUT',1):
            record=await self.host.terminals.start('codex',startup_attempts=2)
            async with asyncio.timeout(45):
                while self.host.terminals.records[record['id']]['startup_state'] in {'starting','retrying'}:
                    await asyncio.sleep(.05)
            self.assertEqual('failed',self.host.terminals.records[record['id']]['startup_state'],
                             [(child._closed,child._failure) for child in children])
            self.assertEqual(2,spawn.await_count)
            self.assertTrue(all(child._closed and child._process_handle is None for child in children))
            self.assertTrue(self.host.terminals.records[record['id']]['stopped_confirmed'])
