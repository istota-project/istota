"""Stage 1 — the fuzzy, multi-edit matching engine (model-free pure logic)."""

import pytest

from istota.session.tools.edit_engine import (
    Edit,
    EditError,
    apply_edits_to_normalized_content,
    detect_line_ending,
    fuzzy_find_text,
    normalize_for_fuzzy_match,
    normalize_to_lf,
    restore_line_endings,
    strip_bom,
)


def _apply(content, edits, path="/f.py"):
    return apply_edits_to_normalized_content(content, [Edit(o, n) for o, n in edits], path)


# --------------------------------------------------------------------------- #
# Line endings & BOM
# --------------------------------------------------------------------------- #


class TestLineEndings:
    def test_detects_lf(self):
        assert detect_line_ending("a\nb\nc") == "\n"

    def test_detects_crlf(self):
        assert detect_line_ending("a\r\nb\r\nc") == "\r\n"

    def test_no_newline_is_lf(self):
        assert detect_line_ending("abc") == "\n"

    def test_mixed_picks_dominant_first(self):
        # First newline is a bare LF → LF dominant.
        assert detect_line_ending("a\nb\r\nc") == "\n"
        # First newline is CRLF → CRLF dominant.
        assert detect_line_ending("a\r\nb\nc") == "\r\n"

    def test_normalize_to_lf(self):
        assert normalize_to_lf("a\r\nb\rc") == "a\nb\nc"

    def test_restore_crlf(self):
        assert restore_line_endings("a\nb", "\r\n") == "a\r\nb"

    def test_restore_lf_noop(self):
        assert restore_line_endings("a\nb", "\n") == "a\nb"


class TestBom:
    def test_strips_bom(self):
        bom, text = strip_bom("﻿hello")
        assert bom == "﻿"
        assert text == "hello"

    def test_no_bom(self):
        bom, text = strip_bom("hello")
        assert bom == ""
        assert text == "hello"


# --------------------------------------------------------------------------- #
# Fuzzy matching primitive
# --------------------------------------------------------------------------- #


class TestFuzzyFind:
    def test_exact_match_not_fuzzy(self):
        m = fuzzy_find_text("alpha beta gamma", "beta")
        assert m.found and not m.used_fuzzy
        assert m.index == 6

    def test_fuzzy_match_trailing_ws(self):
        m = fuzzy_find_text("line one   \nline two", "line one\nline two")
        assert m.found and m.used_fuzzy

    def test_no_match(self):
        m = fuzzy_find_text("alpha", "zzz")
        assert not m.found


# --------------------------------------------------------------------------- #
# Exact single edit
# --------------------------------------------------------------------------- #


class TestExactSingle:
    def test_replaces_unique(self):
        r = _apply("alpha beta gamma", [("beta", "BETA")])
        assert r.new_content == "alpha BETA gamma"

    def test_not_found(self):
        with pytest.raises(EditError) as exc:
            _apply("alpha", [("zzz", "x")])
        assert "Could not find the exact text" in str(exc.value)

    def test_duplicate(self):
        with pytest.raises(EditError) as exc:
            _apply("x x x", [("x", "y")])
        assert "occurrences" in str(exc.value)
        assert "unique" in str(exc.value)

    def test_empty_old_string(self):
        with pytest.raises(EditError) as exc:
            _apply("abc", [("", "x")])
        assert "must not be empty" in str(exc.value)

    def test_no_op(self):
        with pytest.raises(EditError) as exc:
            _apply("hello world", [("hello", "hello")])
        assert "identical" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Fuzzy edits — each normalization class hits
# --------------------------------------------------------------------------- #


class TestFuzzyHits:
    def test_trailing_whitespace(self):
        # File has trailing spaces the model didn't remember.
        r = _apply("def f():   \n    return 1\n", [("def f():\n    return 1", "def f():\n    return 2")])
        assert "return 2" in r.new_content

    def test_smart_single_quote(self):
        r = _apply("x = 'hi'\n", [("x = ‘hi’", "x = 'bye'")])
        assert "bye" in r.new_content

    def test_smart_double_quote(self):
        r = _apply('x = "hi"\n', [("x = “hi”", 'x = "bye"')])
        assert "bye" in r.new_content

    @pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "−"])
    def test_each_dash(self, dash):
        r = _apply(f"a {dash} b\n", [("a - b", "a = b")])
        assert "a = b" in r.new_content

    @pytest.mark.parametrize(
        "space",
        [" ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", "　"],
    )
    def test_each_exotic_space(self, space):
        r = _apply(f"a{space}b\n", [("a b", "a c")])
        assert "a c" in r.new_content

    def test_nfkc_ligature(self):
        # U+FB01 (ﬁ ligature) NFKC-decomposes to "fi".
        r = _apply("deﬁne x\n", [("define x", "define y")])
        assert "define y" in r.new_content

    def test_leading_indent_not_tolerated(self):
        # A leading-indentation change (tab in the file vs spaces in old_string)
        # is NOT a tolerated fuzzy class — neither exact nor fuzzy matches.
        with pytest.raises(EditError):
            _apply("\treturn 1\n", [("    return 1", "    return 2")])


class TestPreserveUnchangedBytes:
    def test_fuzzy_edit_leaves_adjacent_smart_quote_line_intact(self):
        original = "greeting = “hello”\nvalue = 1   \nfarewell = ‘bye’\n"
        # Edit the middle line via fuzzy (trailing ws); the smart-quote lines
        # around it must keep their exact bytes.
        r = _apply(original, [("value = 1", "value = 2")])
        assert "greeting = “hello”" in r.new_content
        assert "farewell = ‘bye’" in r.new_content
        assert "value = 2" in r.new_content


# --------------------------------------------------------------------------- #
# Multi-edit
# --------------------------------------------------------------------------- #


class TestMultiEdit:
    def test_disjoint_edits(self):
        r = _apply("a = 1\nb = 2\nc = 3\n", [("a = 1", "a = 10"), ("c = 3", "c = 30")])
        assert r.new_content == "a = 10\nb = 2\nc = 30\n"

    def test_reverse_order_offset_stability(self):
        # Edits given out of positional order; both must land correctly.
        r = _apply("one two three four", [("three", "THREE"), ("one", "ONE")])
        assert r.new_content == "ONE two THREE four"

    def test_duplicate_in_batch(self):
        with pytest.raises(EditError) as exc:
            _apply("x x\ny\n", [("x", "z"), ("y", "w")])
        assert "edits[0]" in str(exc.value)
        assert "unique" in str(exc.value)

    def test_overlap_error(self):
        with pytest.raises(EditError) as exc:
            _apply("abcdef", [("abcd", "X"), ("cdef", "Y")])
        assert "overlap" in str(exc.value)
        assert "edits[0]" in str(exc.value) and "edits[1]" in str(exc.value)

    def test_empty_old_string_in_batch(self):
        with pytest.raises(EditError) as exc:
            _apply("abc\n", [("abc", "xyz"), ("", "q")])
        assert "edits[1]" in str(exc.value)
        assert "must not be empty" in str(exc.value)

    def test_no_op_batch(self):
        with pytest.raises(EditError) as exc:
            _apply("a\nb\n", [("a", "a")])
        assert "identical" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Line-ending round trip through the engine's caller contract
# --------------------------------------------------------------------------- #


class TestLineEndingRoundTrip:
    def test_crlf_content_matched_in_lf(self):
        # The caller normalizes to LF before calling the engine; the engine
        # returns LF content and the caller restores CRLF.
        crlf = "a = 1\r\nb = 2\r\n"
        ending = detect_line_ending(crlf)
        assert ending == "\r\n"
        lf = normalize_to_lf(crlf)
        r = _apply(lf, [("a = 1", "a = 99")])
        restored = restore_line_endings(r.new_content, ending)
        assert restored == "a = 99\r\nb = 2\r\n"
