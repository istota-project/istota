"""Tests for the ntfy push notification skill CLI."""

import base64
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from istota.skills.ntfy import build_parser, cmd_send, main


def _ok_response():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    return resp


def _err_response(status: int, body: str = "boom"):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "err", request=MagicMock(), response=resp,
    ))
    return resp


class TestNtfySend:
    def test_send_minimal_uses_defaults(self, monkeypatch, capsys):
        monkeypatch.setenv("NTFY_TOPIC", "alerts")
        monkeypatch.delenv("NTFY_SERVER_URL", raising=False)
        monkeypatch.delenv("NTFY_TOKEN", raising=False)
        monkeypatch.delenv("NTFY_USERNAME", raising=False)
        monkeypatch.delenv("NTFY_PASSWORD", raising=False)

        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            rc = cmd_send(args)

        assert rc == 0
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        assert post.call_args.args == ("https://ntfy.sh/alerts",)
        assert kwargs["content"] == "hi"
        assert "Authorization" not in kwargs["headers"]

        out = json.loads(capsys.readouterr().out)
        # Success envelope is intentionally minimal — topic/server omitted so
        # they don't end up in conversation history / BM25 recall.
        assert out == {"status": "ok"}

    def test_send_uses_custom_server_and_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "alerts")
        monkeypatch.setenv("NTFY_SERVER_URL", "https://ntfy.example.com/")

        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            cmd_send(args)
        assert post.call_args.args == ("https://ntfy.example.com/alerts",)

    def test_send_with_token_uses_bearer_auth(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        monkeypatch.setenv("NTFY_TOKEN", "tk_123")
        # Token must take precedence over user/pass.
        monkeypatch.setenv("NTFY_USERNAME", "ignored")
        monkeypatch.setenv("NTFY_PASSWORD", "ignored")

        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            cmd_send(args)
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tk_123"

    def test_send_with_basic_auth(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        monkeypatch.delenv("NTFY_TOKEN", raising=False)
        monkeypatch.setenv("NTFY_USERNAME", "alice")
        monkeypatch.setenv("NTFY_PASSWORD", "s3cret")

        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            cmd_send(args)

        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        assert post.call_args.kwargs["headers"]["Authorization"] == expected

    def test_send_passes_optional_headers(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args([
            "send", "body",
            "--title", "hello",
            "--priority", "4",
            "--tags", "warning,bell",
            "--click", "https://example.com/x",
        ])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            cmd_send(args)
        h = post.call_args.kwargs["headers"]
        assert h["Title"] == "hello"
        assert h["Priority"] == "4"
        assert h["Tags"] == "warning,bell"
        assert h["Click"] == "https://example.com/x"

    def test_send_scrubs_newlines_in_headers(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args([
            "send", "body",
            "--title", "evil\r\ninjected: x",
            "--tags", "ok\ntag",
        ])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            cmd_send(args)
        h = post.call_args.kwargs["headers"]
        assert "\n" not in h["Title"] and "\r" not in h["Title"]
        assert "\n" not in h["Tags"]

    def test_send_no_topic_returns_error(self, monkeypatch, capsys):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post") as post:
            rc = cmd_send(args)
        assert rc == 1
        post.assert_not_called()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "topic" in out["error"].lower()

    def test_send_http_error_returns_error_envelope(self, monkeypatch, capsys):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post", return_value=_err_response(403, "forbidden")):
            rc = cmd_send(args)
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "403" in out["error"]

    def test_send_network_error_returns_error_envelope(self, monkeypatch, capsys):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args(["send", "hi"])
        with patch(
            "istota.skills.ntfy.httpx.post",
            side_effect=httpx.ConnectError("dns failed"),
        ):
            rc = cmd_send(args)
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "ntfy request failed" in out["error"]


class TestTopicValidation:
    @pytest.mark.parametrize("bad_topic", [
        "alerts/extra",        # path smuggling
        "alerts?delay=1h",     # query smuggling
        "alerts#frag",
        "..",
        "a" * 65,              # too long
        "alerts!",
        "alerts.subdomain",    # dots not allowed
    ])
    def test_rejects_malformed_topic(self, monkeypatch, capsys, bad_topic):
        monkeypatch.setenv("NTFY_TOPIC", bad_topic)
        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post") as post:
            rc = cmd_send(args)
        assert rc == 1
        post.assert_not_called()
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "malformed" in out["error"].lower()

    @pytest.mark.parametrize("good_topic", ["alerts", "a", "A_B-c123", "x" * 64])
    def test_accepts_valid_topic(self, monkeypatch, good_topic):
        monkeypatch.setenv("NTFY_TOPIC", good_topic)
        args = build_parser().parse_args(["send", "hi"])
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()) as post:
            rc = cmd_send(args)
        assert rc == 0
        post.assert_called_once()


class TestErrorRedaction:
    def test_authorization_token_redacted_from_error_body(self, monkeypatch, capsys):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args(["send", "hi"])
        # Server reflects our header back in the 4xx body — without redaction
        # the bearer token would land in stdout and get indexed.
        leaky = "rejected: Authorization: Bearer tk_VERY_SECRET_123 was invalid"
        with patch("istota.skills.ntfy.httpx.post", return_value=_err_response(403, leaky)):
            cmd_send(args)
        out = json.loads(capsys.readouterr().out)
        assert "tk_VERY_SECRET_123" not in out["error"]
        assert "[redacted]" in out["error"]

    def test_basic_auth_redacted_from_error_body(self, monkeypatch, capsys):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args(["send", "hi"])
        leaky = "header was Basic dXNlcjpwYXNzd29yZA=="
        with patch("istota.skills.ntfy.httpx.post", return_value=_err_response(401, leaky)):
            cmd_send(args)
        out = json.loads(capsys.readouterr().out)
        assert "dXNlcjpwYXNzd29yZA==" not in out["error"]
        assert "[redacted]" in out["error"]

    def test_unexpected_exception_yields_error_envelope(self, monkeypatch, capsys):
        monkeypatch.setenv("NTFY_TOPIC", "t")
        args = build_parser().parse_args(["send", "hi"])
        # Non-httpx crash path — without the bare-except, we'd lose the JSON
        # envelope contract that scheduler._execute_command_task depends on.
        with patch("istota.skills.ntfy.httpx.post", side_effect=RuntimeError("boom")):
            rc = cmd_send(args)
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "crashed" in out["error"]


class TestSkillFrontmatter:
    def test_bare_push_trigger_dropped(self):
        """Bare 'push' would misfire on 'git push' — Pass 1 is plain substring match."""
        from istota.skills._loader import load_skill_index
        from pathlib import Path
        idx = load_skill_index(Path("config/skills"))
        meta = idx["ntfy"]
        assert "push" not in meta.keywords
        assert "push notification" in meta.keywords


class TestProxyCredentialMapping:
    def test_token_and_password_routed_through_proxy(self):
        from istota.executor import _PROXY_CREDENTIAL_VARS, _CREDENTIAL_SKILL_MAP

        assert "NTFY_TOKEN" in _PROXY_CREDENTIAL_VARS
        assert "NTFY_PASSWORD" in _PROXY_CREDENTIAL_VARS
        assert "ntfy" in _CREDENTIAL_SKILL_MAP["NTFY_TOKEN"]
        assert "ntfy" in _CREDENTIAL_SKILL_MAP["NTFY_PASSWORD"]

    def test_non_credential_ntfy_vars_stay_in_clean_env(self):
        """Topic / server URL / username are passed through clean env, not proxy."""
        from istota.executor import _PROXY_CREDENTIAL_VARS

        assert "NTFY_TOPIC" not in _PROXY_CREDENTIAL_VARS
        assert "NTFY_SERVER_URL" not in _PROXY_CREDENTIAL_VARS
        assert "NTFY_USERNAME" not in _PROXY_CREDENTIAL_VARS


class TestMain:
    def test_main_exits_with_cmd_send_returncode(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["istota-skill", "send", "hi"])
        monkeypatch.setenv("NTFY_TOPIC", "t")
        with patch("istota.skills.ntfy.httpx.post", return_value=_ok_response()):
            assert main() == 0
