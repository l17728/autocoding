import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.claude import ClaudeSession
from autocoding.core import Core
from autocoding.runtime import SessionRuntime
from autocoding.transport import JsonLineProcess, TransportError


class ClaudeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(Path(self.temp.name) / "core.db")
        self.core.create_session("claude_test", "claude", key="session")

    async def runtime(self, mode="normal", timeout=2, tools_enabled=False, controls=False):
        transport = await JsonLineProcess.start([sys.executable, "-u",
            str(ROOT / "tests/fixtures/claude_agent.py"), mode], cwd=ROOT)
        adapter = ClaudeSession(transport, session_id="session_fixture", timeout=timeout,
                                tools_enabled=tools_enabled, controls=controls)
        runtime = SessionRuntime(self.core, "claude_test", adapter)
        self.addAsyncCleanup(runtime.close)
        return runtime

    def enqueue(self, key="input"):
        return self.core.enqueue("claude_test", "pm", 0, "中文输入", key=key)["command_id"]

    async def finish(self, runtime, command):
        events = []
        while self.core.command(command)["state"] == "acknowledged":
            events.append(await runtime.next_event(timeout=2))
        return events

    async def test_replay_completion_and_second_turn(self):
        runtime = await self.runtime()
        self.core.create_task("task_a", "Requires verifier", ["test"], key="task")
        self.core.claim("task_a", "claude", key="claim")
        turns = []
        for key in ["first", "second"]:
            command = self.enqueue(key)
            self.assertEqual("acknowledged", (await runtime.send(command, "中文输入"))["state"])
            events = await self.finish(runtime, command)
            self.assertEqual("completed", self.core.command(command)["state"])
            self.assertEqual("公开回复：中文输入", events[0]["params"]["delta"])
            self.assertNotIn("PRIVATE_CANARY", json.dumps(events))
            turns.append(events[-1]["params"]["turn"]["id"])
        self.assertNotEqual(*turns)
        self.assertEqual("leased", self.core.snapshot()["tasks"][0]["state"])
        self.assertNotIn("中文输入", json.dumps(self.core.events(), ensure_ascii=False))
        replay = await runtime.send(command, "中文输入")
        self.assertFalse(replay["resent"])

    async def test_early_completion_with_concurrent_reader(self):
        runtime = await self.runtime("early")
        command = self.enqueue()
        reader = asyncio.create_task(runtime.next_event(timeout=2))
        await runtime.send(command, "中文输入")
        await reader
        self.assertEqual("completed", self.core.command(command)["state"])

    async def test_missing_ack_freezes_and_no_retry(self):
        runtime = await self.runtime("silent", timeout=.15)
        command = self.enqueue()
        with self.assertRaisesRegex(TransportError, "ACK_TIMEOUT"):
            await runtime.send(command, "中文输入")
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])
        self.assertEqual('ack_wait', runtime.adapter.last_error_diagnostic['failure_phase'])
        self.assertEqual(0, runtime.adapter.last_error_diagnostic['frames_seen'])
        with self.assertRaisesRegex(TransportError, "RECONCILIATION_REQUIRED"):
            await runtime.send(command, "中文输入")

    async def test_ack_timeout_records_redacted_frame_shape_and_correlation(self):
        runtime = await self.runtime("system-frame-silent", timeout=5)
        command = self.core.enqueue("claude_test", "pm", 0, "ack diagnostic probe", key="system-frame-silent")["command_id"]
        with self.assertRaisesRegex(TransportError, "ACK_TIMEOUT"):
            await runtime.send(command, "ack diagnostic probe")
        diagnostic=runtime.adapter.last_error_diagnostic
        self.assertEqual({'system':1},diagnostic['frame_type_counts'])
        self.assertEqual({'system':1},diagnostic['turn_correlated_frame_counts'])
        self.assertEqual('heartbeat',diagnostic['last_frame']['event_subtype'])
        self.assertNotIn('PRIVATE_CANARY',json.dumps(diagnostic))

    async def test_correlated_system_progress_acks_but_does_not_complete_turn(self):
        for mode in ("system-status-ack", "system-thinking-ack"):
            with self.subTest(mode=mode):
                runtime = await self.runtime(mode)
                command = self.core.enqueue("claude_test", "pm", 0, "system progress probe", key=mode)["command_id"]
                receipt = await runtime.send(command, "system progress probe")
                self.assertEqual("acknowledged", receipt["state"])
                self.assertEqual("acknowledged", self.core.command(command)["state"])
                events = await self.finish(runtime, command)
                self.assertTrue(any(event["method"] == "turn/completed" for event in events))
                self.assertEqual("completed", self.core.command(command)["state"])

    async def test_correlated_assistant_turn_is_an_ack_without_legacy_replay(self):
        runtime = await self.runtime("no-replay")
        command = self.enqueue('no-replay')
        self.assertEqual('acknowledged',(await runtime.send(command,'中文输入'))['state'])
        await self.finish(runtime,command)
        self.assertEqual('completed',self.core.command(command)['state'])

    async def test_top_level_tool_frame_without_input_uuid_acks_unique_active_turn(self):
        runtime = await self.runtime("tool-no-uuid", tools_enabled=True, controls=True)
        command = self.core.enqueue("claude_test", "pm", 0, "tool frame input", key="tool-no-uuid")["command_id"]
        self.assertEqual('acknowledged',(await runtime.send(command,"tool frame input"))['state'])
        events = await self.finish(runtime,command)
        self.assertTrue(any(event['method']=='tool/started' for event in events))
        self.assertEqual('completed',self.core.command(command)['state'])

    async def test_mismatched_replay_freezes(self):
        for mode in ["wrong-ack", "wrong-content"]:
            with self.subTest(mode=mode):
                runtime = await self.runtime(mode)
                # Separate sessions because an unknown delivery freezes its owner.
                sid = "session_" + mode
                self.core.create_session(sid, "claude", key=sid)
                runtime.session_id = sid
                command = self.core.enqueue(sid, "pm", 0, "中文输入", key=mode)["command_id"]
                with self.assertRaisesRegex(TransportError, "ACK_MISMATCH"):
                    await runtime.send(command, "中文输入")
                self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_provider_failure_is_terminal_without_raw_error(self):
        runtime = await self.runtime("error")
        command = self.enqueue()
        await runtime.send(command, "中文输入")
        events = await self.finish(runtime, command)
        self.assertEqual("failed", self.core.command(command)["state"])
        self.assertNotIn("PRIVATE_CANARY", json.dumps(events) + json.dumps(self.core.events()))

    async def test_unsupported_event_logs_shape_without_frame_values(self):
        runtime = await self.runtime('unsupported-event')
        command = self.core.enqueue("claude_test", "pm", 0, "unsupported event probe", key="unsupported-event")["command_id"]
        await runtime.send(command, "unsupported event probe")
        with self.assertRaisesRegex(TransportError, 'CLAUDE_UNSUPPORTED_EVENT'):
            await runtime.next_event(timeout=2)
        diagnostic = runtime.adapter.last_error_diagnostic
        self.assertEqual('rate_limit_event', diagnostic['event_type'])
        self.assertTrue(diagnostic['session_matches'])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(diagnostic))

    async def test_tool_progress_is_forwarded_without_raw_provider_frame(self):
        runtime = await self.runtime('tool-progress', timeout=5)
        command = self.core.enqueue("claude_test", "pm", 0, "tool progress probe", key="tool-progress")["command_id"]
        await runtime.send(command, "tool progress probe")
        events = await self.finish(runtime, command)
        progress = next(event for event in events if event['method']=='tool/progress')
        self.assertEqual('Bash', progress['params']['toolName'])
        self.assertEqual(12.5, progress['params']['elapsedSeconds'])
        self.assertNotIn('protocol-event-id', json.dumps(events))

    async def test_tool_activity_and_result_are_available_to_live_console(self):
        runtime=await self.runtime('tool-output',tools_enabled=True)
        command=self.enqueue()
        await runtime.send(command,'中文输入')
        events=await self.finish(runtime,command)
        started=next(event for event in events if event['method']=='tool/started')
        result=next(event for event in events if event['method']=='tool/result')
        self.assertEqual('Bash',started['params']['toolName'])
        self.assertEqual('Bash',result['params']['activity'])
        self.assertEqual('tests passed',result['params']['detail'])
        self.assertEqual('completed',self.core.command(command)['state'])

    async def test_bad_result_or_tool_never_completes_command(self):
        for mode in ["wrong-session", "wrong-turn", "malformed-result", "unknown-result",
                     "tool", "invalid-type", "invalid-block"]:
            with self.subTest(mode=mode):
                runtime = await self.runtime(mode)
                self.core.create_session(mode, "claude", key=mode)
                runtime.session_id = mode
                command = self.core.enqueue(mode, "pm", 0, "中文输入", key=mode)["command_id"]
                try:
                    await runtime.send(command, "中文输入")
                    await self.finish(runtime, command)
                except TransportError:
                    pass
                self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_approval_is_not_automatically_answered(self):
        runtime = await self.runtime("approval")
        command = self.enqueue()
        with self.assertRaisesRegex(TransportError, "APPROVAL_UNSUPPORTED"):
            await runtime.send(command, "中文输入")
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_exit_after_ack_is_unknown(self):
        runtime = await self.runtime("exit")
        command = self.enqueue()
        await runtime.send(command, "中文输入")
        with self.assertRaises(TransportError):
            await self.finish(runtime, command)
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_active_takeover_keeps_session_frozen(self):
        runtime = await self.runtime("hold")
        command = self.enqueue()
        await runtime.send(command, "中文输入")
        result = await runtime.takeover("human", 0, key="take")
        self.assertEqual("reconciling", result["state"])
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_cancel_during_send_is_unknown(self):
        runtime = await self.runtime("silent")
        command = self.enqueue()
        task = asyncio.create_task(runtime.send(command, "中文输入"))
        await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_unconsumed_output_overflow_freezes(self):
        runtime = await self.runtime("flood")
        command = self.enqueue()
        await runtime.send(command, "中文输入")
        with self.assertRaisesRegex(TransportError, "OUTPUT_BACKPRESSURE"):
            await self.finish(runtime, command)
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])


if __name__ == "__main__":
    unittest.main()
