"""`web.tls`: whether the compose nginx serves a valid certificate in `direct`.

The check makes one TLS handshake to the stack's nginx with the public name as
SNI and the system trust store, from the istota container, which mounts no
certificate of its own. These tests stand a real TLS listener up on loopback
with a certificate made for the test, and point the check at it.
"""

from __future__ import annotations

import socket
import ssl
import threading
from pathlib import Path

import pytest

from istota import doctor
from istota.config import Config
from testbed import certs

DOMAIN = "istota.example.test"


@pytest.fixture
def config() -> Config:
    config = Config()
    config.site.hostname = DOMAIN
    return config


@pytest.fixture
def tls_server(tmp_path):
    """A loopback TLS listener presenting a certificate for DOMAIN."""
    crt, key = certs.generate_self_signed(tmp_path / "cert", sans=(DOMAIN,))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(crt, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            try:
                with context.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], crt
    stop.set()
    thread.join(timeout=2)
    listener.close()


def _point_at(monkeypatch, port: int, cafile: Path | None):
    monkeypatch.setattr(doctor, "TLS_CHECK_TARGET", ("127.0.0.1", port))
    if cafile is not None:
        monkeypatch.setattr(
            doctor, "_tls_check_context", lambda: ssl.create_default_context(cafile=str(cafile)),
        )


def test_another_ingress_mode_is_skipped(config, monkeypatch):
    monkeypatch.setenv("INGRESS", "proxied")

    result = doctor.check_web_tls(config, True)

    assert result.status == doctor.SKIP
    assert "proxied" in result.detail


def test_no_handshake_without_probe(config, monkeypatch):
    monkeypatch.setenv("INGRESS", "direct")

    assert doctor.check_web_tls(config, False).status == doctor.SKIP


def test_a_valid_certificate_for_the_public_name_is_ok(config, monkeypatch, tls_server):
    port, crt = tls_server
    monkeypatch.setenv("INGRESS", "direct")
    monkeypatch.setenv("DOMAIN", DOMAIN)
    _point_at(monkeypatch, port, crt)

    result = doctor.check_web_tls(config, True)

    assert result.status == doctor.OK, result
    assert DOMAIN in result.detail
    assert "days" in result.detail


def test_a_certificate_the_trust_store_refuses_fails(config, monkeypatch, tls_server):
    port, _ = tls_server
    monkeypatch.setenv("INGRESS", "direct")
    monkeypatch.setenv("DOMAIN", DOMAIN)
    _point_at(monkeypatch, port, None)
    monkeypatch.setattr(doctor, "_tls_check_context", ssl.create_default_context)

    result = doctor.check_web_tls(config, True)

    assert result.status == doctor.FAIL, result
    assert result.remedy


def test_a_certificate_for_another_name_fails(config, monkeypatch, tls_server):
    port, crt = tls_server
    monkeypatch.setenv("INGRESS", "direct")
    monkeypatch.setenv("DOMAIN", "other.example.test")
    _point_at(monkeypatch, port, crt)

    result = doctor.check_web_tls(config, True)

    assert result.status == doctor.FAIL, result
    assert "other.example.test" in result.detail


def test_nothing_listening_on_443_fails_naming_the_first_issuance(config, monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("INGRESS", "direct")
    monkeypatch.setenv("DOMAIN", DOMAIN)
    _point_at(monkeypatch, port, None)

    result = doctor.check_web_tls(config, True)

    assert result.status == doctor.FAIL, result
    assert "certbot" in result.remedy


def test_it_is_registered_as_a_deployment_check():
    assert ("web.tls", doctor.check_web_tls) in doctor.CHECKS
    assert doctor.CHECK_SCOPES["web.tls"] == doctor.DEPLOYMENT
