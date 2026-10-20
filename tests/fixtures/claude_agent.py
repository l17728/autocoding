"""Claude stream-json contract substitute; no network or credentials."""
import json
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
mode = sys.argv[1]

def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    sid, mid = message["session_id"], message["uuid"]
    if mode == "silent":
        continue
    send({"type": "system", "subtype": "init", "session_id": sid, "private": "PRIVATE_CANARY"})
    if mode == "approval":
        send({"type": "control_request", "request_id": "approve", "request": {"subtype": "can_use_tool"}})
        continue
    result = {"type": "result", "session_id": sid, "user_message_uuid": mid,
              "user_message_uuids": [mid], "subtype": "success", "is_error": False}
    if mode == "early":
        send(result)
        time.sleep(.05)
    if mode != "no-replay":
        replay = {**message, "isReplay": True}
        if mode == "wrong-ack":
            replay["uuid"] = "other"
        if mode == "wrong-content":
            replay["message"] = {"role": "user", "content": "other"}
        send(replay)
    if mode == "invalid-type":
        send({"type": [], "session_id": sid})
        continue
    if mode == "exit":
        break
    if mode in {"hold", "early", "wrong-ack", "wrong-content"}:
        continue
    if mode == "error":
        result.update(subtype="error_during_execution", is_error=True,
                      errors=["PRIVATE_CANARY key=secret"])
        send({"type": "assistant", "session_id": sid, "user_message_uuid": mid,
              "user_message_uuids": [mid], "error": "authentication_failed",
              "message": {"role": "assistant", "content": [
                  {"type": "text", "text": "PRIVATE_CANARY key=secret"}]}})
    if mode == "wrong-session":
        result["session_id"] = "other"
    if mode == "wrong-turn":
        result["user_message_uuid"] = "other"
    if mode == "malformed-result":
        result["is_error"] = "false"
    if mode == "unknown-result":
        result["subtype"] = "future_status"
    if mode == "tool":
        blocks = [{"type": "tool_use", "name": "Bash", "input": {}}]
    else:
        blocks = [{"type": "thinking", "thinking": "PRIVATE_CANARY"},
                  {"type": "text", "text": "公开回复：" + message["message"]["content"]}]
    if mode == "flood":
        blocks = [{"type": "text", "text": "output"}] * 140
    if mode == "invalid-block":
        blocks = [{"type": []}]
    if mode != "error":
        send({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
              "user_message_uuid": mid, "user_message_uuids": [mid],
              "message": {"role": "assistant", "content": blocks}})
    send(result)
