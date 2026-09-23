"""Create a vault credential through the task's real proxy socket."""

import json
import socket
import tempfile
from pathlib import Path

import pytest

from pykeepass import create_database

from istota import credential_shim, db, doctor, secrets_store, secrets_vault
from istota.config import Config, UserConfig
from istota.skill_proxy import SkillProxy


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
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    monkeypatch.setattr("istota.notification_store.deliver_pending", lambda *_: None)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_credentials={}, vault_write_limit=2) as proxy:
        reply = _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        assert reply == {
            "name": "generated_acme", "username_name": "generated_acme_username",
            "url_name": "generated_acme_url", "username": "alice@example.com",
        }
        assert _request(sock, {"type": "vault_credential", "name": reply["name"]})["value"] == proxy.vault_credentials[reply["name"]]
        assert proxy.vault_credentials[reply["name"]] not in json.dumps(reply)
        assert proxy.vault_credentials[reply["name"]] not in caplog.text
        read = secrets_vault.parse_vault(path.read_bytes(), "test-passphrase")
        assert read.services[reply["name"]] == proxy.vault_credentials[reply["name"]]
        assert secrets_vault.vault_status(config, "alice").generated_count == 1
        assert secrets_vault.vault_status(config, "alice", parse=False).generated_count == 1
        report = doctor._vault_contents_result(config, secrets_vault, "security.vault_contents", ["alice"])
        assert "1 in generated/" in report.detail
        with db.get_db(config.db_path) as conn:
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
