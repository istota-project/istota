"""Tests for the newsletter HTML → link-preserving markdown converter.

``briefings/sources/_html.py`` is what lets a newsletter story be cited to its
own article: the old path ran the briefing skill's ``_strip_html``, which
deletes every ``<a href>`` and keeps only the anchor text, so the URL was gone
before the model ever saw it.
"""

from istota.briefings.sources._html import html_to_markdown, unwrap_redirect


_NEWSLETTER = """
<html>
  <head><style>.x { color: red }</style></head>
  <body>
    <div><a href="https://link.semafor.com/click/abc?url=https%3A%2F%2Fsemafor.com%2Fa%2Firan">Iran tensions escalate</a></div>
    <p>Iran's foreign minister warned that forces have their fingers on the trigger.</p>
    <div><a href="https://link.semafor.com/unsubscribe/xyz">Unsubscribe</a></div>
    <div><a href="https://semafor.com/view-in-browser/123">View in browser</a></div>
    <div><a href="https://twitter.com/intent/tweet?text=hi">Share on Twitter</a></div>
    <div><a href="mailto:editors@semafor.com">Email us</a></div>
    <img src="https://track.semafor.com/px.gif" width="1" height="1" />
  </body>
</html>
"""


class TestHtmlToMarkdown:
    def test_preserves_article_link_and_unwraps_redirect(self):
        out = html_to_markdown(_NEWSLETTER)
        assert "[Iran tensions escalate](https://semafor.com/a/iran)" in out

    def test_drops_noise_links_but_keeps_their_text(self):
        out = html_to_markdown(_NEWSLETTER)
        assert "unsubscribe/xyz" not in out
        assert "view-in-browser" not in out
        assert "twitter.com/intent" not in out
        # The words survive as plain text; only the destination is dropped.
        assert "Unsubscribe" in out
        assert "View in browser" in out

    def test_drops_non_http_schemes(self):
        out = html_to_markdown(_NEWSLETTER)
        assert "mailto:" not in out
        assert "Email us" in out

    def test_strips_style_and_tracking_pixel(self):
        out = html_to_markdown(_NEWSLETTER)
        assert "color: red" not in out
        assert "px.gif" not in out

    def test_body_text_survives(self):
        out = html_to_markdown(_NEWSLETTER)
        assert "fingers on the trigger" in out
        assert "<" not in out.replace("[", "").replace("]", "")

    def test_plain_text_passthrough(self):
        assert html_to_markdown("just words") == "just words"

    def test_empty_input(self):
        assert html_to_markdown("") == ""
        assert html_to_markdown(None) == ""

    def test_anchor_with_no_text_is_dropped_entirely(self):
        out = html_to_markdown(
            '<div><a href="https://x.com/a"><img src="p.png"/></a>after</div>'
        )
        assert "https://x.com/a" not in out
        assert "after" in out

    def test_nested_markup_in_anchor_is_flattened(self):
        out = html_to_markdown(
            '<p><a href="https://x.com/a"><span>Big <b>news</b></span></a></p>'
        )
        assert "[Big news](https://x.com/a)" in out

    def test_duplicate_links_keep_first_only(self):
        html = (
            '<p><a href="https://x.com/a">one</a></p>'
            '<p><a href="https://x.com/a">two</a></p>'
        )
        out = html_to_markdown(html)
        assert out.count("https://x.com/a") == 1
        assert "two" in out

    def test_link_cap(self):
        html = "".join(
            f'<p><a href="https://x.com/{i}">item {i}</a></p>' for i in range(10)
        )
        out = html_to_markdown(html, max_links=3)
        assert out.count("](https://x.com/") == 3
        # Text of the over-cap links is kept; only the destinations go.
        assert "item 9" in out

    def test_zero_max_links_means_unlimited(self):
        html = "".join(
            f'<p><a href="https://x.com/{i}">item {i}</a></p>' for i in range(8)
        )
        out = html_to_markdown(html, max_links=0)
        assert out.count("](https://x.com/") == 8

    def test_entities_in_url_are_decoded(self):
        out = html_to_markdown('<a href="https://x.com/a?b=1&amp;c=2">t</a>')
        assert "[t](https://x.com/a?b=1&c=2)" in out

    def test_brackets_in_anchor_text_are_neutralised(self):
        out = html_to_markdown('<a href="https://x.com/a">a [b] c</a>')
        assert "[a (b) c](https://x.com/a)" in out

    def test_very_long_anchor_text_is_truncated(self):
        label = "word " * 200
        out = html_to_markdown(f'<a href="https://x.com/a">{label}</a>')
        assert "](https://x.com/a)" in out
        # The label must not drag a whole paragraph into the link.
        start = out.index("[")
        assert out.index("](") - start < 260

    def test_malformed_html_does_not_raise(self):
        out = html_to_markdown('<div><a href="https://x.com/a">text<p>unclosed')
        assert "text" in out

    def test_url_with_space_or_paren_is_percent_encoded(self):
        out = html_to_markdown('<a href="https://x.com/a (b)">t</a>')
        assert "%20" in out or "https://x.com/a" in out
        assert " (b)" not in out.split("](")[-1]

    def test_regex_fallback_without_bleach(self, monkeypatch):
        """The link-preserving behaviour must not depend on the feeds extra."""
        import istota.briefings.sources._html as mod
        monkeypatch.setattr(mod, "_HAS_BLEACH", False)
        out = html_to_markdown(_NEWSLETTER)
        assert "[Iran tensions escalate](https://semafor.com/a/iran)" in out
        assert "unsubscribe/xyz" not in out

    def test_single_quoted_and_unquoted_href(self, monkeypatch):
        import istota.briefings.sources._html as mod
        monkeypatch.setattr(mod, "_HAS_BLEACH", False)
        out = html_to_markdown(
            "<a href='https://x.com/a'>one</a> <a href=https://x.com/b>two</a>"
        )
        assert "[one](https://x.com/a)" in out
        assert "[two](https://x.com/b)" in out

    def test_noise_anchor_text_drops_redirect_wrapped_link(self):
        """A tracking-wrapped unsubscribe hides the keyword — the label doesn't."""
        out = html_to_markdown(
            '<a href="https://link.x.com/click/deadbeef">Manage your preferences</a>'
        )
        assert "click/deadbeef" not in out
        assert "Manage your preferences" in out


class TestUnwrapRedirect:
    def test_url_param(self):
        assert unwrap_redirect(
            "https://link.x.com/c/1?url=https%3A%2F%2Freal.com%2Fa"
        ) == "https://real.com/a"

    def test_u_param(self):
        assert unwrap_redirect(
            "https://sendgrid.net/ls/click?u=https%3A%2F%2Freal.com%2Fb&upn=x"
        ) == "https://real.com/b"

    def test_redirect_and_target_params(self):
        assert unwrap_redirect(
            "https://t.co/x?redirect=https://real.com/c"
        ) == "https://real.com/c"
        assert unwrap_redirect(
            "https://t.co/x?target=https://real.com/d"
        ) == "https://real.com/d"

    def test_non_url_param_value_is_left_alone(self):
        """`u=` is also a plain user id — only an absolute URL is unwrapped."""
        wrapper = "https://sendgrid.net/ls/click?u=12345&upn=x"
        assert unwrap_redirect(wrapper) == wrapper

    def test_opaque_wrapper_is_kept(self):
        wrapper = "https://link.semafor.com/click/abcdef123"
        assert unwrap_redirect(wrapper) == wrapper

    def test_unknown_param_is_not_unwrapped(self):
        wrapper = "https://x.com/s?q=https://real.com/e"
        assert unwrap_redirect(wrapper) == wrapper

    def test_nested_unwrap_is_bounded(self):
        inner = "https://real.com/f"
        once = f"https://a.com/c?url=https%3A%2F%2Fb.com%2Fc%3Furl%3D{inner.replace(':', '%3A').replace('/', '%2F')}"
        assert unwrap_redirect(once) == inner

    def test_non_http_target_is_not_unwrapped(self):
        wrapper = "https://x.com/c?url=javascript:alert(1)"
        assert unwrap_redirect(wrapper) == wrapper


class TestEntityShapedQueryStrings:
    """A URL query must not be re-decoded by the flattening pass.

    `_strip_html` ends in ``html.unescape``, and `&copy` / `&reg` are valid
    entities even without a semicolon — so an already-decoded URL spliced in
    before that pass would silently lose part of its query.
    """

    def test_copy_param_survives(self):
        out = html_to_markdown('<a href="https://x.com/a?b=1&amp;copy=2">t</a>')
        assert "[t](https://x.com/a?b=1&copy=2)" in out
        assert "©" not in out

    def test_reg_param_survives(self):
        out = html_to_markdown('<a href="https://x.com/a?reg=1">t</a>')
        assert "[t](https://x.com/a?reg=1)" in out
        assert "®" not in out

    def test_no_placeholder_leaks_into_output(self):
        out = html_to_markdown(_NEWSLETTER)
        assert "\x02" not in out
        assert "istota-link-" not in out

    def test_body_text_placeholder_cannot_be_forged(self):
        """A body that literally contains the token text is left as text."""
        out = html_to_markdown(
            '<p>istota-link-0</p><a href="https://x.com/a">real</a>'
        )
        assert "istota-link-0" in out
        assert "[real](https://x.com/a)" in out
