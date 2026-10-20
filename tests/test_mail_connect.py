"""`mail.connect`: every IMAP and SMTP connection is built here.

Three properties, each the reason the module exists:

- the TLS context on every path verifies the certificate and the hostname
  (ISSUE-798's repro, kept here once the construction moved);
- a user account's host is resolved and checked against `net_guard` before
  anything is sent, unless the operator listed it in `private_hosts`, and the
  socket goes to the address that was checked while TLS still checks the name
  that was typed;
- `imap_security` / `smtp_security` decide implicit TLS or STARTTLS, with the
  port as the fallback when they are blank.

The wire cases run the real clients against `tests/support/mail_stub.py` over
loopback, with a certificate issued for a name 127.0.0.1 does not carry, so a
handshake that succeeds proves the hostname check ran against the typed name.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import socket
import ssl
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.mail import connect
from istota.skills.email import EmailConfig

PASSWORD = "pw-3c1f0e"
TYPED = "mail.example.test"
PUBLIC = "93.184.215.14"


def _cfg(**overrides) -> EmailConfig:
    fields = dict(imap_host=TYPED, imap_port=993, imap_user="ada",
                  imap_password=PASSWORD, smtp_host=TYPED, smtp_port=587)
    fields.update(overrides)
    return EmailConfig(**fields)


def _resolves_to(*addresses):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in addresses]
    return fake


def _assert_verifying(context):
    assert isinstance(context, ssl.SSLContext), context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


# --------------------------------------------------------------------------
# which TLS mode
# --------------------------------------------------------------------------


class TestSecurityMode:
    @pytest.mark.parametrize("port,expected", [(993, "tls"), (143, "starttls"), (9993, "starttls")])
    def test_imap_infers_from_the_port_when_blank(self, port, expected):
        assert connect.imap_security(_cfg(imap_port=port)) == expected

    @pytest.mark.parametrize("port,expected", [(465, "tls"), (587, "starttls"), (25, "starttls")])
    def test_smtp_infers_from_the_port_when_blank(self, port, expected):
        assert connect.smtp_security(_cfg(smtp_port=port)) == expected

    def test_an_explicit_mode_wins_over_the_port(self):
        assert connect.imap_security(_cfg(imap_port=9993, imap_security="tls")) == "tls"
        assert connect.smtp_security(_cfg(smtp_port=465, smtp_security="starttls")) == "starttls"

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            connect.imap_security(_cfg(imap_security="none"))
        with pytest.raises(ValueError):
            connect.smtp_security(_cfg(smtp_security="plain"))

    def test_explicit_tls_on_an_unusual_port_is_implicit_tls(self):
        with patch("imaplib.IMAP4_SSL") as ssl_client, patch("imaplib.IMAP4") as plain:
            connect.open_imap(_cfg(imap_port=9993, imap_security="tls"))
        args, kwargs = ssl_client.call_args
        assert args[:2] == (TYPED, 9993)
        _assert_verifying(kwargs["ssl_context"])
        plain.assert_not_called()

    def test_explicit_starttls_smtp_on_465(self):
        with patch("smtplib.SMTP") as smtp, patch("smtplib.SMTP_SSL") as smtp_ssl:
            connect.open_smtp(_cfg(smtp_port=465, smtp_security="starttls"))
        smtp_ssl.assert_not_called()
        _, kwargs = smtp.return_value.starttls.call_args
        _assert_verifying(kwargs["context"])

    def test_explicit_tls_smtp_on_an_unusual_port(self):
        with patch("smtplib.SMTP") as smtp, patch("smtplib.SMTP_SSL") as smtp_ssl:
            connect.open_smtp(_cfg(smtp_port=2465, smtp_security="tls"))
        smtp.assert_not_called()
        args, kwargs = smtp_ssl.call_args
        assert args[:2] == (TYPED, 2465)
        _assert_verifying(kwargs["context"])


# --------------------------------------------------------------------------
# the context, on every path (ISSUE-798)
# --------------------------------------------------------------------------


class TestTheContextVerifies:
    def test_the_shared_context_verifies(self):
        _assert_verifying(connect.tls_context())

    def test_implicit_tls_imap(self):
        with patch("imaplib.IMAP4_SSL") as ssl_client:
            connect.open_imap(_cfg(imap_port=993))
        _assert_verifying(ssl_client.call_args.kwargs["ssl_context"])

    def test_starttls_imap(self):
        with patch("imaplib.IMAP4") as plain_client:
            plain_client.return_value.starttls.return_value = ("OK", [b"go"])
            connect.open_imap(_cfg(imap_port=143))
        (context,), _ = plain_client.return_value.starttls.call_args
        _assert_verifying(context)

    def test_starttls_smtp(self):
        with patch("smtplib.SMTP") as smtp_client:
            connect.open_smtp(_cfg(smtp_port=587))
        _assert_verifying(smtp_client.return_value.starttls.call_args.kwargs["context"])

    def test_implicit_tls_smtp(self):
        with patch("smtplib.SMTP_SSL") as smtp_client:
            connect.open_smtp(_cfg(smtp_port=465))
        _assert_verifying(smtp_client.call_args.kwargs["context"])

    def test_a_failed_starttls_closes_the_smtp_socket(self):
        with patch("smtplib.SMTP") as smtp_client:
            smtp_client.return_value.starttls.side_effect = smtplib.SMTPNotSupportedError("no")
            with pytest.raises(smtplib.SMTPNotSupportedError):
                connect.open_smtp(_cfg(smtp_port=587))
        smtp_client.return_value.close.assert_called_once()

    @pytest.mark.parametrize("security", ["tls", "starttls"])
    def test_the_checked_imap_path_hands_the_verifying_context_to_the_handshake(self, security):
        seen = {}

        def wrap(ctx, sock, *args, server_hostname=None, **kwargs):
            seen["context"], seen["server_hostname"] = ctx, server_hostname
            raise ssl.SSLCertVerificationError("stop here")

        conn = _FakeImapSocket()
        with patch("socket.getaddrinfo", side_effect=_resolves_to(PUBLIC)), \
                patch("socket.create_connection", return_value=conn), \
                patch.object(ssl.SSLContext, "wrap_socket", autospec=True, side_effect=wrap):
            with pytest.raises(ssl.SSLError):
                connect.open_imap(_cfg(imap_security=security, imap_port=1993), check_public=True)
        _assert_verifying(seen["context"])
        assert seen["server_hostname"] == TYPED


class _FakeImapSocket:
    """Enough of a server socket for imaplib to reach STARTTLS."""

    def __init__(self):
        self._tags = []
        self._out = b"* OK ready\r\n"

    def makefile(self, mode):
        return self

    def readline(self, *args):
        line, sep, rest = self._out.partition(b"\r\n")
        self._out = rest
        return line + sep

    def read(self, size):
        chunk, self._out = self._out[:size], self._out[size:]
        return chunk

    def sendall(self, data):
        tag, _, rest = data.decode().partition(" ")
        verb = rest.split(" ", 1)[0].strip().upper()
        if verb == "CAPABILITY":
            self._out += f"* CAPABILITY IMAP4rev1 STARTTLS\r\n{tag} OK done\r\n".encode()
        elif verb == "STARTTLS":
            self._out += f"{tag} OK go\r\n".encode()

    def close(self):
        pass

    def shutdown(self, how):
        pass


# --------------------------------------------------------------------------
# resolve, check, connect to the checked address
# --------------------------------------------------------------------------


def _openers():
    return [
        pytest.param(lambda cfg, **kw: connect.open_imap(cfg, **kw), "imap", id="imap"),
        pytest.param(lambda cfg, **kw: connect.open_smtp(cfg, **kw), "smtp", id="smtp"),
    ]


class TestTheAddressCheck:
    @pytest.mark.parametrize("opener,kind", _openers())
    @pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1",
                                         "::ffff:10.0.0.5"])
    def test_a_private_address_is_refused_before_any_connection(self, opener, kind, address):
        with patch("socket.getaddrinfo", side_effect=_resolves_to(address)), \
                patch("socket.create_connection") as create:
            with pytest.raises(connect.ConnectRefused) as info:
                opener(_cfg(), check_public=True)
        assert info.value.reason == "address_not_public"
        assert TYPED in str(info.value)
        assert address not in str(info.value)
        create.assert_not_called()

    @pytest.mark.parametrize("opener,kind", _openers())
    def test_one_private_address_among_public_ones_is_enough_to_refuse(self, opener, kind):
        with patch("socket.getaddrinfo", side_effect=_resolves_to(PUBLIC, "192.168.1.9")), \
                patch("socket.create_connection") as create:
            with pytest.raises(connect.ConnectRefused):
                opener(_cfg(), check_public=True)
        create.assert_not_called()

    @pytest.mark.parametrize("opener,kind", _openers())
    def test_a_private_literal_is_refused_without_resolving(self, opener, kind):
        with patch("socket.getaddrinfo") as resolve, patch("socket.create_connection") as create:
            with pytest.raises(connect.ConnectRefused) as info:
                opener(_cfg(imap_host="10.1.2.3", smtp_host="10.1.2.3"), check_public=True)
        assert info.value.reason == "address_not_public"
        resolve.assert_not_called()
        create.assert_not_called()

    @pytest.mark.parametrize("opener,kind", _openers())
    def test_a_name_that_does_not_resolve_is_dns_failed(self, opener, kind):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")), \
                patch("socket.create_connection") as create:
            with pytest.raises(connect.ConnectRefused) as info:
                opener(_cfg(), check_public=True)
        assert info.value.reason == "dns_failed"
        create.assert_not_called()

    @pytest.mark.parametrize("opener,kind", _openers())
    @pytest.mark.parametrize("listed", [TYPED, TYPED.upper(), TYPED + "."])
    def test_a_host_the_operator_listed_may_be_private(self, opener, kind, listed):
        with patch("socket.getaddrinfo", side_effect=_resolves_to("10.0.0.5")), \
                patch("socket.create_connection", side_effect=ConnectionRefusedError) as create:
            with pytest.raises(ConnectionRefusedError):
                opener(_cfg(), check_public=True, private_hosts=[listed])
        assert create.call_args.args[0][0] == "10.0.0.5"

    @pytest.mark.parametrize("opener,kind", _openers())
    def test_a_listed_host_is_matched_exactly(self, opener, kind):
        with patch("socket.getaddrinfo", side_effect=_resolves_to("10.0.0.5")), \
                patch("socket.create_connection") as create:
            with pytest.raises(connect.ConnectRefused):
                opener(_cfg(), check_public=True, private_hosts=["example.test", "*.example.test"])
        create.assert_not_called()

    @pytest.mark.parametrize("opener,kind", _openers())
    def test_the_socket_goes_to_the_checked_address(self, opener, kind):
        port = 993 if kind == "imap" else 587
        with patch("socket.getaddrinfo", side_effect=_resolves_to(PUBLIC)), \
                patch("socket.create_connection", side_effect=ConnectionRefusedError) as create:
            with pytest.raises(ConnectionRefusedError):
                opener(_cfg(), check_public=True)
        (address, used_port), *_ = create.call_args.args
        assert (address, used_port) == (PUBLIC, port)

    @pytest.mark.parametrize("opener,kind", _openers())
    def test_the_bot_box_is_not_checked(self, opener, kind):
        """The operator configured it; only a user account's host is checked."""
        with patch("socket.getaddrinfo") as resolve, \
                patch("imaplib.IMAP4_SSL"), patch("smtplib.SMTP"):
            opener(_cfg())
        resolve.assert_not_called()


# --------------------------------------------------------------------------
# over the wire, against the loopback stubs
# --------------------------------------------------------------------------


@pytest.fixture
def certs(tmp_path):
    from testbed.certs import generate_self_signed

    good = generate_self_signed(tmp_path / "good", sans=(TYPED,))
    other = generate_self_signed(tmp_path / "other", sans=("other.example.test",))
    return good, other


def _pinned(monkeypatch, cert: Path | None):
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("127.0.0.1"))
    if cert is None:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    else:
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))


class TestOverTheWire:
    @pytest.mark.parametrize("implicit", [False, True], ids=["starttls", "tls"])
    def test_imap_signs_in_through_the_pinned_address(self, monkeypatch, certs, implicit):
        from tests.support.mail_stub import ImapStub

        (cert, key), _ = certs
        _pinned(monkeypatch, cert)
        with ImapStub(cert=cert, key=key, username="ada", password=PASSWORD,
                      implicit_tls=implicit) as imap:
            cfg = _cfg(imap_port=imap.port, imap_security="tls" if implicit else "starttls")
            with connect.open_imap(cfg, check_public=True, private_hosts=[TYPED]) as mailbox:
                mailbox.login("ada", PASSWORD)
        assert imap.logins == [("ada", True)]
        assert ("STARTTLS" in imap.commands) is (not implicit)

    @pytest.mark.parametrize("implicit", [False, True], ids=["starttls", "tls"])
    def test_smtp_signs_in_through_the_pinned_address(self, monkeypatch, certs, implicit):
        from tests.support.mail_stub import SmtpStub

        (cert, key), _ = certs
        _pinned(monkeypatch, cert)
        with SmtpStub(cert=cert, key=key, username="ada", password=PASSWORD,
                      implicit_tls=implicit) as smtp:
            cfg = _cfg(smtp_port=smtp.port, smtp_security="tls" if implicit else "starttls")
            server = connect.open_smtp(cfg, check_public=True, private_hosts=[TYPED], timeout=5)
            try:
                server.login("ada", PASSWORD)
            finally:
                server.quit()
        assert smtp.logins == [("ada", True)]

    @pytest.mark.parametrize("which", ["untrusted", "wrong_name"])
    def test_imap_refuses_a_certificate_it_cannot_verify(self, monkeypatch, certs, which):
        from tests.support.mail_stub import ImapStub

        good, other = certs
        cert, key = good if which == "untrusted" else other
        _pinned(monkeypatch, None if which == "untrusted" else cert)
        with ImapStub(cert=cert, key=key, username="ada", password=PASSWORD) as imap:
            with pytest.raises(Exception) as info:
                connect.open_imap(_cfg(imap_port=imap.port, imap_security="starttls"),
                                  check_public=True, private_hosts=[TYPED])
        assert connect.classify_error(info.value) == "tls_failed", repr(info.value)
        assert imap.logins == []

    @pytest.mark.parametrize("which", ["untrusted", "wrong_name"])
    def test_smtp_refuses_a_certificate_it_cannot_verify(self, monkeypatch, certs, which):
        from tests.support.mail_stub import SmtpStub

        good, other = certs
        cert, key = good if which == "untrusted" else other
        _pinned(monkeypatch, None if which == "untrusted" else cert)
        with SmtpStub(cert=cert, key=key, username="ada", password=PASSWORD) as smtp:
            with pytest.raises(Exception) as info:
                connect.open_smtp(_cfg(smtp_port=smtp.port), check_public=True,
                                  private_hosts=[TYPED], timeout=5)
        assert connect.classify_error(info.value) == "tls_failed"
        assert smtp.logins == []

    def test_a_wrong_password_is_auth_failed(self, monkeypatch, certs):
        from tests.support.mail_stub import ImapStub

        (cert, key), _ = certs
        _pinned(monkeypatch, cert)
        with ImapStub(cert=cert, key=key, username="ada", password="other") as imap:
            with connect.open_imap(_cfg(imap_port=imap.port), check_public=True,
                                   private_hosts=[TYPED]) as mailbox:
                with pytest.raises(Exception) as info:
                    mailbox.login("ada", PASSWORD)
        assert connect.classify_error(info.value) == "auth_failed"


# --------------------------------------------------------------------------
# classified errors
# --------------------------------------------------------------------------


class TestClassifyError:
    def test_each_failure_has_one_reason(self):
        from imap_tools.errors import MailboxLoginError, MailboxStarttlsError

        cases = [
            (connect.ConnectRefused("address_not_public", TYPED), "address_not_public"),
            (connect.ConnectRefused("dns_failed", TYPED), "dns_failed"),
            (socket.gaierror(-2, "x"), "dns_failed"),
            (ssl.SSLCertVerificationError("x"), "tls_failed"),
            (ssl.SSLError("x"), "tls_failed"),
            (MailboxStarttlsError(("NO", [b"x"]), "OK"), "tls_failed"),
            (MailboxLoginError(("NO", [b"x"]), "OK"), "auth_failed"),
            (smtplib.SMTPAuthenticationError(535, b"x"), "auth_failed"),
            (TimeoutError(), "timeout"),
            (socket.timeout(), "timeout"),
            (ConnectionRefusedError(), "connect_failed"),
            (OSError("x"), "connect_failed"),
            (imaplib.IMAP4.abort("x"), "connect_failed"),
            (RuntimeError("x"), "connect_failed"),
        ]
        for exc, reason in cases:
            assert connect.classify_error(exc) == reason, exc
            assert reason in connect.ERROR_REASONS

    def test_the_refusal_message_names_the_host_and_not_the_address(self):
        exc = connect.ConnectRefused("address_not_public", TYPED)
        assert TYPED in str(exc)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


class TestConfig:
    def test_the_bot_box_reads_both_modes(self):
        from istota.config import config_from_document
        from istota.mail.support import get_email_config

        config = config_from_document({"email": {
            "enabled": True, "imap_host": "h", "smtp_host": "s",
            "imap_security": "starttls", "smtp_security": "tls",
        }})
        email = get_email_config(config)
        assert (email.imap_security, email.smtp_security) == ("starttls", "tls")

    def test_an_unknown_mode_falls_back_to_the_port(self):
        from istota.config import config_from_document

        config = config_from_document({"email": {"imap_security": "ssl", "smtp_security": ""}})
        assert config.email.imap_security == ""
        assert config.email.smtp_security == ""

    def test_the_skill_reads_both_modes_from_its_environment(self, monkeypatch):
        from istota.skills.email import _config_from_env

        monkeypatch.setenv("SMTP_HOST", "s")
        monkeypatch.setenv("IMAP_SECURITY", "tls")
        monkeypatch.setenv("SMTP_SECURITY", "starttls")
        email = _config_from_env()
        assert (email.imap_security, email.smtp_security) == ("tls", "starttls")

    def test_the_skill_manifest_declares_both(self):
        text = (Path(connect.__file__).parents[1] / "skills" / "email" / "skill.md").read_text()
        assert '"var":"IMAP_SECURITY"' in text and '"var":"SMTP_SECURITY"' in text


# --------------------------------------------------------------------------
# one place builds a connection
# --------------------------------------------------------------------------


_CONSTRUCTOR = re.compile(r"\b(MailBox|MailBoxStartTls|MailBoxUnencrypted|SMTP|SMTP_SSL|IMAP4|IMAP4_SSL)\(")


def test_no_connection_is_built_outside_mail_connect():
    """Reads files off disk, so testmon cannot see it (`.claude/rules/testing.md`)."""
    src = Path(connect.__file__).parents[1]
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path == Path(connect.__file__):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _CONSTRUCTOR.search(line):
                offenders.append(f"{path.relative_to(src)}:{number}: {line.strip()}")
    assert offenders == []


def test_the_drift_pattern_sees_what_it_guards():
    assert _CONSTRUCTOR.search("    server = smtplib.SMTP(host, port)")
    assert _CONSTRUCTOR.search("return MailBox(host, port=993)")
    assert not _CONSTRUCTOR.search("class _PinnedSMTP(smtplib.SMTP):")
    assert not _CONSTRUCTOR.search("box = _PinnedMailBox(host)")
