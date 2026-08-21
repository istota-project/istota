"""Tests for devbox proxy wire protocol.

Pure data + serialization. No sockets, no daemon. Mirrors what
`tests/test_skill_proxy.py` does for skill_proxy, but the daemon is
asyncio-based so the protocol module is split out so it can be unit-tested
without dragging in a running event loop.

The two REST actions (``gitlab_api`` / ``github_api``) and the error codes
only they produced are gone — the real ``gh`` and ``glab`` run in the
container behind ``forge_cli.py``, and the proxy's whole forge job is now
handing out a token.
"""

from __future__ import annotations

import json

import pytest

from istota.devbox_proxy_protocol import (
    ACTION_FORGE_TOKEN,
    ACTION_GIT_CREDENTIAL,
    ACTION_PING,
    ALL_ACTIONS,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NO_TOKEN,
    ERR_UNKNOWN_ACTION,
    ERR_UNKNOWN_PROVIDER,
    MAX_REQUEST_BYTES,
    ProtocolError,
    decode_request,
    decode_response,
    encode_error,
    encode_request,
    encode_response,
)


class TestActionConstants:
    def test_known_action_names(self):
        assert ACTION_PING == "ping"
        assert ACTION_GIT_CREDENTIAL == "git_credential"
        assert ACTION_FORGE_TOKEN == "forge_token"

    def test_all_actions_set_is_complete(self):
        assert ALL_ACTIONS == {
            ACTION_PING,
            ACTION_GIT_CREDENTIAL,
            ACTION_FORGE_TOKEN,
        }

    def test_retired_rest_actions_are_gone(self):
        # The wrapper sends `forge_token` and nothing else; a client still
        # sending `gitlab_api` must fall through to unknown_action rather
        # than find a handler. Named here so a revert is loud.
        assert "gitlab_api" not in ALL_ACTIONS
        assert "github_api" not in ALL_ACTIONS

    def test_wrapper_and_protocol_agree_on_the_action_name(self):
        # forge_cli.py cannot import istota, so it carries its own copy of
        # the action name. This is the seam where the two would drift.
        from istota.forge_cli import ACTION_FORGE_TOKEN as WRAPPER_ACTION

        assert WRAPPER_ACTION == ACTION_FORGE_TOKEN
        assert WRAPPER_ACTION in ALL_ACTIONS


class TestErrorCodes:
    def test_error_code_strings_are_stable(self):
        # Stable string identifiers — audit log keys on these.
        assert ERR_NO_TOKEN == "no_token"
        assert ERR_UNKNOWN_ACTION == "unknown_action"
        assert ERR_BAD_REQUEST == "bad_request"
        assert ERR_UNKNOWN_PROVIDER == "unknown_provider"
        assert ERR_INTERNAL == "internal"


class TestEncodeRequest:
    def test_ping_request_is_minimal_envelope(self):
        line = encode_request(action=ACTION_PING)
        assert line.endswith("\n")
        parsed = json.loads(line)
        assert parsed == {"action": "ping"}

    def test_git_credential_get_request_round_trips(self):
        line = encode_request(
            action=ACTION_GIT_CREDENTIAL,
            op="get",
            input="protocol=https\nhost=github.com\n",
        )
        parsed = json.loads(line)
        assert parsed["action"] == "git_credential"
        assert parsed["op"] == "get"
        assert parsed["input"] == "protocol=https\nhost=github.com\n"

    def test_forge_token_request_carries_only_the_provider(self):
        line = encode_request(action=ACTION_FORGE_TOKEN, provider="github")
        parsed = json.loads(line)
        assert parsed == {"action": "forge_token", "provider": "github"}

    def test_forge_token_request_matches_what_the_wrapper_sends(self):
        # forge_cli.fetch_token builds this line by hand rather than through
        # encode_request (it cannot import istota). Assert the two agree on
        # the field names, which is the only thing that has to match.
        line = encode_request(action=ACTION_FORGE_TOKEN, provider="gitlab")
        assert set(json.loads(line)) == {"action", "provider"}

    def test_request_is_single_line(self):
        # Single newline at end, none embedded — line-delimited framing.
        line = encode_request(
            action=ACTION_GIT_CREDENTIAL,
            op="get",
            input="protocol=https\nhost=github.com\n",
        )
        assert line.count("\n") == 1
        assert line.endswith("\n")


class TestEncodeResponse:
    def test_ok_response_marks_ok_true(self):
        line = encode_response(ok=True, user_id="alice", providers=["github"])
        parsed = json.loads(line)
        assert parsed["ok"] is True
        assert parsed["user_id"] == "alice"
        assert parsed["providers"] == ["github"]

    def test_ok_response_is_single_line(self):
        line = encode_response(ok=True, body="x\ny\n")
        assert line.count("\n") == 1
        assert line.endswith("\n")

    def test_git_credential_get_ok_response(self):
        # The stdout field is what the in-container helper echoes verbatim.
        line = encode_response(
            ok=True,
            stdout="protocol=https\nhost=github.com\nusername=x-access-token\npassword=TOK\n",
        )
        parsed = json.loads(line)
        assert parsed["ok"] is True
        assert "password=TOK" in parsed["stdout"]


class TestEncodeError:
    def test_minimal_error_has_ok_false_code_and_message(self):
        line = encode_error(ERR_NO_TOKEN, "no token configured for github")
        parsed = json.loads(line)
        assert parsed == {
            "ok": False,
            "error": "no_token",
            "message": "no token configured for github",
        }

    def test_error_carries_extra_fields(self):
        line = encode_error(
            ERR_UNKNOWN_PROVIDER,
            "unknown provider 'bitbucket'",
            provider="bitbucket",
        )
        parsed = json.loads(line)
        assert parsed["ok"] is False
        assert parsed["error"] == "unknown_provider"
        assert parsed["message"] == "unknown provider 'bitbucket'"
        assert parsed["provider"] == "bitbucket"

    def test_error_envelope_round_trips_through_decode_response(self):
        line = encode_error(ERR_UNKNOWN_PROVIDER, "unknown provider 'bitbucket'")
        resp = decode_response(line)
        assert resp["ok"] is False
        assert resp["error"] == "unknown_provider"
        assert resp["message"] == "unknown provider 'bitbucket'"


class TestDecodeRequest:
    def test_decode_ping(self):
        req = decode_request('{"action":"ping"}\n')
        assert req == {"action": "ping"}

    def test_decode_strips_trailing_newline(self):
        req = decode_request('{"action":"ping"}\n')
        assert req["action"] == "ping"

    def test_decode_strips_leading_and_trailing_whitespace(self):
        req = decode_request('   {"action":"ping"}   \n')
        assert req["action"] == "ping"

    def test_decode_malformed_json_raises_protocol_error_bad_request(self):
        with pytest.raises(ProtocolError) as ei:
            decode_request("not json at all\n")
        assert ei.value.code == ERR_BAD_REQUEST

    def test_decode_empty_payload_is_bad_request(self):
        with pytest.raises(ProtocolError) as ei:
            decode_request("")
        assert ei.value.code == ERR_BAD_REQUEST

    def test_decode_non_object_payload_is_bad_request(self):
        with pytest.raises(ProtocolError) as ei:
            decode_request('["array","not","object"]')
        assert ei.value.code == ERR_BAD_REQUEST

    def test_decode_missing_action_field_is_bad_request(self):
        with pytest.raises(ProtocolError) as ei:
            decode_request('{"op":"get"}')
        assert ei.value.code == ERR_BAD_REQUEST

    def test_decode_oversize_payload_is_bad_request(self):
        oversize = " " * (MAX_REQUEST_BYTES + 1)
        # Even a payload that's just whitespace beyond the cap should fail
        # before JSON parsing — the cap is a defense, not a JSON-shape check.
        with pytest.raises(ProtocolError) as ei:
            decode_request(oversize)
        assert ei.value.code == ERR_BAD_REQUEST


class TestDecodeResponse:
    def test_decode_ok_response(self):
        resp = decode_response('{"ok":true,"stdout":"hi"}\n')
        assert resp["ok"] is True
        assert resp["stdout"] == "hi"

    def test_decode_error_response_preserves_error_code(self):
        resp = decode_response('{"ok":false,"error":"no_token","message":"x"}')
        assert resp["ok"] is False
        assert resp["error"] == "no_token"

    def test_decode_malformed_response_raises_protocol_error(self):
        with pytest.raises(ProtocolError):
            decode_response("not json")


class TestRequestSizeCap:
    def test_max_request_bytes_is_16_mib(self):
        assert MAX_REQUEST_BYTES == 16 * 1024 * 1024
