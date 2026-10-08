"""Literal query matching and plain-text excerpts."""

import sqlite3

import pytest

from istota.lib.text_match import (
    MARK_CLOSE, MARK_OPEN, Term, fts5_match, like_predicate, make_snippet,
    markers_to_offsets, parse_query, terms_to_plain, text_matches,
)


def test_parse_phrases_normalization_and_bounds():
    assert parse_query('  cafe\u0301 "red fox" tail  ') == [
        Term("café", False), Term("red fox", True), Term("tail", False),
    ]
    assert parse_query('one "unfinished words') == [
        Term("one", False), Term('"unfinished', False), Term("words", False),
    ]
    assert parse_query('* ^ : () - + "') == []
    assert len(parse_query(" ".join(str(i) for i in range(20)))) == 8
    assert parse_query("one two", max_terms=0) == []
    assert parse_query('"red fox"', phrases=False) == [
        Term('"red', False), Term('fox"', False),
    ]
    assert terms_to_plain(parse_query('one "red fox"')) == "one red fox"


@pytest.fixture
def fts():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='unicode61 remove_diacritics 2')")
        conn.executemany("INSERT INTO docs VALUES (?)", [
            ("falcon red fox café",), ("falcon fox red",), ("blue fox",),
        ])
        yield conn


def test_fts_prefix_phrase_and_relaxed(fts):
    def ids(query, mode="strict"):
        return [r[0] for r in fts.execute(
            "SELECT rowid FROM docs WHERE docs MATCH ? ORDER BY rowid",
            (fts5_match(parse_query(query), mode),),
        )]
    assert ids('falc "red fox"') == [1]
    assert ids("cafe") == [1]
    assert ids("falc blue", "relaxed") == [1, 2, 3]
    assert ids("falc blue") == []
    assert ids('"falc"') == []


@pytest.mark.parametrize("query", [
    "NEAR(a b)", "col:x", '"', "*", "a OR b", "^x", "-x", "(fox", 'fo"x', "((",
])
def test_hostile_fts_input_is_literal(fts, query):
    for mode in ("strict", "relaxed"):
        fts.execute("SELECT rowid FROM docs WHERE docs MATCH ?",
                    (fts5_match(parse_query(query), mode),)).fetchall()


def test_like_escaping_and_column_validation():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE docs (title TEXT, body TEXT)")
        conn.executemany("INSERT INTO docs VALUES (?, ?)", [
            ("Sale", r"50% a_b c\d"), ("Sale", "500 axb cd"),
        ])
        predicate, params = like_predicate(["title", "body"], parse_query(r"50% a_b c\d"), "strict")
        assert conn.execute(f"SELECT body FROM docs WHERE {predicate}", params).fetchall() == [(r"50% a_b c\d",)]
        predicate, params = like_predicate(["title", "body"], parse_query("missing Sale"), "relaxed")
        assert len(conn.execute(f"SELECT body FROM docs WHERE {predicate}", params).fetchall()) == 2
    with pytest.raises(ValueError):
        like_predicate(["body); DROP TABLE docs;--"], parse_query("fox"), "strict")
    assert like_predicate(["body"], [], "strict") == ("0", [])


def test_text_matches_uses_substrings_and_ascii_case_folding():
    assert text_matches("Falcons red fox", parse_query('falc "red fox"'), "strict")
    assert text_matches("falcon", parse_query("blue falc"), "relaxed")
    assert not text_matches("été", parse_query("ÉTÉ"), "strict")
    assert not text_matches(None, parse_query("fox"), "strict")
    assert not text_matches("fox", [], "strict")


@pytest.mark.parametrize("fn,args", [
    (fts5_match, [[]]), (like_predicate, [["body"], []]), (text_matches, ["fox", []]),
])
def test_invalid_mode(fn, args):
    with pytest.raises(ValueError):
        fn(*args, "unknown")


def test_snippet_collapses_whitespace_and_marks_all_matches():
    assert make_snippet("Fox\n  fox and FOX", parse_query("fox")) == (
        "Fox fox and FOX", [[0, 3], [4, 7], [12, 15]],
    )
    assert make_snippet(None, []) == ("", [])
    assert make_snippet(1234, parse_query("23")) == ("1234", [[1, 3]])
    assert make_snippet("one two three four fox six seven eight nine", parse_query("fox"), width=20) == (
        "…four fox six seven…", [[6, 9]],
    )
    assert make_snippet("one two three four", [], width=8) == ("one two…", [])


def test_marker_offsets_collapse_whitespace_and_discard_unpaired_markers():
    assert markers_to_offsets(f"a\n {MARK_OPEN}red   fox{MARK_CLOSE} end") == (
        "a red fox end", [[2, 9]],
    )
    assert markers_to_offsets(f"{MARK_CLOSE}loose {MARK_OPEN}tail") == ("loose tail", [])
    assert markers_to_offsets(None) == ("", [])
    assert markers_to_offsets(123) == ("123", [])
    assert make_snippet(f"{MARK_OPEN}fox{MARK_CLOSE}", parse_query("fox")) == ("fox", [[0, 3]])


def test_snippet_keeps_a_match_inside_a_long_word():
    assert make_snippet("abcdefghijklmnop", parse_query("hij"), width=8) == (
        "abcdefghijklmnop", [[7, 10]],
    )
    assert make_snippet("abcdefghijklmnop", [], width=8) == ("abcdefghijklmnop", [])
    assert parse_query('"red\nfox"') == [Term("red\nfox", True)]
    assert len(terms_to_plain(parse_query("x" * 201))) == 200
