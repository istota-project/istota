"""Markdown flattened to the text a reader sees, for a plain-text preview."""

import time

import pytest

from istota.lib.markdown_plain import to_plain_text


@pytest.mark.parametrize("markdown, plain", [
    ("See [the docs](https://example.com/a_b) now", "See the docs now"),
    ("![a cat](https://example.com/cat.png)", "a cat"),
    ("Read [the guide][ref] first", "Read the guide first"),
    ("Go to <https://example.com/x>", "Go to https://example.com/x"),
    ("**bold** and __strong__", "bold and strong"),
    ("*italic* and _under_", "italic and under"),
    ("~~gone~~ and ==marked==", "gone and marked"),
    ("Run `uv sync` first", "Run uv sync first"),
    ("Use ``a `tick` b``", "Use a `tick` b"),
    ("## Heading ##", "Heading"),
    ("> quoted line", "quoted line"),
    ("> > nested", "nested"),
    ("- one", "one"),
    ("* two", "two"),
    ("+ three", "three"),
    ("12. twelve", "twelve"),
    ("- [ ] open task", "open task"),
    ("- [x] done task", "done task"),
    ("<b>bold</b> text<br/>", "bold text"),
    (r"\*not emphasis\* and \_this\_", "*not emphasis* and _this_"),
    ("2 * 3 * 4", "2 * 3 * 4"),
    ("replace <name> with yours", "replace <name> with yours"),
    ("Tip: x<y and y>z", "Tip: x<y and y>z"),
    ("[x](https://a/b_(c)) end", "x end"),
    ("Title\n=====", "Title"),
    ("Grüße 🚀 東京", "Grüße 🚀 東京"),
])
def test_inline_and_line_constructs(markdown, plain):
    assert to_plain_text(markdown) == plain


def test_snake_case_and_underscored_urls_survive():
    text = "call snake_case_name at https://example.com/_private_/a_b"
    assert to_plain_text(text) == text


def test_horizontal_rules_are_dropped():
    assert to_plain_text("above\n---\n***\n_ _ _\nbelow") == "above\nbelow"


def test_fenced_code_keeps_its_contents_verbatim():
    text = "Try:\n```python\nx = a_b * c_d\n**not bold**\n```\ndone"
    assert to_plain_text(text) == "Try:\nx = a_b * c_d\n**not bold**\ndone"


def test_a_body_that_is_only_a_fence_yields_its_contents():
    assert to_plain_text("~~~\nprint('hi')\n~~~") == "print('hi')"


@pytest.mark.parametrize("line", [
    " " * 32000 + "x",
    "# a" + " " * 32000 + "b",
    "\\*" * 16000,
])
def test_long_pathological_lines_stay_linear(line):
    started = time.perf_counter()
    to_plain_text(line)
    assert time.perf_counter() - started < 0.5


def test_tables_lose_separators_and_pipes():
    text = "| Name | Size |\n|:-----|-----:|\n| **a** | 1 |\n| b | 2 |"
    assert to_plain_text(text) == "Name · Size\na · 1\nb · 2"
