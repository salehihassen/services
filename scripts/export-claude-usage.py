#!/usr/bin/env python3
"""Export Claude Code subscription quota percentages for Home Assistant."""

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def read_window(data, name):
    window = data.get(name)
    if not isinstance(window, dict):
        raise ValueError(f"Claude usage response has no {name} window")
    used = float(window["utilization"])
    if not math.isfinite(used) or not 0 <= used <= 100:
        raise ValueError(f"Claude usage response has invalid {name} utilization")
    resets_at = window.get("resets_at")
    if resets_at is None and used == 0:
        # An unused window has no running timer; its next reset is unknown.
        reset = 0
    elif isinstance(resets_at, str):
        reset = int(datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp())
        if reset <= 0:
            raise ValueError(f"Claude usage response has invalid {name} reset time")
    else:
        raise ValueError(f"Claude usage response has invalid {name} reset time")
    return round(100 - used, 1), reset


def get_usage():
    credentials_file = required_path("CLAUDE_USAGE_CREDENTIALS_FILE")

    def request_usage():
        credentials = json.loads(credentials_file.read_text(encoding="utf-8"))["claudeAiOauth"]
        token = credentials.get("accessToken")
        if not token:
            raise ValueError("Claude Code is not logged in")
        request = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.283",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    try:
        data = request_usage()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Let Claude Code refresh its own credentials if it can, then retry once.
            subprocess.run(
                [str(required_path("CLAUDE_USAGE_EXECUTABLE")), "auth", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            try:
                data = request_usage()
            except urllib.error.HTTPError as retry_exc:
                raise RuntimeError(f"Claude usage request returned HTTP {retry_exc.code}") from retry_exc
        else:
            raise RuntimeError(f"Claude usage request returned HTTP {exc.code}") from exc

    five_hour_remaining, five_hour_resets_at = read_window(data, "five_hour")
    weekly_remaining, weekly_resets_at = read_window(data, "seven_day")
    return {
        "five_hour_remaining": five_hour_remaining,
        "five_hour_resets_at": five_hour_resets_at,
        "weekly_remaining": weekly_remaining,
        "weekly_resets_at": weekly_resets_at,
        "sampled_at": int(time.time()),
    }


def main():
    output = required_path("CLAUDE_USAGE_OUTPUT")
    usage = get_usage()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(usage, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired, urllib.error.URLError) as exc:
        print(f"Claude usage export failed: {exc}", file=sys.stderr)
        sys.exit(1)
