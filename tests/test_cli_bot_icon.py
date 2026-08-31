"""Tests for ``istota bot-icon`` — the headless way to set the deployment icon.

The web route under ``/admin`` is the interactive door; this is the one an
Ansible play or a scripted install uses. Two things are specific to it and are
what these assert.

**It is idempotent by hash, and says so on stdout.** Setting the same file
twice is a no-op, so a play can call it on every deploy without reporting a
change every time. ``STATE: created|updated|noop`` is the line it computes
``changed_when`` from, matching ``user ensure`` and ``nextcloud
provision-rooms``.

**It stores what ``normalize`` emits, not what was on disk.** The CLI is not a
trusted-input shortcut past the decode path: the same square crop, the same
re-encode and the same refusals a browser upload takes.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from istota import avatars, db


class _FakeArgs:
    def __init__(self, **kwargs):
        defaults = {"config": None, "path": None}
        defaults.update(kwargs)
        self.__dict__.update(defaults)


def _png(path: Path, size=(500, 300), color=(120, 130, 140)) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def cfg_with_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
    )
    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    return cfg, db_path


def _stored(db_path):
    with db.get_db(db_path) as conn:
        return avatars.get_bot_avatar(conn)


class TestSet:
    def test_it_stores_a_normalized_icon(self, cfg_with_db, tmp_path, capsys):
        from istota import cli

        cfg, db_path = cfg_with_db
        src = _png(tmp_path / "icon.png")
        cli.cmd_bot_icon_set(_FakeArgs(config=str(cfg), path=str(src)))

        out = capsys.readouterr().out
        assert "STATE: created" in out

        icon = _stored(db_path)
        assert icon is not None
        assert icon.mime == avatars.NORMALIZED_MIME
        # What the file held was 500x300 PNG. What is stored is the square WebP
        # `normalize` emits — the CLI is not a way past the decode path.
        with Image.open(io.BytesIO(icon.image)) as img:
            assert img.format == "WEBP"
            assert img.size == (avatars.AVATAR_EDGE, avatars.AVATAR_EDGE)
        assert icon.content_hash in out

    def test_the_same_file_twice_is_a_noop(self, cfg_with_db, tmp_path, capsys):
        from istota import cli

        cfg, db_path = cfg_with_db
        src = _png(tmp_path / "icon.png")
        args = _FakeArgs(config=str(cfg), path=str(src))
        cli.cmd_bot_icon_set(args)
        first = _stored(db_path).updated_at
        capsys.readouterr()

        cli.cmd_bot_icon_set(args)
        assert "STATE: noop" in capsys.readouterr().out
        # Not rewritten: an Ansible play calling this every deploy must not
        # churn the row it reports as unchanged.
        assert _stored(db_path).updated_at == first

    def test_a_different_file_updates(self, cfg_with_db, tmp_path, capsys):
        from istota import cli

        cfg, db_path = cfg_with_db
        cli.cmd_bot_icon_set(_FakeArgs(
            config=str(cfg), path=str(_png(tmp_path / "a.png")),
        ))
        capsys.readouterr()
        cli.cmd_bot_icon_set(_FakeArgs(
            config=str(cfg),
            path=str(_png(tmp_path / "b.png", color=(9, 200, 30))),
        ))
        assert "STATE: updated" in capsys.readouterr().out

    def test_a_missing_file_exits_one_and_stores_nothing(
        self, cfg_with_db, tmp_path, capsys,
    ):
        from istota import cli

        cfg, db_path = cfg_with_db
        with pytest.raises(SystemExit) as exc:
            cli.cmd_bot_icon_set(_FakeArgs(
                config=str(cfg), path=str(tmp_path / "nope.png"),
            ))
        assert exc.value.code == 1
        assert "STATE:" not in capsys.readouterr().out
        assert _stored(db_path) is None

    def test_an_undecodable_file_exits_one_and_stores_nothing(
        self, cfg_with_db, tmp_path, capsys,
    ):
        from istota import cli

        cfg, db_path = cfg_with_db
        src = tmp_path / "icon.svg"
        src.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with pytest.raises(SystemExit) as exc:
            cli.cmd_bot_icon_set(_FakeArgs(config=str(cfg), path=str(src)))
        assert exc.value.code == 1
        assert "STATE:" not in capsys.readouterr().out
        assert _stored(db_path) is None

    def test_uploads_being_switched_off_does_not_disable_the_operator(
        self, tmp_path, monkeypatch, capsys,
    ):
        # `max_avatar_kb = 0` turns the *upload endpoint* off. This runs as the
        # operator, reading a file they can already read, so it keeps a working
        # ceiling rather than refusing everything.
        from istota import cli

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "[web]\nmax_avatar_kb = 0\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        cli.cmd_bot_icon_set(_FakeArgs(
            config=str(cfg), path=str(_png(tmp_path / "icon.png")),
        ))
        assert "STATE: created" in capsys.readouterr().out
        assert _stored(db_path) is not None


class TestClear:
    def test_it_removes_the_row(self, cfg_with_db, tmp_path, capsys):
        from istota import cli

        cfg, db_path = cfg_with_db
        cli.cmd_bot_icon_set(_FakeArgs(
            config=str(cfg), path=str(_png(tmp_path / "icon.png")),
        ))
        capsys.readouterr()
        cli.cmd_bot_icon_clear(_FakeArgs(config=str(cfg)))
        assert "STATE: updated" in capsys.readouterr().out
        assert _stored(db_path) is None

    def test_it_is_idempotent(self, cfg_with_db, capsys):
        from istota import cli

        cfg, _ = cfg_with_db
        cli.cmd_bot_icon_clear(_FakeArgs(config=str(cfg)))
        assert "STATE: noop" in capsys.readouterr().out


class TestShow:
    def test_it_reports_the_row_and_never_the_bytes(
        self, cfg_with_db, tmp_path, capsys,
    ):
        from istota import cli

        cfg, db_path = cfg_with_db
        cli.cmd_bot_icon_set(_FakeArgs(
            config=str(cfg), path=str(_png(tmp_path / "icon.png")),
        ))
        capsys.readouterr()
        cli.cmd_bot_icon_show(_FakeArgs(config=str(cfg)))

        out = capsys.readouterr().out
        icon = _stored(db_path)
        assert icon.content_hash in out
        assert avatars.NORMALIZED_MIME in out
        assert str(len(icon.image)) in out
        assert icon.updated_at in out
        # The blob does not go to a terminal, in any encoding.
        import base64
        assert base64.b64encode(icon.image).decode("ascii") not in out
        assert icon.image[:16].hex() not in out

    def test_it_says_so_when_nothing_is_set(self, cfg_with_db, capsys):
        from istota import cli

        cfg, _ = cfg_with_db
        cli.cmd_bot_icon_show(_FakeArgs(config=str(cfg)))
        assert "no bot icon" in capsys.readouterr().out.lower()


class TestItIsRegistered:
    def test_main_routes_each_verb_to_its_handler(
        self, cfg_with_db, tmp_path, monkeypatch, capsys,
    ):
        # A handler nothing routes to is a handler nobody can run, and the
        # parser is built inside `main()`, so registration and dispatch are only
        # testable together. Logging is stubbed because `main()` would otherwise
        # configure handlers against whatever path the config resolves.
        import sys

        from istota import cli

        cfg, db_path = cfg_with_db
        monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
        src = _png(tmp_path / "icon.png")

        for argv in (
            ["istota", "-c", str(cfg), "bot-icon", "set", str(src)],
            ["istota", "-c", str(cfg), "bot-icon", "show"],
            ["istota", "-c", str(cfg), "bot-icon", "clear"],
        ):
            monkeypatch.setattr(sys, "argv", argv)
            cli.main()

        out = capsys.readouterr().out
        assert "STATE: created" in out
        assert "STATE: updated" in out
        # `show` is the verb with no `STATE:` line, so asserting on the two that
        # have one leaves its registration unchecked: route `show` to the clear
        # handler and every other assertion here still holds. Its own output is
        # what separates the two.
        assert "Bot icon:" in out
        assert avatars.NORMALIZED_MIME in out
        assert _stored(db_path) is None


class TestAnUnmigratedDatabase:
    """A database predating the `bot_avatar` table.

    `docs/reference/cli.md` sells `set` as the thing an Ansible play runs on
    every deploy, so the ordering of "code lands, play runs, migrations run"
    decides which of these an operator reads. Every other refusal here is
    `Error: …` and exit 1; a traceback out of `main()` would be the odd one.
    """

    @pytest.fixture
    def unmigrated(self, cfg_with_db):
        cfg, db_path = cfg_with_db
        with db.get_db(db_path) as conn:
            conn.execute("DROP TABLE bot_avatar")
        return cfg, db_path

    def test_set_refuses_cleanly(self, unmigrated, tmp_path, capsys):
        from istota import cli

        cfg, _ = unmigrated
        with pytest.raises(SystemExit) as exc:
            cli.cmd_bot_icon_set(_FakeArgs(
                config=str(cfg), path=str(_png(tmp_path / "icon.png")),
            ))
        assert exc.value.code == 1
        assert "istota init" in capsys.readouterr().err

    def test_clear_refuses_cleanly(self, unmigrated, capsys):
        from istota import cli

        cfg, _ = unmigrated
        with pytest.raises(SystemExit) as exc:
            cli.cmd_bot_icon_clear(_FakeArgs(config=str(cfg)))
        assert exc.value.code == 1
        assert "istota init" in capsys.readouterr().err

    def test_show_refuses_cleanly(self, unmigrated, capsys):
        from istota import cli

        cfg, _ = unmigrated
        with pytest.raises(SystemExit) as exc:
            cli.cmd_bot_icon_show(_FakeArgs(config=str(cfg)))
        assert exc.value.code == 1
        assert "istota init" in capsys.readouterr().err
