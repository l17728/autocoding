"""Probe a real Codex session; model input is sent only with --prompt-file."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.codex import CodexSession
from autocoding.transport import TransportError
from probe_agents import executable, environment


async def run(args):
    binary = executable("codex")
    if not binary:
        raise TransportError("ADAPTER_NOT_FOUND")
    env = environment("codex") if not args.existing_account else None
    session = await CodexSession.connect(binary, cwd=ROOT, env=env)
    report = {"adapter": "codex", "initialized": session.initialized, "model_turn_sent": False}
    try:
        # Isolated local state by default. This does not clone existing credentials.
        thread = await session.start_thread(ROOT, model=args.model)
        report.update(thread)
        if args.prompt_file:
            text = args.prompt_file.read_text(encoding="utf-8-sig")
            report["model_turn_sent"] = True
            report["receipt"] = await session.send_text(text, command_id="cmd_" + uuid.uuid4().hex)
            try:
                async with asyncio.timeout(args.timeout):
                    while session.turn_id:
                        event = await session.next_event()
                        method = event.get("method", "")
                        # Display only explicitly selected public event fields.
                        if "id" in event:
                            report["awaiting_request_id"] = event["id"]
                            report["state"] = "awaiting_operator"
                            print(json.dumps({"type": "operator_request", "method": method,
                                              "request_id": event["id"]}, ensure_ascii=False))
                            break
                        if method == "item/agentMessage/delta":
                            print(event.get("params", {}).get("delta", ""), end="", flush=True)
                        if method == "turn/completed":
                            report["turn_status"] = event.get("params", {}).get("turn", {}).get("status")
            except TimeoutError:
                report["state"] = "timeout_requires_reconciliation"
        else:
            report["state"] = "local_session_created_no_model_call"
        return report
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--existing-account", action="store_true",
                        help="Use existing Codex configuration instead of isolated probe state")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
        code = 0
    except (TransportError, OSError) as exc:
        report = {"adapter": "codex", "state": "blocked",
                  "code": exc.code if isinstance(exc, TransportError) else type(exc).__name__}
        code = 2
    output = ROOT / ".local/codex-session-probe.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

