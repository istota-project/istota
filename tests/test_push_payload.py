"""Attacker-controlled alert text is bounded before reaching APNs."""

import json

import pytest

from istota.push import dispatch


def test_sanitizer_removes_controls_and_collapses_whitespace_preserving_emoji():
    assert dispatch._sanitize("Hi\x00\u202e there\n\t  🚀", 80) == "Hi there 🚀"
    assert dispatch._sanitize("abcdef", 4) == "abcd"


def test_alert_caps_and_envelope_are_preserved():
    target = {"kind": "chat", "room_token": "room:test", "room_id": 3}
    payload = dispatch._alert_payload("🚀" * 100, "z" * 6000, 8, "room:test", target)
    assert len(payload["aps"]["alert"]["title"]) == 80
    assert len(payload["aps"]["alert"]["body"]) == 180
    assert payload["istota"] == target
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < 4096


def test_oversize_serialized_payload_drops_body_then_truncates_title():
    payload = {"aps": {"alert": {"title": "Hello", "body": "b" * 6000}},
               "istota": {"kind": "notification", "notification_id": 1}}
    fitted = dispatch._fit_payload(payload)
    assert fitted["aps"]["alert"] == {"title": "Hello", "body": ""}
    payload["aps"]["alert"]["title"] = "🚀" * 2000
    fitted = dispatch._fit_payload(payload)
    assert len(json.dumps(fitted, ensure_ascii=False, separators=(",", ":")).encode()) <= 4096
    payload["istota"]["link"] = "/" + "a" * 5000
    with pytest.raises(ValueError, match="envelope"):
        dispatch._fit_payload(payload)


def test_preview_off_preserves_deep_link_but_uses_generic_text():
    notification = {"id": 3, "state": "open", "updated_at": "2033-05-18T03:33:20Z",
                    "title": "Secret subject", "body": "Secret text", "source": "confirmation", "link": "/chat"}
    payload, _ = next(dispatch._notification_pushes([notification], 4, False, 0))
    assert payload["aps"]["alert"] == {"title": "Something needs your attention", "body": ""}
    assert payload["istota"] == {"kind": "notification", "notification_id": 3, "link": "/chat"}
    row = {"room_token": "room:test", "room_id": 5, "room_name": "Work", "body": "Secret message",
           "created_at": "2033-05-18T03:33:20Z"}
    payload, collapse = next(dispatch._chat_pushes([row], 4, False, 0))
    assert payload["aps"]["alert"] == {"title": "New message in Work", "body": ""}
    assert payload["istota"] == {"kind": "chat", "room_token": "room:test", "room_id": 5}
    assert collapse == "room:room:test"


def test_unsafe_stored_link_is_not_sent():
    row = {"id": 1, "state": "open", "updated_at": "2033-05-18T03:33:20Z", "title": "Notice",
           "body": "", "source": "confirmation", "link": "https://example.com"}
    payload, _ = next(dispatch._notification_pushes([row], 0, True, 0))
    assert "link" not in payload["istota"]
