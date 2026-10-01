#!/usr/bin/env python3
"""Export Ollama Cloud included-usage percentages for Home Assistant."""

import calendar
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


USAGE_URL = "https://ollama.com/api/usage"
ACCOUNT_URL = "https://ollama.com/api/me"


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def fetch(url, api_key, method="GET"):
    request = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ollama request to {url} returned HTTP {exc.code}") from exc


def next_monthly_reset(signed_up_at, now):
    """Return the first monthly signup anniversary after now, clamping to short months."""
    for offset in range(3):
        index = now.month - 1 + offset
        year, month = now.year + index // 12, index % 12 + 1
        day = min(signed_up_at.day, calendar.monthrange(year, month)[1])
        candidate = signed_up_at.replace(year=year, month=month, day=day)
        if candidate > now:
            return candidate
    raise ValueError("Could not compute the next Ollama reset time")


def get_usage():
    api_key = os.environ.get("OLLAMA_CLOUD_API_KEY")
    if not api_key:
        raise ValueError("OLLAMA_CLOUD_API_KEY is missing")

    monthly = fetch(USAGE_URL, api_key)["limits"]["monthly"]
    # Ollama reports included usage as a 0-1 fraction; the web UI shows it as "% used".
    used = float(monthly["usage"]) * 100
    if not math.isfinite(used) or used < 0:
        raise ValueError("Ollama returned an invalid monthly usage value")
    used = min(used, 100.0)
    requests = sum(int(model["request_count"]) for model in monthly.get("models") or [])

    # /api/me also returns the account email; export only the plan and reset time.
    account = fetch(ACCOUNT_URL, api_key, method="POST")
    plan = account.get("Plan")
    # Ollama's pricing page says Free usage resets monthly from the signup date. The API
    # exposes no reset time, and paid plans reset from the subscription date instead.
    resets_at = None
    if plan == "free":
        signed_up_at = datetime.fromisoformat(account["CreatedAt"].replace("Z", "+00:00"))
        if signed_up_at.tzinfo is None:
            raise ValueError("Ollama returned a signup time without a timezone")
        resets_at = int(next_monthly_reset(signed_up_at, datetime.now(timezone.utc)).timestamp())

    return {
        "monthly_used": round(used, 1),
        "monthly_remaining": round(100 - used, 1),
        "monthly_requests": requests,
        "plan": plan if isinstance(plan, str) else None,
        "monthly_resets_at": resets_at,
        "sampled_at": int(time.time()),
    }


def main():
    output = required_path("OLLAMA_USAGE_OUTPUT")
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
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"Ollama usage export failed: {exc}", file=sys.stderr)
        sys.exit(1)
