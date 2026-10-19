import os
import re
import shutil
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import Core,DomainError
from autocoding.terminal import TerminalSession
from autocoding.transport import TransportError

@unittest.skipUnless(os.name=='nt','ConPTY requires Windows')
class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.cleanup_directory)
        self.core=Core(Path(self.temp.name)/'terminal.db')
        self.core.create_session('terminal','fixture',owner='human',key='s')

    async def cleanup_directory(self):
        # Job termination is asynchronous on Windows. Retry only its temporary
        # sharing violation; persistent leaks and other errors still fail.
        for attempt in range(30):
            try:
                self.temp.cleanup()
                return
            except PermissionError as exc:
                if exc.winerror != 32 or attempt == 29:
                    raise
                await asyncio.sleep(.1)

    async def start(self):
        terminal=await TerminalSession.start(self.core,'terminal',[sys.executable,'-u','-c',
            "import sys; print('READY',flush=True); line=input(); print('ECHO:'+line,flush=True); input()"],cwd=self.temp.name)
        self.addAsyncCleanup(terminal.close)
        text=''
        while 'READY' not in text:
            text+=await terminal.read(timeout=15)
        return terminal

    async def test_unicode_resize_and_write_is_never_acknowledged(self):
        terminal=await self.start()
        await terminal.resize(35,120)
        result=await terminal.write('中文输入\n','human',0,key='input')
        self.assertEqual('sent_unconfirmed',result['state'])
        output=''
        while 'ECHO:' not in output:
            output+=await terminal.read(timeout=5)
        self.assertIn('中文输入',output)
        replay=await terminal.write('中文输入\n','human',0,key='input')
        self.assertFalse(replay['resent'])
        await terminal.close()
        self.assertEqual('delivery_unknown',self.core.command(result['command_id'])['state'])

    async def test_terminal_replies_are_allowlisted_and_never_replayed(self):
        terminal=await self.start()
        for wire in ['\x1b[2J','\x1b]52;c;YWJj\x07','\x1b[1;1Rdanger','\x1b[1;1R\x1b[2J']:
            with self.assertRaisesRegex(DomainError,'TERMINAL_RESPONSE_REJECTED'):
                await terminal.reply(wire,'human',0,key='invalid:'+wire)
        with self.assertRaisesRegex(DomainError,'TERMINAL_RESPONSE_REJECTED'):
            await terminal.reply('\x1b[1;1R'*33,'human',0,key='too-many')
        batch=await terminal.reply('\x1b[1;1R\x1b[?2026;2$y\x1b[?0u','human',0,key='batch')
        self.assertEqual('sent_unconfirmed',batch['state'])
        self.assertFalse((await terminal.reply('\x1b[1;1R\x1b[?2026;2$y\x1b[?0u','human',0,key='batch'))['resent'])
        result=await terminal.reply('\x1b[1;1R','human',0,key='reply')
        self.assertEqual('sent_unconfirmed',result['state'])
        self.assertFalse((await terminal.reply('\x1b[1;1R','human',0,key='reply'))['resent'])
        for index,wire in enumerate(['\x1bP>|XTerm(390)\x1b\\','\x1bP!|00000000\x1b\\',
                                     '\x1b]11;rgb:1111/2222/3333\x1b\\','\x1b[?1;2c']):
            self.assertEqual('sent_unconfirmed',(await terminal.reply(wire,'human',0,key='capability:'+str(index)))['state'])
        await terminal.takeover('pm',0,key='take')
        with self.assertRaisesRegex(TransportError,'TERMINAL_CLOSED'):
            await terminal.reply('\x1b[?1;2c','human',0,key='stale')

    async def test_ownership_and_controls_are_enforced(self):
        terminal=await self.start()
        with self.assertRaisesRegex(DomainError,'STALE_INPUT_EPOCH'):
            await terminal.write('hello','pm',0,key='wrong')
        with self.assertRaisesRegex(DomainError,'CONTROL_SEQUENCE_REJECTED'):
            await terminal.write('\x1b]52;c;secret\x07','human',0,key='escape')
        with self.assertRaisesRegex(DomainError,'INVALID_TERMINAL_SIZE'):
            await terminal.resize(0,100)

    async def test_named_enter_and_whitespace_are_delivered_once(self):
        terminal=await self.start()
        await terminal.write('hello','human',0,key='text')
        await terminal.write(' ','human',0,key='space')
        await terminal.write('world','human',0,key='word')
        receipt=await terminal.press('enter','human',0,key='enter')
        self.assertEqual('sent_unconfirmed',receipt['state'])
        text=''
        while 'ECHO:hello world' not in text:
            text+=await terminal.read(timeout=5)
        self.assertFalse((await terminal.press('enter','human',0,key='enter'))['resent'])
        with self.assertRaises(DomainError):
            await terminal.press('ctrl_c','human',0,key='enter')
        with self.assertRaisesRegex(DomainError,'STALE_INPUT_EPOCH'):
            await terminal.press('ctrl_c','pm',0,key='wrong-owner')
        with self.assertRaisesRegex(DomainError,'UNSUPPORTED_TERMINAL_KEY'):
            await terminal.press('osc52','human',0,key='bad-key')
        await terminal.press('ctrl_c','human',0,key='interrupt')
        await terminal.close()
        self.assertEqual('delivery_unknown',self.core.command(receipt['command_id'])['state'])

    async def test_close_stops_child_waiting_for_input(self):
        terminal=await self.start()
        await terminal.close()
        self.assertTrue(terminal.process.closed)
        self.assertIsNone(terminal.process.pty)
        self.assertFalse(terminal._reader.is_alive())
        self.assertIsNone(terminal._ledger_anchor)
        self.assertNotEqual('TERMINAL_STOP_UNCONFIRMED',terminal._failure)

    @unittest.skipUnless(shutil.which('node'),'Node console fixture required')
    async def test_ctrl_p_reaches_raw_console_once_in_both_input_modes(self):
        terminal=await TerminalSession.start(self.core,'terminal',[shutil.which('node'),
            str(ROOT/'tests/fixtures/terminal_input_probe.js')],cwd=self.temp.name)
        self.addAsyncCleanup(terminal.close)
        output=''
        async with asyncio.timeout(20):
            while 'READY' not in output:
                output+=await terminal.read(timeout=10)
            self.assertTrue(terminal.win32_mode)
            for native in (False, True):
                terminal.native_keys=native
                receipt=await terminal.press('ctrl_p','human',0,key='palette:'+str(native))
                self.assertEqual('sent_unconfirmed',receipt['state'])
                output=''
                while not re.findall(r'WIRE:([0-9a-f]+)',output):
                    output+=await terminal.read(timeout=5)
                self.assertEqual('10',''.join(re.findall(r'WIRE:([0-9a-f]+)',output)))
                self.assertFalse((await terminal.press('ctrl_p','human',0,key='palette:'+str(native)))['resent'])

    @unittest.skipUnless(shutil.which('node'),'Node console fixture required')
    async def test_color_replies_reach_raw_console_without_byte_changes(self):
        terminal=await TerminalSession.start(self.core,'terminal',[shutil.which('node'),
            str(ROOT/'tests/fixtures/terminal_input_probe.js')],cwd=self.temp.name)
        self.addAsyncCleanup(terminal.close)
        output=''
        async with asyncio.timeout(15):
            while 'READY' not in output:
                output+=await terminal.read(timeout=10)
            wire='\x1b]4;0;rgb:2222/3333/4444\x1b\\\x1b]12;rgb:aaaa/bbbb/cccc\x07'
            await terminal.reply(wire,'human',0,key='raw-colors')
            output=''
            while wire.encode().hex() not in ''.join(re.findall(r'WIRE:([0-9a-f]+)',output)):
                output+=await terminal.read(timeout=5)

    async def test_takeover_after_write_keeps_unknown_state(self):
        terminal=await self.start()
        result=await terminal.write('hello\n','human',0,key='input')
        takeover=await terminal.takeover('pm',0,key='takeover')
        self.assertEqual('reconciling',takeover['state'])
        self.assertEqual('delivery_unknown',self.core.command(result['command_id'])['state'])

    async def test_private_input_channel_loss_stops_job_owner(self):
        terminal=await self.start()
        terminal.process.input_channel.close()
        async with asyncio.timeout(5):
            while terminal.is_running():await asyncio.sleep(.05)
        self.assertFalse(terminal.is_running())

    async def test_private_channel_auth_failure_releases_startup_resources(self):
        from autocoding.pty_process import TerminalProcess
        original=TerminalProcess.__init__;started=[]
        def capture(instance,pty):
            original(instance,pty);started.append(instance)
        with patch.object(TerminalProcess,'__init__',capture),patch('autocoding.console_input.receive',return_value={'nonce':'wrong'}):
            with self.assertRaisesRegex(TransportError,'CONPTY_START_FAILED'):
                await TerminalSession.start(self.core,'terminal',[sys.executable,'-u','-c',
                    'import time;print("READY",flush=True);time.sleep(60)'],cwd=self.temp.name)
        self.assertEqual(1,len(started))
        process=started[0]
        self.assertTrue(process.closed)
        self.assertIsNone(process.pty)
        self.assertEqual(-1,process.input_channel.fileno())
        self.assertFalse(process._thread.is_alive())
        self.assertFalse(Path(process.draft_directory.name).exists())

    async def test_close_waits_for_descendants_and_releases_working_directory(self):
        import _winapi
        for attempt in range(3):
            directory=Path(self.temp.name)/('tree-'+str(attempt));directory.mkdir()
            child_code='import time;time.sleep(120)'
            code='import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c",'+repr(child_code)+']);Path("child.pid").write_text(str(p.pid));print("READY",flush=True);time.sleep(120)'
            terminal=await TerminalSession.start(self.core,'terminal',[sys.executable,'-u','-c',code],cwd=directory)
            self.addAsyncCleanup(terminal.close)
            output=''
            async with asyncio.timeout(20):
                while 'READY' not in output:output+=await terminal.read(timeout=10)
            handle=_winapi.OpenProcess(0x00100000,False,int((directory/'child.pid').read_text()))
            try:
                self.assertGreaterEqual(terminal.process.job.active(),2)
                await terminal.close()
                self.assertNotEqual('TERMINAL_STOP_UNCONFIRMED',terminal._failure,getattr(terminal,'shutdown_diagnostic',None))
                self.assertEqual(0,_winapi.WaitForSingleObject(handle,0))
                destination=directory.with_name(directory.name+'-released')
                self.assertTrue(directory.resolve().is_relative_to(Path(self.temp.name).resolve()))
                self.assertTrue(destination.resolve().is_relative_to(Path(self.temp.name).resolve()))
                directory.rename(destination)  # no sleep/retry hides a retained cwd
            finally:_winapi.CloseHandle(handle)

    @unittest.skipUnless(shutil.which('node'),'Node console fixture required')
    async def test_negotiated_win32_input_preserves_unicode_and_separate_enter(self):
        trace=Path(self.temp.name)/'native-wire-bytes.bin'
        trace.write_bytes(b'')
        terminal=await TerminalSession.start(self.core,'terminal',[shutil.which('node'),
            str(ROOT/'tests/fixtures/terminal_input_probe.js')],cwd=self.temp.name,
            env={**os.environ,'AUTOCODING_WIRE_TRACE':str(trace)})
        self.addAsyncCleanup(terminal.close)
        terminal.native_keys=True
        output=''
        async with asyncio.timeout(20):
            while 'READY' not in output:output+=await terminal.read(timeout=10)
            self.assertTrue(terminal.win32_mode)
            # Exceed console-sized short input buffers; native TUI failures
            # must be distinguishable from bridge-level truncation.
            value='Ab中文😀'*100
            await terminal.paste(value,'human',0,bracketed=True,key='native-paste')
            output=''
            while len(trace.read_bytes())<len(value.encode()):
                try:
                    output+=await terminal.read(timeout=.2)
                except TimeoutError:
                    pass
            self.assertEqual(value.encode(),trace.read_bytes())
            await terminal.press('enter','human',0,key='native-enter')
            output=''
            while trace.read_bytes()!=value.encode()+b'\r':
                await asyncio.sleep(.02)

    @unittest.skipUnless(shutil.which('node'),'Node console fixture required')
    async def test_bracketed_paste_is_framed_once_and_enter_is_separate(self):
        terminal=await TerminalSession.start(self.core,'terminal',[shutil.which('node'),
            str(ROOT/'tests/fixtures/terminal_input_probe.js')],cwd=self.temp.name)
        self.addAsyncCleanup(terminal.close)
        output=''
        while 'READY' not in output:
            output+=await terminal.read(timeout=10)
        with self.assertRaisesRegex(DomainError,'CONTROL_SEQUENCE_REJECTED'):
            await terminal.paste('bad\x1b[201~','human',0,bracketed=True,key='bad')
        await terminal.paste('中文\r\nline','human',0,bracketed=True,key='paste')
        self.assertFalse((await terminal.paste('中文\r\nline','human',0,bracketed=True,key='paste'))['resent'])
        expected='\x1b[200~中文\nline\x1b[201~'.encode().hex()
        output=''
        async with asyncio.timeout(10):
            while expected not in ''.join(re.findall(r'WIRE:([0-9a-f]+)',output)):
                output+=await terminal.read(timeout=5)
        self.assertEqual(expected,''.join(re.findall(r'WIRE:([0-9a-f]+)',output)))
        await terminal.press('enter','human',0,key='submit')
        output=''
        async with asyncio.timeout(10):
            while '0d' not in ''.join(re.findall(r'WIRE:([0-9a-f]+)',output)):
                output+=await terminal.read(timeout=5)
