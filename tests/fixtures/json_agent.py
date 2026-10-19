"""Local protocol fixture: never calls a model, network, shell or external files."""
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")
mode = sys.argv[1] if len(sys.argv) > 1 else "echo"


def send(message):
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    if mode == "split":
        for offset in range(0, len(data), 2):
            sys.stdout.buffer.write(data[offset:offset+2])
            sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


if mode == "malformed":
    sys.stdout.buffer.write(b'{"bad":\xff}\n')
    sys.stdout.buffer.flush()
    time.sleep(5)
    raise SystemExit
if mode == "oversize":
    sys.stdout.write('{"text":"' + "x" * 10000 + '"}\n')
    sys.stdout.flush()
    time.sleep(5)
    raise SystemExit
if mode == "flood":
    for i in range(100):
        send({"method": "notice", "params": {"i": i}})
    time.sleep(5)
    raise SystemExit
if mode == "no-read":
    send({"ready": True})
    time.sleep(20)
    raise SystemExit
if mode == "early-exit":
    raise SystemExit
if mode == "stderr":
    sys.stderr.buffer.write(b"x" * (256 * 1024))
    sys.stderr.buffer.flush()
if mode == "reverse":
    first = json.loads(sys.stdin.readline())
    second = json.loads(sys.stdin.readline())
    send({"method": "progress", "params": {"step": 1}})
    send({"id": second["id"], "result": second["params"]})
    send({"id": first["id"], "result": first["params"]})

for line in sys.stdin:
    message = json.loads(line)
    if mode == "silent":
        continue
    if mode in {"echo", "split", "stderr"}:
        send(message)
        continue
    if mode == "reverse":
        continue
    method = message.get("method")
    if method == "test/burst":
        for i in range(100):
            send({"method": "notice", "params": {"i": i}})
        send({"id": message["id"], "result": {"done": True}})
    elif method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fixture"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        send({"method": "thread/started", "params": {"thread": {"id": "thread_fixture"}}})
        send({"id": message["id"], "result": {"thread": {"id": "thread_fixture"}}})
    elif method == "turn/start":
        if mode == 'exit-before-ack':
            raise SystemExit
        if mode == "silent-turn":
            continue
        if mode == "early-complete":
            send({"method": "turn/completed",
                  "params": {"threadId": "thread_fixture", "turn": {"id": "turn_fixture", "status": "completed"}}})
        text = message["params"]["input"][0]["text"]
        send({"id": message["id"], "result": {"turn": {"id": "turn_fixture", "status": "inProgress"}}})
        send({"method": "item/agentMessage/delta",
              "params": {"threadId": "thread_fixture", "turnId": "turn_fixture", "delta": text}})
        if mode == 'exit-after-ack':
            raise SystemExit
        if mode != "hold-turn":
            send({"method": "turn/completed",
                  "params": {"threadId": "other_thread", "turn": {"id": "turn_fixture", "status": "completed"}}})
            send({"method": "turn/completed",
                  "params": {"threadId": "thread_fixture", "turn": {"id": "turn_fixture", "status": "completed"}}})
    elif method == "turn/interrupt":
        send({"id": message["id"], "result": {}})
    elif method == "test/finish":
        send({"id": message["id"], "result": {}})
        send({"method": "turn/completed",
              "params": {"threadId": "thread_fixture", "turn": {"id": "turn_fixture", "status": "interrupted"}}})
    elif method == "test/reverse-request":
        send({"id": "approval_1", "method": "item/commandExecution/requestApproval",
              "params": {"threadId": "thread_fixture", "command": "example"}})
        send({"id": message["id"], "result": {}})
    elif method == "test/error":
        send({"id": message["id"], "error": {"code": -32000, "message": "PRIVATE_CANARY_ERROR"}})
    elif "id" in message and "method" in message:
        send({"id": message["id"], "result": message.get("params", {})})

