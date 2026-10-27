"""Tests for the unified ``istota briefings`` CLI + skill facade."""

import json

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
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )
    db.init_db(cfg.db_path)
    return cfg


class TestSchedule:
    def test_ensure_and_list(self, config, capsys):
        rc = cli_briefings.dispatch(
            ["schedule", "ensure", "-u", "alice", "--name", "Morning",
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

    def test_list_marks_a_held_briefing(self, config, capsys):
        from istota import config as cfgmod
        from istota import user_briefings as ub
        row, _ = ub.ensure_briefing(config.db_path, user_id="alice", name="M",
                                    cron="0 7 * * *", conversation_token="tok")
        ub.set_hold(config.db_path, row.id, admin_id="root")
        cfgmod._apply_user_briefings(config)
        capsys.readouterr()
        assert cli_briefings.dispatch(["schedule", "list", "-u", "alice"], config) == 0
        (line,) = [x for x in capsys.readouterr().out.splitlines() if " M " in x]
        assert line.endswith("(held by an administrator)")

    def test_delete(self, config, capsys):
        cli_briefings.dispatch(
            ["schedule", "ensure", "-u", "alice", "--name", "M",
             "--cron", "0 7 * * *", "--conversation-token", "tok"],
            config,
        )
        capsys.readouterr()
        rc = cli_briefings.dispatch(
            ["schedule", "delete", "-u", "alice", "--name", "M"], config,
        )
        assert rc == 0
        assert "deleted" in capsys.readouterr().out


class TestBlocksSources:
    def test_list_briefings(self, config, capsys):
        from istota.config import BriefingConfig

        config.users["alice"].briefings = [
            BriefingConfig(name="weekly", cron="0 8 * * 1", output="email"),
        ]

        rc = cli_briefings.dispatch(["list", "-u", "alice"], config)

        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {
            "status": "ok",
            "briefings": [
                {
                    "name": "weekly",
                    "block_count": 0,
                    "last_generated_at": None,
                },
            ],
        }

    def test_add_list_block(self, config, capsys):
        rc = cli_briefings.dispatch(
            ["blocks", "add", "-u", "alice", "--briefing", "M", "--title", "News"],
            config,
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["block"]["title"] == "News"

        rc = cli_briefings.dispatch(
            ["blocks", "list", "-u", "alice", "--briefing", "M"], config,
        )
        assert rc == 0
        listed = json.loads(capsys.readouterr().out)
        assert [b["title"] for b in listed["blocks"]] == ["News"]

    def test_add_source_and_list(self, config, capsys):
        cli_briefings.dispatch(
            ["blocks", "add", "-u", "alice", "--briefing", "M", "--title", "News"],
            config,
        )
        block = json.loads(capsys.readouterr().out)["block"]
        bid = block["id"]

        rc = cli_briefings.dispatch(
            ["sources", "add", "-u", "alice", "--block", str(bid),
             "--kind", "email", "--config", '{"mode":"shared"}'],
            config,
        )
        assert rc == 0
        capsys.readouterr()

        rc = cli_briefings.dispatch(
            ["sources", "list", "-u", "alice", "--block", str(bid)], config,
        )
        sources = json.loads(capsys.readouterr().out)["sources"]
        assert sources[0]["kind"] == "email"
        assert sources[0]["config"] == {"mode": "shared"}

    def test_reorder(self, config, capsys):
        for t in ("A", "B", "C"):
            cli_briefings.dispatch(
                ["blocks", "add", "-u", "alice", "--briefing", "M", "--title", t],
                config,
            )
            capsys.readouterr()
        ctx = resolve_for_user("alice", config)
        with bdb.connect(ctx.db_path) as conn:
            ids = [b.id for b in bdb.list_blocks(conn, "M")]
        rc = cli_briefings.dispatch(
            ["blocks", "reorder", "-u", "alice", "--briefing", "M",
             "--ids", f"{ids[2]},{ids[0]},{ids[1]}"],
            config,
        )
        assert rc == 0
        with bdb.connect(ctx.db_path) as conn:
            assert [b.title for b in bdb.list_blocks(conn, "M")] == ["C", "A", "B"]

    def test_invalid_kind_errors(self, config, capsys):
        cli_briefings.dispatch(
            ["blocks", "add", "-u", "alice", "--briefing", "M", "--title", "X"],
            config,
        )
        bid = json.loads(capsys.readouterr().out)["block"]["id"]
        rc = cli_briefings.dispatch(
            ["sources", "add", "-u", "alice", "--block", str(bid), "--kind", "bogus"],
            config,
        )
        assert rc == 1

    def test_refusal_prints_the_envelope_and_exits_one(self, config, capsys):
        cli_briefings.dispatch(
            ["blocks", "add", "-u", "alice", "--briefing", "M", "--title", "X"],
            config,
        )
        bid = json.loads(capsys.readouterr().out)["block"]["id"]

        rc = cli_briefings.dispatch(
            ["blocks", "set", "-u", "alice", "--id", str(bid), "--render-mode", "agent"],
            config,
        )

        assert rc == 1
        assert json.loads(capsys.readouterr().out)["reason"] == "invalid_definition"

    def test_add_options_and_merge_options_are_forwarded(self, config, capsys):
        rc = cli_briefings.dispatch(
            ["blocks", "add", "-u", "alice", "--briefing", "M", "--title", "X",
             "--options", '{"tone": "neutral"}'],
            config,
        )
        assert rc == 0
        bid = json.loads(capsys.readouterr().out)["block"]["id"]

        rc = cli_briefings.dispatch(
            ["blocks", "set", "-u", "alice", "--id", str(bid),
             "--merge-options", '{"story_count": 4}'],
            config,
        )

        assert rc == 0
        options = json.loads(capsys.readouterr().out)["block"]["options"]
        assert options == {"tone": "neutral", "story_count": 4}

    def test_operator_context_carries_the_app_config(self, config, capsys, monkeypatch):
        from istota.briefings import cli as bcli

        seen = {}
        real_ctx = bcli._ctx

        def capture(click_ctx):
            bctx = real_ctx(click_ctx)
            seen["app_config"] = bctx.app_config
            return bctx

        monkeypatch.setattr(bcli, "_ctx", capture)
        cli_briefings.dispatch(["blocks", "list", "-u", "alice", "--briefing", "M"], config)

        assert seen["app_config"] is config

    def test_disabled_module_errors(self, tmp_path, capsys):
        cfg = Config(
            db_path=tmp_path / "istota.db",
            workspace_path=tmp_path / "mount",
            users={"alice": UserConfig(disabled_modules=["briefings"])},
        )
        db.init_db(cfg.db_path)
        rc = cli_briefings.dispatch(
            ["blocks", "list", "-u", "alice", "--briefing", "M"], cfg,
        )
        assert rc == 1
        assert "disabled" in capsys.readouterr().err.lower()


class TestSharedEnsure:
    PROMPT = "Read the front pages and write a linked digest."

    def _ensure(self, config, *extra):
        return cli_briefings.dispatch(
            ["shared", "ensure", "--name", "news", "--cron", "50 5 * * *", *extra],
            config,
        )

    def test_agent_block_with_a_prompt_and_no_sources(self, config, capsys):
        assert self._ensure(config, "--render-mode", "agent", "--directive", self.PROMPT) == 0
        with db.get_db(config.db_path) as conn:
            row = db.get_shared_block_config(conn, "news")
        assert (row.render_mode, row.directive, row.sources, row.trusted) == (
            "agent", self.PROMPT, [], False,
        )

    @pytest.mark.parametrize("extra, message", [
        (["--source-json", '{"kind": "browse", "config": {}}'], "agent blocks take no sources"),
        (["--trusted"], "agent blocks are never trusted"),
        (["--directive", "  "], "agent blocks need a task prompt"),
    ])
    def test_agent_block_breaking_the_rules_is_refused(self, config, capsys, extra, message):
        args = ["--render-mode", "agent", "--directive", self.PROMPT, *extra]
        assert self._ensure(config, *args) == 1
        assert message in capsys.readouterr().err
        with db.get_db(config.db_path) as conn:
            assert db.get_shared_block_config(conn, "news") is None

    def test_unknown_render_mode_is_refused(self, config, capsys):
        assert self._ensure(config, "--render-mode", "weird") == 1
        assert "--render-mode must be one of" in capsys.readouterr().err

    def test_run_on_an_agent_block_names_the_queued_task(self, config, capsys, monkeypatch):
        from istota import scheduler
        from istota.briefings import shared_block_runs

        assert self._ensure(config, "--render-mode", "agent", "--directive", self.PROMPT) == 0
        capsys.readouterr()

        def fake_generate(cfg, block):
            with db.get_db(cfg.db_path) as conn:
                db.shared_kv_set(
                    conn, shared_block_runs.RUN_NAMESPACE,
                    shared_block_runs.AGENT_PREFIX + block.name,
                    json.dumps({"task_id": 41, "captured": False}), "daemon",
                )
            return shared_block_runs.QUEUED

        monkeypatch.setattr(scheduler, "_generate_shared_block", fake_generate)
        assert cli_briefings.dispatch(["shared", "run", "--name", "news"], config) == 0
        out = capsys.readouterr().out
        assert "queued, task #41" in out
        assert "no content written" not in out


class TestArchive:
    def test_archive_list_show(self, config, capsys):
        ctx = resolve_for_user("alice", config)
        bdb.init_db(ctx.db_path)
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="M", subject="Morning", body_md="📰 news",
            )
            conn.commit()
        rc = cli_briefings.dispatch(["archive", "list", "-u", "alice"], config)
        assert rc == 0
        items = json.loads(capsys.readouterr().out)["items"]
        assert items[0]["subject"] == "Morning"

        rc = cli_briefings.dispatch(
            ["archive", "show", "-u", "alice", "--id", str(aid)], config,
        )
        shown = json.loads(capsys.readouterr().out)["briefing"]
        assert shown["body_md"] == "📰 news"


class TestSkillFacade:
    def test_passthrough(self, config, capsys, monkeypatch, tmp_path):
        import istota.skills.briefings as sb

        monkeypatch.setenv("BRIEFINGS_USER", "alice")
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
            config=None, action="ensure", user="alice", name="M",
            cron="0 7 * * *", conversation_token="tok", output="talk",
            disabled=False,
        )
        cmd_briefing(ns)
        captured = capsys.readouterr()
        assert "deprecated" in captured.err.lower()
        assert "Briefing ensured" in captured.out
