import sys
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import Core
from autocoding.terminal import TerminalSession


class TerminalFreezeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.core=Core(Path(temp.name)/'state.db')
        self.core.create_session('target','fixture',owner='human',key='target')
        self.core.create_session('other','fixture',owner='human',key='other')
        self.terminal=object.__new__(TerminalSession)
        self.terminal.core,self.terminal.session_id=self.core,'target'
        with self.core.connection() as conn:
            for sid in ['target','other']:
                for i,state in enumerate(['dispatching','sent_unconfirmed','queued','completed']):
                    conn.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?)',(sid+str(i),sid,'human',0,1,'fixture',state))

    def test_freeze_scopes_updates_and_preserves_each_audit_without_replay(self):
        self.terminal._freeze();self.terminal._freeze()
        self.assertEqual('delivery_unknown',self.core.command('target0')['state'])
        self.assertEqual('delivery_unknown',self.core.command('target1')['state'])
        self.assertEqual('queued',self.core.command('target2')['state'])
        self.assertEqual('completed',self.core.command('target3')['state'])
        self.assertEqual('sent_unconfirmed',self.core.command('other1')['state'])
        events=[e for e in self.core.events() if e['type']=='CommandDeliveryUnknown']
        self.assertEqual({'target0','target1'},{e['command_id'] for e in events})
        self.assertEqual(2,len(events))

    def test_event_failure_rolls_back_entire_freeze(self):
        original=self.core._event;calls=0
        def event(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('injected failure')
            return original(*args,**kwargs)
        with patch.object(self.core,'_event',side_effect=event),self.assertRaises(RuntimeError):
            self.terminal._freeze()
        self.assertEqual('dispatching',self.core.command('target0')['state'])
        self.assertEqual('sent_unconfirmed',self.core.command('target1')['state'])
        self.assertFalse(any(e['type']=='CommandDeliveryUnknown' for e in self.core.events()))

    def test_cancelled_native_close_cannot_claim_confirmed_stop(self):
        async def exercise():
            self.terminal._closed=False;self.terminal._failure=None
            self.terminal._ledger_context=MagicMock();self.terminal._ledger_anchor=object()
            entered=asyncio.Event()
            async def pending_worker(*args):
                entered.set();await asyncio.Future()
            with patch('autocoding.terminal.asyncio.to_thread',side_effect=pending_worker):
                closing=asyncio.create_task(self.terminal._close_process())
                await entered.wait();closing.cancel()
                with self.assertRaises(asyncio.CancelledError):await closing
            self.assertEqual('TERMINAL_STOP_UNCONFIRMED',self.terminal._failure)
            await self.terminal._close_process()
            self.assertEqual('TERMINAL_STOP_UNCONFIRMED',self.terminal._failure)
            self.terminal._ledger_context.__exit__.assert_called_once()
        asyncio.run(exercise())

    def test_missing_console_receipt_freezes_and_does_not_replay(self):
        async def exercise():
            from autocoding.transport import TransportError
            self.terminal._closed=False;self.terminal._failure=None
            self.terminal._lock=asyncio.Lock()
            self.terminal.process=MagicMock()
            self.terminal.process.write.side_effect=OSError('CONSOLE_INPUT_UNCONFIRMED')
            async def stop():self.terminal._closed=True
            self.terminal._close_process=stop
            with self.assertRaisesRegex(TransportError,'TERMINAL_INPUT_UNCONFIRMED'):
                await self.terminal.paste('once','human',0,key='uncertain-paste')
            with self.assertRaises(TransportError):
                await self.terminal.paste('once','human',0,key='uncertain-paste')
            self.terminal.process.write.assert_called_once()
            self.assertTrue(self.terminal._closed)
            with self.core.connection() as conn:
                self.assertEqual('reconciling',conn.execute("SELECT state FROM sessions WHERE id='target'").fetchone()[0])
                self.assertEqual(0,conn.execute("SELECT count(*) FROM commands WHERE session_id='target' AND state IN ('dispatching','sent_unconfirmed')").fetchone()[0])
        asyncio.run(exercise())

    def test_readback_receipt_is_not_completion_or_permission_to_replay(self):
        async def exercise():
            self.terminal._closed=False;self.terminal._failure=None
            self.terminal._lock=asyncio.Lock()
            self.terminal.process=MagicMock()
            self.terminal.process.editor_paste_enabled=True
            proof={'method':'editor_readback','sha256':'a'*64}
            self.terminal.process.paste.return_value=proof
            result=await self.terminal.paste('first\nsecond','human',0,key='readback-once')
            self.assertEqual(proof,result['draft_confirmation'])
            self.assertEqual('sent_unconfirmed',result['state'])
            replay=await self.terminal.paste('first\nsecond','human',0,key='readback-once')
            self.assertNotIn('draft_confirmation',replay)
            self.assertFalse(replay['resent'])
            self.terminal.process.paste.assert_called_once_with('first\nsecond')
            self.terminal.process.write.assert_not_called()
        asyncio.run(exercise())
