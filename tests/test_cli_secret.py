"""Tests for ``istota secret`` CLI subcommands.

The ``secret`` command is the operator/Ansible-facing way to provision
per-user tier-2 credentials into the encrypted ``secrets`` table. Same
contract as ``user ensure`` / ``resource ensure`` / ``briefing ensure``:

* ``ensure`` is idempotent and prints ``STATE: created|updated|noop`` so
  Ansible can compute ``changed_when`` from stdout.
* ``list`` prints which (service, key) pairs are stored — never plaintext.
* ``remove`` deletes a single (user, service, key) row.

Validation is gated by ``secret_schema.all_known_services()``: an unknown
service or key is rejected with exit 1 before any DB write.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from istota import db, secrets_store


class _FakeArgs:
    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "action": None,
            "user": None,
            "service": None,
            "key": None,
            "value": None,
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def cfg_with_db(tmp_path: Path, monkeypatch):
    """Minimal config TOML pointing at a fresh, initialized DB.

    A 32+ char ISTOTA_SECRET_KEY is required by the encryption layer; the
    fixture installs one for the duration of the test.
    """
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
    )
    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    return cfg, db_path


class TestSecretEnsureCreate:
    def test_creates_row_with_state_created(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, db_path = cfg_with_db
        cmd_secret(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            service="karakeep", key="api_key", value="kk-token",
        ))
        out = capsys.readouterr().out
        assert "STATE: created" in out
        # Plaintext must not appear in stdout — the ensure CLI is what
        # ansible logs, so leaking the value would land creds in CI logs.
        assert "kk-token" not in out
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") == "kk-token"

    def test_module_service_accepted(self, cfg_with_db, capsys):
        # Module-owned services (monarch, overland, feeds) must work too —
        # the schema union is what counts, not the connected/module split.
        from istota.cli import cmd_secret

        cfg, db_path = cfg_with_db
        cmd_secret(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            service="overland", key="ingest_token", value="ovl-tok",
        ))
        assert "STATE: created" in capsys.readouterr().out
        assert secrets_store.get_secret(db_path, "alice", "overland", "ingest_token") == "ovl-tok"


class TestSecretEnsureIdempotency:
    def test_second_invocation_with_same_value_is_noop(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        args = _FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            service="karakeep", key="api_key", value="kk",
        )
        cmd_secret(args)
        capsys.readouterr()
        cmd_secret(args)
        assert "STATE: noop" in capsys.readouterr().out

    def test_different_value_reports_updated(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, db_path = cfg_with_db
        cmd_secret(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            service="karakeep", key="api_key", value="old",
        ))
        capsys.readouterr()
        cmd_secret(_FakeArgs(
            action="ensure", config=str(cfg), user="alice",
            service="karakeep", key="api_key", value="new",
        ))
        out = capsys.readouterr().out
        assert "STATE: updated" in out
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") == "new"


class TestSecretEnsureValidation:
    def test_unknown_service_rejected(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit) as excinfo:
            cmd_secret(_FakeArgs(
                action="ensure", config=str(cfg), user="alice",
                service="not-a-service", key="x", value="y",
            ))
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "unknown service" in err.lower()
        assert "not-a-service" in err

    def test_unknown_key_rejected(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit) as excinfo:
            cmd_secret(_FakeArgs(
                action="ensure", config=str(cfg), user="alice",
                service="karakeep", key="not_a_key", value="y",
            ))
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "unknown key" in err.lower()
        assert "not_a_key" in err

    def test_oauth_only_service_rejected(self, cfg_with_db, capsys):
        # google_workspace has no operator-writable keys (OAuth flow only).
        # Pushing a value via the CLI is wrong — surface that loudly.
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit) as excinfo:
            cmd_secret(_FakeArgs(
                action="ensure", config=str(cfg), user="alice",
                service="google_workspace", key="anything", value="y",
            ))
        assert excinfo.value.code == 1

    def test_empty_value_rejected(self, cfg_with_db, capsys):
        # Use `secret remove` for clearing — empty value via ensure is ambiguous.
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit) as excinfo:
            cmd_secret(_FakeArgs(
                action="ensure", config=str(cfg), user="alice",
                service="karakeep", key="api_key", value="",
            ))
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "value" in err.lower()


class TestSecretList:
    def test_list_prints_service_and_key_only(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, db_path = cfg_with_db
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "kk-secret-value")
        secrets_store.set_secret(db_path, "alice", "monarch", "session_id", "sid-alice")

        cmd_secret(_FakeArgs(action="list", config=str(cfg), user="alice"))
        out = capsys.readouterr().out
        assert "karakeep" in out
        assert "api_key" in out
        assert "monarch" in out
        assert "session_id" in out
        # Plaintext never leaks.
        assert "kk-secret-value" not in out
        assert "sid-alice" not in out

    def test_list_empty_for_user_with_no_secrets(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        cmd_secret(_FakeArgs(action="list", config=str(cfg), user="ghost"))
        out = capsys.readouterr().out
        assert "no secrets" in out.lower()


class TestSecretRemove:
    def test_remove_deletes_row(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, db_path = cfg_with_db
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "kk")
        cmd_secret(_FakeArgs(
            action="remove", config=str(cfg), user="alice",
            service="karakeep", key="api_key",
        ))
        assert "STATE: removed" in capsys.readouterr().out
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") is None

    def test_remove_missing_is_noop(self, cfg_with_db, capsys):
        from istota.cli import cmd_secret

        cfg, _ = cfg_with_db
        cmd_secret(_FakeArgs(
            action="remove", config=str(cfg), user="alice",
            service="karakeep", key="api_key",
        ))
        assert "STATE: noop" in capsys.readouterr().out
