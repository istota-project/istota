"""Tests for devbox proxy wire protocol (Stage 1).

Pure data + serialization. No sockets, no daemon. Mirrors what
`tests/test_skill_proxy.py` does for skill_proxy, but the new daemon is
asyncio-based so the protocol module is split out so it can be unit-tested
without dragging in a running event loop.
"""

from __future__ import annotations

import json

import pytest

from istota.devbox_proxy_protocol import (
    ACTION_GH_API,
    ACTION_GIT_CREDENTIAL,
    ACTION_GL_API,
    ACTION_PING,
    ALL_ACTIONS,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NO_TOKEN,
    ERR_NOT_ALLOWED,
    ERR_UNKNOWN_ACTION,
    ERR_UPSTREAM,
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
        assert ACTION_GL_API == "gitlab_api"
        assert ACTION_GH_API == "github_api"

    def test_all_actions_set_is_complete(self):
        assert ALL_ACTIONS == {
            ACTION_PING,
            ACTION_GIT_CREDENTIAL,
            ACTION_GL_API,
            ACTION_GH_API,
        }


class TestErrorCodes:
    def test_error_code_strings_are_stable(self):
        # Stable string identifiers — audit log keys on these.
        assert ERR_NO_TOKEN == "no_token"
        assert ERR_UNKNOWN_ACTION == "unknown_action"
        assert ERR_BAD_REQUEST == "bad_request"
        assert ERR_NOT_ALLOWED == "not_allowed"
        assert ERR_UPSTREAM == "upstream_error"
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

    def test_gitlab_api_request_carries_method_endpoint_body_headers(self):
        line = encode_request(
            action=ACTION_GL_API,
            method="POST",
            endpoint="/projects/42/merge_requests",
            body='{"title":"x"}',
            headers={"X-Trace": "abc"},
        )
        parsed = json.loads(line)
        assert parsed == {
            "action": "gitlab_api",
            "method": "POST",
            "endpoint": "/projects/42/merge_requests",
            "body": '{"title":"x"}',
            "headers": {"X-Trace": "abc"},
        }

    def test_github_api_request_same_shape(self):
        line = encode_request(
            action=ACTION_GH_API,
            method="GET",
            endpoint="/repos/foo/bar",
            body=None,
        )
        parsed = json.loads(line)
        assert parsed["action"] == "github_api"
        assert parsed["method"] == "GET"
        assert parsed["endpoint"] == "/repos/foo/bar"
        assert parsed["body"] is None

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
            ERR_UPSTREAM,
            "github returned 422",
            status=422,
            body='{"error":"invalid"}',
        )
        parsed = json.loads(line)
        assert parsed["ok"] is False
        assert parsed["error"] == "upstream_error"
        assert parsed["message"] == "github returned 422"
        assert parsed["status"] == 422
        assert parsed["body"] == '{"error":"invalid"}'

    def test_error_envelope_round_trips_through_decode_response(self):
        line = encode_error(ERR_NOT_ALLOWED, "endpoint /admin not in allowlist")
        resp = decode_response(line)
        assert resp["ok"] is False
        assert resp["error"] == "not_allowed"
        assert resp["message"] == "endpoint /admin not in allowlist"


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
