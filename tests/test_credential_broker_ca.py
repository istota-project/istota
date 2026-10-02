"""Deployment CA, short-lived leaves, and the public task trust bundle."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import ipaddress
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
import pytest

from istota.credentials.broker import ca
from istota.config import Config, load_config


def test_creation_is_atomic_private_and_stable(tmp_path):
    state = tmp_path / "broker"
    with ThreadPoolExecutor(max_workers=8) as pool:
        authorities = list(pool.map(lambda _: ca.load_or_create_ca(state), range(16)))
    certs = {a.certificate.public_bytes(serialization.Encoding.PEM) for a in authorities}
    assert len(certs) == 1
    authority = authorities[0]
    assert isinstance(authority.private_key.curve, ec.SECP256R1)
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "ca-key.pem").stat().st_mode & 0o777 == 0o600
    assert authority.certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert authority.certificate.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=3649)
    authority.certificate.verify_directly_issued_by(authority.certificate)
    (state / "ca-key.pem").unlink()
    rotated = ca.load_or_create_ca(state)
    assert rotated.certificate.serial_number != authority.certificate.serial_number


def test_refuses_symlink_or_open_permissions(tmp_path):
    authority = ca.load_or_create_ca(tmp_path / "broker")
    key = tmp_path / "broker" / "ca-key.pem"
    key.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        ca.load_or_create_ca(authority.state_dir)
    key.chmod(0o600)
    link = tmp_path / "alias"
    link.symlink_to(authority.state_dir, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        ca.load_or_create_ca(link)


@pytest.mark.parametrize("host,san", [
    ("api.example.com", x509.DNSName("api.example.com")),
    ("192.0.2.1", x509.IPAddress(ipaddress.ip_address("192.0.2.1"))),
    ("2001:db8::1", x509.IPAddress(ipaddress.ip_address("2001:db8::1"))),
])
def test_leaf_identity_validity_and_cache(tmp_path, host, san):
    authority = ca.load_or_create_ca(tmp_path / "broker")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    leaf = ca.mint_leaf(authority, host, now=now)
    assert ca.mint_leaf(authority, host, now=now) is leaf
    cert = leaf.certificate
    cert.verify_directly_issued_by(authority.certificate)
    assert cert.not_valid_after_utc == now + timedelta(hours=24)
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value == x509.SubjectAlternativeName([san])
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ca.mint_leaf(authority, host, now=now + timedelta(hours=25)) is not leaf


def test_bundle_preserves_system_roots_without_exporting_keys(tmp_path):
    authority = ca.load_or_create_ca(tmp_path / "broker")
    env = ca.write_trust_bundle(authority, tmp_path / "task")
    from pathlib import Path
    bundle = Path(env["SSL_CERT_FILE"])
    roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    combined = ssl.create_default_context(cafile=str(bundle)).get_ca_certs(binary_form=True)
    assert set(roots) <= set(combined)
    assert authority.certificate.public_bytes(serialization.Encoding.DER) in combined
    assert set(env) == {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS"}
    assert len(set(v for k, v in env.items() if k != "NODE_EXTRA_CA_CERTS")) == 1
    for path in set(env.values()):
        assert b"PRIVATE KEY" not in Path(path).read_bytes()


def test_server_context_negotiates_only_http11_and_upstream_distrusts_ca(tmp_path):
    authority = ca.load_or_create_ca(tmp_path / "broker")
    env = ca.write_trust_bundle(authority, tmp_path / "task")
    client = ssl.create_default_context(cafile=env["SSL_CERT_FILE"])
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    client.set_alpn_protocols(["h2", "http/1.1"])
    server = ca.server_context(authority, "api.example.com")
    ci, co, si, so = [ssl.MemoryBIO() for _ in range(4)]
    c = client.wrap_bio(ci, co, server_hostname="api.example.com")
    s = server.wrap_bio(si, so, server_side=True)
    for _ in range(10):
        for peer in (c, s):
            try:
                peer.do_handshake()
            except ssl.SSLWantReadError:
                pass
        si.write(co.read())
        ci.write(so.read())
    assert c.selected_alpn_protocol() == "http/1.1"
    assert s.selected_alpn_protocol() == "http/1.1"
    assert authority.certificate.public_bytes(serialization.Encoding.DER) not in ca.upstream_context().get_ca_certs(binary_form=True)
    assert set(os.listdir(authority.state_dir)) == {"ca-key.pem", "ca.lock"}


def test_broker_config_defaults_mapping_and_validation(tmp_path):
    assert not Config().security.credential_broker.enabled
    path = tmp_path / "config.toml"
    path.write_text("[security.credential_broker]\nenabled = true\nscan_max_bytes = 2048\nleaf_validity_hours = 12\n")
    broker = load_config(path).security.credential_broker
    assert broker.enabled and broker.scan_max_bytes == 2048 and broker.leaf_validity_hours == 12
    path.write_text("[security.credential_broker]\nscan_max_bytes = 0\nleaf_validity_hours = -1\n")
    broker = load_config(path).security.credential_broker
    assert broker.scan_max_bytes == 1048576 and broker.leaf_validity_hours == 24


def test_bundle_includes_roots_from_a_capath_only_store(tmp_path, monkeypatch):
    authority = ca.load_or_create_ca(tmp_path / "broker")
    system_ca = ca.load_or_create_ca(tmp_path / "system")
    capath = tmp_path / "roots"
    capath.mkdir()
    (capath / "01234567.0").write_bytes(system_ca.certificate.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    env = ca.write_trust_bundle(authority, tmp_path / "task")
    roots = ssl.create_default_context(cafile=env["SSL_CERT_FILE"]).get_ca_certs(binary_form=True)
    assert system_ca.certificate.public_bytes(serialization.Encoding.DER) in roots


def test_corrupt_record_fails_without_rotating(tmp_path):
    state = tmp_path / "broker"
    ca.load_or_create_ca(state)
    key = state / "ca-key.pem"
    key.write_bytes(b"incomplete record")
    with pytest.raises(ValueError):
        ca.load_or_create_ca(state)
    assert key.read_bytes() == b"incomplete record"


@pytest.mark.parametrize("host", ["*.example.com", "https://example.com", "example.com:443", "x\n.example.com", "-bad.example"])
def test_leaf_rejects_non_host_names(tmp_path, host):
    authority = ca.load_or_create_ca(tmp_path / "broker")
    with pytest.raises(ValueError):
        ca.mint_leaf(authority, host)


def test_state_directory_cannot_be_a_users_temp_directory(tmp_path):
    state = tmp_path.resolve()
    config = Config(db_path=state / "istota.db", temp_dir=state)
    # A user named credential-broker would get this whole directory RW.
    with pytest.raises(ValueError, match="sandbox"):
        ca.state_directory(config)
