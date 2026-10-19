import asyncio
import os
import socket
import struct
import time
import unittest
from unittest.mock import Mock
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from autocoding.windows_disconnect import install, recover_reset


@unittest.skipUnless(os.name == 'nt', 'Windows Proactor regression')
class WindowsDisconnectTests(unittest.TestCase):
    def test_shutdown_reset_finishes_cleanup_without_repeating_protocol_callback(self):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        # Inject the observed syscall error at the exact asyncio call site;
        # use a real socket to prove that recovery releases the OS resource.
        listener=socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1',0));listener.listen(1)
        peer=socket.create_connection(listener.getsockname())
        sock,_=listener.accept()
        self.addCleanup(sock.close)
        transport=object.__new__(_ProactorBasePipeTransport)
        transport._sock=Mock(wraps=sock)
        transport._sock.shutdown.side_effect=ConnectionResetError(10054,'peer reset',None,10054)
        transport._server=server=Mock()
        transport._protocol=protocol=Mock()
        transport._called_connection_lost=False
        transport._closing=True
        peer.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('HH',1,0))
        peer.close()
        time.sleep(.1)
        try:
            transport._call_connection_lost(None)
        except ConnectionResetError as error:
            self.assertEqual(10054,error.winerror)
            context={'exception':error,'handle':Mock(_callback=transport._call_connection_lost)}
            self.assertTrue(recover_reset(context))
            self.assertFalse(recover_reset(context))
        else:
            self.fail('Expected the injected shutdown reset')
        self.assertEqual(-1,sock.fileno())
        self.assertIsNone(transport._sock)
        self.assertIsNone(transport._server)
        self.assertTrue(transport._called_connection_lost)
        transport._call_connection_lost(None)
        protocol.connection_lost.assert_called_once_with(None)
        server._detach.assert_called_once_with()

    def test_unrelated_errors_are_delegated_and_handler_restored(self):
        loop=asyncio.new_event_loop()
        self.addCleanup(loop.close)
        previous=Mock()
        loop.set_exception_handler(previous)
        restore=install(loop)
        handler=loop.get_exception_handler()
        for error in (RuntimeError('unrelated'),ConnectionResetError(10054,'read failed')):
            context={'exception':error}
            handler(loop,context)
            previous.assert_called_with(loop,context)
        restore()
        self.assertIs(previous,loop.get_exception_handler())

    def test_protocol_reset_is_not_misclassified(self):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        transport=object.__new__(_ProactorBasePipeTransport)
        transport._called_connection_lost=False
        transport._sock=None
        error=ConnectionResetError(10054,'protocol reset',None,10054)
        self.assertFalse(recover_reset({'exception':error,'handle':Mock(_callback=transport._call_connection_lost)}))

    def test_real_peer_reset_and_subsequent_connection(self):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen(1)
            for _ in range(3):
                peer=socket.create_connection(listener.getsockname())
                sock,_=listener.accept()
                self.addCleanup(sock.close)
                transport=object.__new__(_ProactorBasePipeTransport)
                transport._sock=sock
                transport._server=server=Mock()
                transport._protocol=protocol=Mock()
                transport._called_connection_lost=False
                transport._closing=True
                peer.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('HH',1,0))
                peer.close()
                time.sleep(.05)
                try:
                    transport._call_connection_lost(None)
                except ConnectionResetError as error:
                    self.assertTrue(recover_reset({'exception':error,'handle':Mock(_callback=transport._call_connection_lost)}))
                self.assertEqual(-1,sock.fileno())
                self.assertTrue(transport._called_connection_lost)
                protocol.connection_lost.assert_called_once_with(None)
                server._detach.assert_called_once_with()

