"""Run a labelled offline orchestration demo. No model calls or subprocesses."""
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.core import Core

local = ROOT / ".local"
local.mkdir(exist_ok=True)
output = pathlib.Path(tempfile.mkdtemp(prefix="demo-", dir=local))
core = Core(output / "demo.db")
core.create_task("task_build", "Simulated implementation", ["unit"], key="create-build")
core.create_task("task_review", "Simulated review", ["review"], ["task_build"], key="create-review")
core.create_session("session_codex", "codex:demo", key="codex")
core.create_session("session_opencode", "opencode:demo", key="opencode")
core.create_session("session_claude", "claude:demo", key="claude")
core.enqueue("session_codex", "pm", 0, "完成实现并提交测试证据。", key="input")
lease = core.claim("task_build", "codex:demo", key="claim")
job = core.report("task_build", "codex:demo", lease["lease_token"], lease["attempt"],
                  "a" * 40, ["simulated:test-result"], key="report")
core.verify(job, "verifier:demo", {"unit": {"passed": True, "artifact_ref": "simulated:unit"}}, key="verify")
core.claim("task_review", "claude:demo", key="claim-review")
core.takeover("session_opencode", "human", 0, key="takeover")
core.enqueue("session_opencode", "human", 1, "检查当前日志，不修改代码。", key="manual")
report = {"mode": "OFFLINE_SIMULATION_NO_MODEL_CALLS", "snapshot": core.snapshot(), "events": core.events()}
(output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("OFFLINE SIMULATION: no real agents, commits, or test execution.")
for event in report["events"]:
    print(f'{event["seq"]:02d} {event["type"]} task={event["task_id"] or "-"} session={event["session_id"] or "-"}')
print("Report:", output / "report.json")

