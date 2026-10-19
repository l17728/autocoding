import asyncio
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.codex import CodexSession
from autocoding.transport import JsonLineProcess, JsonRpcClient, RpcError, TransportError

FIXTURE = str(ROOT / "tests/fixtures/json_agent.py")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def launch(self, mode="echo", **kwargs):
        process = await JsonLineProcess.start([sys.executable, "-u", FIXTURE, mode], cwd=ROOT, **kwargs)
        self.addAsyncCleanup(process.close)
        return process

    async def rpc(self, mode="rpc", **kwargs):
        process = await self.launch(mode)
        client = JsonRpcClient(process, **kwargs)
        self.addAsyncCleanup(client.close)
        return client

    async def test_unicode_split_frames_and_shell_text_remain_data(self):
        process = await self.launch("split")
        message = {"text": "中文😀\n$(not_a_shell_command)"}
        await process.send(message)
        self.assertEqual(message, await asyncio.wait_for(process.receive(), 3))

    async def test_stderr_is_drained_without_persisting_or_blocking(self):
        process = await self.launch("stderr")
        await process.send({"test": 1})
        self.assertEqual({"test": 1}, await asyncio.wait_for(process.receive(), 3))
        self.assertEqual(256 * 1024, process.stderr_bytes)

    async def test_malformed_utf8_poison_is_sanitized(self):
        process = await self.launch("malformed")
        with self.assertRaisesRegex(TransportError, "^PROTOCOL_MALFORMED$"):
            await asyncio.wait_for(process.receive(), 3)
        with self.assertRaises(TransportError):
            await process.send({"text": "must not write"})

    async def test_oversized_frame_has_bounded_read(self):
        process = await self.launch("oversize", max_frame=1024)
        with self.assertRaisesRegex(TransportError, "^FRAME_TOO_LARGE$"):
            await asyncio.wait_for(process.receive(), 3)

    async def test_output_backpressure_is_explicit_not_silent_drop(self):
        process = await self.launch("flood", queue_size=2)
        await asyncio.wait_for(process._failed.wait(), 3)
        self.assertEqual("OUTPUT_BACKPRESSURE", process.failure)
        with self.assertRaisesRegex(TransportError, "^OUTPUT_BACKPRESSURE$"):
            await process.send({"text": "not sent"})

    async def test_buffered_burst_reaches_active_consumer_in_order(self):
        process = await self.launch("flood", queue_size=2)
        received = [await asyncio.wait_for(process.receive(), 3) for _ in range(100)]
        self.assertEqual(list(range(100)), [item['params']['i'] for item in received])
        self.assertIsNone(process.failure)

    async def test_rpc_burst_reaches_active_consumer_in_order(self):
        rpc = await self.rpc("flood", event_capacity=2)
        received = [await rpc.next_event(timeout=3) for _ in range(100)]
        self.assertEqual(list(range(100)), [item['params']['i'] for item in received])

    async def test_rpc_stalled_consumer_still_freezes_connection(self):
        rpc = await self.rpc("flood", event_capacity=2)
        await asyncio.wait_for(rpc._failed.wait(), 3)
        self.assertEqual('OUTPUT_BACKPRESSURE', rpc._error)
        with self.assertRaisesRegex(TransportError, '^OUTPUT_BACKPRESSURE$'):
            await rpc.request('must_not_replay')

    async def test_rpc_reply_after_burst_and_next_request_remain_usable(self):
        rpc = await self.rpc(event_capacity=2)
        async def consume():
            return [await rpc.next_event(timeout=3) for _ in range(100)]
        result, events = await asyncio.gather(rpc.request('test/burst'), consume())
        self.assertEqual({'done': True}, result)
        self.assertEqual(list(range(100)), [event['params']['i'] for event in events])
        self.assertEqual({'ok': True}, await rpc.request('test/echo', {'ok': True}))

    async def test_close_while_stdout_queue_is_backpressured(self):
        process = await self.launch('flood', queue_size=2)
        async with asyncio.timeout(3):
            while not process._messages.full():
                await asyncio.sleep(.001)
        await asyncio.wait_for(asyncio.gather(process.close(), process.close()), 5)
        self.assertTrue(all(task.done() for task in process._readers))
        self.assertIsNotNone(process.process.returncode)
        with self.assertRaises(TransportError):
            await process.send({'must_not_send': True})

    async def test_cancel_event_waiter_then_close_backpressured_rpc(self):
        rpc = await self.rpc(event_capacity=2)
        waiter = asyncio.create_task(rpc.next_event())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        request = asyncio.create_task(rpc.request('test/burst'))
        async with asyncio.timeout(3):
            while not rpc._events.full():
                await asyncio.sleep(.001)
        await asyncio.wait_for(rpc.close(), 5)
        with self.assertRaises(TransportError):
            await request
        self.assertTrue(rpc._reader.done())
        self.assertFalse(rpc._pending)

    async def test_disconnect_before_concurrent_approval_answers_sends_nothing(self):
        rpc = await self.rpc()
        await rpc.request('test/reverse-request')
        await rpc.next_event(timeout=1)
        rpc.transport.process.kill()
        await asyncio.wait_for(rpc._failed.wait(), 3)
        with patch.object(rpc.transport, 'send', wraps=rpc.transport.send) as send:
            results = await asyncio.gather(
                rpc.respond('approval_1', {'decision': 'accept'}),
                rpc.respond('approval_1', {'decision': 'decline'}),
                return_exceptions=True)
            self.assertTrue(all(isinstance(result, TransportError) for result in results))
            send.assert_not_called()

    async def test_close_releases_paused_pipes_before_loop_shutdown(self):
        process = await self.launch('flood', queue_size=2)
        # Bound cold interpreter startup separately from the backpressure
        # deadline; a busy Windows runner can take most of three seconds to boot.
        async with asyncio.timeout(10):
            while not process._messages.full():await asyncio.sleep(.01)
        await asyncio.wait_for(process._failed.wait(), 3)
        self.assertEqual('OUTPUT_BACKPRESSURE',process._failure)
        await asyncio.gather(process.close(), process.close())
        self.assertIsNotNone(process.process.returncode)
        self.assertTrue(all(pipe.disconnected for pipe in process.process._transport._pipes.values()))
        self.assertTrue(all(task.done() for task in process._readers))

    async def test_real_blocked_stdin_deadline_freezes_future_writes(self):
        process = await self.launch("no-read", write_timeout=.1)
        self.assertEqual({"ready": True}, await asyncio.wait_for(process.receive(), 3))
        with self.assertRaisesRegex(TransportError, "^WRITE_TIMEOUT$"):
            await process.send({"text": "x" * (900 * 1024)})
        with self.assertRaisesRegex(TransportError, "^WRITE_TIMEOUT$"):
            await process.send({"text": "later"})
        await process.close()
        self.assertIsNotNone(process.process.returncode)

    async def test_close_is_idempotent_and_rejects_more_input(self):
        process = await self.launch()
        await process.close()
        await process.close()
        self.assertIsNotNone(process.process.returncode)
        with self.assertRaises(TransportError):
            await process.send({"x": 1})

    async def test_rpc_responses_are_correlated_out_of_order(self):
        rpc = await self.rpc("reverse")
        first, second = await asyncio.gather(rpc.request("first", {"n": 1}), rpc.request("second", {"n": 2}))
        self.assertEqual({"n": 1}, first)
        self.assertEqual({"n": 2}, second)
        self.assertEqual("progress", (await rpc.next_event(timeout=1))["method"])

    async def test_rpc_error_does_not_leak_provider_text_or_poison_valid_connection(self):
        rpc = await self.rpc()
        with self.assertRaises(RpcError) as error:
            await rpc.request("test/error")
        self.assertEqual(-32000, error.exception.rpc_code)
        self.assertNotIn("PRIVATE_CANARY", str(error.exception))
        self.assertEqual({"ok": True}, await rpc.request("test/echo", {"ok": True}))

    async def test_reverse_approval_is_exposed_and_not_auto_approved(self):
        rpc = await self.rpc()
        await rpc.request("test/reverse-request")
        event = await rpc.next_event(timeout=1)
        self.assertEqual("approval_1", event["id"])
        self.assertEqual({'"approval_1"'}, rpc._reverse_ids)
        await rpc.respond("approval_1", {"decision": "decline"})
        with self.assertRaisesRegex(TransportError, "^UNKNOWN_SERVER_REQUEST$"):
            await rpc.respond("approval_1", {"decision": "accept"})

    async def test_concurrent_approval_responses_send_only_once(self):
        rpc = await self.rpc()
        await rpc.request("test/reverse-request")
        await rpc.next_event(timeout=1)
        results = await asyncio.gather(
            rpc.respond("approval_1", {"decision": "decline"}),
            rpc.respond("approval_1", {"decision": "accept"}),
            return_exceptions=True)
        self.assertEqual(1, sum(result is None for result in results))
        errors = [result for result in results if isinstance(result, TransportError)]
        self.assertEqual(["UNKNOWN_SERVER_REQUEST"], [error.code for error in errors])

    async def test_rpc_timeout_quarantines_connection_without_retry(self):
        rpc = await self.rpc("silent", request_timeout=.2)
        with self.assertRaisesRegex(TransportError, "^RPC_TIMEOUT_DELIVERY_UNKNOWN$"):
            await rpc.request("may_have_side_effects")
        with self.assertRaisesRegex(TransportError, "^RPC_TIMEOUT_DELIVERY_UNKNOWN$"):
            await rpc.request("must_not_send")

    async def test_process_exit_is_not_task_completion(self):
        rpc = await self.rpc("early-exit")
        with self.assertRaises(TransportError):
            await rpc.request("work")

    async def test_cancelled_request_quarantines_connection(self):
        rpc = await self.rpc("silent")
        request = asyncio.create_task(rpc.request("work"))
        await asyncio.sleep(.05)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        with self.assertRaisesRegex(TransportError, "^RPC_CANCELLED_DELIVERY_UNKNOWN$"):
            await rpc.request("other")

    async def test_codex_roundtrip_ignores_other_thread_completion(self):
        rpc = await self.rpc("rpc")
        session = CodexSession(rpc)
        await session.start_thread(ROOT)
        ack = await session.send_text("中文多行\n第二行", command_id="cmd_fixture")
        self.assertEqual("acknowledged", ack["state"])
        with self.assertRaisesRegex(TransportError, "^TURN_BUSY$"):
            await session.send_text("duplicate", command_id="cmd_second")
        events = [await session.next_event(timeout=2) for _ in range(3)]
        self.assertEqual("other_thread", events[-1]["params"]["threadId"])
        self.assertEqual("turn_fixture", session.turn_id)
        await session.next_event(timeout=2)
        self.assertIsNone(session.turn_id)

    async def test_interrupt_ack_is_not_stop_confirmation(self):
        rpc = await self.rpc("hold-turn")
        session = CodexSession(rpc)
        await session.start_thread(ROOT)
        await session.send_text("work", command_id="cmd")
        await session.interrupt()
        self.assertEqual("turn_fixture", session.turn_id)
        await rpc.request("test/finish")
        for _ in range(3):
            await session.next_event(timeout=2)
        self.assertIsNone(session.turn_id)


if __name__ == "__main__":
    unittest.main()

