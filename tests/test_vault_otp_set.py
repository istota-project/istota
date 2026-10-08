"""Set-once enrollment of a generated credential through the task proxy (ISSUE-686)."""
from istota.credentials import kdbx_import as credential_read

import io
import json

import pytest
from pykeepass import PyKeePass

from istota import db
from istota.config import Config, UserConfig
from istota.credentials import store
from istota.lib import totp
from istota.sandbox import credential_shim
from istota.sandbox.skill_proxy import SkillProxy
from tests.support.kdbx import create_database
from tests import test_skill_proxy_vault_create as _vault_create
from tests.test_skill_proxy_vault_create import _request

# Bound by assignment, not import: ruff reads a test parameter named after an
# imported fixture as a redefinition (F811), and pytest finds either.
sock = _vault_create.sock

SEED = "JBSWY3DP" * 4


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    group = kp.add_group(kp.root_group, "generated")
    kp.add_entry(group, "acme", "alice", "test-value", url="https://acme.example")
    kp.save()
    config = Config(db_path=tmp_path / "test.db", users={"alice": UserConfig(vault_path=str(path))})
    db.init_db(config.db_path)
    store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    preview = credential_read.preview(config.db_path, "alice", path.read_bytes(), "test-passphrase")
    credential_read.apply(config.db_path, "alice", path.read_bytes(), "test-passphrase", selected=[i.name for i in preview.items if i.default_selected], expected_digest=preview.digest, actor="import")
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    return config, path


def test_set_stores_the_seed_and_leaves_the_file(configured, sock):
    config, path = configured
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED}) == {
            "name": "generated_acme", "otp": True}
    stored = store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_totp")
    assert totp.parse_otpauth(stored) == totp.parse_user_input(SEED)
    entry = PyKeePass(str(path), password="test-passphrase").find_entries(title="acme", first=True)
    assert not entry.otp


def test_a_second_enrollment_is_refused_and_writes_nothing(configured, sock):
    config, path = configured
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
        before = path.read_bytes()
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": "JBSWY3DP" * 2})
    assert reply["reason"] == "otp_already_set"
    assert path.read_bytes() == before
    stored = store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_totp")
    assert totp.parse_otpauth(stored) == totp.parse_user_input(SEED)


@pytest.mark.parametrize("source", ["vault", "local"])
def test_a_credential_istota_did_not_generate_is_refused(configured, sock, source):
    """The table's source decides, never the name: a KeePass entry the user
    made, even one titled `generated_x`, is theirs to enroll."""
    from istota.credentials.broker.bindings import parse_binding
    config, path = configured
    store.upsert_secret(config.db_path, "alice", "vault_entries", "generated_mine", "value",
                        binding=parse_binding("acme.example", {}, [], source=source))
    before = path.read_bytes()
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_mine", "otp": SEED})
    assert reply["reason"] == "otp_set_not_generated"
    assert path.read_bytes() == before
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_mine_totp") is None


def test_public_write_notice_and_shared_budget(configured, sock, caplog):
    config, path = configured
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
        assert reply == {"name": "generated_acme", "otp": True}
        assert "generated_acme_totp" in proxy.vault_credentials
        assert "generated_acme_totp" not in proxy._created_names
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})["reason"] == "otp_already_set"
        assert _request(sock, {"type": "vault_create", "slug": "other"})["reason"] == "vault_write_limit"
        assert _request(sock, {"type": "vault_credential", "name": "generated_acme_totp"})["reason"] == "credential_is_otp_seed"
    with db.get_db(config.db_path) as conn:
        notice = conn.execute("SELECT title, severity FROM notifications WHERE source='task_alert'").fetchone()
    assert notice["title"] == "Istota added two-factor to generated acme"
    assert notice["severity"] == "warning"
    assert SEED not in caplog.text


def test_zero_budget_and_invalid_input_leave_the_file(configured, sock):
    config, path = configured
    before = path.read_bytes()
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=0):
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})["reason"] == "vault_write_limit"
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        reply = _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": "invalid seed!"})
    assert reply["reason"] == "invalid_otp"
    assert "invalid seed!" not in json.dumps(reply)
    assert path.read_bytes() == before


def test_shim_reads_only_stdin(monkeypatch, capsys):
    seen = []

    def answer(payload, **kwargs):
        seen.append(payload)
        assert kwargs["timeout"] == credential_shim.CREATE_TIMEOUT_SECONDS
        return {"name": "generated_acme", "otp": True}

    monkeypatch.setattr(credential_shim, "_request", answer)
    monkeypatch.setattr("sys.stdin", io.StringIO(SEED + "\n"))
    assert credential_shim.main(["otp-set", "generated_acme"]) == 0
    assert seen == [{"type": "vault_otp_set", "name": "generated_acme", "otp": SEED}]
    assert SEED not in capsys.readouterr().out
    assert credential_shim.main(["otp-set", "generated_acme", SEED]) == credential_shim.EXIT_REFUSED
    assert len(seen) == 1


def test_create_then_enroll_keeps_same_task_access(configured, sock):
    config, path = configured
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        created = _request(sock, {"type": "vault_create", "slug": "other", "username": "alice", "url": "https://acme.example"})
        reply = _request(sock, {"type": "vault_otp_set", "name": created["name"], "otp": SEED})
        assert reply == {"name": "generated_other", "otp": True}
        assert "generated_other_totp" in proxy._created_names
