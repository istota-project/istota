"""Tests for feed HTML/image helpers in ``istota.feeds.sanitize``.

Covers the image-dedup + hero-strip helpers that keep the reader from
painting the same image twice (hero + body) or N times (resolution
variants). Pure functions — no network, no feedparser.
"""

from istota.feeds.sanitize import (
    dedupe_image_variants,
    extract_images,
    image_identity,
    remove_images,
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


class TestDedupeImageVariants:
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
