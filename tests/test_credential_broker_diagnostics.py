"""Broker readiness reports metadata, with no writes or credential values."""
from istota import db, doctor
from istota.credentials import store as secrets_store
from istota.config import Config
from istota.credentials.broker import ca
from istota.credentials.broker.bindings import parse_binding


def test_disabled_does_not_create_state(tmp_path):
    config = Config(db_path=tmp_path / "db" / "data.db")
    results = doctor.check_credential_broker(config, False)
    assert results[0].status == doctor.SKIP
    assert not config.db_path.parent.exists()


def test_readiness_reports_missing_state_and_no_proxy(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / "db" / "data.db", temp_dir=tmp_path / "temp")
    config.security.credential_broker.enabled = True
    config.security.network.enabled = False
    monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda *args: (False, "test shape"))
    results = {r.name: r for r in doctor.check_credential_broker(config, False)}
    assert results["security.credential_broker.containment"].status == doctor.WARN
    assert results["security.credential_broker.proxy"].status == doctor.FAIL
    assert results["security.credential_broker.ca"].status == doctor.WARN
    assert not config.db_path.parent.exists()


def test_readiness_ca_permissions_bundle_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "db" / "data.db", temp_dir=tmp_path / "temp")
    config.security.credential_broker.enabled = True
    config.security.network.enabled = True
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "private_label", "fixture-secret",
                               binding=parse_binding("https://portal.example", {}, []))
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "unbound_label", "other-fixture-secret",
                               binding=parse_binding("", {}, []))
    authority = ca.load_or_create_ca(ca.state_directory(config))
    ca.write_trust_bundle(authority, config.temp_dir / ".control" / "alice" / "task_1" / "trust")
    monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda *args: (True, ""))
    results = {r.name: r for r in doctor.check_credential_broker(config, False)}
    assert results["security.credential_broker.ca"].status == doctor.OK
    assert results["security.credential_broker.bundle"].status == doctor.OK
    assert "1 unbound" in results["security.credential_broker.bindings"].detail
    assert "1 bound without grants" in results["security.credential_broker.bindings"].detail
    assert "private_label" not in repr(results)
    assert "fixture-secret" not in repr(results)
    (authority.state_dir / "ca-key.pem").chmod(0o644)
    results = {r.name: r for r in doctor.check_credential_broker(config, False)}
    assert results["security.credential_broker.ca"].status == doctor.FAIL


def test_readiness_rejects_ca_record_with_wrong_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    config = Config(db_path=tmp_path / "db" / "data.db", temp_dir=tmp_path / "temp")
    config.security.credential_broker.enabled = True
    original = ca.load_or_create_ca(ca.state_directory(config))
    other = ca.load_or_create_ca(tmp_path / "other")
    record = other.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
    record += original.certificate.public_bytes(serialization.Encoding.PEM)
    (original.state_dir / "ca-key.pem").write_bytes(record)
    monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda *args: (True, ""))
    results = {r.name: r for r in doctor.check_credential_broker(config, False)}
    assert results["security.credential_broker.ca"].status == doctor.FAIL


def _broker_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "db" / "data.db", temp_dir=tmp_path / "temp")
    config.security.credential_broker.enabled = True
    config.security.network.enabled = True
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda *args: (True, ""))
    return config


def _store_entry(config, user_id, entry, host):
    """One KeePassXC entry: the password row plus `_url` and `_username` field rows."""
    binding = parse_binding(f"https://{host}", {}, [])
    for name in (entry, entry + "_url", entry + "_username"):
        secrets_store.upsert_secret(config.db_path, user_id, "vault_entries", name, "fixture-value",
                                   binding=dict(binding, credential=entry))


def _grant(config, user_id, entry):
    from istota.credentials.broker.grants import put_grant
    with db.get_db(config.db_path) as conn:
        put_grant(conn, user_id, entry)


def _bindings(config):
    results = {r.name: r for r in doctor.check_credential_broker(config, False)}
    return results["security.credential_broker.bindings"]


def test_field_rows_of_a_granted_entry_are_not_ungranted(tmp_path, monkeypatch):
    # ISSUE-589: the grant sits on the entry, the field rows map to it.
    config = _broker_config(tmp_path, monkeypatch)
    _store_entry(config, "alice", "portal", "portal.example")
    _grant(config, "alice", "portal")
    result = _bindings(config)
    assert result.status == doctor.OK, result.detail


def test_an_ungranted_entry_counts_once_and_names_its_user(tmp_path, monkeypatch):
    config = _broker_config(tmp_path, monkeypatch)
    _store_entry(config, "alice", "portal", "portal.example")
    _grant(config, "alice", "portal")
    _store_entry(config, "bob", "mail", "mail.example")
    _store_entry(config, "bob", "forum", "forum.example")
    _grant(config, "bob", "forum")
    result = _bindings(config)
    assert result.status == doctor.WARN
    assert "bob: 0 unbound, 1 bound without grants" in result.detail
    assert "alice" not in result.detail
    assert "mail" not in result.detail


def test_an_entry_with_no_password_row_is_judged_by_its_fields(tmp_path, monkeypatch):
    # The owner has no binding row of its own, only `_url` and `_username`.
    config = _broker_config(tmp_path, monkeypatch)
    binding = dict(parse_binding("https://wiki.example", {}, []), credential="wiki")
    for name in ("wiki_url", "wiki_username"):
        secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", name, "fixture-value",
                                   binding=binding)
    assert "alice: 0 unbound, 1 bound without grants" in _bindings(config).detail
    _grant(config, "alice", "wiki")
    assert _bindings(config).status == doctor.OK
