"""Broker readiness reports metadata, with no writes or credential values."""
from istota import db, doctor, secrets_store
from istota.config import Config
from istota.credential_broker import ca
from istota.credential_broker.bindings import parse_binding


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
