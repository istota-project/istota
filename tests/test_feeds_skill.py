"""Tests for the rewritten feeds skill — in-process facade over feeds.cli."""

from __future__ import annotations

import json

import pytest

from istota.config import Config, UserConfig


@pytest.fixture
def istota_config(tmp_path, monkeypatch):
    """Build a minimal istota Config that resolves to a workspace under tmp_path.

    Workspace mode is the only resolution path now; ``resolve_for_user``
    derives ``{nextcloud_mount}/{get_user_bot_path}`` automatically and
    creates ``feeds/`` lazily — no ResourceConfig needed.
    """
    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=tmp_path,
        users={"alice": UserConfig()},
    )

    # Stub load_config so the skill picks up our test config.
    monkeypatch.setattr(
        "istota.config.load_config",
        lambda *a, **kw: config,
    )
    monkeypatch.setenv("FEEDS_USER", "alice")
    return config


def _feeds_ctx(config):
    from istota.feeds import resolve_for_user
    return resolve_for_user("alice", config)


def _seed_over_cap(config):
    """One feed holding five read entries against a maximum of two.

    Uses the count pass rather than the age pass: it needs no observation
    marker and no floor arithmetic, so the fixture stays small while still
    exercising a committed delete.
    """
    from istota.feeds import db as feeds_db
    from istota.skills.feeds import _run

    assert _run(["add", "--url", "https://feed.example.com/rss"])["status"] == "ok"
    ctx = _feeds_ctx(config)
    with feeds_db.connect(ctx.db_path) as conn:
        feed_id = feeds_db.list_feeds(conn)[0].id
        for i in range(5):
            conn.execute(
                "INSERT INTO feed_entries(feed_id, guid, title, fetched_at, "
                "last_seen_at, status) VALUES (?, ?, ?, ?, ?, 'read')",
                (feed_id, f"e{i}", f"e{i}",
                 f"2026-08-0{i + 1}T00:00:00+00:00",
                 f"2026-08-0{i + 1}T00:00:00+00:00"),
            )
        feeds_db.set_max_entries_per_feed(conn, 2)
        conn.commit()


def _entry_guids(config):
    from istota.feeds import db as feeds_db
    ctx = _feeds_ctx(config)
    with feeds_db.connect(ctx.db_path) as conn:
        return [
            r["guid"] for r in conn.execute(
                "SELECT guid FROM feed_entries ORDER BY guid"
            )
        ]


class TestSkillRun:
    def test_list_empty(self, istota_config):
        from istota.skills.feeds import _run
        out = _run(["list"])
        assert out["status"] == "ok"
        assert out["count"] == 0

    def test_add_then_list(self, istota_config):
        from istota.skills.feeds import _run
        added = _run(["add", "--url", "https://example.com/feed.xml", "--category", "blogs"])
        assert added["status"] == "ok"

        listed = _run(["list"])
        urls = [f["url"] for f in listed["feeds"]]
        assert urls == ["https://example.com/feed.xml"]
        assert listed["feeds"][0]["category_slug"] == "blogs"

    def test_no_user_returns_error(self, monkeypatch):
        monkeypatch.delenv("FEEDS_USER", raising=False)
        from istota.skills.feeds import _run
        out = _run(["list"])
        assert out["status"] == "error"
        assert "FEEDS_USER" in out["error"]


class TestSkillExitCodes:
    """Module-skill subprocesses must exit non-zero when they emit a
    `{"status":"error",…}` envelope. The scheduler keys success/failure off
    returncode (with a JSON-envelope fallback as defense-in-depth), so a
    silent zero exit lets failed runs masquerade as successful."""

    def test_main_exits_nonzero_on_error_envelope(self, monkeypatch):
        monkeypatch.delenv("FEEDS_USER", raising=False)
        from istota.skills.feeds import main
        with pytest.raises(SystemExit) as exc_info:
            main(["list"])
        assert exc_info.value.code == 1

    def test_main_exits_zero_on_ok(self, istota_config):
        from istota.skills.feeds import main
        # No SystemExit raised, or SystemExit with code 0/None.
        try:
            main(["list"])
        except SystemExit as e:
            assert e.code in (0, None)


class TestParser:
    def test_subcommands_present(self):
        from istota.skills.feeds import build_parser
        p = build_parser()
        for cmd in ["list", "categories", "entries", "add", "remove",
                    "refresh", "poll", "run-scheduled", "prune",
                    "import-opml", "export-opml"]:
            args = p.parse_args([cmd] + (["--url", "u"] if cmd == "add"
                                          else ["x"] if cmd == "import-opml"
                                          else []))
            assert args.command == cmd

    def test_add_requires_url(self):
        from istota.skills.feeds import build_parser
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["add"])


class TestPrune:
    """The facade half of `feeds prune`. The scheduler dispatches this daily
    and unattended, so what the parser accepts and what the handler forwards
    are the whole contract — a dropped `--dry-run` deletes for real."""

    def test_parses_dry_run(self):
        from istota.skills.feeds import build_parser
        args = build_parser().parse_args(["prune", "--dry-run"])
        assert args.command == "prune"
        assert args.dry_run is True

    def test_dry_run_defaults_off(self):
        """The scheduled job passes no flag, and it must delete for real."""
        from istota.skills.feeds import build_parser
        args = build_parser().parse_args(["prune"])
        assert args.command == "prune"
        assert args.dry_run is False

    def test_dispatch_forwards_dry_run_to_the_click_command(self, monkeypatch):
        from istota.skills import feeds as skill
        sent = []
        monkeypatch.setattr(
            skill, "_run", lambda a: sent.append(a) or {"status": "ok"},
        )
        skill.main(["prune", "--dry-run"])
        assert sent == [["prune", "--dry-run"]]

    def test_dispatch_without_the_flag_sends_a_bare_prune(self, monkeypatch):
        from istota.skills import feeds as skill
        sent = []
        monkeypatch.setattr(
            skill, "_run", lambda a: sent.append(a) or {"status": "ok"},
        )
        skill.main(["prune"])
        assert sent == [["prune"]]

    def test_an_error_envelope_still_exits_nonzero(self, monkeypatch):
        """A failed prune is a failed scheduled task. `_sync_module_jobs`
        counts failures off the return code, so a prune that raised and
        exited 0 would look like a daily job doing its work."""
        from istota.skills import feeds as skill
        monkeypatch.setattr(
            skill, "_run",
            lambda a: {"status": "error", "error": "database is locked"},
        )
        with pytest.raises(SystemExit) as exc_info:
            skill.main(["prune"])
        assert exc_info.value.code == 1

    def test_prune_deletes_for_real_through_the_facade(self, istota_config, capsys):
        """Through `_run` and the Click command, not a mock, and against data
        that actually has something to delete.

        Asserting zero counts on an empty database was the earlier shape of
        this test, and it could not fail: zero is what a correct prune, a
        no-op prune and a stubbed-out `prune_feeds` all return. This is the
        no-flag argv the scheduled job sends, so it is the one that has to be
        shown deleting.

        Driven through `main`, not `_run`: `_run` takes an argv the test wrote
        itself, so it steps over `cmd_prune` — the very code that decides
        whether `--dry-run` is appended. Measured, a `cmd_prune` forced to
        always send `--dry-run` left this test green while it called `_run`.
        """
        from istota.skills.feeds import main
        _seed_over_cap(istota_config)

        main(["prune"])

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["dry_run"] is False
        assert out["entries_deleted_by_cap"] == 3
        assert _entry_guids(istota_config) == ["e3", "e4"]

    def test_dry_run_plans_the_same_deletion_and_leaves_the_rows(
        self, istota_config, capsys,
    ):
        """The control for the test above, on identical data: same planned
        count, nothing gone. Without it, a `--dry-run` that quietly deleted
        would look exactly like a `--dry-run` that worked."""
        from istota.skills.feeds import main
        _seed_over_cap(istota_config)

        main(["prune", "--dry-run"])

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["dry_run"] is True
        assert out["entries_deleted_by_cap"] == 3
        assert _entry_guids(istota_config) == ["e0", "e1", "e2", "e3", "e4"]


class TestLoaderEnvFirst:
    """Phase 1.2 — feeds loader reads env vars before consulting secrets_store.

    Pinned because Phase 1.4 strips ISTOTA_SECRET_KEY from subprocess env;
    once that lands the secrets_store fallback returns None silently and
    cron module jobs would lose access to TUMBLR_API_KEY without env-first
    resolution.
    """

    def test_env_takes_precedence_over_store(self, istota_config, monkeypatch):
        from istota.feeds import _loader
        monkeypatch.setenv("TUMBLR_API_KEY", "from-env")
        called = []
        monkeypatch.setattr(
            "istota.secrets_store.get_secret",
            lambda *a, **kw: called.append(a) or "from-store",
        )
        ctx = _loader.resolve_for_user("alice", istota_config)
        assert ctx.tumblr_api_key == "from-env"
        assert called == []

    def test_store_fallback_when_env_unset(self, istota_config, monkeypatch):
        """Daemon-context: env is unset, master key is present, store wins."""
        from istota.feeds import _loader
        monkeypatch.delenv("TUMBLR_API_KEY", raising=False)
        monkeypatch.setattr(
            "istota.secrets_store.get_secret",
            lambda db, u, s, k: "from-store" if (s, k) == ("feeds", "tumblr_api_key") else None,
        )
        ctx = _loader.resolve_for_user("alice", istota_config)
        assert ctx.tumblr_api_key == "from-store"
