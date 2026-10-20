"""Every IMAP and SMTP connection istota opens is built here.

Three rules, each held in one place so no caller can drift from them:

- **Verified TLS on every path** (ISSUE-798). imap-tools and smtplib, handed
  no context, fall back to ``ssl._create_stdlib_context``, which checks neither
  the certificate nor the hostname. `tls_context` is passed to implicit-TLS
  IMAP, STARTTLS IMAP, STARTTLS SMTP and implicit-TLS SMTP alike, and there is
  no cleartext mode and no switch to turn verification off. A private CA is
  trusted through ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``, which the default
  context reads; there is deliberately no config key for it.
- **Explicit TLS mode.** ``imap_security`` / ``smtp_security`` (``tls`` or
  ``starttls``) decide implicit TLS or STARTTLS; blank falls back to the port,
  993 and 465 being implicit TLS, as the code always inferred.
- **Resolve, check, connect to the checked address**, for a host a user typed
  (``check_public=True``). The name is resolved, the connection is refused if
  any address fails `net_guard.ip_is_public` unless the operator listed the
  exact host in ``[mail_accounts] private_hosts``, and the socket is connected
  to the address that was checked while TLS still verifies the typed name. So
  DNS cannot change its answer between the check and the connect. The bot box
  is operator-configured and is not checked.

`classify_error` turns whatever a connection raised into one reason from
`ERROR_REASONS`, so no caller has to put server text in a message: an auth
failure's text can echo the username or worse.

`tests/test_mail_connect.py` fails if a connection is constructed anywhere else
under ``src/istota``.
"""

from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl

from istota.sandbox import net_guard

try:
    from imap_tools import MailBox, MailBoxStartTls
    from imap_tools.errors import MailboxLoginError, MailboxStarttlsError
    from imap_tools.utils import check_command_status
except ImportError:
    MailBox = None
    MailBoxStartTls = None
    MailboxLoginError = None
    MailboxStarttlsError = None
    check_command_status = None

SECURITY_MODES = ("tls", "starttls")
DEFAULT_IMAP_TIMEOUT = 30
DEFAULT_SMTP_TIMEOUT = 30.0

ERROR_REASONS = (
    "address_not_public", "dns_failed", "connect_failed", "tls_failed",
    "auth_failed", "timeout",
)


class ConnectRefused(Exception):
    """Nothing was sent: the host is not one this connection may reach.

    The message names the host and never the address it resolved to.
    """

    def __init__(self, reason: str, host: str) -> None:
        self.reason = reason
        self.host = host
        if reason == "address_not_public":
            message = (
                f"{host} resolves to a private or reserved address. Only a host the "
                f"operator lists in [mail_accounts] private_hosts may; nothing was sent."
            )
        else:
            message = f"Could not resolve {host}; nothing was sent."
        super().__init__(message)


def tls_context() -> ssl.SSLContext:
    """The context every connection hands its TLS layer."""
    return ssl.create_default_context()


def imap_security(config) -> str:
    return _security(getattr(config, "imap_security", ""), config.imap_port, implicit_port=993)


def smtp_security(config) -> str:
    return _security(getattr(config, "smtp_security", ""), config.smtp_port, implicit_port=465)


def _security(explicit: str, port: int, *, implicit_port: int) -> str:
    if not explicit:
        return "tls" if port == implicit_port else "starttls"
    if explicit not in SECURITY_MODES:
        raise ValueError(f"unknown TLS mode {explicit!r} (tls or starttls)")
    return explicit


def _imap_timeout(config) -> int:
    timeout = getattr(config, "imap_timeout", 0)
    return timeout if timeout and timeout > 0 else DEFAULT_IMAP_TIMEOUT


# --- the address check -----------------------------------------------------------


def checked_address(host: str, port: int, *, private_hosts=()) -> str:
    """The address to connect to for ``host``, or `ConnectRefused`.

    Every address the name resolves to must be public, unless the exact host
    is listed in ``private_hosts``. The first one is the one connected to.
    """
    name = net_guard.normal_host(host)
    addresses = net_guard.host_addresses(host, port)
    if not addresses:
        raise ConnectRefused("dns_failed", name)
    if name not in net_guard.host_set(private_hosts):
        if net_guard.first_non_public(addresses) is not None:
            raise ConnectRefused("address_not_public", name)
    return str(addresses[0])


def _connect(address: str, port: int, timeout) -> socket.socket:
    return socket.create_connection((address, port), timeout)


# --- the pinned clients: connect to the checked address, verify the typed name ----


class _PinnedIMAP4(imaplib.IMAP4):
    def __init__(self, host: str, port: int, *, address: str, timeout) -> None:
        self._address = address
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout):
        return _connect(self._address, self.port, timeout)


class _PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, *, address: str, ssl_context, timeout) -> None:
        self._address = address
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        sock = _connect(self._address, self.port, timeout)
        try:
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host: str, port: int, *, address: str, timeout) -> None:
        self._address = address
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return _connect(self._address, port, timeout)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, host: str, port: int, *, address: str, context, timeout) -> None:
        self._address = address
        super().__init__(host, port, context=context, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        sock = _connect(self._address, port, timeout)
        try:
            return self.context.wrap_socket(sock, server_hostname=self._host)
        except BaseException:
            sock.close()
            raise


if MailBox is not None:

    class _PinnedMailBox(MailBox):
        def __init__(self, host: str, *, port: int, timeout, ssl_context, address: str) -> None:
            self._address = address
            super().__init__(host, port=port, timeout=timeout, ssl_context=ssl_context)

        def _get_mailbox_client(self) -> imaplib.IMAP4:
            return _PinnedIMAP4_SSL(
                self._host, self._port, address=self._address,
                ssl_context=self._ssl_context, timeout=self._timeout,
            )

    class _PinnedMailBoxStartTls(MailBoxStartTls):
        def __init__(self, host: str, *, port: int, timeout, ssl_context, address: str) -> None:
            self._address = address
            super().__init__(host, port=port, timeout=timeout, ssl_context=ssl_context)

        def _get_mailbox_client(self) -> imaplib.IMAP4:
            client = _PinnedIMAP4(self._host, self._port, address=self._address, timeout=self._timeout)
            try:
                check_command_status(client.starttls(self._ssl_context), MailboxStarttlsError)
            except BaseException:
                # A failed handshake has already closed the socket under it.
                try:
                    client.shutdown()
                except OSError:
                    pass
                raise
            return client


# --- the two openers -------------------------------------------------------------


def open_imap(config, *, check_public: bool = False, private_hosts=()):
    """A connected, not signed-in imap-tools mailbox, TLS established.

    ``check_public`` is for a host a user typed; the bot box passes False.
    """
    if MailBox is None:
        raise ImportError("imap-tools not installed. Install with: uv sync --extra email")
    host, port = config.imap_host, config.imap_port
    timeout = _imap_timeout(config)
    implicit = imap_security(config) == "tls"
    if not check_public:
        if implicit:
            return MailBox(host, port=port, timeout=timeout, ssl_context=tls_context())
        return MailBoxStartTls(host, port=port, timeout=timeout, ssl_context=tls_context())
    address = checked_address(host, port, private_hosts=private_hosts)
    pinned = _PinnedMailBox if implicit else _PinnedMailBoxStartTls
    return pinned(host, port=port, timeout=timeout, ssl_context=tls_context(), address=address)


def open_smtp(config, *, timeout: float = DEFAULT_SMTP_TIMEOUT, check_public: bool = False,
              private_hosts=()):
    """A connected, not signed-in SMTP session, TLS established."""
    host, port = config.smtp_host, config.smtp_port
    implicit = smtp_security(config) == "tls"
    if not check_public:
        if implicit:
            return smtplib.SMTP_SSL(host, port, context=tls_context(), timeout=timeout)
        server = smtplib.SMTP(host, port, timeout=timeout)
    else:
        address = checked_address(host, port, private_hosts=private_hosts)
        if implicit:
            return _PinnedSMTP_SSL(host, port, address=address, context=tls_context(), timeout=timeout)
        server = _PinnedSMTP(host, port, address=address, timeout=timeout)
    try:
        server.starttls(context=tls_context())
    except BaseException:
        server.close()
        raise
    return server


# --- classified errors -----------------------------------------------------------


def classify_error(exc: BaseException) -> str:
    """One of `ERROR_REASONS` for anything a connection or sign-in raised.

    Decided on types alone; nothing of the exception's text is used.
    """
    if isinstance(exc, ConnectRefused):
        return exc.reason
    if MailboxLoginError is not None and isinstance(exc, MailboxLoginError):
        return "auth_failed"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth_failed"
    if MailboxStarttlsError is not None and isinstance(exc, MailboxStarttlsError):
        return "tls_failed"
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return "tls_failed"
    if isinstance(exc, socket.gaierror):
        return "dns_failed"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "connect_failed"
