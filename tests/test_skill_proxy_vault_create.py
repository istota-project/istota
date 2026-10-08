"""Create a vault credential through the task's real proxy socket."""

import json
import socket
import sqlite3
import tempfile
from pathlib import Path

import pytest

from tests.support.kdbx import create_database

from istota import db, doctor
from istota.credentials import store as secrets_store
from istota.credentials import vault as secrets_vault
from istota.sandbox import credential_shim
from istota.config import Config, UserConfig
from istota.sandbox.skill_proxy import SkillProxy


@pytest.fixture
def sock():
    directory = Path(tempfile.mkdtemp(prefix="vault-proxy-", dir="/tmp"))
    path = directory / "s.sock"
    yield path
    path.unlink(missing_ok=True)
    directory.rmdir()


def _request(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(path))
        conn.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                raise AssertionError("proxy closed without a reply")
            data += chunk
    return json.loads(data)


def test_create_is_available_in_the_same_task_and_never_returns_password(tmp_path, monkeypatch, sock, caplog):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig(vault_path=str(path), email_addresses=["alice@example.com"])},
    )
    config.email.enabled = True
    config.email.bot_email = "bot@example.com"
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_credentials={}, vault_write_limit=2) as proxy:
        reply = _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        assert reply == {
            "name": "generated_acme", "username_name": "generated_acme_username",
            "url_name": "generated_acme_url", "username": "bot+alice+acme@example.com",
            "confirmation_readable": True,
        }
        assert _request(sock, {"type": "vault_credential", "name": reply["name"]})["value"] == proxy.vault_credentials[reply["name"]]
        assert proxy.vault_credentials[reply["name"]] not in json.dumps(reply)
        assert proxy.vault_credentials[reply["name"]] not in caplog.text
        read = secrets_vault.parse_vault(path.read_bytes(), "test-passphrase")
        assert read.generated[reply["name"]]["password"] == proxy.vault_credentials[reply["name"]]
        assert secrets_store.get_secret(
            config.db_path, "alice", "vault_entries", reply["name"],
        ) == proxy.vault_credentials[reply["name"]]
        assert secrets_vault.vault_status(config, "alice").generated_count == 1
        assert secrets_vault.vault_status(config, "alice", parse=False).generated_count == 1
        report = doctor._vault_contents_result(config, secrets_vault, "security.vault_contents", ["alice"])
        assert "1 in generated/" in report.detail
        with db.get_db(config.db_path) as conn:
            assert db.signup_tag(conn, "alice+acme")["user_id"] == "alice"
            notice = conn.execute(
                "SELECT dedup_key, severity FROM notifications WHERE user_id = ? AND source = ?",
                ("alice", "task_alert"),
            ).fetchone()
        assert notice["dedup_key"].startswith("vault-created:generated_acme")
        assert notice["severity"] == "warning"
        assert _request(sock, {"type": "vault_create", "slug": "acme"})["reason"] != "vault_write_limit"
        assert _request(sock, {"type": "vault_create", "slug": "other"})["reason"] == "vault_write_limit"


def test_zero_budget_disables_writes(sock):
    with SkillProxy(sock, {}, {}, vault_write_limit=0):
        assert _request(sock, {"type": "vault_create", "slug": "acme"})["reason"] == "vault_write_limit"


def test_signup_address_must_not_name_another_user(tmp_path, monkeypatch, sock):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={
            "alice": UserConfig(vault_path=str(path)),
            "alice+team": UserConfig(),
        },
    )
    config.security.allow_unsandboxed_multi_user_vaults = True
    config.email.enabled = True
    config.email.bot_email = "bot@example.com"
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")

    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_create", "slug": "team"})
    assert reply["reason"] == "vault_write_refused"
    assert "generated_team" not in secrets_vault.parse_vault(path.read_bytes(), "test-passphrase").generated
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "generated_team") is None


@pytest.mark.parametrize("stored", [False, True])
def test_stale_pending_signup_tag_recovers_from_the_store(
    tmp_path, monkeypatch, sock, stored,
):
    """A reservation a killed task left pending is reclaimed after five
    minutes, unless the credential it was for was stored."""
    from istota.credentials import generated

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig()},
    )
    config.email.enabled = True
    config.email.bot_email = "bot@example.com"
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        assert db.reserve_signup_tag(conn, "alice", "acme")
        conn.execute(
            "UPDATE signup_tags SET reserved_at = datetime('now', '-10 minutes') WHERE tag = ?",
            ("alice+acme",),
        )
        if stored:
            generated.create(conn, "alice", name="generated_acme", username="bot+alice+acme@example.com",
                             password="saved-password", url="", mirror=False)

    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_create", "slug": "acme"})
    with db.get_db(config.db_path) as conn:
        tag = db.signup_tag(conn, "alice+acme")
    assert tag is not None
    assert ("name" in reply) is (not stored)
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme")


def test_recent_pending_signup_tag_remains_busy(tmp_path, monkeypatch, sock):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig(vault_path=str(path))},
    )
    config.email.enabled = True
    config.email.bot_email = "bot@example.com"
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    with db.get_db(config.db_path) as conn:
        assert db.reserve_signup_tag(conn, "alice", "acme")

    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_create", "slug": "acme"})
    assert reply["reason"] == "VaultWriteRefused"
    with db.get_db(config.db_path) as conn:
        assert conn.execute(
            "SELECT opened_at FROM signup_tags WHERE tag = ?", ("alice+acme",),
        ).fetchone()[0] is None
    assert "generated_acme" not in secrets_vault.parse_vault(path.read_bytes(), "test-passphrase").generated
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") is None


def test_shim_new_sends_only_policy_and_names(monkeypatch, capsys):
    seen = []

    def answer(payload, **kwargs):
        seen.append(payload)
        assert kwargs["timeout"] == credential_shim.CREATE_TIMEOUT_SECONDS
        return {
            "name": "generated_acme", "username_name": "generated_acme_username",
            "url_name": "generated_acme_url", "username": "alice@example.com",
        }

    monkeypatch.setattr(credential_shim, "_request", answer)
    assert credential_shim.main(["new", "acme", "--url", "https://acme.example", "--length", "18", "--no-symbols"]) == 0
    assert seen == [{"type": "vault_create", "slug": "acme", "url": "https://acme.example", "length": 18, "symbols": False}]
    assert json.loads(capsys.readouterr().out)["name"] == "generated_acme"


def test_a_failed_apply_after_the_mirror_still_returns_the_stored_credential(
    tmp_path, monkeypatch, sock,
):
    """The table is written before the file, so an apply that fails after
    the mirror's replace costs nothing but a sync retry."""
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig(vault_path=str(path), email_addresses=["alice@example.com"])},
    )
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)

    def fail_apply(*_):
        raise sqlite3.OperationalError("database busy")

    original_apply = secrets_vault.apply_vault
    monkeypatch.setattr(secrets_vault, "apply_vault", fail_apply)
    secrets_vault.reset_sync_state("alice")
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1) as proxy:
        reply = _request(sock, {"type": "vault_create", "slug": "acme"})
        assert reply["name"] == "generated_acme"
        password = proxy.vault_credentials[reply["name"]]
        assert secrets_vault.parse_vault(path.read_bytes(), "test-passphrase").generated[
            reply["name"]]["password"] == password
        assert "alice" not in secrets_vault._SYNC_STATE
    monkeypatch.setattr(secrets_vault, "apply_vault", original_apply)
    secrets_vault.sync_user(config, "alice", deliver=False)
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == password


def test_generated_count_comes_from_the_group_not_a_flat_name(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    kp.add_entry(kp.root_group, "generated_acme", "alice@example.com", "old-value")
    kp.save()
    read = secrets_vault.parse_vault(path.read_bytes(), "test-passphrase")
    assert read.generated_count == 0
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig(vault_path=str(path))},
    )
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    secrets_vault.sync_user(config, "alice", force=True, deliver=False)
    assert secrets_vault.vault_status(config, "alice", parse=False).generated_count == 0

    group = kp.add_group(kp.root_group, "generated")
    kp.add_entry(group, "other", "alice@example.com", "new-value")
    kp.save()
    assert secrets_vault.parse_vault(path.read_bytes(), "test-passphrase").generated_count == 1
    secrets_vault.sync_user(config, "alice", force=True, deliver=False)
    assert secrets_vault.vault_status(config, "alice", parse=False).generated_count == 1


def test_multi_user_vault_create_requires_opt_in(tmp_path, monkeypatch, sock):
    config = Config(db_path=tmp_path / "test.db", users={
        "alice": UserConfig(vault_path="vault.kdbx"), "bob": UserConfig(),
    })
    db.init_db(config.db_path)
    monkeypatch.setattr("istota.executor._bwrap_available", lambda: False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_create", "slug": "acme"})
    assert reply["reason"] == "vault_isolation_required"
    assert "allow_unsandboxed_multi_user_vaults" in reply["error"]


@pytest.mark.parametrize("source", ["local", "vault"])
@pytest.mark.parametrize("taken", ["generated_acme", "generated_acme_url"])
def test_a_name_held_by_any_stored_row_is_refused(tmp_path, monkeypatch, sock, taken, source):
    """A generated name never shares a row with another credential, whoever wrote it."""
    from istota.credentials.broker.bindings import parse_binding

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    before = path.read_bytes()
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig(vault_path=str(path), email_addresses=["alice@example.com"])},
    )
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    secrets_store.set_secret(
        config.db_path, "alice", secrets_vault.VAULT_ENTRY_SERVICE, taken, "typed-in-istota",
        binding=parse_binding("acme.example", {}, [], source=source),
    )

    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_create", "slug": "acme"})

    assert reply["reason"] == "VaultWriteRefused"
    assert "already exists" in reply["error"] and taken in reply["error"]
    assert path.read_bytes() == before
    assert secrets_store.get_secret(
        config.db_path, "alice", secrets_vault.VAULT_ENTRY_SERVICE, taken,
    ) == "typed-in-istota"
