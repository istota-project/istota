"""Stateless import through the parser and encrypted store."""
import io
from dataclasses import asdict

import pytest

from istota import db
from istota.credentials import generated, store, vault
from istota.credentials.broker import bindings, grants
from tests.support.kdbx import create_database

PASSPHRASE = "SENTINEL-import-passphrase-7f3a"
OTP = "otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&issuer=Example"


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def kdbx(entries, *, scoped=True, keyfile=None):
    out = io.BytesIO()
    kp = create_database(out, password=PASSPHRASE,
                         keyfile=io.BytesIO(keyfile) if keyfile is not None else None)
    group = kp.add_group(kp.root_group, "istota") if scoped else kp.root_group
    for spec in entries:
        spec = dict(spec)
        target = group
        if spec.pop("generated", False):
            target = kp.find_groups(name="generated", first=True) or kp.add_group(group, "generated")
        otp, recovery, tags = spec.pop("otp", None), spec.pop("recovery", None), spec.pop("tags", None)
        entry = kp.add_entry(target, username=spec.pop("username", ""),
                             password=spec.pop("password", "fixture-value"), **spec)
        if otp:
            entry.otp = otp
        if recovery:
            entry.set_custom_property(generated.RECOVERY_FIELD, recovery, protect=True)
        if tags:
            entry.tags = tags
    if scoped:
        kp.add_entry(kp.root_group, "outside", "", "not-imported")
    out = io.BytesIO()
    kp.save(out)
    return out.getvalue()


def seed(path, name, value, *, source="local", owner=None, url=""):
    store.set_secret(path, "alice", "vault_entries", name, value,
                     binding={**bindings.parse_binding(url, {}, [], source=source),
                              "credential": owner or name})


def test_preview_groups_and_selected_apply(database, caplog):
    from istota.credentials import kdbx_import
    seed(database, "changed", "fresh-value")
    seed(database, "changed_username", "old-user", owner="changed")
    seed(database, "same", "fixture-value")
    seed(database, "conflict", "config-value", source="config")
    seed(database, "untouched", "keep-me")
    data = kdbx([{"title": n} for n in ("new", "changed", "same", "conflict")]
                + [{"title": "empty", "password": ""}])
    preview = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    items = {i.name: i for i in preview.items}
    assert {n: i.status for n, i in items.items()} == {
        "new": "new", "changed": "changed", "same": "unchanged",
        "conflict": "conflict", "empty": "skipped"}
    assert items["new"].default_selected
    assert not items["changed"].default_selected
    assert set(items["changed"].changed_fields) == {"value", "username"}
    assert items["conflict"].reason == "name_taken_config"
    assert "fresh-value" not in repr(asdict(preview))
    result = kdbx_import.apply(database, "alice", data, PASSPHRASE,
                               selected=["new", "changed", "conflict", "same"],
                               expected_digest=preview.digest, actor="import")
    assert result.imported == ["new", "changed"]
    assert result.not_imported == {"conflict": "conflict", "same": "unchanged"}
    assert store.get_secret(database, "alice", "vault_entries", "changed_username") is None
    assert store.get_secret(database, "alice", "vault_entries", "untouched") == "keep-me"
    with db.get_db(database) as conn:
        assert bindings.get_binding(conn, "alice", "new")["source"] == "local"
        assert conn.execute("SELECT count(*) FROM credential_audit").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM secrets_history WHERE actor='import'").fetchone()[0] == 2
        assert PASSPHRASE not in "\n".join(conn.iterdump())
    assert PASSPHRASE not in caplog.text


def test_grants_generated_and_replacement(database):
    from istota.credentials import kdbx_import
    data = kdbx([{"title": "portal", "url": "https://portal.example", "otp": OTP},
                 {"title": "declined", "url": "https://portal.example", "tags": ["istota:nogrant"]},
                 {"title": "account", "generated": True, "url": "https://portal.example",
                  "otp": OTP, "recovery": "first-code\nsecond-code"}])
    preview = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    selected = [i.name for i in preview.items if i.default_selected]
    kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=selected,
                      expected_digest=preview.digest, actor="import")
    with db.get_db(database) as conn:
        assert grants.get_grant(conn, "alice", "portal") is not None
        assert grants.get_grant(conn, "alice", "declined") is None
        assert grants.get_grant(conn, "alice", "generated_account") is None
        assert bindings.daemon_only_refusal(conn, "alice", "portal_totp") == "credential_is_otp_seed"
        assert generated.is_generated(conn, "alice", "generated_account")
        assert generated.read_recovery(conn, "alice", "generated_account") == "first-code\nsecond-code"
    assert all(i.status == "unchanged" for i in kdbx_import.preview(database, "alice", data, PASSPHRASE).items)
    changed = kdbx([{"title": "account", "generated": True, "password": "replacement",
                     "url": "https://other.example", "otp": OTP.replace("JBSWY3DPEHPK3PXP", "GEZDGNBVGY3TQOJQ"),
                     "recovery": "replacement-code"}])
    p = kdbx_import.preview(database, "alice", changed, PASSPHRASE)
    assert p.items[0].status == "changed"
    kdbx_import.apply(database, "alice", changed, PASSPHRASE, selected=["generated_account"],
                      expected_digest=p.digest, actor="import")
    with db.get_db(database) as conn:
        assert generated.stored_values(conn, "alice", "generated_account")["password"] == "replacement"
        assert generated.read_recovery(conn, "alice", "generated_account") == "replacement-code"
        with pytest.raises(generated.GeneratedCredentialError, match="otp_already_set"):
            generated.set_otp(conn, "alice", "generated_account", OTP)


def test_digest_selection_and_keyfile(database):
    from istota.credentials import kdbx_import
    key = b"k" * 32
    data = kdbx([{"title": "keyed"}], keyfile=key)
    for supplied in (None, b"x" * 32):
        with pytest.raises(vault.VaultLocked):
            kdbx_import.preview(database, "alice", data, PASSPHRASE, keyfile=supplied)
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE, keyfile=key)
    for selected, digest, reason in [(["keyed"], "0" * 64, "import_file_changed"),
                                     ([], p.digest, "import_nothing_selected")]:
        with pytest.raises(ValueError, match=reason):
            kdbx_import.apply(database, "alice", data, PASSPHRASE, keyfile=key,
                              selected=selected, expected_digest=digest, actor="import")
    kdbx_import.apply(database, "alice", data, PASSPHRASE, keyfile=key,
                      selected=["keyed"], expected_digest=p.digest, actor="import")
    assert store.get_secret(database, "alice", "vault_entries", "keyed") == "fixture-value"


def test_conflict_rechecked_and_unselected_kept(database):
    from istota.credentials import kdbx_import
    data = kdbx([{"title": "account", "generated": True}, {"title": "other"}])
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    seed(database, "generated_account", "new-local-value")
    result = kdbx_import.apply(database, "alice", data, PASSPHRASE,
                               selected=["generated_account"], expected_digest=p.digest, actor="import")
    assert result.not_imported == {"generated_account": "conflict"}
    assert store.get_secret(database, "alice", "vault_entries", "generated_account") == "new-local-value"
    assert store.get_secret(database, "alice", "vault_entries", "other") is None


@pytest.mark.parametrize("bound", [False, True])
def test_import_does_not_take_another_owners_member(database, bound):
    from istota.credentials import kdbx_import
    store.set_secret(database, "alice", "vault_entries", "portal_username", "independent",
                     binding={**bindings.parse_binding("", {}, [], source="local"),
                              "credential": "portal_username"} if bound else None)
    data = kdbx([{"title": "portal", "username": "imported"}])
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    assert p.items[0].status == "conflict"
    result = kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=["portal"],
                               expected_digest=p.digest, actor="import")
    assert result.imported == []
    assert store.get_secret(database, "alice", "vault_entries", "portal_username") == "independent"


def test_entry_and_generated_name_collision_is_skipped(database):
    from istota.credentials import kdbx_import
    data = kdbx([{"title": "generated_account"}, {"title": "account", "generated": True}])
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    assert p.items[0].status == "skipped"
    assert p.items[0].reason == vault.SKIP_DUPLICATE_NAME
    result = kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=["generated_account"],
                               expected_digest=p.digest, actor="import")
    assert result.imported == []


def test_unscoped_import_has_no_auto_grant_and_is_user_isolated(database):
    from istota.credentials import kdbx_import
    data = kdbx([{"title": "portal", "url": "https://portal.example"}], scoped=False)
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    assert not p.scoped
    kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=["portal"],
                      expected_digest=p.digest, actor="import")
    with db.get_db(database) as conn:
        assert grants.get_grant(conn, "alice", "portal") is None
    assert kdbx_import.preview(database, "bob", data, PASSPHRASE).items[0].status == "new"


def test_legacy_source_compares_as_local(database):
    from istota.credentials import kdbx_import
    seed(database, "portal", "old", source="vault")
    data = kdbx([{"title": "portal"}])
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    assert p.items[0].status == "changed"
    kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=["portal"],
                      expected_digest=p.digest, actor="import")
    with db.get_db(database) as conn:
        assert bindings.get_binding(conn, "alice", "portal")["source"] == "local"


def test_imported_otp_is_refused_at_all_task_read_seams(database, monkeypatch):
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    import h11
    from istota.config import Config
    from istota.credentials import kdbx_import
    from istota.credentials.broker import intercept
    from istota.sandbox import credential_shim
    from istota.sandbox.skill_proxy import SkillProxy
    from tests.test_vault_credential_fetch import request

    data = kdbx([{"title": "portal", "url": "https://portal.example", "otp": OTP}])
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=["portal"],
                      expected_digest=p.digest, actor="import")
    config = Config(db_path=database)
    values = store.get_service_secrets(database, "alice", "vault_entries")
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="imp_") as folder:
        sock = Path(folder) / "s"
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock))
        with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_credentials=values):
            response = request(sock, {"type": "vault_credential", "name": "portal_totp", "mode": "read"})
            assert response["reason"] == "credential_is_otp_seed"
            assert "JBSWY3DPEHPK3PXP" not in json.dumps(response)
            with pytest.raises(credential_shim.ProxyError, match="credential_is_otp_seed"):
                credential_shim.fetch_credential("portal_totp", "read")
    broker = SimpleNamespace(config=config, user_id="alice", task_id=1)
    request = h11.Request(method="GET", target="/", headers=[
        ("Host", "portal.example"), ("Authorization", "Bearer {{cred:portal_totp}}")])
    with pytest.raises(intercept.Refused, match="credential_is_otp_seed"):
        intercept._headers(broker, request, "portal.example")


def test_generated_import_checks_reserved_member_names(database):
    from istota.credentials import kdbx_import
    seed(database, "generated_account_username", "independent")
    data = kdbx([{"title": "account", "generated": True}])
    p = kdbx_import.preview(database, "alice", data, PASSPHRASE)
    assert p.items[0].status == "conflict"
    result = kdbx_import.apply(database, "alice", data, PASSPHRASE, selected=["generated_account"],
                               expected_digest=p.digest, actor="import")
    assert result.not_imported == {"generated_account": "conflict"}
