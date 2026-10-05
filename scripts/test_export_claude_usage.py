"""Exercise the exporter against mocked HTTP responses and a real state directory."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "claude_usage", Path(__file__).with_name("export-claude-usage.py")
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

NOW = 1_790_880_000
PAYLOAD = {
    "five_hour": {"utilization": 25, "resets_at": "2026-10-01T22:00:00Z"},
    "seven_day": {"utilization": 10, "resets_at": "2026-10-05T06:00:00Z"},
}


def http_error(code, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(exporter.USAGE_URL, code, "mock failure", headers, None)


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.credentials = root / "credentials.json"
        self.output = root / "usage.json"
        self.state = root / "private" / "state.json"
        self.write_credentials("first-token")
        self.output.write_text(json.dumps({"sampled_at": NOW - 3600, "weekly_remaining": 90}))
        self.original_output = self.output.read_bytes()
        self.env = patch.dict(os.environ, {
            "CLAUDE_USAGE_CREDENTIALS_FILE": str(self.credentials),
            "CLAUDE_USAGE_OUTPUT": str(self.output),
            "CLAUDE_USAGE_STATE_FILE": str(self.state),
            "CLAUDE_USAGE_EXECUTABLE": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch.object(exporter.time, "time", return_value=NOW).start()
        self.addCleanup(patch.stopall)
        patch("random.uniform", return_value=0).start()
        self.http = patch.object(exporter.urllib.request, "urlopen").start()
        self.cli = patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)).start()
        self.stderr = io.StringIO()
        patch.object(exporter.sys, "stderr", self.stderr).start()

    def write_credentials(self, token, expires_at=None):
        self.credentials.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token,
            "expiresAt": expires_at if expires_at is not None else (NOW + 86400) * 1000,
        }}))

    def read_state(self):
        return json.loads(self.state.read_text())

    def success(self):
        return io.StringIO(json.dumps(PAYLOAD))

    def test_success_uses_one_request_and_prevents_early_repeat(self):
        self.http.return_value = self.success()
        exporter.main()
        self.assertEqual(json.loads(self.output.read_text())["five_hour_remaining"], 75)
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 600)
        exporter.main()
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_recent_existing_sample_prevents_request_on_first_run(self):
        self.output.write_text(json.dumps({"sampled_at": NOW - 60}))
        exporter.main()
        self.http.assert_not_called()
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 540)

    def test_expired_token_never_reaches_network(self):
        self.write_credentials("expired-token", (NOW - 1) * 1000)
        exporter.main()
        self.http.assert_not_called()
        self.assertEqual(self.output.read_bytes(), self.original_output)
        self.assertNotIn("expired-token", self.state.read_text())
        self.write_credentials("renewed-token")
        self.http.return_value = self.success()
        exporter.main()
        self.assertEqual(self.http.call_count, 1)

    def enable_refresh(self):
        self.credentials = self.credentials.with_name(".credentials.json")
        self.write_credentials("expired-token", (NOW - 1) * 1000)
        os.environ["CLAUDE_USAGE_CREDENTIALS_FILE"] = str(self.credentials)
        os.environ["CLAUDE_USAGE_EXECUTABLE"] = "/test/bin/claude"

    def test_expired_token_refreshes_once_then_fetches_with_new_token(self):
        self.enable_refresh()
        self.cli.side_effect = lambda *args, **kwargs: self.write_credentials("renewed-token")
        self.http.return_value = self.success()
        exporter.main()
        self.cli.assert_called_once()
        call = self.cli.call_args
        self.assertEqual(call.args[0], ["/test/bin/claude", "auth", "status"])
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertLessEqual(call.kwargs["timeout"], 45)
        self.assertEqual(call.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(self.credentials.parent))
        self.assertEqual(self.http.call_args.args[0].get_header("Authorization"), "Bearer renewed-token")
        self.assertEqual(self.read_state()["status"], "ok")
        exporter.main()
        self.cli.assert_called_once()

    def test_failed_refresh_backs_off_across_runs_and_preserves_sample(self):
        self.enable_refresh()
        for index, delay in enumerate((600, 1200, 2400, 4800, 9600, 10800, 10800), 1):
            now = self.clock.return_value
            exporter.main()
            state = self.read_state()
            self.assertEqual(state["status"], "auth_refresh_failed")
            self.assertEqual(state["next_attempt_at"], now + delay)
            self.assertEqual(self.cli.call_count, index)
            exporter.main()
            self.assertEqual(self.cli.call_count, index)
            self.clock.return_value = state["next_attempt_at"]
        self.http.assert_not_called()
        self.assertEqual(self.output.read_bytes(), self.original_output)

    def test_refresh_timeout_or_missing_binary_is_handled(self):
        self.enable_refresh()
        for error in (subprocess.TimeoutExpired("claude", 45), FileNotFoundError()):
            with self.subTest(error=type(error).__name__):
                self.state.unlink(missing_ok=True)
                self.cli.side_effect = error
                exporter.main()
                self.assertEqual(self.read_state()["status"], "auth_refresh_failed")
                self.assertEqual(self.read_state()["next_attempt_at"], NOW + 600)
        self.http.assert_not_called()

    def test_nonzero_cli_exit_without_renewal_is_backed_off(self):
        self.enable_refresh()
        self.cli.return_value = subprocess.CompletedProcess([], 1)
        exporter.main()
        self.assertEqual(self.read_state()["status"], "auth_refresh_failed")
        self.assertEqual(self.output.read_bytes(), self.original_output)
        self.http.assert_not_called()

    def test_refresh_that_persists_still_expired_token_does_not_fetch(self):
        self.enable_refresh()
        self.cli.side_effect = lambda *args, **kwargs: self.write_credentials("new-but-expired", (NOW - 1) * 1000)
        exporter.main()
        self.assertEqual(self.read_state()["status"], "auth_refresh_failed")
        self.http.assert_not_called()

    def test_successful_renewal_followed_by_429_preserves_server_wait(self):
        self.enable_refresh()
        self.cli.side_effect = lambda *args, **kwargs: self.write_credentials("renewed-token")
        self.http.side_effect = http_error(429, "18000")
        exporter.main()
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 18000)
        self.clock.return_value = NOW + 12000
        self.write_credentials("expired-again", (NOW - 1) * 1000)
        exporter.main()
        self.cli.assert_called_once()
        self.http.assert_called_once()

    def test_corrupt_refresh_count_fails_without_starting_cli(self):
        self.enable_refresh()
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"next_attempt_at": NOW, "auth_refresh_failures": -1}))
        with self.assertRaises(ValueError):
            exporter.main()
        self.cli.assert_not_called()
        self.http.assert_not_called()

    def test_refresh_cannot_bypass_server_cooldown_even_when_token_expires(self):
        self.enable_refresh()
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"next_attempt_at": NOW + 18000, "status": "rate_limited"}))
        exporter.main()
        self.cli.assert_not_called()
        self.http.assert_not_called()
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 18000)

    def test_refresh_reserves_backoff_before_starting_cli(self):
        self.enable_refresh()
        def crash(*args, **kwargs):
            self.assertEqual(self.read_state()["next_attempt_at"], NOW + 600)
            self.assertEqual(self.read_state()["auth_refresh_failures"], 1)
            raise RuntimeError("simulated exporter crash")
        self.cli.side_effect = crash
        with self.assertRaises(RuntimeError):
            exporter.main()
        exporter.main()
        self.cli.assert_called_once()
        self.http.assert_not_called()

    def test_expired_previously_blocked_token_can_refresh(self):
        self.enable_refresh()
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"next_attempt_at": NOW, "blocked_token": exporter.token_fingerprint("expired-token"), "status": "token_expired"}))
        self.cli.side_effect = lambda *args, **kwargs: self.write_credentials("renewed-token")
        self.http.return_value = self.success()
        exporter.main()
        self.cli.assert_called_once()
        self.assertEqual(self.read_state()["status"], "ok")

    def test_valid_or_rejected_unexpired_token_does_not_start_cli(self):
        os.environ["CLAUDE_USAGE_EXECUTABLE"] = "/test/bin/claude"
        self.http.side_effect = http_error(401)
        exporter.main()
        self.clock.return_value = NOW + 600
        exporter.main()
        self.cli.assert_not_called()
        self.assertEqual(self.http.call_count, 1)

    def test_401_blocks_same_token_across_runs_and_recovers_on_change(self):
        self.http.side_effect = http_error(401)
        exporter.main()
        self.clock.return_value = NOW + 86400
        exporter.main()
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.output.read_bytes(), self.original_output)
        self.assertNotIn("first-token", self.state.read_text())
        self.write_credentials("renewed-token", (NOW + 172800) * 1000)
        self.http.side_effect = None
        self.http.return_value = self.success()
        exporter.main()
        self.assertEqual(self.http.call_count, 2)
        self.assertNotIn("blocked_token", self.read_state())

    def test_401_retries_once_when_token_changes_during_request(self):
        def first_request(*args, **kwargs):
            self.write_credentials("new-token")
            raise http_error(401)
        def request(*args, **kwargs):
            if self.http.call_count == 1:
                return first_request(*args, **kwargs)
            return self.success()
        self.http.side_effect = request
        exporter.main()
        self.assertEqual(self.http.call_count, 2)
        tokens = [call.args[0].get_header("Authorization") for call in self.http.call_args_list]
        self.assertEqual(tokens, ["Bearer first-token", "Bearer new-token"])

    def test_second_401_blocks_the_new_token(self):
        def request(*args, **kwargs):
            self.write_credentials("new-token")
            raise http_error(401)
        self.http.side_effect = request
        exporter.main()
        self.clock.return_value = NOW + 600
        exporter.main()
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.read_state()["blocked_token"], exporter.token_fingerprint("new-token"))

    def test_changed_but_expired_token_does_not_trigger_retry(self):
        def request(*args, **kwargs):
            self.write_credentials("expired-token", (NOW - 1) * 1000)
            raise http_error(401)
        self.http.side_effect = request
        exporter.main()
        self.assertEqual(self.http.call_count, 1)

    def test_rate_limit_doubles_and_caps_at_three_hours(self):
        self.http.side_effect = http_error(429)
        for delay in (600, 1200, 2400, 4800, 9600, 10800, 10800):
            now = self.clock.return_value
            exporter.main()
            deadline = self.read_state()["next_attempt_at"]
            self.assertEqual(deadline - now, delay)
            exporter.main()
            self.clock.return_value = deadline
        self.assertEqual(self.http.call_count, 7)
        self.assertEqual(self.output.read_bytes(), self.original_output)

    def test_jitter_never_shortens_backoff_or_exceeds_cap(self):
        self.http.side_effect = http_error(429)
        with patch.object(exporter.random, "uniform", return_value=45):
            exporter.main()
            self.assertEqual(self.read_state()["next_attempt_at"], NOW + 645)
            state = self.read_state()
            state.update(rate_limit_failures=20, next_attempt_at=NOW)
            self.state.write_text(json.dumps(state))
            exporter.main()
            self.assertEqual(self.read_state()["next_attempt_at"], NOW + 10800)

    def test_retry_after_longer_than_cap_survives_restart_and_token_change(self):
        self.http.side_effect = http_error(429, "18000")
        exporter.main()
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 18000)
        self.clock.return_value = NOW + 12000
        self.write_credentials("new-token")
        exporter.main()
        self.assertEqual(self.http.call_count, 1)

    def test_retry_after_date_is_honored(self):
        header = format_datetime(datetime.fromtimestamp(NOW + 20000, timezone.utc), usegmt=True)
        self.http.side_effect = http_error(429, header)
        exporter.main()
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 20000)

    def test_short_zero_and_invalid_retry_after_keep_local_backoff(self):
        for header in ("0", "30", "invalid", "NaN", "Infinity", "-1"):
            with self.subTest(header=header):
                self.state.unlink(missing_ok=True)
                self.http.side_effect = http_error(429, header)
                exporter.main()
                self.assertEqual(self.read_state()["next_attempt_at"], NOW + 600)

    def test_401_retry_after_prevents_immediate_retry_with_new_token(self):
        def request(*args, **kwargs):
            self.write_credentials("new-token")
            raise http_error(401, "18000")
        self.http.side_effect = request
        exporter.main()
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 18000)
        self.clock.return_value = NOW + 12000
        exporter.main()
        self.assertEqual(self.http.call_count, 1)

    def test_429_after_auth_retry_uses_rate_limit_backoff(self):
        def request(*args, **kwargs):
            if self.http.call_count == 1:
                self.write_credentials("new-token")
                raise http_error(401)
            raise http_error(429, "18000")
        self.http.side_effect = request
        exporter.main()
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.read_state()["rate_limit_failures"], 1)
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 18000)

    def test_server_errors_and_timeouts_have_persistent_backoff(self):
        for error in (http_error(503), urllib.error.URLError("mock timeout"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.state.unlink(missing_ok=True)
                self.http.side_effect = error
                self.clock.return_value = NOW
                exporter.main()
                self.clock.return_value = NOW + 600
                exporter.main()
                self.assertEqual(self.read_state()["next_attempt_at"], NOW + 1800)

    def test_success_resets_failure_counts(self):
        self.http.side_effect = http_error(429)
        exporter.main()
        self.clock.return_value = NOW + 600
        self.http.side_effect = None
        self.http.return_value = self.success()
        exporter.main()
        self.assertEqual(self.read_state().get("rate_limit_failures", 0), 0)
        self.assertEqual(self.read_state().get("transient_failures", 0), 0)

    def test_invalid_response_keeps_last_good_sample(self):
        for payload in ('{"five_hour": {"utilization": 999}}', '[]', 'invalid json'):
            with self.subTest(payload=payload):
                self.state.unlink(missing_ok=True)
                self.http.return_value = io.StringIO(payload)
                exporter.main()
                self.assertEqual(self.output.read_bytes(), self.original_output)
                self.assertEqual(self.read_state()["next_attempt_at"], NOW + 600)

    def test_missing_credentials_make_no_network_request(self):
        self.credentials.unlink()
        exporter.main()
        self.http.assert_not_called()
        self.assertEqual(self.read_state()["next_attempt_at"], NOW + 600)

    def test_failure_logs_category_and_deadline_without_credentials(self):
        self.http.side_effect = http_error(429, "18000")
        exporter.main()
        messages = self.stderr.getvalue()
        self.assertIn("HTTP 429", messages)
        self.assertIn("next permitted attempt", messages)
        self.assertNotIn("first-token", messages)

    def test_corrupt_state_fails_closed_without_network_request(self):
        self.state.parent.mkdir()
        for value in ('broken json', '[]', '{}', '{"next_attempt_at": "broken"}'):
            with self.subTest(state=value):
                self.state.write_text(value)
                with self.assertRaises(ValueError):
                    exporter.main()
        self.http.assert_not_called()

    def test_overlapping_run_makes_no_network_request(self):
        import fcntl
        self.state.parent.mkdir()
        lock = self.state.with_suffix(self.state.suffix + ".lock")
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            exporter.main()
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
