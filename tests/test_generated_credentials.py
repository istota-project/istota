"""Credentials Istota generates live in the secrets table (ISSUE-686).

`new` and `otp-set` write the table; the KeePass file is an optional one-way
mirror, and a sync never deletes or overwrites a generated entry from it.
"""

import io

import pytest
from pykeepass import PyKeePass

from istota import db
from istota.config import Config, UserConfig
from istota.credentials import generated
from istota.credentials import store
from istota.credentials import vault
from istota.credentials.broker import bindings, grants
from istota.lib import totp
from istota.sandbox.skill_proxy import SkillProxy
from tests import test_skill_proxy_vault_create as _vault_create
from tests.support.kdbx import create_database
from tests.test_skill_proxy_otp import ask
from tests.test_skill_proxy_vault_create import _request

sock = _vault_create.sock

SEED = "JBSWY3DP" * 4
PASSPHRASE = "test-passphrase"


def _config(tmp_path, monkeypatch, *, with_vault):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    user = UserConfig(email_addresses=["alice@example.com"])
    if with_vault:
        create_database(str(path), password=PASSPHRASE)
        user = UserConfig(vault_path=str(path), email_addresses=["alice@example.com"])
    config = Config(db_path=tmp_path / "daemon" / "test.db", workspace_path=tmp_path / "workspace",
                    users={"alice": user})
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    if with_vault:
        store.upsert_secret(config.db_path, "alice", "vault", "passphrase", PASSPHRASE)
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    return config, path


def _task(config, room="room-a"):
    with db.get_db(config.db_path) as conn:
        return db.create_task(conn, user_id="alice", prompt="sign up", source_type="talk",
                              conversation_token=room)


def _file_entries(path):
    read = vault.parse_vault(path.read_bytes(), PASSPHRASE)
    return read.generated


def _state(config, name):
    with db.get_db(config.db_path) as conn:
        return generated.mirror_state(conn, "alice", name)


def _sync(config):
    return vault.sync_user(config, "alice", force=True, deliver=False)


def test_new_and_otp_set_work_with_no_vault(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        created = _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        assert created["name"] == "generated_acme"
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED}) == {
            "name": "generated_acme", "otp": True}
        password = proxy.vault_credentials["generated_acme"]
    assert not path.exists()
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == password
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "generated_acme")["source"] == generated.SOURCE
        seed = bindings.get_binding(conn, "alice", "generated_acme_totp")
        assert seed["kind"] == "totp" and seed["hosts"] == ["acme.example"]
        assert bindings.credential_name(conn, "alice", "generated_acme_totp") == "generated_acme"
    stored = store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_totp")
    assert totp.parse_otpauth(stored) == totp.parse_user_input(SEED)
    assert _state(config, "generated_acme")["state"] == "off"


def test_a_later_task_in_the_conversation_fills_otp_by_either_name(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    config.security.credential_broker.enabled = True
    first = _task(config)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=str(first),
                    vault_write_limit=2):
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
    second = _task(config)
    with db.get_db(config.db_path) as conn:
        grants.ensure_credential_grants(conn, second, "alice")
    snapshot = store.get_service_secrets(config.db_path, "alice", "vault_entries")
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=str(second),
                    vault_credentials=snapshot) as server:
        for name in ("generated_acme", "generated_acme_totp"):
            assert "code" in ask(server, sock, {"type": "vault_otp", "name": name}, True)


def test_mirror_writes_the_entry_and_its_otp_to_the_file(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as proxy:
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
        password = proxy.vault_credentials["generated_acme"]
    copy = _file_entries(path)["generated_acme"]
    assert copy["password"] == password
    assert totp.parse_otpauth(copy["otp"]) == totp.parse_user_input(SEED)
    assert _state(config, "generated_acme") == {"mirror": True, "state": "mirrored", "divergence": []}


@pytest.mark.parametrize("older", ["before_create", "before_otp"])
def test_an_older_file_deletes_nothing_and_reports_divergence(tmp_path, monkeypatch, sock, older):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    snapshots = {"before_create": path.read_bytes()}
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        snapshots["before_otp"] = path.read_bytes()
        _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
    before = store.get_service_secrets(config.db_path, "alice", "vault_entries")

    path.write_bytes(snapshots[older])
    _sync(config)

    assert store.get_service_secrets(config.db_path, "alice", "vault_entries") == before
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "generated_acme_totp")["kind"] == "totp"
        notice = conn.execute(
            "SELECT title FROM notifications WHERE source='task_alert' AND dedup_key LIKE ?",
            ("generated-diverged:%",),
        ).fetchone()
    expected = "missing" if older == "before_create" else "missing_otp"
    assert _state(config, "generated_acme")["divergence"] == [expected]
    assert _state(config, "generated_acme")["state"] == "diverged"
    assert notice is not None and "generated_acme" in notice["title"].replace(" ", "_")


def test_an_edit_in_the_file_is_reported_not_applied(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1) as proxy:
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        password = proxy.vault_credentials["generated_acme"]
    kp = PyKeePass(str(path), password=PASSPHRASE)
    kp.find_entries(title="acme", first=True).password = "edited-in-keepassxc"
    kp.save()
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == password
    assert _state(config, "generated_acme")["divergence"] == ["changed"]
    vault.mirror_generated(config, "alice", "generated_acme")
    assert _file_entries(path)["generated_acme"]["password"] == password
    assert _state(config, "generated_acme")["state"] == "mirrored"


def test_a_changed_file_during_the_mirror_keeps_the_table_and_retries(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    original = vault.mirror_entry
    calls = []

    def changed(*args, **kwargs):
        calls.append(1)
        raise vault.VaultChanged("the vault changed since it was read")

    monkeypatch.setattr(vault, "mirror_entry", changed)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1) as proxy:
        reply = _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        password = proxy.vault_credentials["generated_acme"]
    assert reply["name"] == "generated_acme" and calls
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == password
    assert "generated_acme" not in _file_entries(path)
    assert _state(config, "generated_acme")["state"] == "pending"

    monkeypatch.setattr(vault, "mirror_entry", original)
    _sync(config)
    assert _file_entries(path)["generated_acme"]["password"] == password
    assert _state(config, "generated_acme")["state"] == "mirrored"


def test_with_mirroring_off_the_file_is_never_written(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with db.get_db(config.db_path) as conn:
        generated.set_default_mirror(conn, "alice", False)
    before = path.read_bytes()
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        assert _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})["name"]
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})["otp"]
    _sync(config)
    assert path.read_bytes() == before
    assert _state(config, "generated_acme") == {"mirror": False, "state": "off", "divergence": []}


def test_retire_removes_the_rows_the_grant_and_the_mirror(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    task = _task(config)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=str(task),
                    vault_write_limit=2):
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
        _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED})
        # Nothing a task can send retires a credential.
        assert "name" not in _request(sock, {"type": "vault_retire", "name": "generated_acme"})
    assert generated.retire(config, "alice", "generated_acme") is True
    assert not [key for key in store.get_service_secrets(config.db_path, "alice", "vault_entries")
                if key.startswith("generated_acme")]
    with db.get_db(config.db_path) as conn:
        assert grants.get_grant(conn, "alice", "generated_acme") is None
    assert "generated_acme" not in _file_entries(path)
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") is None


def test_retire_refuses_a_credential_istota_did_not_generate(tmp_path, monkeypatch):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    store.upsert_secret(config.db_path, "alice", "vault_entries", "github", "token",
                        binding=bindings.parse_binding("github.com", {}, [], source="local"))
    with pytest.raises(generated.GeneratedCredentialError):
        generated.retire(config, "alice", "github")
    assert store.get_secret(config.db_path, "alice", "vault_entries", "github") == "token"


def test_legacy_rows_from_the_generated_group_are_adopted_not_swept(tmp_path, monkeypatch):
    """Before ISSUE-686 the sync imported `generated/` as `vault` rows; the
    first sync after it takes them over, so a file without them deletes nothing."""
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    kp = PyKeePass(str(path), password=PASSPHRASE)
    empty = path.read_bytes()
    group = kp.add_group(kp.root_group, "generated")
    entry = kp.add_entry(group, "acme", "alice@example.com", "legacy-password", url="https://acme.example")
    entry.otp = totp.to_uri(totp.parse_user_input(SEED))
    kp.add_entry(kp.root_group, "github", "alice", "user-token", url="https://github.com")
    kp.save()
    read = vault.parse_vault(path.read_bytes(), PASSPHRASE)
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name, value in {"generated_acme": "legacy-password", "generated_acme_url": "https://acme.example",
                            "generated_acme_totp": entry.otp}.items():
            binding = {**bindings.parse_binding("https://acme.example", {}, []), "credential": "generated_acme",
                       "kind": "totp" if name.endswith("_totp") else "value"}
            store.set_secret(None, "alice", "vault_entries", name, value, binding=binding, connection=conn)
    vault.apply_vault(config.db_path, "alice", read)

    path.write_bytes(empty)
    _sync(config)

    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == "legacy-password"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_totp") == entry.otp
    assert store.get_secret(config.db_path, "alice", "vault_entries", "github") is None
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "generated_acme")["source"] == generated.SOURCE
    assert _state(config, "generated_acme")["divergence"] == ["missing"]


def test_a_file_only_generated_entry_is_imported_once_as_generated(tmp_path, monkeypatch):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    kp = PyKeePass(str(path), password=PASSPHRASE)
    group = kp.add_group(kp.root_group, "generated")
    kp.add_entry(group, "acme", "alice@example.com", "only-in-file", url="https://acme.example")
    kp.save()
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") == "only-in-file"
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "generated_acme")["source"] == generated.SOURCE
    assert _state(config, "generated_acme") == {"mirror": True, "state": "mirrored", "divergence": []}


def test_the_read_repr_carries_no_generated_value(tmp_path, monkeypatch):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    kp = PyKeePass(str(path), password=PASSPHRASE)
    kp.add_entry(kp.add_group(kp.root_group, "generated"), "acme", "alice", "secret-value")
    kp.save()
    read = vault.parse_vault(io.BytesIO(path.read_bytes()).getvalue(), PASSPHRASE)
    assert "secret-value" not in repr(read)
    assert "generated_acme" not in read.services


def test_a_users_own_entry_with_a_generated_prefix_is_still_theirs_to_delete(tmp_path, monkeypatch):
    """Adoption is a one-time upgrade step, not a name rule: an entry the user
    made under a group like `Generated Passwords` goes when they delete it."""
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    _sync(config)
    kp = PyKeePass(str(path), password=PASSPHRASE)
    kp.add_entry(kp.add_group(kp.root_group, "Generated Passwords"), "bank", "me", "user-secret")
    kp.save()
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_passwords_bank") == "user-secret"
    kp = PyKeePass(str(path), password=PASSPHRASE)
    kp.delete_entry(kp.find_entries(title="bank", first=True))
    kp.save()
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_passwords_bank") is None


@pytest.mark.parametrize("how", ["mirror_off", "stale_file"])
def test_a_retired_credential_is_never_imported_back(tmp_path, monkeypatch, sock, how):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
    stale = path.read_bytes()
    if how == "mirror_off":
        with db.get_db(config.db_path) as conn:
            generated.set_mirror(conn, "alice", "generated_acme", False)
    assert generated.retire(config, "alice", "generated_acme") is True
    if how == "stale_file":
        assert "generated_acme" not in _file_entries(path)
        path.write_bytes(stale)
    _sync(config)
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") is None


def test_deleting_a_member_row_retires_the_whole_credential(tmp_path, monkeypatch):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="u", password="pw",
                         url="https://acme.example", mirror=True)
    vault.mirror_generated(config, "alice", "generated_acme")
    assert generated.retire(config, "alice", "generated_acme_url") is True
    _sync(config)
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme") is None
    assert "generated_acme" not in _file_entries(path)


def test_a_rewrite_keeps_the_users_edit_in_keepass_history(tmp_path, monkeypatch, sock):
    config, path = _config(tmp_path, monkeypatch, with_vault=True)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1):
        _request(sock, {"type": "vault_create", "slug": "acme", "url": "https://acme.example"})
    kp = PyKeePass(str(path), password=PASSPHRASE)
    kp.find_entries(title="acme", first=True).password = "rotated-in-keepassxc"
    kp.save()
    vault.mirror_generated(config, "alice", "generated_acme")
    entry = PyKeePass(str(path), password=PASSPHRASE).find_entries(title="acme", first=True)
    assert "rotated-in-keepassxc" in [old.password for old in entry.history]
