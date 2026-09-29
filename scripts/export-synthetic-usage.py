#!/usr/bin/env python3
"""Export Synthetic subscription quota percentages and refill times for Home Assistant."""

import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


URL = "https://api.synthetic.new/v2/quotas"


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Synthetic returned an invalid refill time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Synthetic returned a refill time without a timezone")
    return int(parsed.timestamp())


def money(value):
    try:
        return float(Decimal(str(value).replace("$", "").replace(",", "")))
    except InvalidOperation as exc:
        raise ValueError("Synthetic returned an invalid credit balance") from exc


def get_usage():
    api_key = os.environ.get("SYNTHETIC_API_KEY")
    if not api_key:
        raise ValueError("SYNTHETIC_API_KEY is missing")
    request = urllib.request.Request(URL, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Synthetic quota request returned HTTP {exc.code}") from exc

    five_hour = data["rollingFiveHourLimit"]
    weekly = data["weeklyTokenLimit"]
    remaining_requests = float(five_hour["remaining"])
    request_limit = float(five_hour["max"])
    weekly_percent = float(weekly["percentRemaining"])
    if not all(math.isfinite(x) for x in (remaining_requests, request_limit, weekly_percent)):
        raise ValueError("Synthetic returned nonfinite quota values")
    if request_limit <= 0 or not 0 <= remaining_requests <= request_limit or not 0 <= weekly_percent <= 100:
        raise ValueError("Synthetic returned invalid quota values")

    return {
        "five_hour_remaining": round(100 * remaining_requests / request_limit, 1),
        "five_hour_requests_remaining": round(remaining_requests, 2),
        "five_hour_requests_limit": round(request_limit, 2),
        "five_hour_next_refill_at": timestamp(five_hour["nextTickAt"]),
        "weekly_remaining": round(weekly_percent, 1),
        "weekly_credits_remaining": money(weekly["remainingCredits"]),
        "weekly_credits_limit": money(weekly["maxCredits"]),
        "weekly_next_refill_at": timestamp(weekly["nextRegenAt"]),
        "sampled_at": int(time.time()),
    }


def main():
    output = required_path("SYNTHETIC_USAGE_OUTPUT")
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
        print(f"Synthetic usage export failed: {exc}", file=sys.stderr)
        sys.exit(1)
