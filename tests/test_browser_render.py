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
  firing on every hub page; and
* the iframe splice (ISSUE-516), whose per-frame absolutization is the one
  place this module can hand back a URL that looks absolute and points nowhere,
  which is the class ``skill.md``'s own rules forbid.

The frame tests go through ``_splice_frames`` rather than ``to_markdown``,
because the stub below cannot convert. That is the right level anyway: the
correctness trap is in the soup, not in the markdown.

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
        """Enough of MarkdownConverter to drive `to_markdown` once.

        `convert_soup` returns the soup's text rather than markdown, which is
        all the frame-census assertions need: they are about the `frames` dict
        and the `notes`, not about the conversion. `convert_hN` still refuses,
        so the heuristics tests below keep the guarantee their docstring
        claims — that nothing here converts anything for real.
        """

        def __init__(self, **options):
            self.options = options

        def convert_soup(self, node):
            return node.get_text("\n")

        def convert_hN(self, *a, **k):  # pragma: no cover - never converts here
            raise AssertionError("stub converter used for a real conversion")

    _markdownify.MarkdownConverter = _StubConverter
    sys.modules["markdownify"] = _markdownify

# Three other browser test modules install a stub of their own, each guarded by
# the same `not in sys.modules` test, so whichever is imported first wins — and
# under xdist that differs per worker. Theirs have no `convert_soup`, which the
# frame-census tests below need, so the capability is added to whatever class
# ended up installed rather than to the one this module happens to define. Left
# alone if it is already there, which is the case when the real markdownify is
# installed.
_converter_cls = sys.modules["markdownify"].MarkdownConverter
if not hasattr(_converter_cls, "convert_soup"):
    _converter_cls.convert_soup = lambda self, node: node.get_text("\n")

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


class TestTheFrameSplice:
    """ISSUE-516. A bad re-sync of ``docker/browser/`` that reverts any of
    this fails here rather than in the container."""

    PAGE = (
        '<html><body><h1>Outer</h1>'
        '<iframe src="https://widget.example/embed"></iframe>'
        '<a href="page">Outer link</a></body></html>'
    )
    PAGE_URL = "https://news.example.com/world"

    def _spliced(self, page=None, frames=None, base=None):
        soup = render._soup(page or self.PAGE)
        placed = render._splice_frames(soup, frames or [], base or self.PAGE_URL)
        render._absolutize(soup, base or self.PAGE_URL)
        return placed, str(soup)

    def test_frame_content_lands_at_the_iframes_position(self):
        placed, html = self._spliced(frames=[{
            "url": "https://widget.example/embed",
            "html": "<html><body><p>Framed body.</p></body></html>",
        }])
        assert placed == 1
        assert "Framed body." in html
        assert html.index("Outer") < html.index("Framed body.") < html.index("Outer link")

    def test_a_frame_link_resolves_against_the_frame_not_the_page(self):
        _, html = self._spliced(frames=[{
            "url": "https://widget.example/embed",
            "html": '<html><body><a href="/inner">In</a></body></html>',
        }])
        assert 'href="https://widget.example/inner"' in html
        assert "news.example.com/inner" not in html

    def test_a_frames_base_does_not_become_the_pages_base(self):
        _, html = self._spliced(frames=[{
            "url": "https://widget.example/embed",
            "html": '<base href="https://cdn.frame.example/x/"><a href="deep">D</a>',
        }])
        assert 'href="https://news.example.com/page"' in html

    def test_an_unmatched_frame_is_not_spliced(self):
        placed, html = self._spliced(frames=[{
            "url": "https://other.example/gone",
            "html": "<html><body><p>Framed body.</p></body></html>",
        }])
        assert placed == 0
        assert "Framed body." not in html

    def test_the_census_is_in_the_response(self):
        # Driven through to_markdown rather than through _frame_notes, so it
        # pins the response *shape* the skill CLI hands to the model — the key
        # being present at all, and `found` counting what the walk classed as
        # content. An earlier version of this test called _frame_notes four
        # times and asserted nothing about the dict it names.
        result = render.to_markdown(
            self.PAGE, base_url=self.PAGE_URL,
            frames=[
                {"url": "https://widget.example/embed", "skip": None, "html": None},
                {"url": "https://ads.example/x", "skip": "noise", "html": None},
            ],
        )
        assert result["frames"]["found"] == 1
        assert result["frames"]["included"] == 0
        assert result["frames"]["capped"] is False
        assert result["frames"]["urls"] == ["https://widget.example/embed"]
        assert any("--include-frames" in n for n in result["notes"])

    def test_a_page_with_no_frames_still_carries_the_key(self):
        # A caller has to be able to tell "no frames" from a container that
        # predates the census, and an absent key is how it tells.
        result = render.to_markdown(self.PAGE, base_url=self.PAGE_URL)
        assert result["frames"] == {
            "found": 0, "included": 0, "capped": False, "urls": [],
        }

    def test_included_counts_what_is_in_the_markdown(self):
        result = render.to_markdown(
            self.PAGE, base_url=self.PAGE_URL,
            frames=[{
                "url": "https://widget.example/embed", "skip": None,
                "html": "<html><body><p>Framed body.</p></body></html>",
            }],
            include_frames=True,
        )
        assert result["frames"]["included"] == 1
        assert "Framed body." in result["markdown"]


class TestTheFrameCensusWalk:
    """The always-on half of ISSUE-516, which had no istota-side guard at all.

    ``browsing`` imports ``xdotool`` at module scope, which is container-only —
    stubbed the way ``markdownify`` is above, and never called, since nothing
    here drives a browser.
    """

    @staticmethod
    def _browsing():
        if "xdotool" not in sys.modules:
            stub = types.ModuleType("xdotool")
            stub.xdo = stub.xdo_key = lambda *a, **k: None
            sys.modules["xdotool"] = stub
        return pytest.importorskip("browsing", reason="container module")

    class _Element:
        def __init__(self, visible=True, box=(800, 600)):
            self._visible, self._box = visible, box

        def is_visible(self):
            return self._visible

        def bounding_box(self):
            return None if self._box is None else {
                "width": self._box[0], "height": self._box[1],
            }

    class _Frame:
        def __init__(self, url, parent=None, element=None):
            self.url, self._parent = url, parent
            self._element = element

        @property
        def parent_frame(self):
            return self._parent

        def frame_element(self):
            return self._element

    class _Page:
        def __init__(self):
            self.main_frame = TestTheFrameCensusWalk._Frame("https://news.example/x")
            self.frames = [self.main_frame]

    def _page_with(self, *frames):
        page = self._Page()
        for f in frames:
            f._parent = page.main_frame
        page.frames = [page.main_frame, *frames]
        return page

    def test_a_content_frame_is_kept_and_the_main_frame_is_not_a_record(self):
        browsing = self._browsing()
        page = self._page_with(
            self._Frame("https://widget.example/embed", element=self._Element()),
        )
        records, capped = browsing.survey_frames(page)
        assert [r["skip"] for r in records] == [None]
        assert capped is False

    @pytest.mark.parametrize("url,expected", [
        ("about:blank", "blank"),
        ("https://tpc.googlesyndication.com/safeframe/1/html", "noise"),
        ("https://cdn.cookielaw.org/consent/banner.html", "noise"),
        ("https://widget.example/embed?ref=taboola.com", None),
    ])
    def test_the_url_only_verdicts(self, url, expected):
        browsing = self._browsing()
        page = self._page_with(self._Frame(url, element=self._Element()))
        records, _ = browsing.survey_frames(page)
        assert records[0]["skip"] == expected

    def test_a_tracking_pixel_is_skipped(self):
        browsing = self._browsing()
        page = self._page_with(
            self._Frame("https://widget.example/px", element=self._Element(box=(1, 1))),
        )
        assert browsing.survey_frames(page)[0][0]["skip"] == "small"

    def test_a_nested_frame_is_named_not_descended(self):
        browsing = self._browsing()
        page = self._Page()
        outer = self._Frame("https://widget.example/embed", element=self._Element())
        outer._parent = page.main_frame
        inner = self._Frame("https://deep.example/inner", element=self._Element())
        inner._parent = outer
        page.frames = [page.main_frame, outer, inner]
        records, _ = browsing.survey_frames(page)
        assert {r["url"]: r["skip"] for r in records}[
            "https://deep.example/inner"
        ] == "nested"

    def test_the_probe_budget_is_not_spent_on_url_only_verdicts(self):
        browsing = self._browsing()
        ads = [self._Frame(f"https://a{i}.doubleclick.net/f") for i in range(40)]
        real = self._Frame("https://widget.example/embed", element=self._Element())
        records, capped = browsing.survey_frames(self._page_with(*ads, real), limit=5)
        assert capped is False
        assert [r["url"] for r in records if r["skip"] is None] == [
            "https://widget.example/embed",
        ]


def test_stub_converter_was_never_actually_used():
    """Guards the premise of the markdownify stub above."""
    assert isinstance(sys.modules["markdownify"].MarkdownConverter, type)
    assert not isinstance(sys.modules["markdownify"], mock.MagicMock)
