import asyncio
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.codex import CodexSession
from autocoding.core import Core, DomainError
from autocoding.runtime import SessionRuntime
from autocoding.transport import JsonLineProcess, JsonRpcClient, TransportError

FIXTURE = str(ROOT / "tests/fixtures/json_agent.py")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_process_exit_before_and_after_ack_never_replays(self):
        for mode in ['exit-before-ack','exit-after-ack']:
            with self.subTest(mode=mode):
                sid='disconnect_'+mode
                self.core.create_session(sid,'fixture',key=sid)
                transport=await JsonLineProcess.start([sys.executable,'-u',FIXTURE,mode],cwd=ROOT)
                rpc=JsonRpcClient(transport,request_timeout=5)
                adapter=CodexSession(rpc)
                await adapter.start_thread(ROOT)
                runner=SessionRuntime(self.core,sid,adapter)
                try:
                    cid=self.core.enqueue(sid,'pm',0,'single input',key=sid)['command_id']
                    try:
                        await runner.send(cid,'single input')
                        for _ in range(10):await runner.next_event(timeout=5)
                        self.fail('Expected terminated process')
                    except TransportError:
                        pass
                    self.assertEqual('delivery_unknown',self.core.command(cid)['state'])
                    with self.assertRaises((TransportError,DomainError)):
                        await runner.send(cid,'single input')
                    dispatched=[e for e in self.core.events() if e['type']=='CommandDispatching' and e['command_id']==cid]
                    self.assertEqual(1,len(dispatched))
                finally:
                    await runner.close()
                    await rpc.close()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(pathlib.Path(self.temp.name) / "runtime.db")
        self.core.create_session("session_a", "fixture", key="session")

    async def runtime(self, mode="rpc", timeout=2):
        transport = await JsonLineProcess.start([sys.executable, "-u", FIXTURE, mode], cwd=ROOT)
        rpc = JsonRpcClient(transport, request_timeout=3)
        self.addAsyncCleanup(rpc.close)
        adapter = CodexSession(rpc)
        await adapter.start_thread(ROOT)
        rpc.request_timeout = timeout
        runner = SessionRuntime(self.core, "session_a", adapter)
        self.addAsyncCleanup(runner.close)
        return runner

    def enqueue(self, text="中文指令", key="message"):
        return self.core.enqueue("session_a", "pm", 0, text, key=key)["command_id"]

    async def finish(self, runner, command):
        for _ in range(8):
            await runner.next_event(timeout=2)
            if self.core.command(command)["state"] in {"completed", "failed", "cancelled"}:
                return
        self.fail("Expected correlated completion")

    async def test_pipe_ack_and_completion_persist_but_do_not_complete_task(self):
        runner = await self.runtime()
        self.core.create_task("task_a", "Needs actual verification", ["test"], key="task")
        self.core.claim("task_a", "fixture", key="claim")
        command = self.enqueue()
        ack = await runner.send(command, "中文指令")
        self.assertEqual("acknowledged", ack["state"])
        await self.finish(runner, command)
        self.assertEqual("completed", self.core.command(command)["state"])
        self.assertEqual("leased", self.core.snapshot()["tasks"][0]["state"])
        kinds = [e["type"] for e in self.core.events() if e["command_id"] == command]
        self.assertEqual(["CommandQueued", "CommandDispatching", "CommandAcknowledged", "CommandCompleted"], kinds)

    async def test_retry_of_completed_command_does_not_resend(self):
        runner = await self.runtime()
        command = self.enqueue()
        await runner.send(command, "中文指令")
        await self.finish(runner, command)
        before = self.core.snapshot()["latest_seq"]
        replay = await runner.send(command, "中文指令")
        self.assertFalse(replay["resent"])
        self.assertEqual(before, self.core.snapshot()["latest_seq"])

    async def test_wrong_input_hash_rejected_before_dispatch(self):
        runner = await self.runtime()
        command = self.enqueue()
        with self.assertRaisesRegex(DomainError, "^INPUT_HASH_MISMATCH$"):
            await runner.send(command, "different")
        self.assertEqual("queued", self.core.command(command)["state"])

    async def test_wrong_session_rejected_before_dispatch(self):
        runner = await self.runtime()
        self.core.create_session("session_b", "fixture", key="b")
        command = self.core.enqueue("session_b", "pm", 0, "text", key="b")["command_id"]
        with self.assertRaisesRegex(DomainError, "^WRONG_SESSION$"):
            await runner.send(command, "text")
        self.assertEqual("queued", self.core.command(command)["state"])

    async def test_timeout_freezes_core_and_does_not_retry(self):
        runner = await self.runtime("silent-turn", timeout=.2)
        command = self.enqueue()
        with self.assertRaisesRegex(TransportError, "^RPC_TIMEOUT_DELIVERY_UNKNOWN$"):
            await runner.send(command, "中文指令")
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])
        self.assertEqual("reconciling", self.core.snapshot()["sessions"][0]["state"])
        with self.assertRaisesRegex(TransportError, "^RECONCILIATION_REQUIRED$"):
            await runner.send(command, "中文指令")

    async def test_takeover_cancels_queued_and_allows_human_queue(self):
        runner = await self.runtime()
        command = self.enqueue()
        result = await runner.takeover("human", 0, key="take")
        self.assertEqual("ready", result["state"])
        self.assertEqual("cancelled", self.core.command(command)["state"])
        human = self.core.enqueue("session_a", "human", 1, "人工输入", key="human")["command_id"]
        await runner.send(human, "人工输入")
        await self.finish(runner, human)

    async def test_takeover_active_turn_remains_frozen_after_root_close(self):
        runner = await self.runtime("hold-turn")
        command = self.enqueue()
        await runner.send(command, "中文指令")
        result = await runner.takeover("human", 0, key="take")
        self.assertEqual("reconciling", result["state"])
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])
        self.assertIsNotNone(runner.adapter.rpc.transport.process.returncode)

    async def test_completion_before_ack_with_concurrent_event_reader(self):
        runner = await self.runtime("early-complete")
        command = self.enqueue()
        reader = asyncio.create_task(self.finish(runner, command))
        await runner.send(command, "中文指令")
        await asyncio.wait_for(reader, 3)
        self.assertEqual("completed", self.core.command(command)["state"])

    async def test_cancellation_during_request_records_unknown(self):
        runner = await self.runtime("silent-turn", timeout=10)
        command = self.enqueue()
        task = asyncio.create_task(runner.send(command, "中文指令"))
        await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_process_exit_after_ack_never_invents_completion(self):
        runner = await self.runtime("hold-turn")
        command = self.enqueue()
        await runner.send(command, "中文指令")
        await runner.adapter.close()
        with self.assertRaises(TransportError):
            for _ in range(5):
                await runner.next_event(timeout=1)
        self.assertEqual("delivery_unknown", self.core.command(command)["state"])

    async def test_close_is_idempotent_after_workflow_seals_runtime(self):
        class Adapter:
            def __init__(self):
                self.closes = 0

            async def close(self):
                self.closes += 1
                if self.closes > 1:
                    raise RuntimeError('adapter closed twice')

        adapter = Adapter()
        runner = SessionRuntime(self.core, "session_a", adapter)
        # This is the state deliberately established by Workflow before it
        # snapshots and verifies a completed worktree.
        runner.closed = True
        runner._quarantined = True
        await runner.close()
        self.assertEqual(0, adapter.closes)


if __name__ == "__main__":
    unittest.main()


