import sys
import unittest
import tempfile
import json
import hashlib
from pathlib import Path
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.console_input import records,receive,send
from autocoding.win32_input import text


class ConsoleInputTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='win32','Windows process handles')
    def test_unconfirmed_tree_stop_still_releases_other_resources(self):
        from autocoding.pty_process import TerminalProcess
        process=TerminalProcess.__new__(TerminalProcess)
        process.job=Mock();process.job.stop.side_effect=OSError('TERMINAL_TREE_STOP_UNCONFIRMED')
        process.pty=Mock();process.input_channel=Mock();process.fileobj=Mock()
        process._server=Mock();process._thread=Mock();process._thread.is_alive.return_value=False
        process.draft_directory=Mock()
        with patch('autocoding.pty_process.self_stop') as stop:
            with self.assertRaisesRegex(OSError,'TERMINAL_TREE_STOP_UNCONFIRMED'):process.dispose(handle=123)
            stop.assert_called_once_with(123)
        process.job.close.assert_called_once()
        process.input_channel.close.assert_called_once()
        process.fileobj.close.assert_called_once()
        process._server.close.assert_called_once()
        process.draft_directory.cleanup.assert_called_once()

    @unittest.skipUnless(sys.platform=='win32','Windows process handles')
    def test_terminating_process_access_denied_still_requires_signalled_handle(self):
        from autocoding.pty_process import self_stop
        denied=PermissionError(5,'already terminating',None,5)
        with patch('_winapi.TerminateProcess',side_effect=denied),patch('_winapi.WaitForSingleObject',side_effect=[258,0]):
            self_stop(123)
        with patch('_winapi.TerminateProcess',side_effect=denied),patch('_winapi.WaitForSingleObject',side_effect=[258,258]):
            with self.assertRaisesRegex(OSError,'TERMINAL_STOP_UNCONFIRMED'):self_stop(123)

    def test_editor_handoff_is_exact_once_and_preserves_existing_drafts(self):
        from autocoding.terminal_editor import apply
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root);target=directory/'draft.md'
            value='中文😀 first  line\nsecond\n'
            def prepare():
                (directory/'pending.json').write_text(json.dumps({'id':'probe','paste':value}),encoding='utf-8')
            target.write_text('',encoding='utf-8');prepare()
            self.assertEqual({'id':'probe','sha256':hashlib.sha256(value.encode()).hexdigest(),'phase':'write'},apply(directory,target))
            self.assertEqual(value.encode(),target.read_bytes())
            with self.assertRaises(FileNotFoundError):apply(directory,target)
            prepare()
            with self.assertRaises(ValueError):apply(directory,target)
            self.assertEqual(value.encode(),target.read_bytes())
            self.assertIn('error',json.loads((directory/'done.json').read_text()))

    def test_editor_readback_verifies_large_composer_and_never_repairs_a_mismatch(self):
        from autocoding.terminal_editor import apply
        from autocoding.console_input import editor_exchange
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root);target=directory/'draft.md'
            value=('中文😀  exact spaces\n'*1000)+'END'
            for mismatch in (False,True):
                phases=[];target.write_text('',encoding='utf-8')
                def open_editor(items):
                    request=json.loads((directory/'pending.json').read_text(encoding='utf-8'))
                    phases.append(request['phase'])
                    if request['phase']=='verify' and mismatch:
                        target.write_text(value[:-1],encoding='utf-8',newline='')
                    try:apply(directory,target)
                    except ValueError:pass
                with patch('autocoding.console_input.write_records',side_effect=open_editor):
                    result=editor_exchange(directory,{'id':'unique','paste':value})
                self.assertEqual(['write','verify'],phases)
                if mismatch:
                    self.assertIn('error',result)
                    self.assertEqual(value[:-1].encode(),target.read_bytes())
                else:
                    self.assertEqual({'id':'unique','sha256':hashlib.sha256(value.encode()).hexdigest(),'phase':'verify'},result)
                    self.assertEqual(value.encode(),target.read_bytes())

    @unittest.skipUnless(sys.platform=='win32','Windows PTY adapter')
    def test_initial_write_receipt_cannot_confirm_composer_readback(self):
        from autocoding.pty_process import TerminalProcess
        from autocoding.transport import TransportError
        process=object.__new__(TerminalProcess);process.closed=True;process.input_channel=Mock()
        value='long\ntext'
        def receive_written(sock):
            return {'id':'fixed','sha256':hashlib.sha256(value.encode()).hexdigest(),'phase':'write'}
        with patch('autocoding.pty_process.secrets.token_hex',return_value='fixed'),patch('autocoding.console_input.send') as sent,patch('autocoding.console_input.receive',side_effect=receive_written):
            with self.assertRaisesRegex(TransportError,'EDITOR_INPUT_UNCONFIRMED'):process.paste(value)
            sent.assert_called_once()

    def test_unicode_events_preserve_utf16_and_reject_extra_control_sequences(self):
        value='a中文😀\n'
        items=records(text(value,paste=True))
        units=b''.join(uc.to_bytes(2,'little') for _,_,uc,down,_,_ in items if down)
        self.assertEqual(value.replace('\n','\r'),units.decode('utf-16-le'))
        self.assertEqual(16,items[-2][4])
        surrogates=[item for item in items if 0xd800<=item[2]<=0xdfff]
        self.assertEqual([(0xd83d,1),(0xde00,1)],[(item[2],item[3]) for item in surrogates])
        for wire in (text('a')+'\x1b[201~', '\x1b[0;0;999999;1;0;1_', ''):
            with self.assertRaises(ValueError):records(wire)

    def test_fragmented_channel_frames_and_missing_receipt(self):
        sock=Mock()
        send(sock,{'count':1200})
        wire=sock.sendall.call_args.args[0]
        parts=iter(bytes([byte]) for byte in wire)
        sock.recv.side_effect=lambda count:next(parts,b'')
        self.assertEqual({'count':1200},receive(sock))
        with self.assertRaises(EOFError):receive(sock)

    @unittest.skipUnless(sys.platform=='win32','Windows PTY adapter')
    def test_native_write_never_retries_an_uncertain_receipt(self):
        from autocoding.pty_process import TerminalProcess
        process=object.__new__(TerminalProcess)
        process.closed=True  # no native resources in this isolated test
        process.input_channel=Mock()
        with patch('autocoding.console_input.send') as sent,patch('autocoding.console_input.receive',return_value={'count':1}):
            with self.assertRaisesRegex(OSError,'CONSOLE_INPUT_UNCONFIRMED'):
                process.write(text('abc'))
            sent.assert_called_once()
