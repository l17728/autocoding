"""Real loopback socket failures; provider model and remote service are not used."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import Core,DomainError
from autocoding.opencode import OpenCodeSession
from autocoding.runtime import SessionRuntime
from autocoding.transport import TransportError


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_opencode_socket_loss_before_and_after_acceptance_never_replays(self):
        for acknowledged in [False,True]:
            with self.subTest(acknowledged=acknowledged),tempfile.TemporaryDirectory() as directory:
                core=Core(Path(directory)/'state.db')
                core.create_session('s','opencode',key='s')
                requests=[]
                async def connection(reader,writer):
                    try:
                        headers=await reader.readuntil(b'\r\n\r\n')
                        first=headers.split(b'\r\n',1)[0]
                        requests.append(first.decode('ascii'))
                        size=next((int(line.split(b':',1)[1]) for line in headers.lower().split(b'\r\n')
                                   if line.startswith(b'content-length:')),0)
                        if size:await reader.readexactly(size)
                        if acknowledged and first.startswith(b'POST '):
                            writer.write(b'HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n')
                            await writer.drain()
                        # GET polling, or an unacknowledged POST, loses its socket.
                    finally:
                        writer.close()
                        await writer.wait_closed()
                server=await asyncio.start_server(connection,'127.0.0.1',0)
                port=server.sockets[0].getsockname()[1]
                client=httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=5)
                adapter=OpenCodeSession(client)
                adapter.thread_id='session_fixture'
                runtime=SessionRuntime(core,'s',adapter)
                try:
                    cid=core.enqueue('s','pm',0,'one operation',key='input')['command_id']
                    with self.assertRaises(TransportError):
                        await runtime.send(cid,'one operation')
                        await runtime.next_event(timeout=10)
                    self.assertEqual('delivery_unknown',core.command(cid)['state'])
                    with self.assertRaises((TransportError,DomainError)):
                        await runtime.send(cid,'one operation')
                    self.assertEqual(1,sum(r.startswith('POST ') for r in requests))
                    self.assertEqual('reconciling',core.snapshot()['sessions'][0]['state'])
                finally:
                    await runtime.close()
                    server.close()
                    await server.wait_closed()
