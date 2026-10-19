"""Trusted single-host command routing between Core and a provider session.

All input/takeover operations for this session must go through this owner.
There is no HTTP authentication or multi-host Runner fencing in this phase.
"""
from __future__ import annotations

import asyncio
import uuid
from .core import Core, DomainError, digest
from .transport import TransportError


class SessionRuntime:
    def __init__(self, core: Core, session_id: str, adapter):
        self.core, self.session_id, self.adapter = core, session_id, adapter
        self._lock = asyncio.Lock()
        self._instance = uuid.uuid4().hex
        self._turns = {}
        self._quarantined = False
        self.closed = False

    def _unknown(self, command_id, reason):
        command = self.core.command(command_id)
        if command["state"] in {"dispatching", "sent", "acknowledged"}:
            self.core.record_command(command_id, "delivery_unknown", reason=reason,
                                     key=f"unknown:{self._instance}:{command_id}")
        self._quarantined = True

    async def send(self, command_id: str, text: str):
        async with self._lock:
            if self._quarantined:
                raise TransportError("RECONCILIATION_REQUIRED")
            command = self.core.command(command_id)
            if command["session_id"] != self.session_id:
                raise DomainError("WRONG_SESSION")
            if digest(text) != command["payload_hash"]:
                raise DomainError("INPUT_HASH_MISMATCH")
            if command["state"] != "queued":
                # Cached state is a receipt only. Never resend a previous dispatch.
                return {"command_id": command_id, "state": command["state"], "resent": False}
            self.core.begin_dispatch(command_id, key=f"dispatch:{self._instance}:{command_id}")
            try:
                receipt = await self.adapter.send_text(text, command_id=command_id)
                turn_id = receipt["turn_id"]
                self._turns[turn_id] = command_id
                return self.core.record_command(
                    command_id, "acknowledged", provider_turn_id=turn_id,
                    key=f"ack:{self._instance}:{command_id}")
            except asyncio.CancelledError:
                self._unknown(command_id, "CANCELLED_DURING_SEND")
                await self.adapter.close()
                raise
            except Exception:
                self._unknown(command_id, "SEND_OR_ACK_FAILED")
                await self.adapter.close()
                raise

    async def next_event(self, *, timeout=None):
        try:
            event = await self.adapter.next_event(timeout=timeout)
        except TransportError:
            self._quarantined = True
            for command_id in self._turns.values():
                self._unknown(command_id, "CONNECTION_LOST")
            raise
        async with self._lock:
            if event.get("method") == "turn/completed":
                params = event.get("params", {})
                if params.get("threadId") != self.adapter.thread_id:
                    return event
                turn = params.get("turn", {})
                command_id = self._turns.get(turn.get("id"))
                if command_id:
                    # Provider completion does not mean task verification.
                    status = turn.get("status")
                    states = {"completed": "completed", "failed": "failed", "interrupted": "cancelled"}
                    if status not in states:
                        self._unknown(command_id, "UNKNOWN_TURN_STATUS")
                    elif self.core.command(command_id)["state"] == "acknowledged":
                        self.core.record_command(command_id, states[status],
                                                 provider_turn_id=turn["id"],
                                                 key=f"finish:{self._instance}:{command_id}")
                    self._turns.pop(turn.get("id"), None)
        return event



    async def takeover(self, owner: str, expected_epoch: int, *, key: str, timeout=5):
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=timeout)
        except TimeoutError:
            # The caller sees a bounded pending-control result. Existing send
            # still owns the arbiter and its transport deadline remains active.
            raise TransportError("INPUT_CONTROL_PENDING") from None
        try:
            result = self.core.takeover(self.session_id, owner, expected_epoch, key=key)
            if result["state"] == "reconciling":
                self._quarantined = True
                try:
                    await self.adapter.interrupt()
                except TransportError:
                    pass
                finally:
                    self.closed = True
                    await self.adapter.close()
                # Root close is not proof of remote/descendant stop: keep frozen.
            return result
        finally:
            self._lock.release()

    async def close(self):
        async with self._lock:
            self.closed = True
            for command_id in list(self._turns.values()):
                self._unknown(command_id, "RUNTIME_CLOSED")
            self._quarantined = True
            await self.adapter.close()

