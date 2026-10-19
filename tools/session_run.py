"""Run one observable, audited input through a subprocess and SessionRuntime.

Default fixture mode exercises real pipes with a simulated provider. Live modes
require --prompt-file. Codex uses isolated credentials unless --existing-account;
Claude uses the configured Zhipu credential and disables all tools.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.codex import CodexSession
from autocoding.claude import ClaudeSession
from autocoding.providers import ProviderError, resolve_zhipu, claude_environment
from autocoding.core import Core, DomainError
from autocoding.runtime import SessionRuntime
from autocoding.transport import JsonLineProcess, JsonRpcClient, TransportError
from probe_agents import executable, environment


def display_text(text):
    # Public model text is untrusted: never execute terminal controls.
    return "".join(c for c in str(text) if c in "\n\t" or not unicodedata.category(c).startswith("C"))


async def run(args):
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="session-", dir=local))
    core = Core(output / "session.db")
    adapter = None
    runtime = None
    report = {"mode": args.agent, "model_turn_sent": False, "task_verified": False}
    exit_code = 2
    try:
        text = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else "验证中文指令与回执。"
        if args.agent == "fixture":
            print("SIMULATED PROVIDER / REAL SUBPROCESS PIPES / NO MODEL CALL")
            transport = await JsonLineProcess.start(
                [sys.executable, "-u", str(ROOT / "tests/fixtures/json_agent.py"), "rpc"], cwd=ROOT)
            adapter = CodexSession(JsonRpcClient(transport))
        elif args.agent == "claude":
            config_path = args.config
            if config_path is None and (local / "credentials/opencode.json").exists():
                config_path = local / "credentials/opencode.json"
            config = resolve_zhipu(config_path, model_override=args.model)
            binary = executable("claude")
            if not binary:
                raise TransportError("ADAPTER_NOT_FOUND")
            env = claude_environment(config, "https://open.bigmodel.cn/api/anthropic")
            config_dir = output / "claude-config"
            config_dir.mkdir()
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
            adapter = await ClaudeSession.connect(binary, cwd=ROOT, env=env,
                                                 model=config.model, timeout=args.timeout)
            report.update(thread_id=adapter.thread_id, model=config.model,
                          tools_disabled=True, protocol="stream-json")
        else:
            binary = executable("codex")
            if not binary:
                raise TransportError("ADAPTER_NOT_FOUND")
            adapter = await CodexSession.connect(binary, cwd=ROOT,
                env=None if args.existing_account else environment("codex"))
        if args.agent != "claude":
            report.update(await adapter.start_thread(ROOT, model=args.model, sandbox="danger-full-access"))
        core.create_session("session_cli", args.agent, owner="human",
                            simulated=args.agent == "fixture", key="session")
        runtime = SessionRuntime(core, "session_cli", adapter)
        command = core.enqueue("session_cli", "human", 0, text, key="input")["command_id"]
        report["command_id"] = command
        print("INPUT:", display_text(text))
        report["model_turn_sent"] = args.agent in {"codex", "claude"}
        receipt = await runtime.send(command, text)
        print("RECEIPT:", json.dumps(receipt, ensure_ascii=False))
        async with asyncio.timeout(args.timeout):
            while core.command(command)["state"] == "acknowledged":
                event = await runtime.next_event()
                method = event.get("method", "")
                params = event.get("params", {})
                if params.get("threadId") not in (None, adapter.thread_id):
                    continue
                if "id" in event:
                    print("OPERATOR ACTION REQUIRED:", display_text(method))
                    report["state"] = "awaiting_operator_no_automatic_approval"
                    break
                if method == "item/agentMessage/delta":
                    print(display_text(event.get("params", {}).get("delta", "")), end="", flush=True)
                elif method == "turn/completed":
                    print("\nTURN:", core.command(command)["state"])
        exit_code = 0 if core.command(command)["state"] == "completed" else 2
    except (TransportError, DomainError, ProviderError, OSError, TimeoutError) as exc:
        report["error"] = getattr(exc, "code", type(exc).__name__)
    finally:
        if runtime:
            await runtime.close()
        elif adapter:
            await adapter.close()
        report["snapshot"] = core.snapshot()
        report["events"] = core.events()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for event in report["events"]:
            print(f'{event["seq"]:03d} {event["type"]} command={event["command_id"] or "-"}')
        if report.get("error"):
            print("ERROR:", report["error"])
        print("AUDIT:", output / "report.json")
        print("Provider completion is not task verification. Input/output text is not saved in this report.")
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=["fixture", "codex", "claude"], default="fixture")
    parser.add_argument("--config", type=Path, help="Claude Zhipu credential configuration")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--existing-account", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.agent in {"codex", "claude"} and not args.prompt_file:
        parser.error("Live agents require --prompt-file")
    if args.existing_account and args.agent != "codex":
        parser.error("--existing-account is only supported for Codex")
    if args.config and args.agent != "claude":
        parser.error("--config is only supported for Claude")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
