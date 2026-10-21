"""Codex app-server adapter based on the locally exported v2 schema."""
from __future__ import annotations

import asyncio
from pathlib import Path
from .transport import JsonLineProcess, JsonRpcClient, TransportError


class CodexSession:
    def __init__(self, rpc: JsonRpcClient):
        self.rpc = rpc
        self.thread_id = None
        self.turn_id = None
        self.initialized = False
        self._turn_lock = asyncio.Lock()
        self._questions = {}
        self._approvals = {}
        self._mcp_receipts = {}
        self._mcp_started = {}

    @classmethod
    async def connect(cls, executable, *, cwd, env=None, timeout=15):
        from .codex_environment import child_environment
        env=child_environment(env)
        transport = await JsonLineProcess.start(
            [str(executable), "app-server", "--listen", "stdio://"], cwd=cwd, env=env)
        rpc = JsonRpcClient(transport, request_timeout=timeout)
        result = cls(rpc)
        try:
            await rpc.request("initialize", {"clientInfo": {"name": "autocoding_local", "version": "0.1.0"}})
            await rpc.notify("initialized")
            result.initialized = True
            return result
        except BaseException:
            await rpc.close()
            raise

    async def start_thread(self, cwd, *, model=None, sandbox='read-only', resume=None, mcp_servers=None, approval_policy='on-request'):
        if self.thread_id:
            raise TransportError("THREAD_ALREADY_STARTED")
        if sandbox not in {'read-only','workspace-write','danger-full-access'}:
            raise TransportError('INVALID_SANDBOX')
        if approval_policy not in {'never','on-request'}:
            raise TransportError('INVALID_APPROVAL_POLICY')
        params = {"cwd": str(Path(cwd).resolve()), "sandbox": sandbox, "approvalPolicy": approval_policy}
        if model:
            params["model"] = model
        if mcp_servers:
            params['config'] = {'mcp_servers': mcp_servers}
        if resume:
            params['threadId'] = resume
        result = await self.rpc.request('thread/resume' if resume else 'thread/start', params)
        try:
            thread_id = result["thread"]["id"]
        except (TypeError, KeyError):
            self.rpc._fail("PROTOCOL_MALFORMED")
            raise TransportError("PROTOCOL_MALFORMED") from None
        if not isinstance(thread_id, str) or not thread_id:
            self.rpc._fail("PROTOCOL_MALFORMED")
            raise TransportError("PROTOCOL_MALFORMED")
        self.thread_id = thread_id
        if resume and thread_id != resume:
            raise TransportError('RESUME_SESSION_MISMATCH')
        return {"thread_id": thread_id, "sandbox": sandbox}

    async def send_text(self, text, *, command_id):
        if not self.thread_id:
            raise TransportError("THREAD_REQUIRED")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 65536:
            raise TransportError("INVALID_INPUT")
        async with self._turn_lock:
            if self.turn_id:
                raise TransportError("TURN_BUSY")
            response = await self.rpc.request("turn/start", {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": command_id,
            })
            try:
                self.turn_id = response["turn"]["id"]
                if not isinstance(self.turn_id, str) or not self.turn_id:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                self.rpc._fail("PROTOCOL_MALFORMED")
                raise TransportError("PROTOCOL_MALFORMED") from None
            return {"state": "acknowledged", "thread_id": self.thread_id, "turn_id": self.turn_id}

    async def interrupt(self):
        if not self.thread_id or not self.turn_id:
            raise TransportError("NO_ACTIVE_TURN")
        # Response means accepted cancellation request, not proof that execution stopped.
        await self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
        return {"state": "interrupt_requested", "turn_id": self.turn_id}

    async def next_event(self, *, timeout=None):
        event = await self.rpc.next_event(timeout=timeout)
        if event.get('method') == 'item/started':
            params = event.get('params', {})
            item = params.get('item', {})
            if params.get('threadId') == self.thread_id and item.get('type') == 'mcpToolCall':
                self._mcp_started[item['id']] = (item.get('server'),item.get('tool'),params.get('turnId'))
        if event.get('method') == 'mcpServer/elicitation/request' and 'id' in event:
            params = event.get('params', {})
            meta = params.get('_meta') or {}
            schema = params.get('requestedSchema') or {}
            if not isinstance(meta, dict) or not isinstance(schema, dict):
                raise TransportError('PROTOCOL_MALFORMED')
            candidates = [(iid,identity) for iid,identity in self._mcp_started.items()
                          if identity[0]==params.get('serverName') and identity[2]==params.get('turnId')]
            item_id, identity = candidates[0] if len(candidates)==1 else (None,None)
            tool_name = meta.get('tool_name') or (identity[1] if identity else None)
            self.unmapped_request_shape = {'server':params.get('serverName'), 'mode':params.get('mode'),
                'schema_type':schema.get('type'), 'properties':list(schema.get('properties') or {}),
                'meta_keys':list(meta), 'approval_kind':meta.get('codex_approval_kind'),
                'tool_name':meta.get('tool_name'), 'thread_matches':params.get('threadId')==self.thread_id}
            if (params.get('threadId') == self.thread_id and params.get('serverName') == 'autocoding'
                    and params.get('mode') == 'form' and schema.get('type') == 'object'
                    and not schema.get('properties') and not schema.get('required')
                    and meta.get('codex_approval_kind') == 'mcp_tool_call'
                    and not meta.get('codex_requires_user_input') and isinstance(tool_name, str)):
                operation = {'method':event['method'], 'params':params, 'mcp_tool':tool_name, 'mcp_item_id':item_id}
                local_id = str(event['id'])
                self._approvals[local_id] = (event['id'], operation)
                del self.unmapped_request_shape
                return {**event,'id':local_id,'method':'tool/requestApproval','params':{'operation':operation}}
        if event.get('method') in {'item/commandExecution/requestApproval','item/fileChange/requestApproval'} and 'id' in event:
            operation = {'method':event['method'],'params':event.get('params',{})}
            if operation['params'].get('threadId') != self.thread_id:
                raise TransportError('APPROVAL_SESSION_MISMATCH')
            local_id = str(event['id'])
            self._approvals[local_id] = (event['id'],operation)
            return {**event,'id':local_id,'method':'tool/requestApproval','params':{'operation':operation}}
        if event.get('method') == 'item/tool/requestUserInput' and 'id' in event:
            params = event.get('params', {})
            if params.get('threadId') != self.thread_id:
                raise TransportError('QUESTION_SESSION_MISMATCH')
            self._questions[event['id']] = params
            return {**event,'method':'agent/question','params':{**params,'operation':params}}
        if event.get('method') == 'item/completed':
            params = event.get('params',{})
            item = params.get('item',{})
            if params.get('threadId')==self.thread_id and item.get('type')=='mcpToolCall':
                self._mcp_started.pop(item.get('id'),None)
                receipt = self._mcp_receipts.pop(item.get('id'),None) or self._mcp_receipts.pop(
                    (item.get('server'),item.get('tool'),params.get('turnId')),None)
                return {'method':'tool/result','params':{'threadId':self.thread_id,
                    'toolUseId':receipt or item.get('id'), 'isError':item.get('status')=='failed','activity':str(item.get('tool') or 'MCP tool')[:160], 'detail':str(item.get('aggregatedOutput') or item.get('output') or '')[:8000]}}
            if params.get('threadId')==self.thread_id and item.get('type') in {'commandExecution','fileChange'}:
                return {'method':'tool/result','params':{'threadId':self.thread_id,'toolUseId':item.get('id'),
                        'isError':item.get('status')=='failed','activity':'command' if item.get('type')=='commandExecution' else 'file change', 'detail':str(item.get('aggregatedOutput') or item.get('output') or item.get('command') or '')[:8000]}}
        if event.get("method") == "turn/completed":
            params = event.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("turn"), dict):
                self.rpc._fail("PROTOCOL_MALFORMED")
                raise TransportError("PROTOCOL_MALFORMED")
            turn = params["turn"]
            # A completion can arrive before the turn/start response is handled.
            async with self._turn_lock:
                if params.get("threadId") == self.thread_id and turn.get("id") == self.turn_id:
                    self.turn_id = None
        return event

    async def respond_question(self, request_id, operation_hash, answers):
        from .core import digest
        operation = self._questions.get(request_id)
        if operation is None or digest(operation) != operation_hash or len(answers) != len(operation['questions']):
            raise TransportError('QUESTION_ANSWER_MISMATCH')
        del self._questions[request_id]
        await self.rpc.respond(request_id, {'answers':{q['id']:{'answers':a} for q,a in zip(operation['questions'],answers)}})
        return {'state':'sent_unconfirmed'}

    async def respond_approval(self, request_id, operation_hash, *, allow):
        from .core import digest
        pending = self._approvals.get(request_id)
        if pending is None or digest(pending[1])!=operation_hash or type(allow) is not bool:
            raise TransportError('APPROVAL_OPERATION_MISMATCH')
        wire_id,operation = pending
        del self._approvals[request_id]
        if operation['method'] == 'mcpServer/elicitation/request':
            params = operation['params']
            receipt = 'mcp-approval:' + request_id
            identity = operation.get('mcp_item_id') or (params['serverName'],operation['mcp_tool'],params.get('turnId'))
            if identity in self._mcp_receipts:
                raise TransportError('MCP_APPROVAL_AMBIGUOUS')
            self._mcp_receipts[identity] = receipt
            await self.rpc.respond(wire_id,{'action':'accept' if allow else 'decline', 'content':{} if allow else None})
            return {'state':'sent_unconfirmed','tool_use_id':receipt}
        await self.rpc.respond(wire_id,{'decision':'accept' if allow else 'decline'})
        return {'state':'sent_unconfirmed','tool_use_id':operation['params'].get('itemId')}

    async def close(self):
        await self.rpc.close()

