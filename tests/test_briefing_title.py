"""Tests for deterministic briefing titles.

A briefing's title is composed from its configured ``title`` (falling back to a
humanized briefing name) plus the run date in the user's timezone. The model no
longer supplies it, so archive / email / ntfy can't disagree.
"""

from datetime import datetime, timezone

from istota.briefings.generate import (
    default_briefing_title,
    format_briefing_title,
    resolve_briefing_title,
)
from istota.config import BriefingConfig, Config, UserConfig


class TestDefaultBriefingTitle:
    def test_humanizes_a_slug(self):
        assert default_briefing_title("morning") == "Morning Briefing"
        assert default_briefing_title("evening") == "Evening Briefing"

    def test_splits_separators(self):
        assert default_briefing_title("weekly-digest") == "Weekly Digest Briefing"
        assert default_briefing_title("weekly_digest") == "Weekly Digest Briefing"

    def test_does_not_double_the_word_briefing(self):
        assert default_briefing_title("morning-briefing") == "Morning Briefing"
        assert default_briefing_title("Daily Brief") == "Daily Brief"

    def test_preserves_existing_capitalization(self):
        # An acronym in the name must not be lowercased by title-casing.
        assert default_briefing_title("EU markets") == "EU Markets Briefing"

    def test_blank_name_falls_back(self):
        assert default_briefing_title("") == "Briefing"
        assert default_briefing_title("   ") == "Briefing"


class TestFormatBriefingTitle:
    def test_appends_the_date(self):
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert format_briefing_title("Morning Briefing", when) == (
            "Morning Briefing — Monday, 27 July"
        )

    def test_day_is_not_zero_padded(self):
        when = datetime(2026, 7, 5, 6, 0, tzinfo=timezone.utc)
        assert format_briefing_title("Morning Briefing", when).endswith(
            "Sunday, 5 July"
        )

    def test_blank_label_falls_back(self):
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert format_briefing_title("  ", when) == "Briefing — Monday, 27 July"

    def test_is_deterministic(self):
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert format_briefing_title("X", when) == format_briefing_title("X", when)


def _config(name: str = "morning", **briefing_kwargs) -> Config:
    briefing = BriefingConfig(name=name, cron="0 6 * * *", **briefing_kwargs)
    return Config(
        users={"alice": UserConfig(timezone="Europe/Lisbon", briefings=[briefing])},
    )


class TestResolveBriefingTitle:
    def test_uses_the_configured_title(self):
        cfg = _config(title="Daily Rundown")
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert resolve_briefing_title(cfg, "alice", "morning", when) == (
            "Daily Rundown — Monday, 27 July"
        )

    def test_falls_back_to_the_briefing_name(self):
        cfg = _config()
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert resolve_briefing_title(cfg, "alice", "morning", when) == (
            "Morning Briefing — Monday, 27 July"
        )

    def test_unknown_briefing_still_yields_a_title(self):
        cfg = _config()
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert resolve_briefing_title(cfg, "alice", "weekly", when) == (
            "Weekly Briefing — Monday, 27 July"
        )

    def test_unknown_user_still_yields_a_title(self):
        cfg = _config()
        when = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
        assert resolve_briefing_title(cfg, "nobody", "morning", when) == (
            "Morning Briefing — Monday, 27 July"
        )

    def test_date_is_rendered_in_the_user_timezone(self):
        """A 23:30 UTC run is already the next day in Lisbon (+02:00)."""
        cfg = _config(title="Evening Wrap")
        when = datetime(2026, 7, 27, 23, 30, tzinfo=timezone.utc)
        assert resolve_briefing_title(cfg, "alice", "morning", when) == (
            "Evening Wrap — Tuesday, 28 July"
        )

    def test_naive_datetime_is_treated_as_utc(self):
        cfg = _config(title="Evening Wrap")
        naive = datetime(2026, 7, 27, 23, 30)
        assert resolve_briefing_title(cfg, "alice", "morning", naive) == (
            "Evening Wrap — Tuesday, 28 July"
        )

    def test_defaults_to_now_when_no_datetime_given(self):
        cfg = _config(title="Daily Rundown")
        assert resolve_briefing_title(cfg, "alice", "morning", None).startswith(
            "Daily Rundown — "
        )


class TestBriefingTitleForTask:
    def _task(self, **kw):
        from istota.db import Task

        defaults = dict(
            id=5, status="completed", source_type="briefing", user_id="alice",
            prompt="Generate a morning briefing for user alice.",
            conversation_token="", briefing_name="evening",
            created_at="2026-07-27 18:00:00",
        )
        defaults.update(kw)
        return Task(**defaults)

    def test_dates_from_the_task_creation_time(self):
        from istota.scheduler import briefing_title_for_task

        cfg = _config("evening", title="Evening Wrap")
        assert briefing_title_for_task(cfg, self._task()) == (
            "Evening Wrap — Monday, 27 July"
        )

    def test_ignores_the_clock_derived_wording_in_the_prompt(self):
        """The prompt says "morning"; the briefing is the evening one.

        The old email path regex-scraped that prompt line, so an evening
        briefing that fired before noon was mailed as "Morning Briefing".
        """
        from istota.scheduler import briefing_title_for_task

        cfg = _config("evening")
        title = briefing_title_for_task(cfg, self._task())
        assert title.startswith("Evening Briefing")
        assert "Morning" not in title

    def test_unparseable_created_at_still_yields_a_title(self):
        from istota.scheduler import briefing_title_for_task

        cfg = _config("evening", title="Evening Wrap")
        assert briefing_title_for_task(
            cfg, self._task(created_at="not-a-date"),
        ).startswith("Evening Wrap — ")

    def test_every_surface_gets_the_same_string(self):
        """Archive, email and ntfy call this independently — they must agree."""
        from istota.scheduler import briefing_title_for_task

        cfg = _config("evening", title="Evening Wrap")
        task = self._task()
        assert (
            briefing_title_for_task(cfg, task)
            == briefing_title_for_task(cfg, task)
            == "Evening Wrap — Monday, 27 July"
        )


class TestPromptDropsSubject:
    """The model no longer supplies a subject — the title is computed."""

    def test_prompt_does_not_ask_for_a_subject(self, tmp_path):
        from istota import db as fdb
        from istota.briefings import db as bdb
        from istota.briefings import ensure_initialised, resolve_for_user
        from istota.briefings.generate import assemble_briefing_input

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig(timezone="UTC")},
        )
        ctx = resolve_for_user("alice", cfg)
        ensure_initialised(ctx, app_config=cfg)
        fdb.init_db(cfg.db_path)
        with bdb.connect(ctx.db_path) as conn:
            bid = bdb.add_block(conn, briefing_name="morning", title="News")
            bdb.add_source(conn, block_id=bid, kind="todos", config={"path": "x.md"})
            conn.commit()

        with fdb.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "morning", cfg, conn=conn)

        assert result is not None
        assert '"subject"' not in result.prompt
        assert '"body"' in result.prompt
