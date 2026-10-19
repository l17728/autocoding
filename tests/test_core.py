import concurrent.futures
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from autocoding.core import Core, DomainError


SHA = "a" * 40


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.database = pathlib.Path(self.temp.name) / "core.db"
        self.core = Core(self.database, clock=lambda: self.now)

    def task(self, name="task_one", deps=()):
        return self.core.create_task(name, "Implement a tested change", ["unit", "behavior"], deps, key=name)

    def claim(self, name="task_one", agent="agent:a", key="claim"):
        return self.core.claim(name, agent, key=key)

    def report(self, lease, key="report"):
        return self.core.report(lease["task_id"], "agent:a", lease["lease_token"],
                                lease["attempt"], SHA, ["artifact:test.log"], key=key)

    def checks(self, passed=True):
        return {name: {"passed": passed, "artifact_ref": "artifact:" + name} for name in ["unit", "behavior"]}

    def assertCode(self, code, fn):
        with self.assertRaises(DomainError) as error:
            fn()
        self.assertEqual(code, error.exception.code)

    def test_completion_requires_independent_full_verification(self):
        self.task()
        job = self.report(self.claim())
        self.assertEqual("verifying", self.core.snapshot()["tasks"][0]["state"])
        self.assertCode("INDEPENDENT_VERIFIER_REQUIRED",
                        lambda: self.core.verify(job, "agent:a", self.checks(), key="bad"))
        self.assertCode("INCOMPLETE_VERIFICATION",
                        lambda: self.core.verify(job, "verifier:test", {}, key="incomplete"))
        self.assertEqual("done", self.core.verify(job, "verifier:test", self.checks(), key="verify")["state"])

    def test_failed_check_requeues_for_new_attempt(self):
        self.task()
        job = self.report(self.claim())
        checks = self.checks()
        checks["behavior"]["passed"] = False
        self.assertEqual("todo", self.core.verify(job, "verifier:test", checks, key="failed")["state"])
        second = self.claim(key="second")
        self.assertEqual(2, second["attempt"])
        self.assertCode("STALE_VERIFICATION",
                        lambda: self.core.verify(job, "verifier:test", self.checks(), key="late"))

    def test_dependency_waits_for_verified_done(self):
        self.task()
        self.task("task_next", ["task_one"])
        self.assertCode("DEPENDENCY_NOT_READY", lambda: self.claim("task_next"))
        job = self.report(self.claim())
        self.assertCode("DEPENDENCY_NOT_READY", lambda: self.claim("task_next", key="again"))
        self.core.verify(job, "verifier:test", self.checks(), key="verify")
        self.assertEqual("leased", self.claim("task_next", key="ready")["state"])

    def test_cycle_rejected_atomically(self):
        self.task()
        self.task("task_next", ["task_one"])
        before = self.core.snapshot()["latest_seq"]
        self.assertCode("DEPENDENCY_CYCLE", lambda: self.core.set_dependencies(
            "task_one", ["task_next"], expected_revision=0, key="cycle"))
        self.assertEqual(before, self.core.snapshot()["latest_seq"])
        self.assertEqual("leased", self.claim()["state"])

    def test_unknown_dependency_has_no_partial_task(self):
        self.assertCode("NOT_FOUND", lambda: self.task(deps=["task_missing"]))
        self.assertEqual([], self.core.snapshot()["tasks"])
        self.assertEqual([], self.core.events())

    def test_two_independent_connections_cannot_claim_same_task(self):
        self.task()
        barrier = threading.Barrier(2)
        def claim(agent):
            other = Core(self.database, clock=lambda: self.now)
            barrier.wait()
            try:
                return other.claim("task_one", agent, key="claim")["state"]
            except DomainError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["agent:a", "agent:b"]))
        self.assertCountEqual(["leased", "TASK_NOT_READY"], results)
        self.assertEqual(1, sum(e["type"] == "TaskLeased" for e in self.core.events()))

    def test_successful_claim_replays_after_state_changed(self):
        self.task()
        original = self.claim()
        self.report(original)
        self.now += 100
        before = len(self.core.events())
        self.assertEqual(original, self.claim())
        self.assertEqual(before, len(self.core.events()))

    def test_successful_report_replays_after_verification(self):
        self.task()
        lease = self.claim()
        job = self.report(lease)
        self.core.verify(job, "verifier:test", self.checks(), key="verify")
        self.now += 100
        self.assertEqual(job, self.report(lease))
        self.assertCode("IDEMPOTENCY_CONFLICT", lambda: self.core.report(
            "task_one", "agent:a", lease["lease_token"], 1, "b" * 40, ["other"], key="report"))

    def test_idempotency_key_with_changed_request_rejected(self):
        self.task()
        self.assertCode("IDEMPOTENCY_CONFLICT", lambda: self.core.create_task(
            "task_one", "Changed meaning", ["unit"], key="task_one"))
        self.assertEqual(1, len(self.core.events()))

    def test_lease_expiry_blocks_without_reassignment(self):
        self.task()
        lease = self.claim()
        self.now += 61
        self.assertCode("STALE_LEASE", lambda: self.report(lease))
        self.assertEqual(1, self.core.recover(key="recovery")["tasks_blocked"])
        self.assertCode("TASK_NOT_READY", lambda: self.claim(agent="agent:b", key="new"))
        self.assertEqual("RECONCILIATION_REQUIRED", self.core.snapshot()["tasks"][0]["reason"])

    def test_heartbeat_extends_valid_lease(self):
        self.task()
        lease = self.claim()
        self.now += 50
        self.core.heartbeat("task_one", "agent:a", lease["lease_token"], 1, key="heartbeat")
        self.now += 20
        self.assertEqual("verifying", self.report(lease)["state"])

    def test_cancel_during_verification_rejects_late_result(self):
        self.task()
        job = self.report(self.claim())
        self.core.cancel_task("task_one", expected_revision=job["task_revision"], key="cancel")
        self.assertCode("STALE_VERIFICATION",
                        lambda: self.core.verify(job, "verifier:test", self.checks(), key="late"))
        self.assertEqual("cancelled", self.core.snapshot()["tasks"][0]["state"])

    def test_job_identity_and_acceptance_hash_are_checked(self):
        self.task()
        job = self.report(self.claim())
        for field in ["verification_id", "task_revision", "acceptance_hash", "attempt", "head_sha"]:
            altered = dict(job)
            altered[field] = "wrong"
            self.assertCode("STALE_VERIFICATION",
                            lambda: self.core.verify(altered, "verifier:test", self.checks(), key=field))

    def test_live_lease_cancel_requires_reconciliation(self):
        self.task()
        self.claim()
        self.assertCode("RECONCILIATION_REQUIRED",
                        lambda: self.core.cancel_task("task_one", expected_revision=1, key="cancel"))

    def session(self):
        return self.core.create_session("session_a", "agent:a", key="session")

    def test_takeover_cancels_unsent_and_rejects_old_epoch(self):
        self.session()
        command = self.core.enqueue("session_a", "pm", 0, "继续完成测试", key="cmd")
        result = self.core.takeover("session_a", "human", 0, key="take")
        self.assertEqual(1, result["epoch"])
        self.assertEqual("cancelled", self.core.snapshot()["commands"][0]["state"])
        self.assertCode("STALE_INPUT_EPOCH",
                        lambda: self.core.enqueue("session_a", "pm", 0, "late", key="late"))
        self.assertCode("DISPATCH_NOT_ALLOWED",
                        lambda: self.core.begin_dispatch(command["command_id"], key="late-send"))
        self.assertEqual("queued", self.core.enqueue("session_a", "human", 1, "检查日志", key="manual")["state"])

    def test_inflight_takeover_freezes_new_input(self):
        self.session()
        command = self.core.enqueue("session_a", "pm", 0, "work", key="cmd")
        self.core.begin_dispatch(command["command_id"], key="send")
        state = self.core.takeover("session_a", "human", 0, key="take")
        self.assertEqual("reconciling", state["state"])
        self.assertCode("RECONCILIATION_REQUIRED",
                        lambda: self.core.enqueue("session_a", "human", 1, "new", key="new"))
        self.assertEqual("delivery_unknown", self.core.snapshot()["commands"][0]["state"])

    def test_restart_keeps_uncertain_dispatch_unknown(self):
        self.session()
        command = self.core.enqueue("session_a", "pm", 0, "work", key="cmd")
        self.core.begin_dispatch(command["command_id"], key="send")
        restarted = Core(self.database, clock=lambda: self.now)
        self.assertEqual(1, restarted.recover(key="restart")["commands_unknown"])
        self.assertEqual("delivery_unknown", restarted.snapshot()["commands"][0]["state"])
        self.assertCode("RECONCILIATION_REQUIRED",
                        lambda: restarted.enqueue("session_a", "pm", 0, "again", key="again"))

    def test_command_replay_after_takeover_does_not_resend(self):
        self.session()
        first = self.core.enqueue("session_a", "pm", 0, "work", key="cmd")
        self.core.takeover("session_a", "human", 0, key="take")
        self.assertEqual(first, self.core.enqueue("session_a", "pm", 0, "work", key="cmd"))
        self.assertEqual(1, len(self.core.snapshot()["commands"]))
        self.assertEqual("cancelled", self.core.snapshot()["commands"][0]["state"])

    def test_only_one_dispatch_intent_per_session(self):
        self.session()
        a = self.core.enqueue("session_a", "pm", 0, "a", key="a")
        b = self.core.enqueue("session_a", "pm", 0, "b", key="b")
        self.core.begin_dispatch(a["command_id"], key="a")
        self.assertCode("SESSION_INPUT_BUSY", lambda: self.core.begin_dispatch(b["command_id"], key="b"))

    def test_unicode_and_shell_text_are_data_and_not_logged(self):
        self.session()
        message = "中文 😀\n$(Write-Output should_not_run)\nCANARY_PRIVATE_INPUT_987"
        self.core.enqueue("session_a", "pm", 0, message, key="utf8")
        serialized = json.dumps(self.core.events(), ensure_ascii=False) + json.dumps(self.core.snapshot())
        self.assertNotIn("CANARY_PRIVATE_INPUT_987", serialized)
        with self.core.connection() as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("CANARY_PRIVATE_INPUT_987", dump)

    def test_utf8_limit_and_terminal_controls(self):
        self.session()
        self.assertCode("INPUT_TOO_LARGE",
                        lambda: self.core.enqueue("session_a", "pm", 0, "中" * 22000, key="large"))
        self.assertCode("CONTROL_SEQUENCE_REJECTED",
                        lambda: self.core.enqueue("session_a", "pm", 0, "\x1b]52;clipboard", key="escape"))
        self.assertEqual([], self.core.snapshot()["commands"])

    def test_snapshot_cursor_joins_subsequent_events(self):
        self.task()
        snapshot = self.core.snapshot()
        self.claim()
        following = self.core.events(snapshot["latest_seq"])
        self.assertTrue(all(e["seq"] > snapshot["latest_seq"] for e in following))
        self.assertEqual(["TaskLeased"], [e["type"] for e in following])

    def test_state_and_audit_commit_together_under_failure(self):
        self.task()
        with self.core.connection() as conn:
            conn.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events "
                         "WHEN NEW.type='TaskLeased' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.claim()
        self.assertEqual("todo", self.core.snapshot()["tasks"][0]["state"])
        self.assertEqual(["TaskCreated"], [e["type"] for e in self.core.events()])
        with self.core.connection() as conn:
            conn.execute("DROP TRIGGER fail_event")
        self.assertEqual("leased", self.claim()["state"])

    def test_done_task_cannot_be_replanned_without_invalidation_support(self):
        self.task()
        job = self.report(self.claim())
        self.core.verify(job, "verifier:test", self.checks(), key="verify")
        revision = self.core.snapshot()["tasks"][0]["revision"]
        self.assertCode("TASK_PLAN_IMMUTABLE",
                        lambda: self.core.set_dependencies("task_one", [], expected_revision=revision, key="edit"))
        self.assertCode("STATE_CONFLICT",
                        lambda: self.core.cancel_task("task_one", expected_revision=revision, key="cancel"))


if __name__ == "__main__":
    unittest.main()


