"""`istota-stack logs`'s redaction (#968): `lib/url_redact.redact_web_addresses`
and `maintenance/log_redact`, which runs on the VM's host with the host's
python3 and the checkout's `src/` on its path.

The script-level half (the verb, its window, the order of its sources and its
exit codes) is `tests/test_istota_stack.py::TestLogs`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from istota.lib.url_redact import redact_web_addresses
from istota.maintenance import log_redact

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

#: How `host/istota-stack` runs the filter; held equal to the script by
#: `test_the_script_runs_the_filter_this_way`.
HOST_INVOCATION = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from istota.maintenance.log_redact import main; sys.exit(main(sys.argv[2:]))"
)


def _filter(stdin: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", HOST_INVOCATION, str(SRC), *args],
        input=stdin.encode(), capture_output=True, timeout=60,
    )


class TestWebAddresses:
    def test_userinfo_is_replaced(self):
        assert redact_web_addresses("GET https://bot:hunter2@cloud.example.com/x done") == (
            "GET https://[redacted]@cloud.example.com/x done"
        )

    def test_the_query_and_fragment_go(self):
        line = "open https://istota.example.com/istota/setup#ABCDE-FGHIJ and https://h.example.com/set-password?token=s3cret&x=1"
        assert redact_web_addresses(line) == (
            "open https://istota.example.com/istota/setup#[redacted] and https://h.example.com/set-password?[redacted]"
        )

    def test_an_address_with_neither_is_left_alone(self):
        line = "fetched https://example.com/feed.xml (200), see https://example.com/."
        assert redact_web_addresses(line) == line

    def test_a_password_that_ended_the_netloc_early_takes_the_whole_address(self):
        # `pa?ss` ends the netloc at the `?`; dropping only the query would
        # print `user:pa`.
        out = redact_web_addresses("x https://user:pa?ss@host.example.com/ y")
        assert out == "x https://[redacted] y"
        assert "pa" not in out.replace("[redacted]", "")

    def test_a_request_target_without_a_scheme_loses_its_query(self):
        line = '10.0.0.1 - "GET /istota/api/auth/enrol?token=abc123 HTTP/1.1" 200'
        assert redact_web_addresses(line) == '10.0.0.1 - "GET /istota/api/auth/enrol?[redacted] HTTP/1.1" 200'

    def test_a_plain_path_is_left_alone(self):
        line = "wrote /data/config/config.toml and /srv/app/istota/secrets"
        assert redact_web_addresses(line) == line

    @pytest.mark.parametrize("prefix", ["foo_", "1", "a" * 40])
    def test_an_address_glued_to_the_word_before_it_still_matches(self, prefix):
        out = redact_web_addresses(f"{prefix}https://user:pass@host.example.com/x?t=1")
        assert "user:pass" not in out and "t=1" not in out
        assert out.endswith("[redacted]@host.example.com/x?[redacted]")

    @pytest.mark.parametrize("unit", ["=/", ":/", "a-", "a."])
    def test_a_long_word_costs_linear_time(self, unit):
        import time
        line = unit * 100_000
        started = time.monotonic()
        redact_web_addresses(line)
        assert time.monotonic() - started < 1.0

    def test_other_schemes_lose_userinfo_too(self):
        assert redact_web_addresses("redis://:pw@redis:6379/0") == "redis://[redacted]@redis:6379/0"


@pytest.fixture
def secrets(tmp_path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "istota_nextcloud_app_password").write_text("nc-app-password-1234\n")
    (directory / "istota_email_smtp_password").write_text("")
    (directory / "istota_provider_openrouter_extra_headers").write_text(
        json.dumps({"Authorization": "Bearer or-header-token-5678", "X-Title": "1"})
    )
    (directory / "claude_code_oauth_token").write_text("line-one-of-a-token\nline-two-of-a-token\n")
    return directory


class TestSecretValues:
    def test_every_value_and_each_line_and_json_leaf_of_one(self, secrets):
        values = log_redact.secret_values(str(secrets), [])
        assert "nc-app-password-1234" in values
        assert "Bearer or-header-token-5678" in values
        assert "line-one-of-a-token" in values and "line-two-of-a-token" in values
        # A short piece of a value is not redacted on its own; the whole value is.
        assert "1" not in values
        assert "" not in values

    def test_a_missing_directory_or_extra_file_holds_nothing(self, tmp_path):
        assert log_redact.secret_values(str(tmp_path / "none"), [str(tmp_path / "key")]) == []

    def test_an_env_file_gives_its_credential_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            'ISTOTA_TAG=v1\nISTOTA_SECRETS_DIR=/srv/app/istota/secrets\n'
            'ISTOTA_TALK_SIGNALING_SECRET="quoted-signaling-secret"\nRCLONE_RC_PASS=plain-rc-password\n'
        )
        values = log_redact.secret_values(str(tmp_path / "none"), [], [str(env)])
        assert "quoted-signaling-secret" in values and "plain-rc-password" in values
        assert "v1" not in values and "/srv/app/istota/secrets" not in values

    def test_an_extra_file_is_read(self, tmp_path, secrets):
        key = tmp_path / ".secret_key"
        key.write_text("master-key-abcdefgh")
        assert "master-key-abcdefgh" in log_redact.secret_values(str(secrets), [str(key)])

    def test_a_link_in_the_directory_is_refused(self, tmp_path, secrets):
        target = tmp_path / "elsewhere"
        target.write_text("linked-secret-value")
        (secrets / "istota_caldav_password").symlink_to(target)
        with pytest.raises(log_redact.Unreadable):
            log_redact.secret_values(str(secrets), [])

    def test_a_fifo_is_refused_without_blocking(self, secrets):
        os.mkfifo(secrets / "istota_caldav_password")
        with pytest.raises(log_redact.Unreadable):
            log_redact.secret_values(str(secrets), [])

    @pytest.mark.requires_dac
    def test_an_unreadable_file_is_refused(self, secrets):
        path = secrets / "istota_caldav_password"
        path.write_text("caldav-password-9999")
        path.chmod(0)
        with pytest.raises(log_redact.Unreadable):
            log_redact.secret_values(str(secrets), [])


class TestTheFilter:
    def test_no_secret_reaches_stdout(self, secrets):
        stdin = (
            "daemon-1 | nextcloud login with nc-app-password-1234 failed\n"
            "web-1 | header Bearer or-header-token-5678 sent\n"
            "daemon-1 | token line-two-of-a-token\n"
            "web-1 | GET https://bot:nc-app-password-1234@cloud.example.com/remote.php?x=1\n"
            "case of 12, version 1\n"
        )
        result = _filter(stdin, str(secrets), "--file", str(secrets.parent / "absent"))
        assert result.returncode == 0, result.stderr
        out = result.stdout.decode()
        for value in ("nc-app-password-1234", "or-header-token-5678", "line-two-of-a-token", "x=1"):
            assert value not in out
        assert out.splitlines() == [
            "daemon-1 | nextcloud login with [redacted] failed",
            "web-1 | header [redacted] sent",
            "daemon-1 | token [redacted]",
            "web-1 | GET https://[redacted]@cloud.example.com/remote.php?[redacted]",
            "case of 12, version 1",
        ]

    def test_a_longer_value_holding_a_shorter_one_goes_whole(self, tmp_path):
        directory = tmp_path / "secrets"
        directory.mkdir()
        (directory / "a").write_text("abcdefgh")
        (directory / "b").write_text("abcdefgh-and-more")
        result = _filter("x abcdefgh-and-more y\n", str(directory))
        assert result.stdout.decode() == "x [redacted] y\n"

    def test_a_secret_file_it_cannot_read_prints_nothing(self, tmp_path, secrets):
        target = tmp_path / "elsewhere"
        target.write_text("linked-secret-value")
        (secrets / "istota_caldav_password").symlink_to(target)
        result = _filter("linked-secret-value nc-app-password-1234\n", str(secrets))
        assert result.returncode == log_redact.EXIT_UNREADABLE == 78
        assert result.stdout == b""
        assert b"istota_caldav_password" in result.stderr
        assert b"linked-secret-value" not in result.stderr

    @pytest.mark.parametrize("args", [[], ["dir", "--file"], ["dir", "--other", "x"]])
    def test_malformed_arguments_print_nothing(self, args):
        result = _filter("secret\n", *args)
        assert result.returncode == 64
        assert result.stdout == b""

    def test_bytes_that_are_not_utf_8_pass_through(self, secrets):
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", HOST_INVOCATION, str(SRC), str(secrets)],
            input=b"\xff\xfe nc-app-password-1234\n", capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"\xff\xfe [redacted]\n"

    def test_it_imports_only_the_standard_library_and_url_redact(self):
        """The host has no istota installed: anything else this imports is a
        `logs` that fails on the VM and passes here."""
        probe = (
            HOST_INVOCATION.replace("sys.exit(main(sys.argv[2:]))", "")
            + "\nimport json; print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'istota')))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe, str(SRC)], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [
            "istota", "istota.lib", "istota.lib.url_redact", "istota.maintenance", "istota.maintenance.log_redact",
        ]

    def test_the_script_runs_the_filter_this_way(self):
        script = (REPO / "host" / "istota-stack").read_text()
        assert f"LOGS_REDACT_PY='{HOST_INVOCATION}'" in script
