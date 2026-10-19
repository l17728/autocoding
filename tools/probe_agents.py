"""Read-only version/capability probes; optional Codex initialize without a model turn."""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def executable(name):
    native = shutil.which(name + ".exe") or shutil.which(name)
    if native and pathlib.Path(native).suffix.lower() not in {".cmd", ".ps1", ".bat"}:
        return native
    packages = {
        "opencode": "opencode-ai/bin/opencode.exe",
        "claude": "@anthropic-ai/claude-code/bin/claude.exe",
    }
    if name in packages:
        npm = pathlib.Path(os.environ.get("APPDATA", "")) / "npm/node_modules" / packages[name]
        if npm.is_file():
            return str(npm)
    return None


def environment(name):
    env = os.environ.copy()
    base = ROOT / ".local/probes" / name
    base.mkdir(parents=True, exist_ok=True)
    if name == "codex":
        env["CODEX_HOME"] = str(base.resolve())
    if name == "opencode":
        for variable, directory in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                    ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")]:
            env[variable] = str((base / directory).resolve())
    return env


def version_probe(name):
    binary = executable(name)
    if not binary:
        return {"agent": name, "status": "missing"}
    env = environment(name)
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=env, timeout=20)
        if result.returncode:
            return {"agent": name, "status": "probe_failed", "exit_code": result.returncode}
        help_result = subprocess.run([binary, "--help"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", env=env, timeout=20)
        help_text = help_result.stdout + help_result.stderr
        patterns = {
            "codex": {"app-server": r"(?m)^\s+app-server\s"},
            "opencode": {"serve": r"opencode serve\s", "attach": r"opencode attach\s", "acp": r"opencode acp\s"},
            "claude": {"stream-json": r"stream-json", "--replay-user-messages": r"--replay-user-messages", "--resume": r"--resume"},
        }
        capabilities = [label for label, pattern in patterns[name].items() if re.search(pattern, help_text)]
        status = "local_help_verified" if help_result.returncode == 0 and capabilities else "version_verified_help_unavailable"
        return {"agent": name, "status": status, "version": result.stdout.strip()[:100],
                "executable": binary, "capabilities_from_help": capabilities, "live_model_verified": False}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"agent": name, "status": "probe_failed", "error_type": type(exc).__name__}


def codex_initialize():
    binary = executable("codex")
    if not binary:
        return {"probe": "codex_initialize", "ok": False, "reason": "missing"}
    process = subprocess.Popen([binary, "app-server", "--listen", "stdio://"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", env=environment("codex"))
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
    def drain_stderr():
        for _ in process.stderr:
            pass  # Do not persist potentially sensitive environment/config diagnostics.
    readers = [threading.Thread(target=read, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        process.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "autocoding_probe", "version": "0.1.0"}}}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                message = messages.get(timeout=.5)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if message.get("id") == 1:
                return {"probe": "codex_initialize", "ok": "result" in message,
                        "result_keys": list(message.get("result", {})), "model_turn_sent": False}
        return {"probe": "codex_initialize", "ok": False, "reason": "timeout_or_exit"}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for reader in readers:
            reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-handshake", action="store_true")
    args = parser.parse_args()
    report = {"agents": [version_probe(name) for name in ["codex", "opencode", "claude"]]}
    if args.codex_handshake:
        report["codex_handshake"] = codex_initialize()
    target = ROOT / ".local/agent-probes.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


