"""Bounded, fail-closed JSON-lines subprocess and JSON-RPC transport.

No shell, no model policy overrides, no raw protocol persistence. A poisoned
connection never accepts another write. Process-tree isolation is a separate
Runner requirement; closing this transport proves only direct-child exit.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


class TransportError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RpcError(TransportError):
    def __init__(self, error_code: Any):
        super().__init__("RPC_REJECTED")
        self.rpc_code = error_code  # Never expose an arbitrary provider error message.


def json_bytes(message: dict) -> bytes:
    try:
        return (json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise TransportError("INVALID_OUTBOUND_MESSAGE") from None


async def enqueue_output(queue, message):
    """Keep queues bounded while allowing an active consumer to drain a burst.

    Buffered readline/get calls need not yield. Immediate overflow can therefore
    reject a healthy consumer before it gets scheduled. A bounded wait applies
    backpressure without indefinitely hiding RPC replies behind notifications.
    """
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        try:
            await asyncio.wait_for(queue.put(message), timeout=1.0)
        except TimeoutError:
            raise TransportError("OUTPUT_BACKPRESSURE") from None


class JsonLineProcess:
    def __init__(self, process, *, max_frame: int, queue_size: int, write_timeout: float):
        self.process = process
        self.max_frame = max_frame
        self.write_timeout = write_timeout
        self._messages = asyncio.Queue(maxsize=queue_size)
        self._failure: str | None = None
        self._failed = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._closing_task = None
        self.stderr_bytes = 0
        self._readers = [
            asyncio.create_task(self._read_stdout(), name="agent-stdout"),
            asyncio.create_task(self._drain_stderr(), name="agent-stderr"),
        ]

    @classmethod
    async def start(cls, argv, *, cwd: str | Path, env: dict[str, str] | None = None,
                    max_frame=1024 * 1024, queue_size=128, write_timeout=5.0):
        if not argv or not all(isinstance(x, str) and "\0" not in x for x in argv):
            raise TransportError("INVALID_ARGV")
        if Path(argv[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise TransportError("NATIVE_EXECUTABLE_REQUIRED")
        if max_frame < 128 or queue_size < 1 or write_timeout <= 0:
            raise TransportError("INVALID_TRANSPORT_LIMIT")
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW; UI renders protocol events.
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=max_frame + 1, creationflags=flags,
            )
        except (OSError, ValueError):
            raise TransportError("PROCESS_START_FAILED") from None
        return cls(process, max_frame=max_frame, queue_size=queue_size, write_timeout=write_timeout)

    @property
    def failure(self):
        return self._failure

    @property
    def pid(self):
        return self.process.pid

    def poison(self, code: str):
        if self._failure is None:
            self._failure = code
            self._failed.set()

    async def _read_stdout(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    self.poison("PROCESS_STREAM_CLOSED")
                    return
                if len(line) > self.max_frame:
                    self.poison("FRAME_TOO_LARGE")
                    return
                try:
                    message = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, RecursionError):
                    self.poison("PROTOCOL_MALFORMED")
                    return
                if not isinstance(message, dict):
                    self.poison("PROTOCOL_MALFORMED")
                    return
                try:
                    await enqueue_output(self._messages, message)
                except TransportError as exc:
                    self.poison(exc.code)
                    return
        except ValueError:  # StreamReader line limit exceeded.
            self.poison("FRAME_TOO_LARGE")
        except (OSError, asyncio.IncompleteReadError):
            self.poison("PROCESS_READ_FAILED")

    async def _drain_stderr(self):
        try:
            while chunk := await self.process.stderr.read(8192):
                # Drain concurrently, count only. No unbounded buffer or accidental key persistence.
                self.stderr_bytes += len(chunk)
        except OSError:
            self.poison("PROCESS_READ_FAILED")

    async def receive(self) -> dict:
        # Deliver valid frames that preceded EOF, then signal the stream closure.
        if not self._messages.empty():
            return self._messages.get_nowait()
        if self._failure:
            raise TransportError(self._failure)
        get = asyncio.create_task(self._messages.get())
        failed = asyncio.create_task(self._failed.wait())
        try:
            done, _ = await asyncio.wait({get, failed}, return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                return get.result()
            if not self._messages.empty():
                return self._messages.get_nowait()
            raise TransportError(self._failure or "PROCESS_STREAM_CLOSED")
        finally:
            for task in (get, failed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get, failed, return_exceptions=True)

    async def send(self, message: dict):
        data = json_bytes(message)
        if len(data) > self.max_frame:
            raise TransportError("FRAME_TOO_LARGE")
        try:
            await asyncio.wait_for(self._write_lock.acquire(), timeout=self.write_timeout)
        except TimeoutError:
            self.poison("WRITE_TIMEOUT")
            raise TransportError("WRITE_TIMEOUT") from None
        try:
            if self._closed or self._failure:
                raise TransportError(self._failure or "TRANSPORT_CLOSED")
            self.process.stdin.write(data)
            try:
                await asyncio.wait_for(self.process.stdin.drain(), timeout=self.write_timeout)
            except TimeoutError:
                self.poison("WRITE_TIMEOUT")
                raise TransportError("WRITE_TIMEOUT") from None
            except (BrokenPipeError, ConnectionError, OSError):
                self.poison("DELIVERY_UNKNOWN")
                raise TransportError("DELIVERY_UNKNOWN") from None
        except (BrokenPipeError, ConnectionError, OSError):
            self.poison("DELIVERY_UNKNOWN")
            raise TransportError("DELIVERY_UNKNOWN") from None
        except asyncio.CancelledError:
            self.poison("DELIVERY_UNKNOWN")
            raise
        finally:
            self._write_lock.release()

    async def close(self):
        if self._closing_task is None:
            self._closed = True
            self.poison("TRANSPORT_CLOSED")
            self._closing_task = asyncio.create_task(self._close_process())
        await asyncio.shield(self._closing_task)

    async def _close_process(self):
        self.process.stdin.close()
        # Graceful EOF first, then bounded direct-child termination.
        try:
            await asyncio.wait_for(self.process.wait(), timeout=1.0)
        except TimeoutError:
            if self.process.returncode is None:
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except TimeoutError:
                if self.process.returncode is None:
                    self.process.kill()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=1.0)
                except TimeoutError:
                    self.poison("PROCESS_STOP_UNCONFIRMED")
        for task in self._readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)
        # Process.wait() can return as soon as returncode is set, while a
        # paused stdout pipe still owns Windows handles. Process has no public
        # close API; explicitly close its asyncio transport and await all pipe
        # callbacks before allowing the event loop to be destroyed.
        transport = self.process._transport
        transport.close()
        try:
            await asyncio.wait_for(self.process.stdin.wait_closed(), timeout=1)
        except (OSError, TimeoutError):
            pass
        async with asyncio.timeout(2):
            while any(not pipe.disconnected for pipe in transport._pipes.values() if pipe is not None):
                await asyncio.sleep(.01)
            await asyncio.sleep(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


class JsonRpcClient:
    """Correlate responses while preserving notifications and reverse requests."""
    def __init__(self, transport: JsonLineProcess, *, request_timeout=15.0, event_capacity=128):
        self.transport = transport
        self.request_timeout = request_timeout
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events = asyncio.Queue(maxsize=event_capacity)
        self._error = None
        self._failed = asyncio.Event()
        self._reverse_ids: set[str] = set()
        self._request_capacity = event_capacity
        self._reader = asyncio.create_task(self._dispatch(), name="rpc-dispatch")

    def _fail(self, code):
        if self._error is None:
            self._error = code
            self._failed.set()
            self.transport.poison(code)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(TransportError(code))

    async def _dispatch(self):
        try:
            while True:
                message = await self.transport.receive()
                if "method" in message:
                    if not isinstance(message["method"], str):
                        raise TransportError("PROTOCOL_MALFORMED")
                    if "id" in message:
                        rid = message["id"]
                        if type(rid) not in (str, int):
                            raise TransportError("PROTOCOL_MALFORMED")
                        identity = json.dumps(rid)
                        if identity in self._reverse_ids:
                            raise TransportError("DUPLICATE_SERVER_REQUEST")
                        if len(self._reverse_ids) >= self._request_capacity:
                            raise TransportError("PENDING_APPROVAL_LIMIT")
                        self._reverse_ids.add(identity)
                    await enqueue_output(self._events, message)
                elif "id" in message:
                    rid = message["id"]
                    if type(rid) is not int or (("result" in message) == ("error" in message)):
                        raise TransportError("PROTOCOL_MALFORMED")
                    future = self._pending.get(rid)
                    if future is None or future.done():
                        raise TransportError("UNEXPECTED_RPC_RESPONSE")
                    if "error" in message:
                        error = message["error"]
                        if not isinstance(error, dict):
                            raise TransportError("PROTOCOL_MALFORMED")
                        future.set_exception(RpcError(error.get("code")))
                    else:
                        future.set_result(message["result"])
                else:
                    raise TransportError("PROTOCOL_MALFORMED")
        except TransportError as exc:
            self._fail(exc.code)
        except asyncio.CancelledError:
            raise

    async def request(self, method, params=None, *, timeout=None):
        if self._error:
            raise TransportError(self._error)
        if len(self._pending) >= self._request_capacity:
            raise TransportError("PENDING_REQUEST_LIMIT")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.transport.send({"id": request_id, "method": method, "params": params or {}})
            try:
                return await asyncio.wait_for(asyncio.shield(future), self.request_timeout if timeout is None else timeout)
            except TimeoutError:
                self._fail("RPC_TIMEOUT_DELIVERY_UNKNOWN")
                raise TransportError("RPC_TIMEOUT_DELIVERY_UNKNOWN") from None
        except asyncio.CancelledError:
            self._fail("RPC_CANCELLED_DELIVERY_UNKNOWN")
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume poison/timeout exceptions even when caller was cancelled.

    async def notify(self, method, params=None):
        if self._error:
            raise TransportError(self._error)
        await self.transport.send({"method": method, "params": params or {}})

    async def respond(self, request_id, result):
        identity = json.dumps(request_id)
        if identity not in self._reverse_ids:
            raise TransportError("UNKNOWN_SERVER_REQUEST")
        if self._error:
            raise TransportError(self._error)
        # Reserve before yielding, so two callers cannot answer one approval twice.
        self._reverse_ids.remove(identity)
        await self.transport.send({"id": request_id, "result": result})

    async def next_event(self, *, timeout=None):
        if not self._events.empty():
            return self._events.get_nowait()
        if self._error:
            raise TransportError(self._error)
        get = asyncio.create_task(self._events.get())
        failed = asyncio.create_task(self._failed.wait())
        try:
            done, _ = await asyncio.wait({get, failed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                return get.result()
            if not done:
                raise TimeoutError
            raise TransportError(self._error or "TRANSPORT_CLOSED")
        finally:
            for task in (get, failed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get, failed, return_exceptions=True)

    async def close(self):
        self._fail("TRANSPORT_CLOSED")
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        await self.transport.close()


