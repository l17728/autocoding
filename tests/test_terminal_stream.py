import asyncio
from collections import deque
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from starlette.websockets import WebSocketDisconnect
from autocoding.api import create_app
from autocoding.host import Host


class TerminalStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_gap_keeps_stream_open_until_startup_is_abandoned(self):
        with tempfile.TemporaryDirectory() as folder:
            host=Host(folder,Path(folder)/'state')
            app=create_app(host,'fixture-token')
            sid='terminal_retry'
            entry={'seq':0,'frames':deque(),'changed':asyncio.Event()}
            host.terminals.output[sid]=entry
            host.terminals.sessions[sid]=SimpleNamespace(_closed=True)
            host.terminals.records[sid]={'startup_state':'retrying'}
            packets=[]
            class Socket:
                headers={'host':'127.0.0.1:8765','origin':'http://127.0.0.1:8765'}
                async def accept(self):pass
                async def receive_text(self):return json.dumps({'token':'fixture-token'})
                async def close(self,code):pass
                async def send_json(self,packet):
                    packets.append(packet)
                    host.terminals.records[sid]['startup_state']='failed'
                    entry['changed'].set()
            endpoint=next(r.endpoint for r in app.routes if r.path.endswith('/terminals/{sid}/stream'))
            try:
                await asyncio.wait_for(endpoint(Socket(),host.project_id,sid),2)
                self.assertEqual([False,True],[packet['closed'] for packet in packets])
            finally:
                host.terminals.sessions.clear()
                await host.close()

    async def test_output_produced_during_send_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            host=Host(folder,Path(folder)/'state')
            app=create_app(host,'fixture-token')
            sid='terminal_fixture'
            entry={'seq':1,'frames':deque([{'seq':1,'text':'first'}],maxlen=256),'changed':asyncio.Event()}
            host.terminals.output[sid]=entry
            host.terminals.sessions[sid]=SimpleNamespace(_closed=False)
            host.terminals.records[sid]={}
            packets=[]
            class Socket:
                headers={'host':'127.0.0.1:8765','origin':'http://127.0.0.1:8765'}
                async def accept(self): pass
                async def receive_text(self): return json.dumps({'token':'fixture-token'})
                async def close(self,code): pass
                async def send_json(self,packet):
                    packets.append(packet)
                    if len(packets)==1:
                        # Producer advances while the consumer awaits network I/O.
                        entry['seq']=2
                        entry['frames'].append({'seq':2,'text':'second'})
                        entry['changed'].set()
                        host.terminals.sessions[sid]._closed=True
                        await asyncio.sleep(0)
                    else:
                        raise WebSocketDisconnect()
            endpoint=next(r.endpoint for r in app.routes if r.path.endswith('/terminals/{sid}/stream'))
            try:
                await asyncio.wait_for(endpoint(Socket(),host.project_id,sid),2)
                self.assertEqual([[1],[2]],[[f['seq'] for f in p['frames']] for p in packets])
                self.assertTrue(all(not f['replay'] for p in packets for f in p['frames']))
                self.assertEqual([False,True],[p['closed'] for p in packets])
                class Reconnect(Socket):
                    async def send_json(self,packet):
                        packets.append(packet)
                        raise WebSocketDisconnect()
                await endpoint(Reconnect(),host.project_id,sid)
                self.assertEqual([1,2],[f['seq'] for f in packets[-1]['frames']])
                self.assertTrue(all(f['replay'] for f in packets[-1]['frames']))
            finally:
                host.terminals.sessions.clear()
                await host.close()
