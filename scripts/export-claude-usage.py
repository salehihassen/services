#!/usr/bin/env python3
"""Export Claude Code subscription quota percentages for Home Assistant."""

import fcntl
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
POLL_INTERVAL = 600
MAX_BACKOFF = 3 * 60 * 60
REFRESH_TIMEOUT = 45


class RequestFailure(Exception):
    def __init__(self, status, retry_at=0, blocked_token=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_at = retry_at
        self.blocked_token = blocked_token


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def read_window(data, name):
    if not isinstance(data, dict):
        raise ValueError("Claude usage response is not an object")
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


def read_credentials(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("claudeAiOauth"), dict):
        raise ValueError("Invalid Claude credentials")
    credentials = data["claudeAiOauth"]
    token = credentials.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("Claude Code is not logged in")
    expires_at = credentials.get("expiresAt")
    if expires_at is not None:
        expires_at = float(expires_at)
        if not math.isfinite(expires_at):
            raise ValueError("Invalid credential expiry")
    return token, expires_at


def token_fingerprint(token):
    # Persist only a digest so a rejected token can be recognized across runs.
    return hashlib.sha256(token.encode()).hexdigest()


def token_expired(credentials):
    return credentials[1] is not None and credentials[1] <= time.time() * 1000


def retry_after_deadline(headers):
    value = headers.get("Retry-After") if headers else None
    if not value:
        return 0
    try:
        seconds = float(value)
        return time.time() + seconds if math.isfinite(seconds) and seconds >= 0 else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, date.timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0


def get_usage(token):
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.283",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    five_hour_remaining, five_hour_resets_at = read_window(data, "five_hour")
    weekly_remaining, weekly_resets_at = read_window(data, "seven_day")
    return {
        "five_hour_remaining": five_hour_remaining,
        "five_hour_resets_at": five_hour_resets_at,
        "weekly_remaining": weekly_remaining,
        "weekly_resets_at": weekly_resets_at,
        "sampled_at": int(time.time()),
    }


def fetch_usage(credentials_file, credentials):
    for attempt in range(2):
        try:
            return get_usage(credentials[0])
        except urllib.error.HTTPError as exc:
            retry_at = retry_after_deadline(exc.headers)
            if exc.code != 401:
                raise RequestFailure(exc.code, retry_at) from exc
            rejected_token = token_fingerprint(credentials[0])
            try:
                latest = read_credentials(credentials_file)
            except (OSError, KeyError, ValueError, TypeError):
                latest = credentials
            # Claude Code owns renewal. Repeating a rejected token cannot repair it.
            # Even a new token must wait if the server supplied Retry-After.
            if (attempt == 0 and latest[0] != credentials[0]
                    and not token_expired(latest) and retry_at <= time.time()):
                credentials = latest
                continue
            raise RequestFailure(401, retry_at, rejected_token) from exc


def write_json(output, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def load_state(path, output):
    if not path.exists():
        # Preserve the normal polling interval during the first run after upgrade.
        try:
            sampled_at = float(json.loads(output.read_text(encoding="utf-8"))["sampled_at"])
            if not math.isfinite(sampled_at):
                raise ValueError("Invalid sample timestamp")
        except (OSError, KeyError, ValueError, TypeError):
            sampled_at = 0
        return {"next_attempt_at": sampled_at + POLL_INTERVAL}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("Invalid retry state; refusing to reset cooldown")
    deadline = state.get("next_attempt_at")
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline < 0:
        raise ValueError("Invalid retry deadline; refusing to reset cooldown")
    for key in ("rate_limit_failures", "transient_failures", "auth_refresh_failures"):
        count = state.get(key, 0)
        if not isinstance(count, int) or not 0 <= count <= 20:
            raise ValueError("Invalid retry count; refusing to reset cooldown")
    if "blocked_token" in state and not isinstance(state["blocked_token"], str):
        raise ValueError("Invalid blocked credential state")
    return state


def log(message):
    print(f"Claude usage export: {message}", file=sys.stderr)


def next_attempt_text(deadline):
    return datetime.fromtimestamp(deadline, timezone.utc).isoformat()


def backoff_delay(count):
    return min(MAX_BACKOFF, POLL_INTERVAL * 2 ** min(count - 1, 5)
               + random.uniform(0, 60))


def renew_expired_credentials(credentials_file, credentials, state_file, state):
    if not os.environ.get("CLAUDE_USAGE_EXECUTABLE"):
        state.update(blocked_token=token_fingerprint(credentials[0]), status="token_expired")
        write_json(state_file, state)
        log("token expired; renewal not configured; waiting for renewed credentials")
        return None
    # Reserve the failure backoff before starting Claude Code, including if we crash.
    count = min(20, state.get("auth_refresh_failures", 0) + 1)
    state.update(auth_refresh_failures=count, status="auth_refreshing")
    state["next_attempt_at"] = max(state["next_attempt_at"], time.time() + backoff_delay(count))
    write_json(state_file, state)
    detail = "credentials were not renewed"
    try:
        executable = required_path("CLAUDE_USAGE_EXECUTABLE")
        if credentials_file.name != ".credentials.json":
            raise ValueError("Claude Code renewal requires a .credentials.json file")
        environment = os.environ.copy()
        # Use the configured credential store rather than an injected API key/token.
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CCR_OAUTH_TOKEN_FILE"):
            environment.pop(name, None)
        environment.update(CLAUDE_CONFIG_DIR=str(credentials_file.parent),
                           CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
        log("token expired; attempting renewal through Claude Code")
        subprocess.run([str(executable), "auth", "status"], env=environment,
                       cwd=credentials_file.parent, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=REFRESH_TIMEOUT, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Never log subprocess output or credential material.
        detail = type(exc).__name__
    try:
        latest = read_credentials(credentials_file)
        if latest[0] != credentials[0] and not token_expired(latest):
            state.pop("auth_refresh_failures", None)
            state.pop("blocked_token", None)
            # Successful renewal can fetch now, while the next process must wait.
            state["next_attempt_at"] = time.time() + POLL_INTERVAL
            write_json(state_file, state)
            log("credentials renewed; fetching quota")
            return latest
    except (OSError, KeyError, ValueError, TypeError) as exc:
        detail = type(exc).__name__
    state["status"] = "auth_refresh_failed"
    write_json(state_file, state)
    log(f"renewal failed ({detail}); next permitted attempt "
        f"{next_attempt_text(state['next_attempt_at'])}; if login is revoked, use Claude Code /login")
    return None


def record_failure(state_file, state, failure):
    now = time.time()
    if isinstance(failure, RequestFailure) and failure.status == 401:
        state.update(blocked_token=failure.blocked_token, status="authentication_rejected")
        # Require both the existing request spacing and any server cooldown.
        state["next_attempt_at"] = max(state["next_attempt_at"], failure.retry_at)
        detail = "HTTP 401; waiting for renewed credentials"
    else:
        limited = isinstance(failure, RequestFailure) and failure.status == 429
        key = "rate_limit_failures" if limited else "transient_failures"
        count = min(20, state.get(key, 0) + 1)
        state[key] = count
        delay = backoff_delay(count)
        state["next_attempt_at"] = max(now + delay, getattr(failure, "retry_at", 0))
        state["status"] = "rate_limited" if limited else "fetch_failed"
        detail = str(failure) if isinstance(failure, RequestFailure) else type(failure).__name__
    write_json(state_file, state)
    log(f"{detail}; next permitted attempt {next_attempt_text(state['next_attempt_at'])}")


def export_locked(output, credentials_file, state_file):
    state = load_state(state_file, output)
    now = time.time()
    if now < state["next_attempt_at"]:
        # Save an initial deadline too, so restarts cannot bypass the first-run guard.
        if not state_file.exists():
            write_json(state_file, state)
        log(f"cooldown; next permitted attempt {next_attempt_text(state['next_attempt_at'])}")
        return
    try:
        credentials = read_credentials(credentials_file)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        record_failure(state_file, state, exc)
        return
    if token_expired(credentials):
        credentials = renew_expired_credentials(credentials_file, credentials, state_file, state)
        if credentials is None:
            return
    fingerprint = token_fingerprint(credentials[0])
    if state.get("blocked_token") == fingerprint:
        log("waiting for renewed credentials; no network request")
        return
    # Write before the request: a crash or a second process must not fetch immediately.
    state["next_attempt_at"] = now + POLL_INTERVAL
    write_json(state_file, state)
    try:
        usage = fetch_usage(credentials_file, credentials)
        write_json(output, usage)
    except (RequestFailure, OSError, KeyError, ValueError, TypeError,
            urllib.error.URLError) as exc:
        record_failure(state_file, state, exc)
        return
    deadline = time.time() + POLL_INTERVAL
    write_json(state_file, {"next_attempt_at": deadline, "status": "ok"})
    log(f"updated; next permitted attempt {next_attempt_text(deadline)}")


def main():
    output = required_path("CLAUDE_USAGE_OUTPUT")
    credentials_file = required_path("CLAUDE_USAGE_CREDENTIALS_FILE")
    state_file = (required_path("CLAUDE_USAGE_STATE_FILE")
                  if os.environ.get("CLAUDE_USAGE_STATE_FILE")
                  else credentials_file.with_name(".usage-export-state.json"))
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = state_file.with_suffix(state_file.suffix + ".lock")
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another exporter is running; no network request")
            return
        export_locked(output, credentials_file, state_file)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, TypeError, RuntimeError, urllib.error.URLError) as exc:
        print(f"Claude usage export failed: {exc}", file=sys.stderr)
        sys.exit(1)
