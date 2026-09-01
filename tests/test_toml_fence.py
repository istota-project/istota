"""The shared ```toml fence rule (ISSUE-386).

Four modules parsed a TOML block out of a user-written markdown file, each
with its own copy of an expression that anchored neither marker. The tests
here are the rule itself; each caller's own file covers what it does with
the answer.
"""

import time

import pytest

from istota.toml_fence import (
    BACKTICK_RUN_RE,
    FENCE_CLOSE_RE,
    FENCE_OPEN_RE,
    find_toml_block,
)


def _body(text):
    """The block `find_toml_block` picks out, or None."""
    span = find_toml_block(text)
    return None if span is None else text[span[0]:span[1]]


class TestAMarkerIsALine:
    """The fix: three backticks are a marker only when alone on their line."""

    def test_a_marker_in_a_comment_does_not_close_the_block(self):
        text = '```toml\na = 1\n# paste a ```toml fence when sharing\nb = 2\n```\n'
        assert _body(text) == 'a = 1\n# paste a ```toml fence when sharing\nb = 2\n'

    def test_a_marker_in_a_string_value_does_not_close_the_block(self):
        text = '```toml\na = "see ```toml in docs"\nb = 2\n```\n'
        assert _body(text) == 'a = "see ```toml in docs"\nb = 2\n'

    def test_prose_before_the_opener_is_not_an_opener(self):
        assert find_toml_block("see ```toml\na = 1\n```\n") is None

    def test_a_decorated_closer_does_not_close(self):
        """CommonMark forbids an info string on a closer.

        Honouring that is what keeps a ```python line inside a multi-line
        prompt from ending the block early.
        """
        assert find_toml_block("```toml\na = 1\n``` end\n") is None


class TestTheBoundsAreLoose:
    """Every bound is looser than CommonMark, deliberately.

    The expression this replaced had no ``^`` at all, so it accepted any
    prefix. Almost any bound is therefore a narrowing, and a narrowing
    breaks a file that used to work — which for ``cron_loader`` means the
    user's document is rewritten from the database.
    """

    @pytest.mark.parametrize("label,text", [
        ("no-indent", "```toml\na = 1\n```\n"),
        ("indent-3", "   ```toml\na = 1\n   ```\n"),
        ("indent-8", "        ```toml\na = 1\n        ```\n"),
        ("indent-tab", "\t```toml\na = 1\n\t```\n"),
        ("four-backticks", "````toml\na = 1\n````\n"),
        ("mixed-lengths", "````toml\na = 1\n```\n"),
        ("info-string", '```toml title="x"\na = 1\n```\n'),
        ("trailing-space", "```toml\na = 1\n```   \n"),
        ("trailing-nbsp", "```toml\na = 1\n```\xa0\n"),
        ("bom", "﻿```toml\na = 1\n```\n"),
        ("no-trailing-newline", "```toml\na = 1\n```"),
    ])
    def test_a_shape_the_old_expression_accepted_is_still_accepted(self, label, text):
        assert _body(text) == "a = 1\n", label

    def test_crlf(self):
        """``$`` under MULTILINE matches before ``\\n``, never before ``\\r``.

        Nothing normalises newlines on the read path, so a file from a
        Windows client or a web editor arrives with its ``\\r`` intact.
        """
        assert _body("```toml\r\na = 1\r\n```\r\n") == "a = 1\r\n"


class TestTheSpanExcludesBothMarkers:
    def test_an_indented_closer_keeps_its_indent_outside_the_span(self):
        """A caller splices over the span, so an indent inside it is lost."""
        text = "```toml\na = 1\n    ```\n"
        span = find_toml_block(text)
        assert text[span[0]:span[1]] == "a = 1\n"
        assert text[span[1]:] == "    ```\n"

    def test_content_before_and_after_is_outside_the_span(self):
        text = "# Title\n\n```toml\na = 1\n```\n\nnotes\n"
        span = find_toml_block(text)
        assert text[:span[0]] == "# Title\n\n```toml\n"
        assert text[span[1]:] == "```\n\nnotes\n"


class TestTheSearchIsLinear:
    def test_many_openers_and_no_closer_returns_quickly(self):
        """A combined ``open(.*?)close`` expression is quadratic here.

        Every opener is a fresh start position and each rescans to EOF: the
        combined form took 65s on 256 KB of this shape. These files are
        user-writable and one caller parses them on the scheduler's tick.
        """
        started = time.monotonic()
        assert find_toml_block("```toml\n" * 20000) is None
        assert time.monotonic() - started < 2.0


class TestTheHoldGuardPattern:
    """``BACKTICK_RUN_RE`` exists for ``cron_loader``'s destructive branch.

    It has to fire on every shape the two markers refuse, since the
    alternative verdict authorizes rewriting the user's document.
    """

    @pytest.mark.parametrize("text", [
        "see ```toml\na = 1\n```\n",
        "> ```toml\n> a = 1\n> ```\n",
        "```toml\na = 1\n``` end\n",
        "````\nnot toml\n````\n",
    ])
    def test_it_fires_on_shapes_the_markers_refuse(self, text):
        assert find_toml_block(text) is None
        assert BACKTICK_RUN_RE.search(text) is not None

    def test_it_does_not_fire_on_a_file_with_no_backticks(self):
        assert BACKTICK_RUN_RE.search("# Notes\n\nNothing here.\n") is None


class TestTheTwoMarkersAgree:
    @pytest.mark.parametrize("indent,ticks,info", [
        ("", "```", ""),
        ("   ", "````", ""),
        ("\t", "```", ' title="x"'),
        ("        ", "``````", ""),
    ])
    def test_an_opener_marker_also_reads_as_a_closer(self, indent, ticks, info):
        """The two expressions have to accept the same indents and lengths.

        Where they do not, a well-formed fence is reported as "an opener
        that never closes" and its jobs never sync — which is exactly what
        the earlier fix did to a CRLF file.
        """
        assert FENCE_OPEN_RE.match(f"{indent}{ticks}toml{info}\n")
        # A closer is the same indent and the same backticks, and by
        # CommonMark carries no info string of its own.
        assert FENCE_CLOSE_RE.match(f"{indent}{ticks}\n")
