"""Tests for the bounded cross-entry image-suppression rule (ISSUE-162).

A reblog is a genuinely distinct entry, so the entry always renders — only
the repeated *image tile* is suppressed, and only when the same picture was
already shown by a newer entry inside the look-back window. Pure logic; the
SQLite side lives in ``tests/test_feeds_db.py``.
"""

from __future__ import annotations

from istota.feeds.image_dedupe import (
    DEFAULT_WINDOW_DAYS,
    PageEntry,
    parse_seen_ts,
    plan_suppression,
)


DAY = 86400
IMG_A = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash-a.jpg"
IMG_A_VARIANT = "https://72.media.tumblr.com/aaa/bbb-01/s1280x1920/hash-a.jpg"
IMG_B = "https://64.media.tumblr.com/ccc/ddd-01/s500x750/hash-b.jpg"


def _entry(entry_id: int, seen_ts: int, urls: list[str]) -> PageEntry:
    return PageEntry(entry_id=entry_id, seen_ts=seen_ts, image_urls=urls)


class TestParseSeenTs:
    def test_iso_with_offset(self):
        assert parse_seen_ts("2026-07-16T10:00:00+00:00") == 1784196000

    def test_iso_with_z(self):
        assert parse_seen_ts("2026-07-16T10:00:00Z") == parse_seen_ts(
            "2026-07-16T10:00:00+00:00"
        )

    def test_naive_iso_is_utc(self):
        assert parse_seen_ts("2026-07-16T10:00:00") == parse_seen_ts(
            "2026-07-16T10:00:00+00:00"
        )

    def test_rfc822_fallback(self):
        assert parse_seen_ts("Thu, 16 Jul 2026 10:00:00 +0000") == parse_seen_ts(
            "2026-07-16T10:00:00+00:00"
        )

    def test_unparseable_is_none(self):
        assert parse_seen_ts("not a date") is None
        assert parse_seen_ts("") is None
        assert parse_seen_ts(None) is None


class TestPlanSuppression:
    def test_older_entry_loses_the_repeated_tile(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        older = _entry(1, 999 * DAY, [IMG_A])

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=14,
        )

        assert plan.get(1) == {IMG_A}
        assert 2 not in plan  # the newest carrier still renders it

    def test_resolution_variants_count_as_the_same_picture(self):
        newer = _entry(2, 1000 * DAY, [IMG_A_VARIANT])
        older = _entry(1, 999 * DAY, [IMG_A])

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=14,
        )

        assert plan.get(1) == {IMG_A}

    def test_repeat_outside_the_window_renders_normally(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        older = _entry(1, 980 * DAY, [IMG_A])  # 20 days earlier

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=14,
        )

        assert plan == {}

    def test_repeat_exactly_at_the_window_edge_is_suppressed(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        older = _entry(1, 986 * DAY, [IMG_A])  # exactly 14 days

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=14,
        )

        assert plan.get(1) == {IMG_A}

    def test_distinct_images_are_untouched(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        older = _entry(1, 999 * DAY, [IMG_B])

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=14,
        )

        assert plan == {}

    def test_only_the_repeated_url_is_dropped_from_a_multi_image_entry(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        older = _entry(1, 999 * DAY, [IMG_A, IMG_B])

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=14,
        )

        assert plan.get(1) == {IMG_A}

    def test_ties_on_timestamp_break_deterministically_by_id(self):
        a = _entry(1, 1000 * DAY, [IMG_A])
        b = _entry(2, 1000 * DAY, [IMG_A])

        plan = plan_suppression([a, b], _owners_for([a, b]), window_days=14)

        # Higher id wins the tile; the plan is identical whichever order the
        # page arrives in, so paging can't flip which card shows the image.
        assert plan == {1: {IMG_A}}
        assert plan_suppression([b, a], _owners_for([b, a]), window_days=14) == plan

    def test_an_owner_outside_the_page_still_suppresses(self):
        # The newer carrier was on a previous page; the current page only
        # holds the older entry, and must still hide the repeat.
        from istota.feeds.sanitize import image_identity

        older = _entry(1, 999 * DAY, [IMG_A])
        owners = [(image_identity(IMG_A), 2, 1000 * DAY)]

        plan = plan_suppression([older], owners, window_days=14)

        assert plan.get(1) == {IMG_A}

    def test_entry_never_suppresses_its_own_duplicate_listing(self):
        solo = _entry(1, 1000 * DAY, [IMG_A, IMG_A_VARIANT])

        plan = plan_suppression([solo], _owners_for([solo]), window_days=14)

        assert plan == {}

    def test_window_zero_disables_suppression(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        older = _entry(1, 999 * DAY, [IMG_A])

        plan = plan_suppression(
            [newer, older], _owners_for([newer, older]), window_days=0,
        )

        assert plan == {}

    def test_entries_without_a_timestamp_are_skipped(self):
        newer = _entry(2, 1000 * DAY, [IMG_A])
        undated = PageEntry(entry_id=1, seen_ts=None, image_urls=[IMG_A])

        plan = plan_suppression(
            [newer, undated], _owners_for([newer]), window_days=14,
        )

        assert plan == {}

    def test_default_window_is_two_weeks(self):
        assert DEFAULT_WINDOW_DAYS == 14


def _owners_for(entries: list[PageEntry]) -> list[tuple[str, int, int]]:
    """Build the owner rows the DB would return for these entries."""
    from istota.feeds.sanitize import image_identity

    rows: list[tuple[str, int, int]] = []
    for e in entries:
        if e.seen_ts is None:
            continue
        for url in e.image_urls:
            rows.append((image_identity(url), e.entry_id, e.seen_ts))
    return rows
