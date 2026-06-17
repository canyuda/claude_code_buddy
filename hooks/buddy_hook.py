#!/usr/bin/env python3
"""
buddy_hook.py — Claude Code hook script for claude-code-buddy.

Communicates with the buddyd.py BLE daemon via Unix domain socket.
Reads hook input from stdin (JSON), sends commands to the daemon,
and outputs hook response to stdout.

This script has ZERO third-party dependencies — it only uses Python stdlib.

Usage (by Claude Code hooks):
    The hook input JSON is read from stdin.
    Hook output JSON is written to stdout.

Manual test:
    echo '{"hook_event_name":"SessionStart"}' | python3 buddy_hook.py
    echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' | python3 buddy_hook.py
"""

import json
import os
import socket
import sys
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOCK_PATH = os.environ.get(
    "BUDDY_SOCK",
    os.path.expanduser("~/.claude/buddy.sock"),
)
SOCKET_TIMEOUT = 3.0       # seconds to wait for socket connect / state response
PERMISSION_TIMEOUT = 60.0  # seconds to wait for button press

# ---------------------------------------------------------------------------
# Logging (optional, only if BUDDY_HOOK_LOG is set)
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    log_path = os.environ.get("BUDDY_HOOK_LOG", "")
    if not log_path:
        return
    try:
        log_path = os.path.expanduser(log_path)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Socket communication
# ---------------------------------------------------------------------------

def socket_send(cmd: dict[str, Any], timeout: float = SOCKET_TIMEOUT) -> Optional[dict]:
    """Send a JSON command to buddyd via Unix socket, return response or None."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCK_PATH)
        sock.sendall(json.dumps(cmd).encode("utf-8"))

        # Read response (for state: quick ack; for prompt: waits for button)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass

        sock.close()

        if chunks:
            raw = b"".join(chunks).decode("utf-8").strip()
            if raw:
                return json.loads(raw)
        return None
    except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
        log(f"Socket error: {e}")
        return None
    except json.JSONDecodeError as e:
        log(f"JSON decode error: {e}")
        return None


# ---------------------------------------------------------------------------
# Hook input parsing
# ---------------------------------------------------------------------------

def read_hook_input() -> dict[str, Any]:
    """Read and parse JSON from stdin (provided by Claude Code)."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"Failed to read hook input: {e}")
        return {}


# ---------------------------------------------------------------------------
# State mapping — convert hook events to buddy state JSON
# ---------------------------------------------------------------------------

def build_state_update(data: dict[str, Any]) -> dict[str, Any]:
    """Build a state update JSON for the ESP32 based on hook event."""
    event = data.get("hook_event_name", "")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    notification_type = data.get("notification_type", "")

    state: dict[str, Any] = {
        "total": 1,
        "running": 0,
        "waiting": 0,
        "msg": "",
    }

    if event == "SessionStart":
        state["running"] = 1
        state["msg"] = "session started"
        # Also request time sync on session start
        state["time"] = [int(time.time()), _tz_offset()]

    elif event == "SessionEnd":
        state["msg"] = "session ended"

    elif event == "UserPromptSubmit":
        state["running"] = 1
        input_text = data.get("user_input", "")
        if input_text:
            state["msg"] = input_text[:24]
        else:
            state["msg"] = "thinking..."

    elif event == "PreToolUse":
        state["running"] = 1
        state["waiting"] = 1
        state["msg"] = f"tool: {tool_name}"[:24]

    elif event == "PostToolUse":
        state["running"] = 1
        state["waiting"] = 0
        state["msg"] = f"done: {tool_name}"[:24]

    elif event == "PostToolUseFailure":
        state["running"] = 1
        state["msg"] = f"error: {tool_name}"[:24]

    elif event == "Stop":
        state["msg"] = "idle"

    elif event == "StopFailure":
        state["msg"] = "error"

    elif event == "Notification":
        if notification_type == "permission_prompt":
            state["waiting"] = 1
            state["msg"] = "waiting for approval"
        elif notification_type == "elicitation_dialog":
            state["waiting"] = 1
            state["msg"] = "waiting for input"
        else:
            return {}  # Don't update for idle_prompt etc.

    elif event == "PermissionRequest":
        state["waiting"] = 1
        state["msg"] = "permission needed"

    elif event == "PermissionDenied":
        state["msg"] = "denied"

    else:
        return {}  # Unknown event, don't update

    return state


def _tz_offset() -> int:
    """Return local timezone offset in seconds."""
    if time.daylight and time.localtime().tm_isdst:
        return -time.altzone
    return -time.timezone


def build_prompt_id(data: dict[str, Any]) -> str:
    """Generate a unique prompt ID from hook data."""
    session = data.get("session_id", "s")[:8]
    tool = data.get("tool_name", "t")[:8]
    ts = int(time.time() * 1000) % 1000000
    return f"buddy_{session}_{tool}_{ts}"


# ---------------------------------------------------------------------------
# Hook output
# ---------------------------------------------------------------------------

def output_noop() -> None:
    """Output empty JSON — no-op, don't block Claude Code."""
    print("{}")


def output_permission_decision(decision: str) -> None:
    """Output a permission decision for Claude Code."""
    if decision in ("once", "always"):
        decision_value = "allow"
    elif decision == "deny":
        decision_value = "deny"
    else:
        decision_value = "ask"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision_value}}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    data = read_hook_input()
    event = data.get("hook_event_name", "")

    log(f"Hook event: {event}")

    # Check if daemon is running (quick socket check)
    if not os.path.exists(SOCK_PATH):
        log("Daemon not running (socket not found)")
        output_noop()
        return 0

    # --- Informational events: send state update, return immediately ---
    if event == "PreToolUse":
        # Special handling: send permission prompt and wait for button
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        # Only intercept tools that need permission (not read-only/harmless tools)
        # Read-only tools (Read, Glob, Grep) and Task tools (TodoWrite family:
        # TaskCreate/TaskUpdate/TaskList/TaskGet are pure in-memory todo state)
        # never need physical approval on the device.
        readonly_tools = {
            "Read", "Glob", "Grep",
            "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
        }
        if tool_name in readonly_tools:
            # Just update state, don't prompt for approval
            state = build_state_update(data)
            if state:
                socket_send({"action": "state", "data": state})
            output_noop()
            return 0

        prompt_id = build_prompt_id(data)
        hint = ""
        if tool_name == "Bash":
            hint = tool_input.get("command", "")[:44]
        elif tool_name == "Write":
            hint = tool_input.get("file_path", "")[:44]
        elif tool_name == "Edit":
            hint = tool_input.get("file_path", "")[:44]
        else:
            hint = json.dumps(tool_input, separators=(",", ":"))[:44]

        log(f"Sending prompt: id={prompt_id} tool={tool_name} hint={hint}")

        resp = socket_send(
            {"action": "prompt", "id": prompt_id, "tool": tool_name, "hint": hint,
             "timeout": PERMISSION_TIMEOUT},
            timeout=PERMISSION_TIMEOUT,
        )

        if resp and "decision" in resp:
            decision = resp["decision"]
            log(f"Got decision: {decision}")
            output_permission_decision(decision)
        else:
            log("No decision received, falling back to ask")
            output_permission_decision("ask")

        return 0

    else:
        # Informational event: update state, return immediately
        state = build_state_update(data)
        if state:
            socket_send({"action": "state", "data": state})
        output_noop()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
