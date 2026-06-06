#!/usr/bin/env python3
"""
install_hooks.py — Configure Claude Code hooks for claude-code-buddy.

Adds hook entries to ~/.claude/settings.json that invoke buddy_hook.py
on session lifecycle events.

Usage:
    python3 install_hooks.py [--uninstall] [--project]
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOOK_SCRIPT = Path(__file__).parent / "buddy_hook.py"
SETTINGS_GLOBAL = Path.home() / ".claude" / "settings.json"

# Hook events and their configurations
HOOK_EVENTS = {
    "SessionStart":      {"matcher": "*", "timeout": 5},
    "UserPromptSubmit":  {"matcher": "*", "timeout": 5},
    "PreToolUse":        {"matcher": "*", "timeout": 30},
    "PostToolUse":       {"matcher": "*", "timeout": 5},
    "Stop":              {"matcher": "*", "timeout": 5},
    "Notification":      {"matcher": "*", "timeout": 5},
}


def load_settings(path: Path) -> dict[str, Any]:
    """Load settings.json, return empty dict if not found."""
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to load {path}: {e}", file=sys.stderr)
            return {}
    return {}


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    """Save settings.json with pretty formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")


def build_hook_command() -> str:
    """Build the command string for the hook."""
    hook_path = HOOK_SCRIPT.resolve()
    return f"python3 {hook_path}"


def install(settings_path: Path) -> None:
    """Install hooks into settings.json."""
    settings = load_settings(settings_path)
    hook_cmd = build_hook_command()

    if "hooks" not in settings:
        settings["hooks"] = {}

    installed = 0
    for event, cfg in HOOK_EVENTS.items():
        hook_entry = {
            "matcher": cfg["matcher"],
            "hooks": [
                {
                    "type": "command",
                    "command": hook_cmd,
                    "timeout": cfg["timeout"],
                }
            ],
        }

        if event not in settings["hooks"]:
            settings["hooks"][event] = []

        # Check if our hook is already installed
        already_installed = False
        for existing in settings["hooks"][event]:
            existing_hooks = existing.get("hooks", [])
            for h in existing_hooks:
                if h.get("command") == hook_cmd:
                    already_installed = True
                    break

        if not already_installed:
            settings["hooks"][event].append(hook_entry)
            installed += 1
            print(f"  ✓ {event} (timeout: {cfg['timeout']}s)")
        else:
            print(f"  · {event} (already installed)")

    save_settings(settings_path, settings)
    print(f"\nInstalled {installed} new hooks to {settings_path}")


def uninstall(settings_path: Path) -> None:
    """Remove buddy hooks from settings.json."""
    settings = load_settings(settings_path)
    hook_cmd = build_hook_command()

    if "hooks" not in settings:
        print("No hooks section found in settings.")
        return

    removed = 0
    for event in list(settings["hooks"].keys()):
        original_len = len(settings["hooks"][event])
        settings["hooks"][event] = [
            entry for entry in settings["hooks"][event]
            if not any(
                h.get("command") == hook_cmd
                for h in entry.get("hooks", [])
            )
        ]
        removed += original_len - len(settings["hooks"][event])
        # Clean up empty event lists
        if not settings["hooks"][event]:
            del settings["hooks"][event]

    if not settings["hooks"]:
        del settings["hooks"]

    save_settings(settings_path, settings)
    print(f"Removed {removed} hooks from {settings_path}")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Configure Claude Code hooks for buddy")
    parser.add_argument("--uninstall", action="store_true", help="Remove buddy hooks")
    parser.add_argument("--project", action="store_true", help="Install to project .claude/settings.json instead of global")
    args = parser.parse_args()

    settings_path = SETTINGS_GLOBAL
    if args.project:
        settings_path = Path.cwd() / ".claude" / "settings.json"

    # Verify hook script exists
    if not HOOK_SCRIPT.exists():
        print(f"Error: Hook script not found at {HOOK_SCRIPT}", file=sys.stderr)
        return 1

    if args.uninstall:
        print("Uninstalling buddy hooks...")
        uninstall(settings_path)
    else:
        print(f"Installing buddy hooks to {settings_path}...")
        print(f"  Hook script: {HOOK_SCRIPT.resolve()}")
        print()
        install(settings_path)
        print()
        print("Next steps:")
        print("  1. Install bleak: pip install bleak")
        print("  2. Start daemon: ./buddyctl.sh start")
        print("  3. Flash ESP32 with the Arduino IDE sketch")
        print("  4. Restart Claude Code")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
