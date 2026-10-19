import os
import codecs
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))


@unittest.skipUnless(os.name=='nt','Pinned Windows ConPTY bridge')
class PtyBridgeTests(unittest.TestCase):
    def test_reader_preserves_split_unicode_and_literal_sentinel(self):
        from autocoding.pty_process import TerminalProcess
        process=object.__new__(TerminalProcess)
        process.closed=True
        process._decoder=codecs.getincrementaldecoder('utf-8')()
        raw='中文😀'.encode()
        chunks=iter([bytes([byte]) for byte in raw]+[b'0011Ignore',b''])
        process.fileobj=SimpleNamespace(recv=lambda size:next(chunks))
        output=''
        with self.assertRaises(EOFError):
            while True:output+=process.read()
        self.assertEqual('中文😀0011Ignore',output)

    def test_unicode_stream_and_literal_vendor_sentinel_are_not_lost(self):
        from autocoding.pty_process import forward_output
        chunks=iter(['中文😀','0011Ignore','x'*200000,'END'])
        done=False
        def read(**kwargs):
            nonlocal done
            value=next(chunks)
            done=value=='END'
            return value
        pty=SimpleNamespace(read=read,iseof=lambda:done)
        with socket.socket() as server:
            server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(5)
            thread=threading.Thread(target=forward_output,args=(server.getsockname(),pty,True))
            thread.start()
            connection,_=server.accept()
            with connection:
                connection.settimeout(5)
                data=bytearray()
                while value:=connection.recv(137):data.extend(value)
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(('中文😀0011Ignore'+'x'*200000+'END').encode(),data)

    def test_bridge_disables_nagle_and_uses_complete_writes(self):
        from autocoding.pty_process import forward_output
        with patch('autocoding.pty_process.socket.socket') as factory:
            client=factory.return_value.__enter__.return_value
            forward_output(('127.0.0.1',1),SimpleNamespace(read=lambda **kw:'tail',iseof=lambda:True),True)
            client.setsockopt.assert_called_once_with(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            client.sendall.assert_called_once_with(b'tail')
            client.send.assert_not_called()
