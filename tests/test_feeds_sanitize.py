"""Tests for feed HTML/image helpers in ``istota.feeds.sanitize``.

Covers the image-dedup + hero-strip helpers that keep the reader from
painting the same image twice (hero + body) or N times (resolution
variants), plus the inline-``<video>`` rules. Pure functions — no network,
no feedparser.
"""

from istota.feeds.sanitize import (
    dedupe_image_variants,
    extract_images,
    image_identity,
    remove_images,
    sanitize_html,
)


class TestImageIdentity:
    def test_resolution_variants_share_identity(self):
        # Guardian serves the same photo at several widths; the width/signature
        # query params differ but the path is identical.
        a = "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=140&s=aaa"
        b = "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=700&s=ccc"
        assert image_identity(a) == image_identity(b)

    def test_distinct_images_differ(self):
        a = "https://cdn.example.com/one.jpg?width=700"
        b = "https://cdn.example.com/two.jpg?width=700"
        assert image_identity(a) != image_identity(b)

    def test_non_image_path_keeps_query(self):
        # A CDN that distinguishes images purely by query must NOT collapse.
        a = "https://cdn.example.com/image.php?id=1"
        b = "https://cdn.example.com/image.php?id=2"
        assert image_identity(a) != image_identity(b)


class TestTumblrImageIdentity:
    """Tumblr NPF media URLs vary by CDN shard and rendered size.

    ``https://64.media.tumblr.com/<media-key>/<post-key>-NN/s500x750/<hash>.jpg``
    — the leading ``64.`` is a shard and ``s500x750`` a size segment; the
    filename is a content hash. Neither the shard nor the size changes which
    picture it is (ISSUE-162).
    """

    BASE = "media.tumblr.com/aaa111/bbb222-01/{size}/deadbeef.jpg"

    def test_shard_and_size_variants_share_identity(self):
        a = "https://64." + self.BASE.format(size="s500x750")
        b = "https://72." + self.BASE.format(size="s1280x1920")
        assert image_identity(a) == image_identity(b)

    def test_shardless_host_matches_sharded(self):
        a = "https://64." + self.BASE.format(size="s500x750")
        b = "https://" + self.BASE.format(size="s500x750")
        assert image_identity(a) == image_identity(b)

    def test_distinct_content_hashes_differ(self):
        a = "https://64.media.tumblr.com/aaa111/bbb222-01/s500x750/deadbeef.jpg"
        b = "https://64.media.tumblr.com/aaa111/bbb222-01/s500x750/cafebabe.jpg"
        assert image_identity(a) != image_identity(b)

    def test_media_key_alone_does_not_merge(self):
        # Deliberate non-goal: same media-key, different content hash stays
        # distinct (could be a different crop/edit). See ISSUE-162.
        a = "https://64.media.tumblr.com/aaa111/bbb222-01/s500x750/deadbeef.jpg"
        b = "https://64.media.tumblr.com/aaa111/ccc333-02/s640x960/cafebabe.jpg"
        assert image_identity(a) != image_identity(b)

    def test_query_string_ignored(self):
        a = "https://64." + self.BASE.format(size="s500x750")
        b = "https://64." + self.BASE.format(size="s500x750") + "?v=2"
        assert image_identity(a) == image_identity(b)

    def test_non_tumblr_host_with_size_shaped_segment_untouched(self):
        # A path segment that merely *looks* like a tumblr size segment must
        # not be stripped on some other host.
        a = "https://cdn.example.com/s500x750/one.jpg"
        b = "https://cdn.example.com/s100x100/one.jpg"
        assert image_identity(a) != image_identity(b)


class TestDedupeImageVariants:
    def test_collapses_tumblr_size_variants_keeping_largest(self):
        small = "https://64.media.tumblr.com/aaa/bbb-01/s250x400/hash.jpg"
        large = "https://72.media.tumblr.com/aaa/bbb-01/s1280x1920/hash.jpg"
        assert dedupe_image_variants([small, large]) == [large]
        # Order of arrival must not change which one wins.
        assert dedupe_image_variants([large, small]) == [large]

    def test_collapses_variants_keeping_widest(self):
        urls = [
            "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=140&s=aaa",
            "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=460&s=bbb",
            "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=700&s=ccc",
        ]
        out = dedupe_image_variants(urls)
        assert out == [
            "https://i.guim.co.uk/img/media/abc/master/3049.jpg?width=700&s=ccc"
        ]

    def test_keeps_distinct_images_in_order(self):
        urls = [
            "https://cdn.example.com/a.jpg?width=100",
            "https://cdn.example.com/b.jpg?width=100",
        ]
        assert dedupe_image_variants(urls) == urls

    def test_empty(self):
        assert dedupe_image_variants([]) == []


class TestRemoveImages:
    def test_removes_matching_img_and_empty_wrappers(self):
        html = (
            '<p class="feature-image"><a href="https://p.com/a">'
            '<img src="https://p.com/cover.jpg"></a></p>'
            "<p>Looking to save on gear</p>"
        )
        out = remove_images(html, ["https://p.com/cover.jpg"])
        assert "cover.jpg" not in out
        assert "Looking to save on gear" in out
        # The now-empty <a>/<p> wrappers are cleaned up, no dangling link.
        assert "https://p.com/a" not in out

    def test_matches_by_identity_ignoring_resolution_query(self):
        html = '<p><img src="https://x.com/lead.jpg?width=1600"></p><p>body</p>'
        out = remove_images(html, ["https://x.com/lead.jpg?width=700"])
        assert "lead.jpg" not in out
        assert "body" in out

    def test_preserves_non_hero_inline_images(self):
        html = (
            '<p><img src="https://x.com/lead.jpg"></p><p>intro</p>'
            '<figure><img src="https://x.com/mid.jpg"></figure><p>more</p>'
        )
        out = remove_images(html, ["https://x.com/lead.jpg"])
        assert "lead.jpg" not in out
        assert "mid.jpg" in out
        assert extract_images(out) == ["https://x.com/mid.jpg"]

    def test_noop_without_targets(self):
        html = '<p><img src="https://x.com/a.jpg"></p>'
        assert remove_images(html, []) == html


class TestVideoPlayability:
    """A stored ``<video>`` must be startable.

    ``video`` has been an allowed tag for a while, but the attribute allowlist
    dropped everything that makes one usable: ``controls`` survived only if the
    author happened to set it, and ``loop``/``muted`` were stripped outright. A
    Tumblr GIF-as-mp4 — authored as an autoplaying muted loop with no controls,
    precisely so it does not look like a video player — therefore stored as a
    tag with no way to start it: a dead black box in the card and the reader.

    So ``controls`` is added when the author left it off, and the two
    attributes describing how it should play once started are kept.
    """

    def test_adds_controls_when_absent(self):
        out = sanitize_html('<video src="https://x.test/clip.mp4"></video>')
        assert "controls" in out
        assert 'src="https://x.test/clip.mp4"' in out

    def test_keeps_existing_controls_without_duplicating(self):
        out = sanitize_html('<video controls src="https://x.test/clip.mp4"></video>')
        assert out.count("controls") == 1

    def test_source_child_video_gets_controls(self):
        out = sanitize_html('<video><source src="https://x.test/clip.mp4"></video>')
        assert "<video controls>" in out

    def test_preserves_loop_and_muted(self):
        out = sanitize_html('<video loop muted src="https://x.test/clip.mp4"></video>')
        assert "loop" in out
        assert "muted" in out

    def test_video_mentioned_in_text_is_untouched(self):
        # Escaped markup is prose, not a tag; it must not sprout an attribute.
        out = sanitize_html("<p>Use the &lt;video&gt; element</p>")
        assert "controls" not in out


class TestVideoStripping:
    """What deliberately does *not* survive."""

    def test_strips_autoplay(self):
        # A grid of feed cards that all start playing on scroll is a different
        # request from "let me play this one".
        out = sanitize_html('<video autoplay src="https://x.test/clip.mp4"></video>')
        assert "autoplay" not in out

    def test_strips_intrinsic_dimensions(self):
        # The card and the reader bound a video with CSS; stored dimensions
        # would fight that and are what made a clip overflow its container.
        out = sanitize_html('<video width="1920" height="1080" src="https://x.test/c.mp4"></video>')
        assert "1920" not in out
        assert "1080" not in out

    def test_strips_event_handlers(self):
        out = sanitize_html('<video onerror="alert(1)" src="https://x.test/c.mp4"></video>')
        assert "onerror" not in out
        assert "alert(1)" not in out

    def test_content_without_video_is_unchanged(self):
        html = '<p>Hello <a href="https://example.com">link</a></p>'
        assert sanitize_html(html) == html
