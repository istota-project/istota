"""Tests for the unified ``istota briefings`` CLI + skill facade."""

import json
from pathlib import Path

import pytest

from istota import cli_briefings
from istota import db
from istota.briefings import db as bdb
from istota.briefings import resolve_for_user
from istota.config import Config, UserConfig


@pytest.fixture()
def config(tmp_path):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )
    db.init_db(cfg.db_path)
    return cfg


class TestSchedule:
    def test_ensure_and_list(self, config, capsys):
        rc = cli_briefings.dispatch(
            ["schedule", "ensure", "-u", "stefan", "--name", "Morning",
             "--cron", "0 7 * * *", "--conversation-token", "tok"],
            config,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Briefing ensured" in out
        assert "STATE:" in out

        # It landed in the framework briefing_configs.
        from istota import user_briefings as ub
        rows = ub.list_briefings(config.db_path)
        assert any(r.name == "Morning" for r in rows)

    def test_delete(self, config, capsys):
        cli_briefings.dispatch(
            ["schedule", "ensure", "-u", "stefan", "--name", "M",
             "--cron", "0 7 * * *", "--conversation-token", "tok"],
            config,
        )
        capsys.readouterr()
        rc = cli_briefings.dispatch(
            ["schedule", "delete", "-u", "stefan", "--name", "M"], config,
        )
        assert rc == 0
        assert "deleted" in capsys.readouterr().out


class TestBlocksSources:
    def test_add_list_block(self, config, capsys):
        rc = cli_briefings.dispatch(
            ["blocks", "add", "-u", "stefan", "--briefing", "M", "--title", "News"],
            config,
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["block"]["title"] == "News"

        rc = cli_briefings.dispatch(
            ["blocks", "list", "-u", "stefan", "--briefing", "M"], config,
        )
        assert rc == 0
        listed = json.loads(capsys.readouterr().out)
        assert [b["title"] for b in listed["blocks"]] == ["News"]

    def test_add_source_and_list(self, config, capsys):
        cli_briefings.dispatch(
            ["blocks", "add", "-u", "stefan", "--briefing", "M", "--title", "News"],
            config,
        )
        block = json.loads(capsys.readouterr().out)["block"]
        bid = block["id"]

        rc = cli_briefings.dispatch(
            ["sources", "add", "-u", "stefan", "--block", str(bid),
             "--kind", "email", "--config", '{"mode":"shared"}'],
            config,
        )
        assert rc == 0
        capsys.readouterr()

        rc = cli_briefings.dispatch(
            ["sources", "list", "-u", "stefan", "--block", str(bid)], config,
        )
        sources = json.loads(capsys.readouterr().out)["sources"]
        assert sources[0]["kind"] == "email"
        assert sources[0]["config"] == {"mode": "shared"}

    def test_reorder(self, config, capsys):
        for t in ("A", "B", "C"):
            cli_briefings.dispatch(
                ["blocks", "add", "-u", "stefan", "--briefing", "M", "--title", t],
                config,
            )
            capsys.readouterr()
        ctx = resolve_for_user("stefan", config)
        with bdb.connect(ctx.db_path) as conn:
            ids = [b.id for b in bdb.list_blocks(conn, "M")]
        rc = cli_briefings.dispatch(
            ["blocks", "reorder", "-u", "stefan", "--briefing", "M",
             "--ids", f"{ids[2]},{ids[0]},{ids[1]}"],
            config,
        )
        assert rc == 0
        with bdb.connect(ctx.db_path) as conn:
            assert [b.title for b in bdb.list_blocks(conn, "M")] == ["C", "A", "B"]

    def test_invalid_kind_errors(self, config, capsys):
        cli_briefings.dispatch(
            ["blocks", "add", "-u", "stefan", "--briefing", "M", "--title", "X"],
            config,
        )
        bid = json.loads(capsys.readouterr().out)["block"]["id"]
        rc = cli_briefings.dispatch(
            ["sources", "add", "-u", "stefan", "--block", str(bid), "--kind", "bogus"],
            config,
        )
        assert rc == 1

    def test_disabled_module_errors(self, tmp_path, capsys):
        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"stefan": UserConfig(disabled_modules=["briefings"])},
        )
        db.init_db(cfg.db_path)
        rc = cli_briefings.dispatch(
            ["blocks", "list", "-u", "stefan", "--briefing", "M"], cfg,
        )
        assert rc == 1
        assert "disabled" in capsys.readouterr().err.lower()


class TestArchive:
    def test_archive_list_show(self, config, capsys):
        ctx = resolve_for_user("stefan", config)
        bdb.init_db(ctx.db_path)
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="M", subject="Morning", body_md="📰 news",
            )
            conn.commit()
        rc = cli_briefings.dispatch(["archive", "list", "-u", "stefan"], config)
        assert rc == 0
        items = json.loads(capsys.readouterr().out)["items"]
        assert items[0]["subject"] == "Morning"

        rc = cli_briefings.dispatch(
            ["archive", "show", "-u", "stefan", "--id", str(aid)], config,
        )
        shown = json.loads(capsys.readouterr().out)["briefing"]
        assert shown["body_md"] == "📰 news"


class TestSkillFacade:
    def test_passthrough(self, config, capsys, monkeypatch, tmp_path):
        import istota.skills.briefings as sb

        monkeypatch.setenv("BRIEFINGS_USER", "stefan")
        monkeypatch.setattr("istota.config.load_config", lambda *a, **k: config)

        result = sb._run(["blocks", "add", "--briefing", "M", "--title", "News"])
        assert result["status"] == "ok"
        assert result["block"]["title"] == "News"

    def test_missing_env(self, monkeypatch):
        import istota.skills.briefings as sb

        monkeypatch.delenv("BRIEFINGS_USER", raising=False)
        result = sb._run(["blocks", "list", "--briefing", "M"])
        assert result["status"] == "error"
        assert "BRIEFINGS_USER" in result["error"]


class TestDeprecationShim:
    def test_istota_briefing_warns_and_works(self, config, capsys, monkeypatch):
        from istota.cli import cmd_briefing
        import argparse

        monkeypatch.setattr("istota.cli.load_config", lambda *a, **k: config)
        ns = argparse.Namespace(
            config=None, action="ensure", user="stefan", name="M",
            cron="0 7 * * *", conversation_token="tok", output="talk",
            disabled=False,
        )
        cmd_briefing(ns)
        captured = capsys.readouterr()
        assert "deprecated" in captured.err.lower()
        assert "Briefing ensured" in captured.out
