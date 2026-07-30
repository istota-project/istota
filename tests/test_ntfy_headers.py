"""Tests for ntfy_headers — RFC 2047 encoding of ntfy HTTP header values.

ISSUE-213: httpx serializes header values as ASCII, so an emoji/arrow/em-dash
title raised UnicodeEncodeError and the whole push was lost. ntfy decodes
RFC 2047 encoded-words in any header, so the fix is lossless.
"""

from __future__ import annotations

from email.header import decode_header

import httpx
import pytest

from istota.ntfy_headers import (
    MAX_ENCODED_WORD_CHARS,
    ascii_header_value,
    encode_header_value,
    scrub_header_value,
)


def _decode(value: str) -> str:
    """Decode an RFC 2047 header value the way ntfy's Go decoder does."""
    return "".join(
        part.decode(charset or "ascii") if isinstance(part, bytes) else part
        for part, charset in decode_header(value)
    )


class TestScrubHeaderValue:
    def test_strips_cr_and_folds_lf(self):
        assert scrub_header_value("evil\r\ninjected: x") == "evil injected: x"

    def test_leaves_plain_text_alone(self):
        assert scrub_header_value("Alert!") == "Alert!"


class TestEncodeHeaderValue:
    def test_ascii_passes_through_unchanged(self):
        assert encode_header_value("NOK BUY") == "NOK BUY"

    def test_emoji_title_is_encoded_and_round_trips(self):
        encoded = encode_header_value("📈 NOK BUY ↑")
        assert encoded.isascii()
        assert encoded.startswith("=?UTF-8?B?")
        assert _decode(encoded) == "📈 NOK BUY ↑"

    @pytest.mark.parametrize(
        "value",
        ["Kurs — heute", "Äpfel", "→ up", "café ☕", "日本語のタイトル"],
    )
    def test_non_ascii_round_trips(self, value):
        assert _decode(encode_header_value(value)) == value

    def test_encoded_value_is_accepted_by_httpx(self):
        # The actual regression: httpx encodes header values as ASCII.
        with pytest.raises(UnicodeEncodeError):
            httpx.Headers({"Title": "📈 NOK BUY ↑"})
        headers = httpx.Headers({"Title": encode_header_value("📈 NOK BUY ↑")})
        assert headers["Title"].isascii()

    def test_long_value_splits_into_bounded_encoded_words(self):
        value = "📈 " * 60
        encoded = encode_header_value(value)
        words = encoded.split(" ")
        assert len(words) > 1
        for word in words:
            assert len(word) <= MAX_ENCODED_WORD_CHARS
            assert word.startswith("=?UTF-8?B?") and word.endswith("?=")
        assert _decode(encoded) == value

    def test_newlines_are_stripped_before_encoding(self):
        encoded = encode_header_value("📈 evil\r\ninjected: x")
        assert "\r" not in encoded and "\n" not in encoded
        assert _decode(encoded) == "📈 evil injected: x"

    def test_lone_surrogate_does_not_raise(self):
        # A total function: a bad title must never cost us the notification body.
        assert encode_header_value("bad \ud800 title").isascii()


class TestAsciiHeaderValue:
    def test_drops_non_ascii_and_collapses_whitespace(self):
        assert ascii_header_value("📈 NOK BUY ↑") == "NOK BUY"

    def test_strips_newlines(self):
        assert ascii_header_value("evil\r\ninjected: x") == "evil injected: x"

    def test_all_non_ascii_becomes_empty(self):
        assert ascii_header_value("📈↑") == ""
