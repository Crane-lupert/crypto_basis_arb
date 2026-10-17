#!/usr/bin/env python3
"""PreToolUse guard (Edit|Write): fail-secure protection of runtime state, secrets and the broker module.

Blocks (exit 2):
  * any Edit/Write under state/  (written only by the daemon)
  * any Edit/Write to a .env* file (secrets never live in files)
  * any Edit/Write to src/basis_arb/live/broker.py whose new content mentions a mainnet trading host
Unparseable hook input also blocks: a guard that cannot read its input must not permit the action.
"""
from __future__ import annotations

import json
import sys
from pathlib import PurePosixPath

MAINNET_HOSTS = ("api.binance.com", "fapi.binance.com", "dapi.binance.com")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {}) or {}
        path = str(tool_input.get("file_path", "")).replace("\\", "/")
    except Exception as exc:  # noqa: BLE001
        print(f"guard_runtime_paths: cannot parse hook input ({exc}); blocking (fail-secure)", file=sys.stderr)
        return 2
    if tool not in ("Edit", "Write") or not path:
        return 0
    parts = PurePosixPath(path).parts
    name = PurePosixPath(path).name
    if "state" in parts and parts.index("state") >= len(parts) - 3:
        print("guard_runtime_paths: state/ is written only by the daemon", file=sys.stderr)
        return 2
    if name.startswith(".env"):
        print("guard_runtime_paths: secrets never live in files", file=sys.stderr)
        return 2
    if path.endswith("src/basis_arb/live/broker.py"):
        new_text = str(tool_input.get("content", "")) + str(tool_input.get("new_string", ""))
        if any(host in new_text for host in MAINNET_HOSTS):
            print("guard_runtime_paths: mainnet trading host in broker.py is forbidden", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
