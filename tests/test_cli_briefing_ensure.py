"""Tests for ``istota briefing ensure`` (Phase 7b CLI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import db, user_briefings


class _FakeArgs:
    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "user": None,
            "name": None,
            "cron": None,
            "conversation_token": None,
            "output": "talk",
            "components_json": None,
            "component": None,
            "disabled": False,
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def cfg_with_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
        "\n[users.alice]\n"
        'display_name = "Alice"\n'
    )
    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    return cfg, db_path


class TestBriefingEnsureCreate:
    def test_creates_row_with_state_created(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="morning", cron="0 7 * * 1-5",
            conversation_token="tok123",
            components_json='{"calendar": true, "email": true}',
        )
        cmd_briefing(args)
        out = capsys.readouterr().out
        assert "STATE: created" in out
        rows = user_briefings.list_briefings(db_path, "alice")
        assert len(rows) == 1
        assert rows[0].name == "morning"
        assert rows[0].components == {"calendar": True, "email": True}

    def test_component_kv_pairs_parse(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", conversation_token="t",
            component=["calendar=true", "todos=true"],
        )
        cmd_briefing(args)
        rows = user_briefings.list_briefings(db_path, "alice")
        assert rows[0].components == {"calendar": True, "todos": True}

    def test_talk_output_requires_conversation_token(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, _ = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", output="talk",
        )
        with pytest.raises(SystemExit):
            cmd_briefing(args)
        err = capsys.readouterr().err
        assert "conversation-token" in err

    def test_email_output_does_not_require_token(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="weekly", cron="0 9 * * 1", output="email",
        )
        cmd_briefing(args)
        rows = user_briefings.list_briefings(db_path, "alice")
        assert rows[0].output == "email"

    def test_ntfy_output_does_not_require_token(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="push", cron="0 9 * * 1", output="ntfy",
        )
        cmd_briefing(args)
        rows = user_briefings.list_briefings(db_path, "alice")
        assert rows[0].output == "ntfy"

    def test_comma_list_output_with_talk_requires_token(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, _ = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", output="talk,email",
        )
        with pytest.raises(SystemExit):
            cmd_briefing(args)
        assert "conversation-token" in capsys.readouterr().err

    def test_comma_list_output_persists(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", output="talk,email",
            conversation_token="t",
        )
        cmd_briefing(args)
        rows = user_briefings.list_briefings(db_path, "alice")
        assert rows[0].output == "talk,email"


class TestBriefingEnsureIdempotency:
    def test_second_invocation_is_noop(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, _ = cfg_with_db
        kwargs = dict(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", conversation_token="t",
            components_json='{"calendar": true}',
        )
        cmd_briefing(_FakeArgs(**kwargs))
        capsys.readouterr()
        cmd_briefing(_FakeArgs(**kwargs))
        out = capsys.readouterr().out
        assert "STATE: noop" in out

    def test_change_cron_updates(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, _ = cfg_with_db
        cmd_briefing(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", conversation_token="t",
        ))
        capsys.readouterr()
        cmd_briefing(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 8 * * *", conversation_token="t",
        ))
        out = capsys.readouterr().out
        assert "STATE: updated" in out


class TestBriefingDelete:
    def test_delete_removes_row(self, cfg_with_db, capsys):
        from istota.cli import cmd_briefing

        cfg, db_path = cfg_with_db
        cmd_briefing(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            name="m", cron="0 7 * * *", conversation_token="t",
        ))
        capsys.readouterr()
        cmd_briefing(_FakeArgs(
            action="delete", config=str(cfg), user="alice", name="m",
        ))
        rows = user_briefings.list_briefings(db_path, "alice")
        assert rows == []

    def test_delete_missing_exits_non_zero(self, cfg_with_db):
        from istota.cli import cmd_briefing

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit) as excinfo:
            cmd_briefing(_FakeArgs(
                action="delete", config=str(cfg), user="alice", name="ghost",
            ))
        assert excinfo.value.code == 1
