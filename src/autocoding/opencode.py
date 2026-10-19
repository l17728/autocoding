"""Loopback-only OpenCode server adapter using its locally exported OpenAPI.

prompt_async 204 acknowledges receipt; a correlated completed assistant message
finishes a turn. Polling retrieves complete public text, not token deltas.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import uuid

import httpx
from .transport import TransportError
from .opencode_compat import configure_glob_path


class OpenCodeSession:
    def __init__(self, client, *, process=None, model='glm-5.3'):
        self.client = client
        self.process = process
        self.model = model
        self.thread_id = None
        self.turn_id = None
        self._events = asyncio.Queue(maxsize=128)
        self._poller = None
        self._failure = None
        self._lock = asyncio.Lock()
        self._questions = {}
        self._approvals = {}
        self._denied_tools = set()

    @classmethod
    async def connect(cls, executable, *, cwd, env, storage, model='glm-5.3', timeout=90, resume=None, permission_mode='bypass'):
        if permission_mode not in {'bypass','review'}:
            raise TransportError('INVALID_PERMISSION_MODE')
        storage = Path(storage).resolve()
        storage.mkdir(parents=True, exist_ok=True)
        env = configure_glob_path(env, storage, cwd)
        for variable, folder in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),
                                 ('XDG_CACHE_HOME','cache'),('XDG_STATE_HOME','state')]:
            target = storage / folder
            target.mkdir(exist_ok=True)
            env[variable] = str(target)
        password = secrets.token_urlsafe(32)
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_AUTOUPDATE='true')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        argv = [str(executable), 'serve', '--hostname', '127.0.0.1', '--port', str(port)]
        # The trusted supervisor joins a Job before it starts OpenCode, closing
        # the create-process/assign-job race. The password stays off argv.
        worker = Path(__file__).with_name('execution_worker.py')
        process = subprocess.Popen([sys.executable, '-I', str(worker)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        process.stdin.write((json.dumps({'argv': argv, 'cwd': str(cwd), 'env': env}) + '\n').encode())
        process.stdin.close()
        client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', auth=('opencode', password),
            params={'directory': str(Path(cwd).resolve())},
            timeout=httpx.Timeout(30,connect=5,write=10,pool=5), trust_env=False)
        adapter = cls(client, process=process, model=model)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        response = await client.get('/global/health')
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        raise TransportError('OPENCODE_SERVER_EXITED')
                    await asyncio.sleep(.25)
            startup_timeout = httpx.Timeout(timeout, connect=5, write=10, pool=5)
            response = await adapter._request('GET', '/session/' + resume, timeout=startup_timeout) if resume else await adapter._request('POST', '/session', timeout=startup_timeout, json={
                'title': 'Autocoding session',
                'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow' if permission_mode=='bypass' else 'ask'},
                               {'permission':'question','pattern':'*','action':'allow'}]})
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not data['id'].startswith('ses'):
                raise TransportError('PROTOCOL_MALFORMED')
            adapter.thread_id = data['id']
            if resume and adapter.thread_id != resume:
                raise TransportError('RESUME_SESSION_MISMATCH')
            return adapter
        except TimeoutError:
            await adapter.close()
            raise TransportError('OPENCODE_START_TIMEOUT') from None
        except BaseException:
            await adapter.close()
            raise

    async def _request(self, method, path, **kwargs):
        if self._failure:
            raise TransportError(self._failure)
        try:
            async with self.client.stream(method, path, **kwargs) as response:
                if response.status_code not in {200, 204}:
                    self._failure = 'OPENCODE_HTTP_REJECTED'
                    raise TransportError(self._failure)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        self._failure = 'FRAME_TOO_LARGE'
                        raise TransportError(self._failure)
                    chunks.append(chunk)
                return httpx.Response(response.status_code, content=b''.join(chunks), request=response.request)
        except httpx.TimeoutException:
            self._failure = 'OPENCODE_HTTP_TIMEOUT'
            raise TransportError(self._failure) from None
        except httpx.HTTPError:
            self._failure = 'OPENCODE_CONNECTION_LOST'
            raise TransportError(self._failure) from None

    async def send_text(self, text, *, command_id):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 65536:
            raise TransportError('INVALID_INPUT')
        async with self._lock:
            if self.turn_id:
                raise TransportError('TURN_BUSY')
            mid = 'msg_' + uuid.uuid4().hex
            await self._request('POST', f'/session/{self.thread_id}/prompt_async', json={
                'messageID': mid, 'model': {'providerID': 'zhipuai-coding-plan', 'modelID': self.model},
                'parts': [{'type': 'text', 'text': text}]})
            self.turn_id = mid
            self._denied_tools.clear()
            self._poller = asyncio.create_task(self._poll(mid))
            return {'state': 'acknowledged', 'thread_id': self.thread_id, 'turn_id': mid}

    async def _poll(self, mid):
        try:
            while True:
                permission_response = await self._request('GET', '/permission')
                permissions = permission_response.json()
                if not isinstance(permissions, list):
                    raise TransportError('PROTOCOL_MALFORMED')
                for operation in permissions:
                    if operation.get('sessionID') != self.thread_id or operation.get('id') in self._approvals:
                        continue
                    if not isinstance(operation.get('id'), str) or not operation['id'].startswith('per'):
                        raise TransportError('PROTOCOL_MALFORMED')
                    self._approvals[operation['id']] = operation
                    self._events.put_nowait({'id':operation['id'],'method':'tool/requestApproval','params':{
                        'threadId':self.thread_id,'turnId':mid,'operation':operation}})
                question_response = await self._request('GET', '/question')
                questions = question_response.json()
                if not isinstance(questions, list):
                    raise TransportError('PROTOCOL_MALFORMED')
                for question in questions:
                    if question.get('sessionID') != self.thread_id or question.get('id') in self._questions:
                        continue
                    if not isinstance(question.get('id'), str) or not question['id'].startswith('que'):
                        raise TransportError('PROTOCOL_MALFORMED')
                    self._questions[question['id']] = question
                    self._events.put_nowait({'id':question['id'],'method':'agent/question','params':{
                        'threadId':self.thread_id,'turnId':mid,'operation':question}})
                response = await self._request('GET', f'/session/{self.thread_id}/message', params={
                    **dict(self.client.params), 'limit': 20})
                messages = response.json()
                if not isinstance(messages, list):
                    raise TransportError('PROTOCOL_MALFORMED')
                status_response = await self._request('GET', '/session/status')
                statuses = status_response.json()
                if not isinstance(statuses, dict):
                    raise TransportError('PROTOCOL_MALFORMED')
                status = statuses.get(self.thread_id, {'type':'idle'})
                if status.get('type') != 'idle':
                    await asyncio.sleep(.5)
                    continue
                for message in reversed(messages):
                    info = message.get('info', {})
                    if info.get('role') != 'assistant' or info.get('parentID') != mid:
                        continue
                    if info.get('sessionID') != self.thread_id:
                        raise TransportError('OPENCODE_SESSION_MISMATCH')
                    if not info.get('time', {}).get('completed'):
                        continue
                    tool_parts=[part for part in message.get('parts',[]) if part.get('type')=='tool']
                    denied=(any((mid,part.get('callID')) in self._denied_tools
                                and part.get('state',{}).get('status')=='error' for part in tool_parts)
                            and all(part.get('state',{}).get('status') in {'completed','error'} for part in tool_parts)
                            and not any(op.get('sessionID')==self.thread_id for op in permissions+questions))
                    if not info.get('error') and info.get('finish') in {'tool-calls','unknown'} and not denied:
                        continue
                    if not info.get('error'):
                        for part in message.get('parts', []):
                            if part.get('type') == 'text' and isinstance(part.get('text'), str):
                                self._events.put_nowait({'method': 'item/agentMessage/delta', 'params': {
                                    'threadId': self.thread_id, 'turnId': mid, 'delta': part['text']}})
                    self._events.put_nowait({'method': 'turn/completed', 'params': {'threadId': self.thread_id,
                        'turn': {'id': mid, 'status': 'failed' if info.get('error') else 'interrupted' if denied else 'completed'}}})
                    return
                await asyncio.sleep(.5)
        except (TransportError, ValueError, TypeError, AttributeError, asyncio.QueueFull) as exc:
            self._failure = getattr(exc, 'code', 'PROTOCOL_MALFORMED')

    async def next_event(self, *, timeout=None):
        async with asyncio.timeout(timeout):
            while self._events.empty():
                if self._failure:
                    raise TransportError(self._failure)
                await asyncio.sleep(.05)
            event = self._events.get_nowait()
        if event['method'] == 'turn/completed':
            async with self._lock:
                self.turn_id = None
        return event

    async def interrupt(self):
        if not self.turn_id:
            raise TransportError('NO_ACTIVE_TURN')
        await self._request('POST', f'/session/{self.thread_id}/abort')
        return {'state': 'interrupt_requested', 'turn_id': self.turn_id}

    async def respond_question(self, request_id, operation_hash, answers):
        from .core import digest
        operation = self._questions.get(request_id)
        if operation is None or digest(operation) != operation_hash or len(answers) != len(operation['questions']):
            raise TransportError('QUESTION_ANSWER_MISMATCH')
        self._questions[request_id] = None  # Reserve before HTTP; never send twice.
        await self._request('POST', '/question/' + request_id + '/reply', json={'answers':answers})
        return {'state':'delivered'}

    async def respond_approval(self, request_id, operation_hash, *, allow):
        from .core import digest
        operation = self._approvals.get(request_id)
        if operation is None or digest(operation) != operation_hash or not isinstance(allow, bool):
            raise TransportError('APPROVAL_OPERATION_MISMATCH')
        self._approvals[request_id] = None
        await self._request('POST', '/permission/' + request_id + '/reply',
                            json={'reply':'once' if allow else 'reject'})
        call_id=operation.get('tool',{}).get('callID')
        if not allow and self.turn_id and call_id:
            self._denied_tools.add((self.turn_id,call_id))
        return {'state':'delivered','tool_use_id':operation.get('tool',{}).get('callID',request_id)}

    async def close(self):
        self._failure = self._failure or 'TRANSPORT_CLOSED'
        if self._poller:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
        await self.client.aclose()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 5)
