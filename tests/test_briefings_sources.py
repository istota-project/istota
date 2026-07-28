"""Tests for the briefings source resolvers (fail-soft contract)."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from istota.briefings.sources import GatheredSource, SourceContext, resolve_source
from istota.config import BrowserConfig, Config, EmailConfig, UserConfig


def _ctx(tmp_path, *, conn=None, now=None, browser=False, users=("alice",),
         briefings=None):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        browser=BrowserConfig(enabled=browser, api_url="http://browser:9223"),
        users={u: UserConfig(timezone="UTC") for u in users},
    )
    if briefings is not None:
        cfg.briefings = briefings
    return SourceContext(app_config=cfg, user_id="alice", conn=conn, now=now)


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
            users={"alice": UserConfig(disabled_modules=["feeds"])},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice")
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
            users={"alice": UserConfig()},
        )
        fctx_db = cfg.module_db_path("alice", "feeds")
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

        ctx = SourceContext(app_config=cfg, user_id="alice")
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
            users={"alice": UserConfig()},
        )
        fdb.init_db(cfg.module_db_path("alice", "feeds"))
        ctx = SourceContext(app_config=cfg, user_id="alice")
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
            users={"alice": UserConfig(email_addresses=["alice@x.com"])},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())

        shared = _Env("1", "news@semafor.com")
        owned = _Env("2", "alice@x.com")  # owned by a configured user

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
            lambda config, conn, e: None if e.sender == "news@semafor.com" else "alice",
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
            users={"alice": UserConfig()},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())
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
            users={"alice": UserConfig()},
        )
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())

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
            users={"alice": UserConfig()},
        )
        now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object(), now=now)

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

    def test_preset_fetch_uses_markdown(self, tmp_path, monkeypatch):
        """Markdown, so a headline keeps its URL (ISSUE-192)."""
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "status": "ok",
                    "markdown": "## Top\n\n* [Headline one](https://apnews.com/a)",
                }

        def _post(url, **kwargs):
            calls.append((url, kwargs["json"]))
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))

        assert gs.ok is True
        assert "AP News" in gs.text
        assert "[Headline one](https://apnews.com/a)" in gs.text
        assert calls[0][0].endswith("/render")
        assert calls[0][1]["mode"] == "full"
        assert calls[0][1]["max_chars"] == browse_mod._MARKDOWN_MAX_CHARS

    def test_article_mode_forwarded(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "body text"}

        def _post(url, **kwargs):
            calls.append(kwargs["json"])
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        browse_mod.resolve(
            {"url": "https://example.com/story", "mode": "article", "max_chars": 4000},
            _ctx(tmp_path, browser=True),
        )
        assert calls[0]["mode"] == "article"
        assert calls[0]["max_chars"] == 4000

    def test_unknown_mode_falls_back_to_full(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "body text"}

        def _post(url, **kwargs):
            calls.append(kwargs["json"])
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        browse_mod.resolve(
            {"url": "https://example.com", "mode": "readable"},
            _ctx(tmp_path, browser=True),
        )
        assert calls[0]["mode"] == "full"

    def test_operator_budget_caps_the_markdown_request(self, tmp_path, monkeypatch):
        """[briefings] max_browse_chars is the knob; a source's own wins over it."""
        from istota.config import BriefingsModuleConfig

        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "body text"}

        def _post(url, **kwargs):
            calls.append(kwargs["json"])
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        ctx = _ctx(
            tmp_path, browser=True,
            briefings=BriefingsModuleConfig(max_browse_chars=3000),
        )
        browse_mod.resolve({"preset": "ap"}, ctx)
        assert calls[0]["max_chars"] == 3000

        browse_mod.resolve({"preset": "ap", "max_chars": 8000}, ctx)
        assert calls[1]["max_chars"] == 8000

    def test_truncation_footer_is_kept_out_of_the_prompt(self, tmp_path, monkeypatch):
        """/render's footer names CLI flags that mean nothing to the synthesis model."""
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "status": "ok",
                    "truncated": True,
                    "markdown": (
                        "## Top\n\n* [Headline](https://apnews.com/a)\n\n"
                        "[Markdown truncated at 20000 characters — "
                        "raise --max-chars or switch to --mode article]"
                    ),
                }

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))

        assert gs.ok is True
        assert "[Headline](https://apnews.com/a)" in gs.text
        assert "--max-chars" not in gs.text
        assert "Markdown truncated" not in gs.text
        # The fact survives, as provenance rather than an instruction.
        assert "truncated" in gs.provenance

    def test_content_is_marked_untrusted(self, tmp_path, monkeypatch):
        """An arbitrary web page — assembly wraps it in the do-not-follow frame."""
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "## Top"}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.untrusted is True

    def test_client_timeout_outlives_the_container_watchdog(self):
        """Else the client gives up first and the container works on a dead request."""
        import istota.briefings.sources.browse as browse_mod

        # BROWSE_WATCHDOG_DEADLINE_S in docker/browser/browse_api.py.
        assert browse_mod._FETCH_TIMEOUT > 90

    def test_fetches_are_serialized_against_the_single_threaded_browser(
        self, tmp_path, monkeypatch,
    ):
        import threading

        import istota.briefings.sources.browse as browse_mod

        concurrent = []
        active = 0
        guard = threading.Lock()

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "## Top"}

        def _post(*a, **k):
            nonlocal active
            with guard:
                active += 1
                concurrent.append(active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return _Resp()

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        ctx = _ctx(tmp_path, browser=True)
        threads = [
            threading.Thread(target=browse_mod.resolve, args=({"preset": "ap"}, ctx))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert concurrent, "no fetch ran"
        assert max(concurrent) == 1

    def test_a_source_that_never_gets_the_browser_fails_soft(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        monkeypatch.setattr(browse_mod, "_QUEUE_WAIT_TIMEOUT", 0.01)
        browse_mod._BROWSER_LOCK.acquire()
        try:
            gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        finally:
            browse_mod._BROWSER_LOCK.release()

        assert gs.ok is False
        assert "busy" in gs.provenance

    def test_the_lock_is_released_when_a_fetch_raises(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        def _boom(*a, **k):
            raise RuntimeError("browser down")

        monkeypatch.setattr(browse_mod.httpx, "post", _boom)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False
        assert browse_mod._BROWSER_LOCK.acquire(timeout=1) is True
        browse_mod._BROWSER_LOCK.release()

    def test_untruncated_render_has_a_plain_provenance(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "## Top", "truncated": False}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.provenance == "frontpage of AP News"

    def test_falls_back_to_text_on_old_browser_image(self, tmp_path, monkeypatch):
        """A container predating /render 404s — degrade, don't fail the source."""
        import istota.briefings.sources.browse as browse_mod

        calls = []

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def _post(url, **kwargs):
            calls.append(url)
            if url.endswith("/render"):
                return _Resp(404, {})
            return _Resp(200, {"status": "ok", "text": "Headline one. Headline two."})

        monkeypatch.setattr(browse_mod.httpx, "post", _post)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))

        assert gs.ok is True
        assert "Headline one" in gs.text
        assert [c.rsplit("/", 1)[-1] for c in calls] == ["render", "browse"]

    def test_custom_url(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "custom page"}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"url": "https://example.com"}, _ctx(tmp_path, browser=True))
        assert gs.ok is True
        assert "example.com" in gs.text

    def test_empty_render_is_not_ok(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        class _Resp:
            status_code = 200

            def json(self):
                return {"status": "ok", "markdown": "   "}

        monkeypatch.setattr(browse_mod.httpx, "post", lambda *a, **k: _Resp())
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False
        assert "no content" in gs.provenance

    def test_fetch_failure_is_soft(self, tmp_path, monkeypatch):
        import istota.briefings.sources.browse as browse_mod

        def _boom(*a, **k):
            raise RuntimeError("browser down")

        monkeypatch.setattr(browse_mod.httpx, "post", _boom)
        gs = browse_mod.resolve({"preset": "ap"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False
        assert "fetch failed" in gs.provenance

    def test_unknown_preset(self, tmp_path):
        gs = resolve_source("browse", {"preset": "nope"}, _ctx(tmp_path, browser=True))
        assert gs.ok is False

    def test_presets_well_formed(self):
        from istota.briefings.sources.browse import BROWSE_PRESETS

        assert BROWSE_PRESETS, "expected bundled presets"
        for key, preset in BROWSE_PRESETS.items():
            assert key == key.lower() and " " not in key, f"bad slug {key!r}"
            assert preset["name"], f"{key} missing name"
            assert preset["url"].startswith("https://"), f"{key} url not https"
        # The core reputable set must remain available as pick-list keys.
        assert {"ap", "reuters", "guardian", "bbc"} <= set(BROWSE_PRESETS)


# ---------------------------------------------------------------------------
# Builtins — todos / reminders / notes (path is a source property)
# ---------------------------------------------------------------------------


def _write_user_file(ctx, rel: str, content: str):
    """Write a file relative to the user's own /Users/<uid>/ folder."""
    path = ctx.app_config.nextcloud_mount_path / "Users" / ctx.user_id / rel.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestResolveUserPath:
    """The path a user types is relative to their own /Users/<uid>/ folder."""

    def test_relative_scoped_to_user_folder(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        assert _resolve_user_path("alice", "shared/x.md") == "Users/alice/shared/x.md"
        assert (
            _resolve_user_path("alice", "istota/config/TODO.md")
            == "Users/alice/istota/config/TODO.md"
        )

    def test_blank_is_none(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        assert _resolve_user_path("alice", "") is None
        assert _resolve_user_path("alice", "   ") is None
        assert _resolve_user_path("alice", None) is None

    def test_own_full_path_passthrough(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        assert (
            _resolve_user_path("alice", "Users/alice/shared/x.md")
            == "Users/alice/shared/x.md"
        )
        assert (
            _resolve_user_path("alice", "/Users/alice/shared/x.md")
            == "Users/alice/shared/x.md"
        )

    def test_parent_escape_stripped(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        # `..` segments are dropped — can never climb above the user folder.
        assert (
            _resolve_user_path("alice", "../../etc/passwd")
            == "Users/alice/etc/passwd"
        )

    def test_other_user_path_not_honored(self):
        from istota.briefings.sources.builtins import _resolve_user_path
        # A path naming another user is treated as a subpath under *your own*
        # folder (nonexistent), never a cross-user read.
        assert (
            _resolve_user_path("alice", "Users/dana/shared/secret.md")
            == "Users/alice/Users/dana/shared/secret.md"
        )


class TestBuiltinTodos:
    def test_no_path_returns_not_configured(self, tmp_path):
        gs = resolve_source("todos", {}, _ctx(tmp_path))
        assert gs.ok is False
        assert "path" in gs.provenance.lower()

    def test_missing_todo_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False

    def test_path_reads_user_folder_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "istota/config/TODO.md", "- [ ] custom item\n")
        gs = resolve_source("todos", {"path": "istota/config/TODO.md"}, ctx)
        assert gs.ok is True
        assert gs.items[0]["text"] == "- [ ] custom item"

    def test_path_reads_shared_folder_file(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "shared/team-todo.md", "- [ ] shared item\n")
        gs = resolve_source("todos", {"path": "shared/team-todo.md"}, ctx)
        assert gs.ok is True
        assert gs.items[0]["text"] == "- [ ] shared item"

    def test_plain_dash_bullets(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- buy milk\n- call bank\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- buy milk", "- call bank"]

    def test_star_and_plus_bullets(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "* star item\n+ plus item\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["* star item", "+ plus item"]

    def test_numbered_lists(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "1. first\n2) second\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["1. first", "2) second"]

    def test_checked_items_excluded_unchecked_kept(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "- [ ] pending one\n- [x] done one\n- [X] done two\n* [ ] pending two\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- [ ] pending one", "* [ ] pending two"]

    def test_all_checked_returns_not_ok(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- [x] done one\n- [X] done two\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False

    def test_headings_and_rules_and_blanks_skipped(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(
            ctx,
            "TODO.md",
            "# My todos\n\n- real item\n\n---\n\n## Later\n* another\n",
        )
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- real item", "* another"]

    def test_indented_items_supported(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "- parent\n    - child\n\t* deep\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is True
        assert [i["text"] for i in gs.items] == ["- parent", "- child", "* deep"]

    def test_prose_lines_without_markers_ignored(self, tmp_path):
        ctx = _ctx(tmp_path)
        _write_user_file(ctx, "TODO.md", "Just some prose.\nAnother sentence.\n")
        gs = resolve_source("todos", {"path": "TODO.md"}, ctx)
        assert gs.ok is False


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
        _write_user_file(ctx, "shared/reminders.md", "Drink water\n\nStand up straight\n")
        gs = resolve_source("reminders", {"path": "shared/reminders.md"}, ctx)
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
        _write_user_file(ctx, "istota/notes/agenda.md", "agenda item")
        gs = resolve_source("notes", {"path": "istota/notes/agenda.md"}, ctx)
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
            lambda mc, mode, tz_str=None: "📈 MARKETS\nES=F +0.5%",
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


class TestCleanBody:
    """_clean_body routes HTML newsletters through the link-preserving converter."""

    def test_html_body_keeps_article_links(self):
        from istota.briefings.sources.email import _clean_body
        body = (
            '<html><body><div>'
            '<a href="https://semafor.com/a/iran">Iran tensions</a>'
            '</div><p>Body text.</p></body></html>'
        )
        out = _clean_body(body)
        assert "[Iran tensions](https://semafor.com/a/iran)" in out
        assert "Body text." in out

    def test_plain_body_passes_through(self):
        from istota.briefings.sources.email import _clean_body
        assert _clean_body("just words\nsecond line") == "just words\nsecond line"

    def test_max_links_is_threaded(self):
        from istota.briefings.sources.email import _clean_body
        body = "<html><body>" + "".join(
            f'<div><a href="https://x.com/{i}">item {i}</a></div>' for i in range(5)
        ) + "</body></html>"
        out = _clean_body(body, max_links=2)
        assert out.count("](https://x.com/") == 2

    def test_converter_failure_falls_back_to_strip_html(self, monkeypatch):
        from istota.briefings.sources import email as email_mod

        def boom(*a, **kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(email_mod, "html_to_markdown", boom)
        out = email_mod._clean_body("<html><body><p>Body text.</p></body></html>")
        assert "Body text." in out
        assert "<p>" not in out

    def test_resolve_threads_the_config_cap(self, tmp_path, monkeypatch):
        """The `[briefings] newsletter_max_links_per_source` knob reaches the body."""
        import istota.briefings.sources.email as email_mod
        from istota.config import BriefingsModuleConfig

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            email=EmailConfig(enabled=True, imap_host="imap.x", bot_email="bot@x.com"),
            users={"alice": UserConfig()},
        )
        cfg.briefings = BriefingsModuleConfig(newsletter_max_links_per_source=1)
        ctx = SourceContext(app_config=cfg, user_id="alice", conn=object())

        html = "<html><body>" + "".join(
            f'<div><a href="https://x.com/{i}">item {i}</a></div>' for i in range(4)
        ) + "</body></html>"

        monkeypatch.setattr("istota.email_support.get_email_config", lambda c: cfg.email)
        monkeypatch.setattr(
            "istota.skills.email.list_emails", lambda **kw: [_Env("1", "n@semafor.com")],
        )
        monkeypatch.setattr(
            "istota.skills.email.fetch_emails_full", lambda **kw: [_Full("1", html)],
        )
        monkeypatch.setattr(
            "istota.email_ownership.resolve_email_owner", lambda config, conn, e: None,
        )

        gs = _call_email(email_mod, {"mode": "shared"}, ctx)
        assert gs.ok is True
        assert gs.items[0]["body"].count("](https://x.com/") == 1
