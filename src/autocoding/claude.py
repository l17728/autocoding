"""Claude Code stream-json adapter with explicitly enabled tool approvals.

Replay UUID is the acceptance receipt; a pipe write is not acceptance. Output
uses the session event vocabulary consumed by SessionRuntime. No raw provider
errors, thinking blocks, settings or replayed input enter the event stream.
"""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from .transport import JsonLineProcess, TransportError


class ClaudeSession:
    def __init__(self, transport, *, session_id, timeout=30, event_capacity=128, controls=False, tools_enabled=False):
        self.transport = transport
        self.thread_id = session_id
        self.turn_id = None
        self.timeout = timeout
        self._ack = None
        self._input = None
        self._result_received = False
        self._events = asyncio.Queue(maxsize=event_capacity)
        self._error = None
        self._failed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.controls = controls
        self.tools_enabled = tools_enabled
        self._controls = {}
        self._permissions = {}
        self._permission_replies = {}
        self._tool_names = {}
        self.last_error_diagnostic = None
        self._frames_seen = 0
        self._last_frame_diagnostic = None
        self._frame_type_counts = {}
        self._turn_correlated_frames = {}
        self._reader = asyncio.create_task(self._dispatch(), name="claude-dispatch")

    @classmethod
    async def connect(cls, executable, *, cwd, env, model, timeout=30, tools=(), persist=False, resume=None, skip_permissions=False, mcp_config=None):
        if any(tool not in {'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'AskUserQuestion'} for tool in tools):
            raise TransportError('UNSUPPORTED_TOOL')
        if resume and not persist:
            raise TransportError('PERSISTENCE_REQUIRED')
        try:
            session_id = str(uuid.UUID(resume)) if resume else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            raise TransportError('INVALID_SESSION_ID') from None
        argv = [
            str(executable), "-p", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--replay-user-messages",
            "--model", model, "--tools", ','.join(tools),
            "--strict-mcp-config", "--setting-sources", "",
            '--permission-prompt-tool', 'stdio',
        ]
        if skip_permissions:
            argv += ['--dangerously-skip-permissions']
        else:
            argv += ['--permission-mode', 'manual']
            if not mcp_config:
                argv += ['--safe-mode']
        if mcp_config:
            argv += ['--mcp-config', str(mcp_config)]
        argv += ['--resume=' + session_id] if resume else ['--session-id', session_id]
        if not persist:
            argv.append('--no-session-persistence')
        transport = await JsonLineProcess.start(argv, cwd=cwd, env=env)
        adapter = cls(transport, session_id=session_id, timeout=timeout, controls=True, tools_enabled=bool(tools))
        try:
            await adapter.control({'subtype': 'initialize', 'hooks': None})
            return adapter
        except BaseException:
            await adapter.close()
            raise

    def _fail(self, code):
        if self._error is None:
            self._error = code
            self._failed.set()
            self.transport.poison(code)
            if self._ack is not None and not self._ack.done():
                self._ack.set_exception(TransportError(code))
            for future in self._controls.values():
                if not future.done():
                    future.set_exception(TransportError(code))

    def _emit(self, method, **params):
        try:
            self._events.put_nowait({"method": method, "params": {
                "threadId": self.thread_id, **params}})
        except asyncio.QueueFull:
            raise TransportError("OUTPUT_BACKPRESSURE") from None

    def _correlated(self, message):
        self.last_correlation = {'type': message.get('type'), 'has_turn': bool(self.turn_id),
            'result_received': self._result_received,
            'single_matches': message.get('user_message_uuid') == self.turn_id,
            'list_matches': message.get('user_message_uuids') == [self.turn_id],
            'single_present': 'user_message_uuid' in message,
            'list_present': 'user_message_uuids' in message}
        # Tool-bearing assistant frames in CLI 2.1.296 omit input UUID fields.
        # A top-level tool call is itself a receipt that the only active turn
        # reached the model, even when the CLI omitted the legacy user replay.
        # Attach it only to that one pending turn; child frames remain progress
        # and final result still requires the input UUID before completion.
        if (self.controls and message.get('type') == 'assistant'
                and 'user_message_uuid' not in message and 'user_message_uuids' not in message
                and self.turn_id and not self._result_received and self._ack is not None):
            if (self._ack.done() and not self._ack.cancelled()
                    and self._ack.exception() is None):
                return
            body = message.get('message')
            blocks = body.get('content') if isinstance(body, dict) else None
            has_top_level_tool_call = (message.get('parent_tool_use_id') is None
                and isinstance(blocks, list)
                and any(isinstance(block, dict) and block.get('type') == 'tool_use'
                        for block in blocks))
            if has_top_level_tool_call:
                if not self._ack.done():
                    self._ack.set_result(True)
                return
        if (not self.turn_id or self._result_received
                or message.get("user_message_uuid") != self.turn_id
                or message.get("user_message_uuids") != [self.turn_id]):
            raise TransportError("CLAUDE_TURN_MISMATCH")
        # Recent Claude CLI versions can begin a correlated assistant/result
        # turn without first emitting the legacy replayed user frame. A matched
        # provider turn is an equally strong acceptance receipt; waiting only
        # for that legacy frame produces a false delivery-unknown timeout.
        if self._ack is not None and not self._ack.done():
            self._ack.set_result(True)

    def _handle(self, message):
        self._frames_seen += 1
        self._last_frame_diagnostic = self._event_diagnostic(message)
        kind = message.get('type') if isinstance(message,dict) else type(message).__name__
        if isinstance(kind,str):
            self._frame_type_counts[kind] = self._frame_type_counts.get(kind,0)+1
        correlated = isinstance(message,dict) and self.turn_id and (
            any(message.get(field)==self.turn_id for field in ('uuid','user_message_uuid'))
            or (isinstance(message.get('user_message_uuids'),list)
                and self.turn_id in message['user_message_uuids']))
        if correlated:
            if isinstance(kind,str):
                self._turn_correlated_frames[kind] = self._turn_correlated_frames.get(kind,0)+1
        kind = message.get("type")
        if not isinstance(kind, str):
            raise TransportError("PROTOCOL_MALFORMED")
        if kind == 'control_response':
            response = message.get('response')
            if not isinstance(response, dict) or not isinstance(response.get('request_id'), str):
                raise TransportError('PROTOCOL_MALFORMED')
            future = self._controls.get(response['request_id'])
            if future is None and response['request_id'] in self._permission_replies:
                tool_id = self._permission_replies.pop(response['request_id'])
                if response.get('subtype') != 'success':
                    raise TransportError('CLAUDE_APPROVAL_REJECTED')
                self._emit('approval/delivered', requestId=response['request_id'], toolUseId=tool_id)
                return
            if future is None or future.done():
                self.last_control = {'known_control': future is not None,
                    'permission_replies_pending': len(self._permission_replies),
                    'subtype_success': response.get('subtype') == 'success'}
                raise TransportError('UNEXPECTED_CONTROL_RESPONSE')
            if response.get('subtype') == 'success':
                future.set_result(response.get('response', {}))
            else:
                future.set_exception(TransportError('CLAUDE_CONTROL_REJECTED'))
            return
        # Permission operation data stays in memory; the ledger keeps its hash.
        if kind == "control_request":
            if not self.controls or not self.tools_enabled:
                raise TransportError("CLAUDE_APPROVAL_UNSUPPORTED")
            request, rid = message.get('request'), message.get('request_id')
            if (not isinstance(request, dict) or request.get('subtype') != 'can_use_tool'
                    or not isinstance(rid, str) or not rid or len(rid) > 200
                    or not isinstance(request.get('tool_name'), str) or not isinstance(request.get('input'), dict)
                    or not isinstance(request.get('tool_use_id'), str) or not request['tool_use_id']):
                raise TransportError('PROTOCOL_MALFORMED')
            if rid in self._permissions or rid in self._permission_replies or len(self._permissions) + len(self._permission_replies) >= 128:
                raise TransportError('PENDING_APPROVAL_LIMIT')
            self._permissions[rid] = request
            try:
                self._events.put_nowait({'id': rid, 'method': 'agent/question' if request['tool_name']=='AskUserQuestion' else 'tool/requestApproval',
                    'params': {'threadId': self.thread_id, 'turnId': self.turn_id, 'operation': request}})
            except asyncio.QueueFull:
                raise TransportError('OUTPUT_BACKPRESSURE') from None
            return
        if kind == 'control_cancel_request':
            rid = message.get('request_id')
            if not isinstance(rid, str):
                raise TransportError('PROTOCOL_MALFORMED')
            self._permissions.pop(rid, None)
            self._emit('approval/cancelled', requestId=rid)
            return
        if message.get("session_id") != self.thread_id:
            raise TransportError("CLAUDE_SESSION_MISMATCH")
        if kind == 'system':
            # The Zhipu-backed CLI emits correlated status/thinking progress
            # frames before the legacy replay-user receipt. The exact input
            # UUID and session match prove delivery, never turn completion.
            if (message.get('subtype') in {'status','thinking_tokens'} and self.turn_id
                    and message.get('user_message_uuid') == self.turn_id
                    and not self._result_received and self._ack is not None
                    and not self._ack.done()):
                self._ack.set_result(True)
                self._input = None
            return
        if kind == "command_lifecycle":
            return  # Local metadata is neither a receipt nor public model text.
        if kind == 'tool_progress':
            name=message.get('tool_name')
            tool_id=message.get('tool_use_id')
            elapsed=message.get('elapsed_time_seconds')
            if (not isinstance(name,str) or not name or len(name)>160
                    or not isinstance(tool_id,str) or not tool_id or len(tool_id)>200
                    or isinstance(elapsed,bool) or not isinstance(elapsed,(int,float))
                    or not math.isfinite(elapsed) or elapsed < 0 or elapsed > 86400):
                raise TransportError('PROTOCOL_MALFORMED')
            self._emit('tool/progress', turnId=self.turn_id, toolUseId=tool_id,
                       toolName=name, elapsedSeconds=elapsed)
            return
        if kind == "user":
            body = message.get("message")
            if self.tools_enabled and isinstance(body, dict) and isinstance(body.get('content'), list):
                for block in body['content']:
                    if not isinstance(block, dict) or block.get('type') != 'tool_result' or not isinstance(block.get('tool_use_id'), str):
                        raise TransportError('PROTOCOL_MALFORMED')
                    content=block.get('content','')
                    if isinstance(content,list):
                        content='\n'.join(part.get('text','') for part in content
                                          if isinstance(part,dict) and part.get('type')=='text')
                    elif not isinstance(content,str):
                        content=json.dumps(content,ensure_ascii=False) if content is not None else ''
                    self._emit('tool/result', turnId=self.turn_id, toolUseId=block['tool_use_id'],
                               isError=block.get('is_error', False) is True,
                               activity=self._tool_names.pop(block['tool_use_id'],'Claude tool'),
                               detail=content[:8000])
                return
            if (not self.turn_id or message.get("uuid") != self.turn_id
                    or message.get("isReplay") is not True
                    or message.get("parent_tool_use_id") is not None
                    or not isinstance(body, dict) or body.get("role") != "user"
                    or body.get("content") != self._input
                    or self._ack is None or self._ack.done()):
                raise TransportError("CLAUDE_ACK_MISMATCH")
            self._ack.set_result(True)
            self._input = None
            return
        if kind not in {"assistant", "result"}:
            raise TransportError("CLAUDE_UNSUPPORTED_EVENT")
        self._correlated(message)
        if kind == "assistant":
            if message.get("error") is not None:
                return  # CLI-generated API error text is not public model output.
            # Current Claude CLI releases may emit a child assistant frame
            # while a tool is streaming.  It has no independently
            # deliverable turn result and must not terminate the parent task.
            # The enclosing assistant frame already carries the tool start;
            # final completion remains strictly correlated below.
            if message.get("parent_tool_use_id") is not None:
                return
            body = message.get("message")
            if not isinstance(body, dict) or body.get("role") != "assistant" or not isinstance(body.get("content"), list):
                raise TransportError("PROTOCOL_MALFORMED")
            for block in body["content"]:
                if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                    raise TransportError("PROTOCOL_MALFORMED")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    self._emit("item/agentMessage/delta", turnId=self.turn_id, delta=block["text"])
                elif block.get('type') == 'tool_use' and self.tools_enabled:
                    tool_id=block.get('id')
                    if isinstance(tool_id,str):
                        self._tool_names[tool_id]=str(block.get('name') or 'Claude tool')[:160]
                    self._emit('tool/started', turnId=self.turn_id, toolUseId=block.get('id'), toolName=block.get('name'))
                elif block.get("type") not in {"thinking", "redacted_thinking"}:
                    raise TransportError("CLAUDE_UNSUPPORTED_CONTENT")
            return
        error = message.get("is_error")
        subtype = message.get("subtype")
        if type(error) is not bool or not isinstance(subtype, str):
            raise TransportError("PROTOCOL_MALFORMED")
        if subtype == "success" and not error:
            status = "completed"
        elif error and (subtype.startswith("error_") or subtype == 'success'):
            status = "failed"
        else:
            raise TransportError("PROTOCOL_MALFORMED")
        self._result_received = True
        self._emit("turn/completed", turn={"id": self.turn_id, "status": status})

    async def _dispatch(self):
        try:
            while True:
                message = await self.transport.receive()
                try:
                    self._handle(message)
                except TransportError as exc:
                    if exc.code == 'CLAUDE_UNSUPPORTED_EVENT':
                        self.last_error_diagnostic = self._event_diagnostic(message)
                    raise
        except TransportError as exc:
            self._fail(exc.code)

    def _event_diagnostic(self, message):
        """Return protocol shape only; never persist provider frame values."""
        if not isinstance(message, dict):
            return {'event_type': type(message).__name__, 'frame_type': type(message).__name__}
        event_type = message.get('type')
        if not isinstance(event_type, str) or len(event_type) > 100:
            event_type = type(event_type).__name__
        subtype = message.get('subtype')
        if not isinstance(subtype,str) or not 1<=len(subtype)<=80 or not all(c.isalnum() or c in '._-' for c in subtype):
            subtype = None
        nested = message.get('message')
        blocks = nested.get('content') if isinstance(nested, dict) else None
        block_types = sorted({block.get('type') for block in blocks
                              if isinstance(block, dict) and isinstance(block.get('type'), str)})[:16] \
                      if isinstance(blocks, list) else []
        return {'event_type': event_type, 'event_subtype': subtype,
                'frame_keys': sorted(k for k in message if isinstance(k, str)
                                     and len(k) <= 80 and k.replace('_', '').isalnum())[:24],
                'message_keys': sorted(k for k in nested if isinstance(k, str)
                                       and len(k) <= 80 and k.replace('_', '').isalnum())[:24]
                                if isinstance(nested, dict) else [],
                'content_block_types': block_types,
                'session_matches': message.get('session_id') == self.thread_id,
                'input_uuid_present': 'user_message_uuid' in message,
                'input_uuids_present': 'user_message_uuids' in message}

    async def send_text(self, text, *, command_id):
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 65536:
            raise TransportError("INVALID_INPUT")
        async with self._send_lock:
            if self._error:
                raise TransportError(self._error)
            if self.turn_id:
                raise TransportError("TURN_BUSY")
            # Core command IDs need not be UUIDs; the local runtime maps this
            # protocol UUID to command_id without asking Claude to deduplicate.
            self.turn_id = str(uuid.uuid4())
            self._result_received = False
            self._input = text
            self._ack = asyncio.get_running_loop().create_future()
            try:
                await self.transport.send({"type": "user", "session_id": self.thread_id,
                    "uuid": self.turn_id, "parent_tool_use_id": None,
                    "message": {"role": "user", "content": text}})
                await asyncio.wait_for(asyncio.shield(self._ack), timeout=self.timeout)
                return {"state": "acknowledged", "thread_id": self.thread_id, "turn_id": self.turn_id}
            except TimeoutError:
                self.last_error_diagnostic = {
                    'failure_phase': 'ack_wait',
                    'frames_seen': self._frames_seen,
                    'frame_type_counts': dict(sorted(self._frame_type_counts.items(),
                                                     key=lambda item:(-item[1],item[0]))[:16]),
                    'turn_correlated_frame_counts': dict(sorted(self._turn_correlated_frames.items(),
                                                                key=lambda item:(-item[1],item[0]))[:16]),
                    'last_frame': self._last_frame_diagnostic,
                }
                self._fail("CLAUDE_ACK_TIMEOUT_DELIVERY_UNKNOWN")
                raise TransportError("CLAUDE_ACK_TIMEOUT_DELIVERY_UNKNOWN") from None
            except asyncio.CancelledError:
                self._fail("CLAUDE_CANCELLED_DELIVERY_UNKNOWN")
                raise
            except TransportError as exc:
                self._fail(exc.code)
                raise
            finally:
                self._input = None
                if not self._ack.done():
                    self._ack.cancel()
                elif not self._ack.cancelled():
                    self._ack.exception()

    async def next_event(self, *, timeout=None):
        if not self._events.empty():
            event = self._events.get_nowait()
        else:
            if self._error:
                raise TransportError(self._error)
            get = asyncio.create_task(self._events.get())
            failed = asyncio.create_task(self._failed.wait())
            try:
                done, _ = await asyncio.wait({get, failed}, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    event = get.result()
                elif not done:
                    raise TimeoutError
                else:
                    raise TransportError(self._error or "TRANSPORT_CLOSED")
            finally:
                for task in (get, failed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(get, failed, return_exceptions=True)
        if event["method"] == "turn/completed":
            # Do not clear the active ID while send_text is returning its receipt.
            async with self._send_lock:
                self.turn_id = None
        return event

    async def interrupt(self):
        if not self.controls:
            raise TransportError("CLAUDE_INTERRUPT_UNSUPPORTED")
        if not self.turn_id:
            raise TransportError('NO_ACTIVE_TURN')
        await self.control({'subtype': 'interrupt'})
        return {'state': 'interrupt_requested', 'turn_id': self.turn_id}

    async def control(self, request):
        if self._error:
            raise TransportError(self._error)
        if len(self._controls) >= 16:
            raise TransportError('PENDING_REQUEST_LIMIT')
        rid = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._controls[rid] = future
        try:
            await self.transport.send({'type': 'control_request', 'request_id': rid, 'request': request})
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.CancelledError:
            self._fail('CLAUDE_CONTROL_DELIVERY_UNKNOWN')
            raise
        except TimeoutError:
            self._fail('CLAUDE_CONTROL_DELIVERY_UNKNOWN')
            raise TransportError('CLAUDE_CONTROL_DELIVERY_UNKNOWN') from None
        finally:
            self._controls.pop(rid, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def respond_approval(self, request_id, operation_hash, *, allow, answers=None):
        from .core import digest
        request = self._permissions.get(request_id)
        if request is None or digest(request) != operation_hash or type(allow) is not bool:
            raise TransportError('APPROVAL_OPERATION_MISMATCH')
        if self._error:
            raise TransportError(self._error)
        updated = request['input']
        if answers is not None:
            if request['tool_name'] != 'AskUserQuestion' or len(answers) != len(updated.get('questions', [])):
                raise TransportError('QUESTION_ANSWER_MISMATCH')
            updated = {**updated, 'answers':{q['question']:a[0] if len(a)==1 else a
                for q,a in zip(updated['questions'], answers)}}
        del self._permissions[request_id]  # Reserve before awaiting; never double-send.
        self._permission_replies[request_id] = request.get('tool_use_id')
        response = {'behavior': 'allow', 'updatedInput': updated} if allow else {
            'behavior': 'deny', 'message': 'Denied by operator'}
        await self.transport.send({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': request_id, 'response': response}})
        return {'state': 'sent_unconfirmed', 'tool_use_id': request.get('tool_use_id')}

    async def respond_question(self, request_id, operation_hash, answers):
        return await self.respond_approval(request_id, operation_hash, allow=True, answers=answers)

    async def close(self):
        self._fail("TRANSPORT_CLOSED")
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        await self.transport.close()
