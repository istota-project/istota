"""Bound-host HTTP/1.1 interception; values stay on the daemon side.

Request prefixes are scanned before any HTTP bytes reach upstream. Responses
up to the cap are scrubbed; larger responses deliberately stream unscanned.
Content encodings and trailers are refused, rather than silently bypassing the
scan. TLS and HTTP failures use fixed reasons, never parser exception text.
"""

import base64
from contextlib import nullcontext
from urllib.parse import urlsplit
import ipaddress
from dataclasses import dataclass
import logging
import re
import socket
import ssl

import h11

from istota import db
from istota.credentials import store as secrets_store
from . import ca
from .bindings import get_entry_binding, https_host, credential_host
from .grants import check_credential_grant

logger = logging.getLogger("istota.credentials.broker")
PLACEHOLDER = re.compile(rb"\{\{cred:([A-Za-z0-9_.-]+)\}\}")
STRUCTURAL = {b"host", b"content-length", b"transfer-encoding", b"connection",
              b"trailer", b"upgrade", b"expect", b"proxy-authorization"}


class Refused(Exception):
    """Only fixed, non-secret reason identifiers may be supplied."""


@dataclass
class Broker:
    config: object
    task_id: int
    user_id: str
    authority: ca.Authority

    def covers(self, host):
        with db.get_db(self.config.db_path) as conn:
            for row in conn.execute(
                "SELECT name FROM credential_task_grants WHERE task_id=? AND user_id=?",
                (self.task_id, self.user_id),
            ):
                binding = get_entry_binding(conn, self.user_id, row[0])
                if binding and host in binding["hosts"]:
                    return True
        return False


def _event(parser, sock):
    while True:
        event = parser.next_event()
        if event is not h11.NEED_DATA:
            return event
        parser.receive_data(sock.recv(65536))


def _send(parser, sock, event):
    data = parser.send(event)
    if data:
        sock.sendall(data)


def _prefix(parser, sock, cap):
    """Return bounded data, completion, and whether more body remains."""
    body = bytearray()
    while True:
        event = _event(parser, sock)
        if isinstance(event, h11.EndOfMessage):
            if event.headers:
                raise Refused("unsupported_trailers")
            return bytes(body), True
        if not isinstance(event, h11.Data):
            raise Refused("invalid_framing")
        body.extend(event.data)
        # At most cap + one receive buffer; do not accumulate a large body.
        if len(body) > cap:
            return bytes(body), False


def _relay_body(parser, sock, destination_parser, destination):
    while True:
        event = _event(parser, sock)
        if isinstance(event, h11.EndOfMessage):
            if event.headers:
                raise Refused("unsupported_trailers")
            _send(destination_parser, destination, h11.EndOfMessage())
            return
        if not isinstance(event, h11.Data):
            raise Refused("invalid_framing")
        _send(destination_parser, destination, event)


def _replace(data, replacements):
    for value in sorted(replacements, key=len, reverse=True):
        data = data.replace(value, replacements[value])
    return data


def _headers(broker, request, host):
    """Authorize and resolve every placeholder in one consistent DB view."""
    headers = []
    replacements = {}
    names = set()
    with db.get_db(broker.config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for header, original in request.headers:
            value = original
            basic = header == b"authorization" and value.lower().startswith(b"basic ")
            if basic:
                try:
                    value = base64.b64decode(value[6:].strip(), validate=True)
                except ValueError:
                    raise Refused("invalid_basic") from None
            def resolve(match):
                name = match[1].decode("ascii")
                if header in STRUCTURAL:
                    raise Refused("credential_header_not_allowed")
                reason = check_credential_grant(
                    conn, broker.task_id, broker.user_id, name, host,
                    header.decode("ascii"), config=broker.config,
                )
                if reason:
                    raise Refused(reason)
                if name in ("forge.gitlab", "forge.github"):
                    secret = getattr(broker.config.developer, name.split(".")[1] + "_token")
                else:
                    secret = secrets_store.get_secret(broker.config.db_path, broker.user_id,
                                                     "vault_entries", name, connection=conn)
                if not secret:
                    raise Refused("credential_unavailable")
                encoded = secret.encode("utf-8")
                if any(byte < 32 or byte == 127 for byte in encoded):
                    raise Refused("credential_invalid_value")
                replacements[encoded] = match[0]
                names.add(name)
                return encoded
            substituted = PLACEHOLDER.sub(resolve, value)
            # Unknown/malformed placeholder syntax is never sent through as auth.
            if b"{{cred:" in PLACEHOLDER.sub(b"", value):
                raise Refused("invalid_placeholder")
            if basic:
                substituted = b"Basic " + base64.b64encode(substituted)
            if substituted != original:
                replacements[substituted] = original
            headers.append((header, substituted))
    return headers, replacements, names


def _validate(request, host, *, plain_http=False):
    headers = dict(request.headers)
    try:
        authority = headers[b"host"].decode("ascii")
        if any(char in authority for char in "/?#@"):
            raise ValueError("invalid authority")
        actual = credential_host(("http://" if plain_http else "https://") + authority,
                                 allow_http=plain_http)
    except (ValueError, UnicodeError, KeyError):
        raise Refused("host_mismatch") from None
    if actual != host:
        raise Refused("host_mismatch")
    if b"{{cred:" in request.target:
        raise Refused("credential_in_url")
    if not request.target.startswith(b"/") or request.target.startswith(b"//"):
        raise Refused("unsupported_target")
    if b"content-encoding" in headers and headers[b"content-encoding"].lower() != b"identity":
        raise Refused("unsupported_encoding")
    if b"trailer" in headers or b"upgrade" in headers:
        raise Refused("unsupported_framing")
    if b"transfer-encoding" in headers and b"content-length" in headers:
        raise Refused("invalid_framing")
    if headers.get(b"expect", b"").lower() not in (b"", b"100-continue"):
        raise Refused("unsupported_expectation")


def _refusal(broker, sock, reason, status=403):
    logger.warning("credential_refused task=%s user=%s reason=%s", broker.task_id, broker.user_id, reason)
    try:
        sock.sendall(f"HTTP/1.1 {status} Refused\r\nX-Istota-Refused: {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    except OSError:
        pass


def intercept(broker, client, host, port):
    """Serve one CONNECT. The caller has already checked peer and allowlist."""
    target = https_host(f"https://{host}:{port}")
    # CONNECT uses brackets for IPv6; TLS and socket APIs take the address.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    context = ca.server_context(broker.authority, host,
                               validity_hours=broker.config.security.credential_broker.leaf_validity_hours)
    # This callback is stable for the per-host cached context, with no task state.
    def check_sni(sock, name, ctx):
        # RFC 6066 excludes IP literals from SNI. The exact CONNECT address
        # and per-request Host check still bind this connection to one origin.
        if name is None and literal_ip:
            return None
        if name is None or name.lower() != host.lower():
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
    context.set_servername_callback(check_sni)
    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    try:
        with context.wrap_socket(client, server_side=True) as tls:
            tls.settimeout(30)
            _requests(broker, tls, host, port, target)
    except (OSError, ValueError):
        # In particular, do not log SSL/parser exceptions containing wire data.
        logger.warning("credential_tls_failed task=%s user=%s", broker.task_id, broker.user_id)


def _audit(broker, names, target, method, status, request_limited, response_limited):
    for name in sorted(names):
        logger.info("credential_substituted task=%s user=%s name=%s host=%s method=%s status=%s request_scan_limited=%s response_scrub_limited=%s",
                    broker.task_id, broker.user_id, name, target, method.decode("ascii"),
                    status, request_limited, response_limited)


def _requests(broker, tls, host, port, target, *, plain_http=False, initial_data=b""):
    downstream = h11.Connection(h11.SERVER, max_incomplete_event_size=65536)
    if initial_data:
        downstream.receive_data(initial_data)
    cap = broker.config.security.credential_broker.scan_max_bytes
    response_started = False
    sent_names = set()
    method = b""
    complete = small = False
    status = 502
    try:
        while True:
            response_started = False
            sent_names = set()
            status = 502
            small = False
            request = _event(downstream, tls)
            if isinstance(request, h11.ConnectionClosed):
                return
            if not isinstance(request, h11.Request):
                raise Refused("invalid_framing")
            method = request.method
            if plain_http:
                url = request.target.decode("ascii")
                parsed = urlsplit(url)
                if parsed.scheme != "http" or parsed.fragment or credential_host(url, allow_http=True) != target:
                    raise Refused("host_mismatch")
                if b"{{cred:" in request.target:
                    raise Refused("credential_in_url")
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                request = h11.Request(method=request.method, target=path.encode("ascii"),
                                     headers=request.headers)
            _validate(request, target, plain_http=plain_http)
            # Validate headers before sending 100 Continue or reading the body.
            headers, replacements, names = _headers(broker, request, target)
            if downstream.they_are_waiting_for_100_continue:
                _send(downstream, tls, h11.InformationalResponse(status_code=100, headers=[]))
            body, complete = _prefix(downstream, tls, cap)
            if b"{{cred:" in body[:cap]:
                raise Refused("credential_in_body")
            # Recheck after a potentially slow upload prefix, before using values.
            headers, replacements, names = _headers(broker, request, target)
            with socket.create_connection((host, port), timeout=10) as raw:
                with (nullcontext(raw) if plain_http else
                      ca.upstream_context().wrap_socket(raw, server_hostname=host)) as upstream:
                    upstream.settimeout(30)
                    if not plain_http and upstream.selected_alpn_protocol() not in (None, "http/1.1"):
                        raise Refused("upstream_requires_h2")
                    outbound = h11.Connection(h11.CLIENT, max_incomplete_event_size=65536)
                    headers = [(k, v) for k, v in headers if k not in (b"expect", b"accept-encoding")]
                    headers.append((b"accept-encoding", b"identity"))
                    sent_names = names
                    _send(outbound, upstream, h11.Request(method=request.method, target=request.target, headers=headers))
                    if body:
                        _send(outbound, upstream, h11.Data(data=body))
                    if complete:
                        _send(outbound, upstream, h11.EndOfMessage())
                    else:
                        _relay_body(downstream, tls, outbound, upstream)
                    response = _event(outbound, upstream)
                    while isinstance(response, h11.InformationalResponse):
                        if response.status_code == 101:
                            raise Refused("unsupported_upgrade")
                        response = _event(outbound, upstream)
                    if not isinstance(response, h11.Response):
                        raise Refused("invalid_upstream")
                    status = response.status_code
                    # A placeholder is not a valid header name. Refuse rather
                    # than disclose a value reflected into a field name.
                    if any(_replace(name, replacements) != name
                           for name, _ in response.headers.raw_items()):
                        raise Refused("credential_in_response_header_name")
                    response_headers = dict(response.headers)
                    if replacements and response_headers.get(b"content-encoding", b"identity").lower() != b"identity":
                        raise Refused("unsupported_encoding")
                    if b"trailer" in response_headers:
                        raise Refused("unsupported_trailers")
                    payload, small = _prefix(outbound, upstream, cap)
                    if small:
                        payload = _replace(payload, replacements)
                    # Let h11 choose framing after a length-changing rewrite.
                    headers = [(k, _replace(v, replacements)) for k, v in response.headers
                               if k not in (b"content-length", b"transfer-encoding", b"connection")]
                    if small and request.method != b"HEAD" and response.status_code not in (204, 304):
                        headers.append((b"content-length", str(len(payload)).encode()))
                    _send(downstream, tls, h11.Response(status_code=response.status_code,
                          reason=_replace(response.reason, replacements), headers=headers))
                    response_started = True
                    if payload:
                        _send(downstream, tls, h11.Data(data=payload))
                    if small:
                        _send(downstream, tls, h11.EndOfMessage())
                    else:
                        _relay_body(outbound, upstream, downstream, tls)
                    _audit(broker, sent_names, target, method, status, not complete, not small)
                    sent_names = set()
            if downstream.our_state is h11.MUST_CLOSE or downstream.their_state is h11.MUST_CLOSE:
                return
            downstream.start_next_cycle()
    except Refused as exc:
        _audit(broker, sent_names, target, method, status, not complete, not small)
        if not response_started:
            reason = str(exc)
            _refusal(broker, tls, reason, 502 if reason in (
                "credential_in_response_header_name", "upstream_requires_h2", "invalid_upstream",
            ) else 403)
    except (OSError, h11.ProtocolError, ValueError):
        _audit(broker, sent_names, target, method, status, not complete, not small)
        if not response_started:
            _refusal(broker, tls, "broker_protocol_error", 502)
