"""Tests for ``istota resource ensure`` — idempotent CLI upsert.

The ``ensure`` action replaces ansible's per-user TOML resource templating.
Same partial-update + status-output contract as ``istota user ensure``.
"""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from istota import db


class _FakeArgs:
    def __init__(self, **kwargs):
        # Defaults match the parser's `default=None` semantics so omitted
        # flags are unambiguously distinguishable from explicit empty values.
        defaults = {
            "config": None,
            "user": None,
            "type": None,
            "path": None,
            "name": None,
            "permissions": None,
            "extras": None,           # repeatable key=value
            "extras_json": None,      # full JSON dict override
            "extras_clear": False,    # explicit "wipe extras"
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def cfg_with_db(tmp_path: Path, monkeypatch):
    """Minimal config TOML pointing at a fresh, initialized DB."""
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


class TestResourceEnsureCreate:
    def test_creates_row_with_state_created(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="folder", path="/Documents", name="Docs",
            permissions="read",
        )
        cmd_resource(args)
        out = capsys.readouterr().out
        assert "STATE: created" in out
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert len(rows) == 1
        assert rows[0].resource_type == "folder"
        assert rows[0].resource_path == "/Documents"
        assert rows[0].display_name == "Docs"

    def test_path_defaults_to_type_for_module_resources(self, cfg_with_db, capsys):
        # Module-shaped resources (feeds, money, overland, karakeep, monarch)
        # don't carry a real filesystem path. Match the web UI behavior of
        # using the type as the implicit unique-key path.
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="feeds", name="Feeds",
        )
        cmd_resource(args)
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert rows[0].resource_path == "feeds"

    def test_extras_key_value_pairs_persist(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="overland", name="GPS",
            extras=["ingest_token=tok-xyz", "default_radius=75"],
        )
        cmd_resource(args)
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        # Numeric strings are coerced to int so int-typed fields like
        # default_radius round-trip without operators having to know JSON.
        assert rows[0].extras == {"ingest_token": "tok-xyz", "default_radius": 75}

    def test_extras_json_overrides_kv_pairs(self, cfg_with_db, capsys):
        # --extras-json wins. Lets ansible pass through a complex nested dict
        # (e.g. money's `ledgers` array) without splitting into kv pairs.
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="money", name="Money",
            extras=["data_dir=/ignored"],
            extras_json='{"ledgers": ["main"], "data_dir": "/used"}',
        )
        cmd_resource(args)
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert rows[0].extras == {"ledgers": ["main"], "data_dir": "/used"}


class TestResourceEnsureIdempotency:
    def test_second_invocation_with_same_inputs_is_noop(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="folder", path="/x", name="X", permissions="read",
        )
        cmd_resource(args)
        capsys.readouterr()  # drain "STATE: created"

        cmd_resource(args)
        out = capsys.readouterr().out
        assert "STATE: noop" in out

    def test_changing_display_name_reports_updated(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        cmd_resource(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="folder", path="/x", name="Old", permissions="read",
        ))
        capsys.readouterr()

        cmd_resource(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="folder", path="/x", name="New", permissions="read",
        ))
        out = capsys.readouterr().out
        assert "STATE: updated" in out
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert rows[0].display_name == "New"

    def test_omitted_extras_preserves_existing(self, cfg_with_db, capsys):
        # Mirrors `user ensure`: a flag the operator doesn't pass should
        # leave the column alone — not silently wipe it.
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        cmd_resource(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="overland", name="GPS",
            extras=["ingest_token=preserved"],
        ))
        capsys.readouterr()

        cmd_resource(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="overland", name="GPS",
        ))
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert rows[0].extras == {"ingest_token": "preserved"}

    def test_extras_clear_wipes_extras(self, cfg_with_db, capsys):
        # The escape hatch for "I really do want extras gone." Distinct from
        # omitting --extras (which preserves) so operators can be explicit.
        from istota.cli import cmd_resource

        cfg, db_path = cfg_with_db
        cmd_resource(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="overland", name="GPS",
            extras=["ingest_token=goodbye"],
        ))
        capsys.readouterr()

        cmd_resource(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="overland", name="GPS",
            extras_clear=True,
        ))
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert rows[0].extras == {}


class TestResourceEnsureValidation:
    def test_missing_type_errors(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, _ = cfg_with_db
        args = _FakeArgs(action="ensure", config=str(cfg), user="alice")
        with pytest.raises(SystemExit) as exc:
            cmd_resource(args)
        assert exc.value.code != 0

    def test_malformed_kv_pair_errors(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, _ = cfg_with_db
        # No `=` in the pair — operators should get a clean error rather
        # than have it silently dropped.
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="overland", name="GPS",
            extras=["malformed"],
        )
        with pytest.raises(SystemExit):
            cmd_resource(args)

    def test_malformed_extras_json_errors(self, cfg_with_db, capsys):
        from istota.cli import cmd_resource

        cfg, _ = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="money", name="Money",
            extras_json="{not valid",
        )
        with pytest.raises(SystemExit):
            cmd_resource(args)

    def test_filesystem_type_requires_explicit_path(self, cfg_with_db, capsys):
        # Without this guard, a missing --path on `folder` would silently
        # create a row at path="folder", which is almost never what the
        # operator wanted.
        from istota.cli import cmd_resource

        cfg, _ = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            type="folder", name="Docs",
        )
        with pytest.raises(SystemExit):
            cmd_resource(args)
