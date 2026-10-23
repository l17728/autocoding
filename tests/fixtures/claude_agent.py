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
    if mode == "unsupported-event":
        send({**message, "isReplay": True})
        send({"type": "rate_limit_event", "session_id": sid,
              "api_key": "PRIVATE_CANARY", "message": {"content": "PRIVATE_CANARY"}})
        continue
    result = {"type": "result", "session_id": sid, "user_message_uuid": mid,
              "user_message_uuids": [mid], "subtype": "success", "is_error": False}
    if mode == "tool-progress":
        send({**message, "isReplay": True})
        send({"type":"tool_progress","session_id":sid,"tool_name":"Bash",
              "tool_use_id":"tool-progress-1","elapsed_time_seconds":12.5,
              "heartbeat":True,"uuid":"protocol-event-id"})
        send({"type":"assistant","session_id":sid,"user_message_uuid":mid,
              "user_message_uuids":[mid],"parent_tool_use_id":None,
              "message":{"role":"assistant","content":[{"type":"text","text":"Progress handled."}]}})
        send(result)
        continue
    if mode == "approval":
        send({"type": "control_request", "request_id": "approve", "request": {"subtype": "can_use_tool"}})
        continue
    if mode == "tool-no-uuid":
        send({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
              "message": {"role": "assistant", "content": [
                  {"type": "tool_use", "id": "tool-no-uuid", "name": "Bash", "input": {"command": "true"}}]}})
        send({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
              "message": {"role": "assistant", "content": [{"type": "text", "text": "tool is running"}]}})
        send(result)
        continue
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
    elif mode == "tool-output":
        blocks = [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pytest"}}]
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
    if mode == "tool-output":
        send({"type":"user","session_id":sid,"message":{"role":"user","content":[
            {"type":"tool_result","tool_use_id":"tool-1","content":[{"type":"text","text":"tests passed"}],"is_error":False}]}})
        send({"type":"assistant","session_id":sid,"parent_tool_use_id":None,
              "user_message_uuid":mid,"user_message_uuids":[mid],
              "message":{"role":"assistant","content":[{"type":"text","text":"Tests passed."}]}})
    send(result)
