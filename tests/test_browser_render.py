"""Unit tests for the browser container's markdown-render heuristics.

``docker/browser/`` is vendored from the stealth-browser repo, which owns the
full suite for this module (conversion, article extraction, the API endpoint).
What is re-tested here is the subset istota's briefings depend on directly, so a
bad re-sync fails istota's own CI rather than surfacing as briefings quietly
reading front pages as dead again (ISSUE-192):

* ``_url_looks_like_index`` — decides whether ``--mode article`` is honoured or
  overridden to full, which is the difference between a headline grid arriving
  and being silently discarded; and
* ``_has_dominant_article`` — the content veto over that URL guess, whose
  exclusion of ``main`` / ``[role=main]`` is the one thing keeping the veto from
  firing on every hub page.

The browser app runs only inside its own Docker image. ``bs4`` happens to be
installed in the istota test env, but ``markdownify`` is a container-only dep,
so it is stubbed the way ``test_browser_chrome_watchdog.py`` stubs
``patchright``. Nothing here converts anything, so the stub is never called;
``trafilatura`` is imported lazily inside the extraction path and needs none.
"""

import sys
import types
from pathlib import Path
from unittest import mock

import pytest

# Stub markdownify before importing render -- render does
# `from markdownify import MarkdownConverter` at module top and subclasses it.
if "markdownify" not in sys.modules:
    _markdownify = types.ModuleType("markdownify")

    class _StubConverter:
        def __init__(self, **options):
            self.options = options

        def convert_hN(self, *a, **k):  # pragma: no cover - never converts here
            raise AssertionError("stub converter used for a real conversion")

    _markdownify.MarkdownConverter = _StubConverter
    sys.modules["markdownify"] = _markdownify

_BROWSER_DIR = Path(__file__).resolve().parent.parent / "docker" / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

render = pytest.importorskip(
    "render", reason="browser render module needs bs4",
)


PROSE = " ".join(["Firefighters worked through the night to contain the blaze."] * 12)

ARTICLE_HTML = f"""
<html><body>
  <nav><a href="/world">World</a></nav>
  <article><h1>Wildfires force evacuations</h1><p>{PROSE}</p><p>{PROSE}</p></article>
  <aside><a href="/sponsored">Sponsored</a></aside>
</body></html>
"""

HUB_CARDS_HTML = "".join([
    "<html><body><nav><a href='/world'>World</a></nav><h2>Top stories</h2>",
    *[
        f"<article><h3><a href='/story-{i}'>Story number {i}</a></h3>"
        f"<p>A teaser sentence for story {i}, of the length a card carries.</p>"
        "</article>"
        for i in range(12)
    ],
    "</body></html>",
])


class TestUrlIndexGuess:
    @pytest.mark.parametrize("url", [
        "https://www.reuters.com",
        "https://www.lemonde.fr/en/",
        "https://www.theguardian.com/world",
        "https://www.spiegel.de/international/",
        "https://apnews.com/hub/world-news",
    ])
    def test_section_fronts_read_as_index(self, url):
        """These are the briefing presets — misreading one loses its whole grid."""
        assert render._url_looks_like_index(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.reuters.com/world/berlin-pride-called-off-2026-07-25/",
        "https://www.theguardian.com/world/2026/jul/25/wildfires-force-evacuations",
    ])
    def test_dated_or_deep_slugs_read_as_articles(self, url):
        assert render._url_looks_like_index(url) is False


class TestDominantArticleVeto:
    def test_one_big_article_vetoes_the_url_guess(self):
        assert render._has_dominant_article(render._soup(ARTICLE_HTML)) is True

    def test_a_grid_of_cards_does_not_veto(self):
        assert render._has_dominant_article(render._soup(HUB_CARDS_HTML)) is False

    def test_main_wrapping_a_grid_does_not_veto(self):
        """`main` is excluded on purpose: a hub wraps its whole grid in one."""
        html = HUB_CARDS_HTML.replace("<h2>", "<main><h2>").replace(
            "</body>", "</main></body>",
        )
        assert render._has_dominant_article(render._soup(html)) is False

    def test_no_article_node_does_not_veto(self):
        html = "<html><body><p>Just prose, no article element.</p></body></html>"
        assert render._has_dominant_article(render._soup(html)) is False

    def test_teaser_length_article_does_not_veto(self):
        html = "<html><body><article><p>Three words here.</p></article></body></html>"
        assert render._has_dominant_article(render._soup(html)) is False

    def test_empty_page_does_not_veto(self):
        assert render._has_dominant_article(render._soup("")) is False


class TestPostprocess:
    def test_drops_a_repeat_separated_by_a_blank_line(self):
        assert render._postprocess("Ukraine\n\nUkraine\n\nOther") == "Ukraine\n\nOther"

    def test_keeps_a_repeat_with_content_between(self):
        assert render._postprocess("Ukraine\n\nGaza\n\nUkraine").count("Ukraine") == 2

    def test_identical_table_rows_survive(self):
        table = "| AAPL | 100 |\n| AAPL | 100 |"
        assert render._postprocess(table) == table


class TestTruncationContract:
    def test_marker_shape_is_what_the_briefing_source_strips(self):
        """briefings/sources/browse.py greps this footer out of the prompt."""
        from istota.briefings.sources.browse import _TRUNCATION_FOOTER_RE

        text, truncated = render._truncate("x" * 500, 100)
        assert truncated is True
        assert _TRUNCATION_FOOTER_RE.search(text) is not None
        assert _TRUNCATION_FOOTER_RE.sub("", text).strip() == "x" * 100

    def test_under_the_cap_is_untouched(self):
        assert render._truncate("short", 100) == ("short", False)

    def test_zero_disables_the_cap(self):
        assert render._truncate("x" * 500, 0) == ("x" * 500, False)


def test_stub_converter_was_never_actually_used():
    """Guards the premise of the markdownify stub above."""
    assert isinstance(sys.modules["markdownify"].MarkdownConverter, type)
    assert not isinstance(sys.modules["markdownify"], mock.MagicMock)
