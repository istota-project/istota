"""Regressions at the import, owner-edit, history and export seams."""
import io

import pytest
from pykeepass import PyKeePass

from istota import db
from istota.credentials import generated, kdbx_import, local, recovery_fill, store
from istota.credentials.broker import bindings
from tests.test_kdbx_export import cheap, PASSWORD  # noqa: F401
from tests.test_kdbx_import import database, kdbx, PASSPHRASE, seed  # noqa: F401


def import_acme(db_path, *, password="fixture", fields=None):
    raw = kdbx([{"title": "Acme", "password": password, "username": "alice", "url": "https://old.example"}])
    kp = PyKeePass(io.BytesIO(raw), password=PASSPHRASE)
    entry = kp.find_entries(title="Acme", first=True)
    entry.tags = ["istota:reveal"]
    for name, value in (fields or {}).items():
        entry.set_custom_property(name, value, protect=True)
    output = io.BytesIO()
    kp.save(output)
    data = output.getvalue()
    preview = kdbx_import.preview(db_path, "alice", data, PASSPHRASE)
    assert kdbx_import.apply(db_path, "alice", data, PASSPHRASE, selected=["acme"],
                             expected_digest=preview.digest, actor="import").imported == ["acme"]


@pytest.mark.parametrize("password", ["fixture", ""])
def test_imported_custom_fields_follow_owner_edits(database, password):  # noqa: F811
    import_acme(database, password=password, fields={"Token": "fixture-token", "istota_hosts": "other.example"})
    with db.get_db(database) as conn:
        assert local.stored_fields(conn, "alice", "acme")["extra_hosts"] == "other.example"
        local.update(conn, "alice", "acme", value=None, username=None, url="https://new.example",
                     extra_hosts="api.example", headers="x-api-key", revealable=False)
        for name in bindings.credential_groups(conn, "alice")["acme"]:
            binding = bindings.get_binding(conn, "alice", name)
            assert binding["hosts"] == ["api.example", "new.example"]
            assert binding["headers"] == ["x-api-key"]
            assert binding["revealable"] is False
        assert not local.is_local(conn, "alice", "acme_token")
    assert store.get_secret(database, "alice", "vault_entries", "acme_token") == "fixture-token"
    assert local.delete(database, "alice", "acme")
    with db.get_db(database) as conn:
        assert not bindings.credential_groups(conn, "alice")


def test_custom_totp_is_an_ordinary_export_field(database, cheap):  # noqa: F811
    import_acme(database, fields={"TOTP": "custom-value"})
    data, summary = cheap.build_kdbx(database, "alice", password=PASSWORD, keyfile=None, options=cheap.INTERACTIVE)
    read = kdbx_import.parse_vault(data, PASSWORD)
    assert read.services["acme_totp"] == "custom-value"
    assert read.bindings["acme_totp"].get("kind", "value") == "value"
    assert summary.otp == 0


def test_restore_history_across_vault_retirement(database):  # noqa: F811
    seed(database, "acme", "old-value", source="vault")
    import_acme(database)
    history = store.list_history(database, "alice", "acme")
    store.restore_history(database, "alice", history[0]["id"], actor="restore")
    assert store.get_secret(database, "alice", "vault_entries", "acme") == "old-value"
    with db.get_db(database) as conn:
        assert bindings.get_binding(conn, "alice", "acme")["source"] == "local"


def test_restored_recovery_set_does_not_reuse_replacement_state(database):  # noqa: F811
    with db.get_db(database) as conn:
        generated.create(conn, "alice", name="generated_site", username="alice", password="fixture", url="https://site.example")
        generated.set_recovery(conn, "alice", "generated_site", "old1\nold2\nold3", fmt="codes")
        conn.execute("UPDATE recovery_code_state SET spent='[0]'")
        generated.set_recovery(conn, "alice", "generated_site", "new1", fmt="codes")
    history_id = store.list_history(database, "alice", "generated_site")[0]["id"]
    store.restore_history(database, "alice", history_id, actor="restore")
    with db.get_db(database) as conn:
        assert generated.recovery_state(conn, "alice", "generated_site") == {
            "format": "block", "spent": [], "total": 3, "remaining": None}
        task = db.create_task(conn, user_id="alice", prompt="sign in", source_type="web")
        db.update_task_status(conn, task, "running")
        with pytest.raises(recovery_fill.RecoveryFillError, match="recovery_fill_format"):
            recovery_fill.claim(conn, user_id="alice", task_id=task, name="generated_site",
                                host="site.example", enrollment=True)


@pytest.mark.parametrize("fmt,text,spent", [
    ("codes", "abcd1234\nefgh5678", []),
    ("codes", "abcd1234\nefgh5678", [0]),
    ("phrase", "alpha beta gamma delta", []),
    ("block", "(used) literal text\nsecond line", []),
])
def test_recovery_round_trip_preserves_format_and_state(database, cheap, tmp_path, fmt, text, spent):  # noqa: F811
    import json
    with db.get_db(database) as conn:
        generated.create(conn, "alice", name="generated_site", username="alice", password="fixture", url="https://site.example")
        generated.set_recovery(conn, "alice", "generated_site", text, fmt=fmt)
        conn.execute("UPDATE recovery_code_state SET spent=?", (json.dumps(spent),))
        expected = generated.recovery_state(conn, "alice", "generated_site")
    data, _ = cheap.build_kdbx(database, "alice", password=PASSWORD, keyfile=None, options=cheap.INTERACTIVE)
    target = tmp_path / "restored.db"
    db.init_db(target)
    preview = kdbx_import.preview(target, "alice", data, PASSWORD)
    kdbx_import.apply(target, "alice", data, PASSWORD, selected=["generated_site"],
                      expected_digest=preview.digest, actor="import")
    with db.get_db(target) as conn:
        assert generated.read_recovery(conn, "alice", "generated_site") == text
        assert generated.recovery_state(conn, "alice", "generated_site") == expected
    assert kdbx_import.preview(target, "alice", data, PASSWORD).items[0].status == "unchanged"


def test_restoring_identical_recovery_value_keeps_spent_state(database):  # noqa: F811
    with db.get_db(database) as conn:
        generated.create(conn, "alice", name="generated_site", username="alice", password="fixture", url="https://site.example")
        generated.set_recovery(conn, "alice", "generated_site", "old1\nold2", fmt="codes")
        generated.set_recovery(conn, "alice", "generated_site", "new1", fmt="codes")
    history_id = store.list_history(database, "alice", "generated_site")[0]["id"]
    with db.get_db(database) as conn:
        generated.set_recovery(conn, "alice", "generated_site", "old1\nold2", fmt="codes")
        conn.execute("UPDATE recovery_code_state SET spent='[0]'")
    store.restore_history(database, "alice", history_id, actor="restore")
    with db.get_db(database) as conn:
        assert generated.recovery_state(conn, "alice", "generated_site") == {
            "format": "codes", "total": 2, "spent": [0], "remaining": 1}
