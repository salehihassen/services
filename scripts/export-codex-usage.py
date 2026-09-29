#!/usr/bin/env python3
"""Export ChatGPT Codex quota percentages for Home Assistant.

Uses the documented Codex app-server account/rateLimits/read method. Only
percentages and timestamps are written; Codex credentials stay in the user's
normal Codex installation.
"""

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TIMEOUT = 20


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def read_response(process, request_id):
    deadline = time.monotonic() + TIMEOUT
    buffer = b""
    while time.monotonic() < deadline:
        readable, _, _ = select.select(
            [process.stdout], [], [], max(0, deadline - time.monotonic())
        )
        if not readable:
            break
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError("Codex app-server rejected the rate-limit request")
                return message["result"]
    raise TimeoutError("Codex app-server did not respond in time")


def send(process, message):
    process.stdin.write((json.dumps(message) + "\n").encode())
    process.stdin.flush()


def get_limits():
    process = subprocess.Popen(
        [str(required_path("CODEX_USAGE_EXECUTABLE")), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        send(
            process,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "home_assistant_codex_usage",
                        "title": "Home Assistant Codex Usage",
                        "version": "1.0.0",
                    }
                },
            },
        )
        read_response(process, 1)
        send(process, {"method": "initialized", "params": {}})
        send(process, {"method": "account/rateLimits/read", "id": 2})
        result = read_response(process, 2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    buckets = result.get("rateLimitsByLimitId") or {}
    limits = buckets.get("codex") or result.get("rateLimits")
    if not limits or limits.get("limitId") != "codex":
        raise ValueError("Codex rate limits are absent from the account response")

    windows = {}
    for item in (limits.get("primary"), limits.get("secondary")):
        if not item:
            continue
        duration = item.get("windowDurationMins")
        used = float(item["usedPercent"])
        reset = int(item["resetsAt"])
        if not 0 <= used <= 100 or reset <= 0:
            raise ValueError("Codex returned an invalid rate-limit window")
        windows[duration] = (round(100 - used, 1), reset)

    if 300 not in windows or 10080 not in windows:
        raise ValueError("Expected five-hour and weekly Codex windows")

    reset_credits = result.get("rateLimitResetCredits")
    if not isinstance(reset_credits, dict):
        raise ValueError("Codex reset credits are absent from the account response")
    available_count = reset_credits.get("availableCount")
    credit_entries = reset_credits.get("credits")
    if not isinstance(available_count, int) or available_count < 0 or not isinstance(credit_entries, list):
        raise ValueError("Codex returned invalid reset credits")
    now = int(time.time())
    expirations = sorted(
        int(credit["expiresAt"])
        for credit in credit_entries
        if credit.get("status") == "available" and int(credit["expiresAt"]) > now
    )
    if len(expirations) != available_count:
        raise ValueError("Codex reset credit count does not match its expiration list")

    return {
        "five_hour_remaining": windows[300][0],
        "five_hour_resets_at": windows[300][1],
        "weekly_remaining": windows[10080][0],
        "weekly_resets_at": windows[10080][1],
        "banked_resets_available": available_count,
        "reset_credit_expires_at": expirations,
        "sampled_at": now,
    }


def main():
    output = required_path("CODEX_USAGE_OUTPUT")
    data = get_limits()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"Codex usage export failed: {exc}", file=sys.stderr)
        sys.exit(1)
