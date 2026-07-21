"""Tests for the briefings source resolvers (fail-soft contract)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from istota.briefings.sources import GatheredSource, SourceContext, resolve_source
from istota.config import BrowserConfig, Config, EmailConfig, UserConfig


def _ctx(tmp_path, *, conn=None, now=None, browser=False, users=("stefan",)):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        browser=BrowserConfig(enabled=browser, api_url="http://browser:9223"),
        users={u: UserConfig(timezone="UTC") for u in users},
    )
    return SourceContext(app_config=cfg, user_id="stefan", conn=conn, now=now)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_unknown_kind_fails_soft(self, tmp_path):
        gs = resolve_source("bogus", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "unknown" in gs.provenance.lower()

    def test_resolver_exception_is_caught(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        def boom(config, ctx):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(browse_mod, "resolve", boom)
        # Force cache rebuild so the patched resolver is picked up.
        from istota.briefings import sources as srcpkg
        srcpkg._RESOLVERS._cache = None
        gs = resolve_source("browse", {}, _ctx(tmp_path))
        srcpkg._RESOLVERS._cache = None
        assert gs.ok is False


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


class TestRss:
    def test_feeds_off_returns_note(self, tmp_path):
        # Feeds module disabled for the user → soft-degrade.
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"stefan": UserConfig(disabled_modules=["feeds"])},
        )
        ctx = SourceContext(app_config=cfg, user_id="stefan")
        gs = resolve_source("rss", {"feed_ref": {"kind": "category", "value": "world"}}, ctx)
        assert gs.ok is False
        assert "feeds" in gs.provenance.lower()

    def test_reads_recent_entries(self, tmp_path):
        # Real feeds DB with one recent entry.
        from istota.feeds import db as fdb
        from istota.feeds.models import EntryRecord

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"stefan": UserConfig()},
        )
        fctx_db = cfg.module_db_path("stefan", "feeds")
        fdb.init_db(fctx_db)
        with fdb.connect(fctx_db) as conn:
            cat = fdb.upsert_category(conn, "world", "World")
            feed_id = fdb.upsert_feed(
                conn, url="http://x/feed", title="X", site_url="http://x",
                source_type="rss", category_id=cat, poll_interval_minutes=30,
            )
            now = datetime.now(timezone.utc).isoformat()
            fdb.insert_entries(conn, feed_id, [
                EntryRecord(id=0, feed_id=feed_id, guid="g1", title="Recent",
                            url="http://x/1", author=None, content_html=None,
                            content_text="body", published_at=now, fetched_at=now),
            ])
            conn.commit()

        ctx = SourceContext(app_config=cfg, user_id="stefan")
        gs = resolve_source(
            "rss",
            {"feed_ref": {"kind": "category", "value": "world"}, "limit": 5},
            ctx,
        )
        assert gs.ok is True
        assert gs.items[0]["title"] == "Recent"

    def test_missing_category_note(self, tmp_path):
        from istota.feeds import db as fdb

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"stefan": UserConfig()},
        )
        fdb.init_db(cfg.module_db_path("stefan", "feeds"))
        ctx = SourceContext(app_config=cfg, user_id="stefan")
        gs = resolve_source(
            "rss", {"feed_ref": {"kind": "category", "value": "ghost"}}, ctx,
        )
        assert gs.ok is False
        assert "not found" in gs.provenance.lower()


# ---------------------------------------------------------------------------
# Email (shared pool)
# ---------------------------------------------------------------------------


class _Env:
    """Minimal envelope duck-type for ownership resolution + rendering."""

    def __init__(self, uid, sender, subject="s", to=(), cc=(), references=None):
        self.id = uid
        self.sender = sender
        self.subject = subject
        self.date = "2026-07-20"
        self.snippet = "snippet"
        self.to = to
        self.cc = cc
        self.references = references


class _Full:
    def __init__(self, uid, body):
        self.id = uid
        self.body = body


class TestEmail:
    def test_fail_closed_without_conn(self, tmp_path):
        gs = resolve_source("email", {"mode": "shared"}, _ctx(tmp_path, conn=None))
        assert gs.ok is False
        assert "ownership" in gs.provenance.lower()

    def test_shared_pool_filters_owned(self, tmp_path, monkeypatch):
        import istota.briefings.sources.email as email_mod

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"stefan": UserConfig(email_addresses=["stefan@x.com"])},
        )
        ctx = SourceContext(app_config=cfg, user_id="stefan", conn=object())

        shared = _Env("1", "news@semafor.com")
        owned = _Env("2", "stefan@x.com")  # owned by a configured user

        # The resolver imports these lazily from their source modules, so patch
        # at the source (the from-import at call time binds the patched name).
        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr(
            "istota.skills.email.list_emails",
            lambda **kw: [shared, owned],
        )
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full("1", "Semafor body")],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None if e.sender == "news@semafor.com" else "stefan",
        )

        gs = _call_email(email_mod, {"mode": "shared"}, ctx)
        assert gs.ok is True
        assert len(gs.items) == 1
        assert gs.items[0]["sender"] == "news@semafor.com"
        assert "Semafor body" in gs.items[0]["body"]

    def test_senders_mode_narrows(self, tmp_path, monkeypatch):
        import istota.briefings.sources.email as email_mod

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"stefan": UserConfig()},
        )
        ctx = SourceContext(app_config=cfg, user_id="stefan", conn=object())
        e1 = _Env("1", "news@semafor.com")
        e2 = _Env("2", "digest@axios.com")

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr("istota.skills.email.list_emails", lambda **kw: [e1, e2])
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full("1", "b1"), _Full("2", "b2")],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None,
        )
        gs = _call_email(
            email_mod,
            {"mode": "senders", "senders": ["*@semafor.com"]},
            ctx,
        )
        assert gs.ok is True
        assert [i["sender"] for i in gs.items] == ["news@semafor.com"]

    def test_windowed_fetch_no_message_cap(self, tmp_path, monkeypatch):
        """Regression: the shared-pool fetch must use a date window with NO
        fixed message cap — a newsletter beyond the old 100th recent message
        still surfaces."""
        import istota.briefings.sources.email as email_mod

        captured = {}

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"stefan": UserConfig()},
        )
        ctx = SourceContext(app_config=cfg, user_id="stefan", conn=object())

        many = [_Env(str(i), f"n{i}@x.com") for i in range(150)]

        def fake_list(**kw):
            captured["limit"] = kw.get("limit")
            captured["criteria"] = kw.get("criteria")
            return many

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr("istota.skills.email.list_emails", fake_list)
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full(str(i), f"body{i}") for i in range(150)],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None,
        )
        gs = _call_email(email_mod, {"mode": "shared"}, ctx)
        # No fixed cap: all 150 windowed messages kept, limit passed as None.
        assert captured["limit"] is None
        assert len(gs.items) == 150

    def test_hour_window_trims_day_granular_surplus(self, tmp_path, monkeypatch):
        """IMAP date_gte is day-granular, so the server fetch is over-inclusive.
        A message older than the exact hour cutoff is trimmed client-side so the
        'past Nh' provenance stays honest (a datetime-dated envelope, unlike the
        string-dated mock used elsewhere, exercises the filter)."""
        import istota.briefings.sources.email as email_mod

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"stefan": UserConfig()},
        )
        now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        ctx = SourceContext(app_config=cfg, user_id="stefan", conn=object(), now=now)

        recent = _Env("1", "fresh@x.com")
        recent.date = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)  # within 12h
        stale = _Env("2", "stale@x.com")
        stale.date = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)  # >12h, day surplus

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr(
            "istota.skills.email.list_emails", lambda **kw: [recent, stale]
        )
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full",
            lambda **kw: [_Full("1", "fresh body")],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner",
            lambda config, conn, e: None,
        )

        gs = _call_email(email_mod, {"mode": "shared", "lookback_hours": 12}, ctx)
        assert gs.ok is True
        assert [i["sender"] for i in gs.items] == ["fresh@x.com"]  # stale trimmed
        assert "past 12h" in gs.provenance


def _call_email(email_mod, config, ctx):
    """Invoke the email resolver directly (bypassing the lazy dispatcher cache
    so monkeypatched names are used)."""
    return email_mod.resolve(config, ctx)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------


class TestBrowse:
    def test_browser_off_note(self, tmp_path):
        gs = resolve_source("browse", {"preset": "ap"}, _ctx(tmp_path, browser=False))
        assert gs.ok is False
        assert "browser" in gs.provenance.lower()

    def test_preset_fetch(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            def json(self):
                return {"status": "ok", "text": "Headline one. Headline two."}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is True
        assert "AP News" in gs.text
        assert "Headline one" in gs.text

    def test_custom_url(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            def json(self):
                return {"status": "ok", "text": "custom page"}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"url": "https://example.com"}, _ctx(tmp_path, browser=True))
        assert gs.ok is True
        assert "example.com" in gs.text

    def test_unknown_preset(self, tmp_path):
        gs = resolve_source("browse", {"preset": "nope"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False


# ---------------------------------------------------------------------------
# Builtins — todos / reminders / notes (path is a source property)
# ---------------------------------------------------------------------------


def _write_workspace_file(cfg: Config, rel: str, content: str):
    path = cfg.nextcloud_mount_path / rel.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestBuiltinTodos:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("todos", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_todo_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        rel = "custom/mytodos.md"
        p = ctx.app_config.nextcloud_mount_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("- [ ] custom item\n")
        gs = resolve_source("todos", {"path": rel}, ctx)
        assert gs.ok is True
        assert gs.items[0]["text"] == "- [ ] custom item"


class TestBuiltinReminders:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("reminders", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_reminders_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("reminders", {"path": "reminders.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        from istota import db
        db.init_db(ctx.app_config.db_path)
        rel = "my/reminders.md"
        p = ctx.app_config.nextcloud_mount_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("Drink water\n\nStand up straight\n")
        gs = resolve_source("reminders", {"path": rel}, ctx)
        assert gs.ok is True
        assert gs.text in ("Drink water", "Stand up straight")


class TestBuiltinNotes:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("notes", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_notes(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("notes", {"path": "NOTES.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        rel = "my/agenda.md"
        p = ctx.app_config.nextcloud_mount_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("agenda item")
        gs = resolve_source("notes", {"path": rel}, ctx)
        assert gs.ok is True
        assert "agenda item" in gs.text


# ---------------------------------------------------------------------------
# Builtins — markets (byte-identical wrap)
# ---------------------------------------------------------------------------


class TestBuiltinMarkets:
    def test_wraps_market_data(self, tmp_path, monkeypatch):
        import istota.briefings.sources.builtins as bi

        # A weekday morning so quotes are fetched.
        monday_morning = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        ctx = _ctx(tmp_path, now=monday_morning)

        import istota.skills.briefing as briefing_mod
        monkeypatch.setattr(
            briefing_mod, "_fetch_market_data",
            lambda mc, mode: "📈 MARKETS\nES=F +0.5%",
        )
        gs = bi.resolve_markets({"futures": ["ES=F"]}, ctx)
        assert gs.ok is True
        assert "ES=F" in gs.text

    def test_weekend_no_quotes(self, tmp_path):
        import istota.briefings.sources.builtins as bi
        saturday = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
        ctx = _ctx(tmp_path, now=saturday)
        gs = bi.resolve_markets({}, ctx)
        assert gs.ok is False
        assert "weekend" in gs.provenance.lower()
