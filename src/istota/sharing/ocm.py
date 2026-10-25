"""Receiving Open Cloud Mesh folder shares: discovery, the offer, the signature.

Istota implements the receiving side of OCM for ``file`` resources and
``user`` share types, served on ``[site] hostname``, so a federated id is
``<user_id>@<site hostname>``. The shapes handled here are the ones Nextcloud
30 and 31 were observed to send (the open-protocol-sharing spec, Stage 1):
the OCM 1.0 offer body whatever version is advertised, cloud ids that carry a
scheme after the ``@`` (``carol@http://cloud.example.org``), a numeric
``providerId`` in offers, a placeholder permissions string, and from Nextcloud
31 a draft-cavage-http-signatures-12 ``Signature`` header. Nothing else in the
tree parses any of them.

Istota publishes no ``publicKey`` and signs nothing: Nextcloud 31 does not
require a recipient's requests to be signed, and once it has verified one
signed request from a host it refuses unsigned ones from that host for good.

`receive_share` takes an offer through eight steps in a fixed order, and each
refusal answers the OCM error shape with fixed text and writes nothing:

1. the body: bounded, a JSON object of the observed shape (400);
2. the rate limits, before any outbound request or user lookup (429);
3. the sender: its ``/ocm-provider/`` document fetched through the guarded
   client, and its signature verified when that document publishes a key
   (403 ``sender_unverified``);
4. the recipient, after the sender, so an unverified sender learns nothing
   about which users exist; an unknown user and an unknown host answer the
   same 404;
5. the WebDAV base, from the verified discovery document, never from the
   offer alone;
6. the store, keyed ``<sender host>|<providerId>``, with the secret in the
   secrets store under ``shared_source`` until the user accepts;
7. the fixed-text bell row (`notifications.resolvers.shared_source`), whose
   push is handed to the daemon in the same transaction, since this runs in
   the web process;
8. 201 with the user's display name.

The offer proves only that the sender runs OCM. That the secret is real is
proved at accept, by a PROPFIND with it against the verified host (Stage 7).
"""

from __future__ import annotations

import base64
import binascii
import email.utils
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from istota.lib.rate_limit import SlidingWindow

logger = logging.getLogger("istota.sharing.ocm")

API_VERSION = "1.1.0"
PROVIDER = "Istota"
OFFER_MAX_BYTES = 64 * 1024
DISCOVERY_MAX_BYTES = 64 * 1024
DISCOVERY_TIMEOUT_SECONDS = 5.0
STRING_MAX_CHARS = 255
DATE_SKEW_SECONDS = 300

KIND = "ocm_share"
# Per minute. A Nextcloud sends one request per share and retries a failure on
# the legacy OCS path rather than here, so these bound a flood, not real use.
ADDRESS_MAX_PER_MINUTE = 30
SENDER_MAX_PER_MINUTE = 20
WINDOW_SECONDS = 60

# What the signature must cover, as Nextcloud 31 signs it.
REQUIRED_SIGNED_HEADERS = ("(request-target)", "content-length", "date", "digest", "host")
_SIGNATURE_PARAM = re.compile(r'\s*([a-zA-Z]+)\s*=\s*"([^"]*)"\s*(?:,|$)')

_FIXED_FIELDS = ("shareWith", "shareType", "name", "resourceType", "providerId",
                 "owner", "sharedBy", "protocol")
_OPTIONAL_STRINGS = ("description", "ownerDisplayName", "sharedByDisplayName")
_HOST_PORT = re.compile(r"\A[a-z0-9.-]+(:[0-9]{1,5})?\Z|\A\[[0-9a-f:.]+\](:[0-9]{1,5})?\Z")


@dataclass(frozen=True)
class CloudId:
    """A federated cloud id: ``user@[scheme://]host[:port][/path]``."""

    user: str
    host: str          # host[:port], lower-case
    scheme: str | None  # "https", "http", or None when the id carries none
    path: str = ""      # "" or "/path", no trailing slash


@dataclass
class OcmAnswer:
    status: int
    body: dict | list = field(default_factory=dict)


class _Refused(Exception):
    def __init__(self, status: int, message: str, name: str | None = None,
                 detail: str = "invalid") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.name = name
        self.detail = detail

    def answer(self) -> OcmAnswer:
        errors = [{"name": self.name, "message": self.detail}] if self.name else []
        return OcmAnswer(self.status, {"message": self.message, "validationErrors": errors})


# ------------------------------------------------------------------ discovery

def endpoint(config) -> str:
    """``https://<site hostname>/ocm``; http on a loopback name, as `external_origin` says."""
    from istota.webui.origin import external_origin

    host, scheme = external_origin(config)
    return f"{scheme}://{host}/ocm"


def discovery_document(config) -> dict:
    """What ``/ocm-provider/`` and ``/.well-known/ocm`` answer.

    Nextcloud requires a non-empty ``apiVersion`` and ``endPoint`` and checks
    nothing else; it parses ``enabled`` and ignores it. No ``publicKey``:
    Istota signs nothing.
    """
    return {
        "enabled": True,
        "apiVersion": API_VERSION,
        "endPoint": endpoint(config),
        "provider": PROVIDER,
        "resourceTypes": [
            {"name": "file", "shareTypes": ["user"], "protocols": {"webdav": "/webdav/"}},
        ],
    }


def parse_cloud_id(raw: object) -> CloudId | None:
    """Split at the last ``@``; strip and remember a leading ``https://`` or ``http://``.

    The rest is a host, an optional port and an optional path. None for
    anything else, including userinfo, a query or another scheme.
    """
    if not isinstance(raw, str) or not raw or len(raw) > STRING_MAX_CHARS:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw):
        return None
    user, at, remote = raw.rpartition("@")
    # A user part holding a scheme or a slash is a remote with userinfo in it.
    if not at or not user or not remote or "/" in user or ":" in user:
        return None
    scheme = None
    lowered = remote.lower()
    for candidate in ("https", "http"):
        if lowered.startswith(candidate + "://"):
            scheme = candidate
            remote = remote[len(candidate) + 3:]
            break
    else:
        if "://" in remote:
            return None
    if any(ch in remote for ch in "?#@\\"):
        return None
    host, slash, path = remote.partition("/")
    host = host.lower()
    if not host or not _HOST_PORT.fullmatch(host):
        return None
    path = ("/" + path).rstrip("/") if slash else ""
    if "/." in path or "//" in path:
        return None
    return CloudId(user=user, host=host, scheme=scheme, path=path)


# ------------------------------------------------------------------ the limits

_lock = threading.Lock()
_address_window = SlidingWindow(WINDOW_SECONDS, ADDRESS_MAX_PER_MINUTE)
_sender_window = SlidingWindow(WINDOW_SECONDS, SENDER_MAX_PER_MINUTE)
_discovery_window: SlidingWindow | None = None


def reset_limits() -> None:
    """Forget every recorded hit; for tests."""
    global _address_window, _sender_window, _discovery_window
    with _lock:
        _address_window = SlidingWindow(WINDOW_SECONDS, ADDRESS_MAX_PER_MINUTE)
        _sender_window = SlidingWindow(WINDOW_SECONDS, SENDER_MAX_PER_MINUTE)
        _discovery_window = None


def _discovery_limit(max_per_minute: int) -> SlidingWindow:
    global _discovery_window
    with _lock:
        if _discovery_window is None or _discovery_window.max_hits != max_per_minute:
            _discovery_window = SlidingWindow(WINDOW_SECONDS, max_per_minute, max_keys=1)
        return _discovery_window


def _admit(config, address: str | None, sender_key: str, tick: float) -> bool:
    if address is not None and not _address_window.admit(address, tick):
        return False
    if not _sender_window.admit(sender_key, tick):
        return False
    window = _discovery_limit(int(config.sharing.ocm.max_discovery_fetches_per_minute))
    return window.admit("discovery", tick)


# ----------------------------------------------------------------- signatures

def _signature_params(value: str) -> dict[str, str] | None:
    params: dict[str, str] = {}
    position = 0
    while position < len(value):
        match = _SIGNATURE_PARAM.match(value, position)
        if not match:
            return None
        params[match.group(1).lower()] = match.group(2)
        position = match.end()
    return params


def _key_host(key_id: str) -> str | None:
    try:
        parts = urlsplit(key_id)
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or not parts.netloc or "@" in parts.netloc:
        return None
    return parts.netloc.lower()


def verify_signature(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    public_key_pem: str,
    sender_host: str,
    now: float,
) -> str | None:
    """A refusal code, or None when ``body`` is signed by ``public_key_pem``.

    draft-cavage-http-signatures-12, as Nextcloud 31 sends it: ``keyId``
    ``https://<host>/ocm#signature``, whose host (the scheme ignored) must be
    the sender host; ``rsa-sha512`` or ``rsa-sha256`` (PKCS#1 v1.5); the
    signed headers covering `REQUIRED_SIGNED_HEADERS`; a ``Digest`` of the raw
    body; a ``Date`` within five minutes of ``now``. ``headers`` is keyed in
    lower case.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    raw = headers.get("signature")
    if not raw:
        return "signature_missing"
    params = _signature_params(raw)
    if not params or not {"keyid", "algorithm", "headers", "signature"} <= set(params):
        return "signature_malformed"
    if _key_host(params["keyid"]) != sender_host.lower():
        return "key_host_mismatch"
    algorithm = params["algorithm"].lower()
    if algorithm == "rsa-sha512":
        hash_algorithm = hashes.SHA512()
    elif algorithm == "rsa-sha256":
        hash_algorithm = hashes.SHA256()
    else:
        return "algorithm_refused"
    signed = params["headers"].lower().split()
    if not set(REQUIRED_SIGNED_HEADERS) <= set(signed):
        return "headers_not_signed"

    digest = headers.get("digest") or ""
    label, _, encoded = digest.partition("=")
    digests = {"SHA-512": hashlib.sha512, "SHA-256": hashlib.sha256}
    if label.upper() not in digests:
        return "digest_mismatch"
    try:
        claimed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return "digest_mismatch"
    if claimed != digests[label.upper()](body).digest():
        return "digest_mismatch"
    if (headers.get("content-length") or "").strip() != str(len(body)):
        return "digest_mismatch"

    try:
        sent = email.utils.parsedate_to_datetime(headers.get("date") or "").timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return "date_stale"
    if abs(now - sent) > DATE_SKEW_SECONDS:
        return "date_stale"

    lines = []
    for name in signed:
        if name == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
        elif name in headers:
            lines.append(f"{name}: {headers[name]}")
        else:
            return "signature_malformed"
    try:
        signature = base64.b64decode(params["signature"], validate=True)
    except (binascii.Error, ValueError):
        return "signature_malformed"
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
    except (ValueError, TypeError, AttributeError):
        return "key_unusable"
    if not isinstance(key, rsa.RSAPublicKey):
        return "key_unusable"
    try:
        key.verify(signature, "\n".join(lines).encode(), padding.PKCS1v15(), hash_algorithm)
    except InvalidSignature:
        return "signature_invalid"
    return None


# ------------------------------------------------------------------ the offer

def _string(body: dict, name: str, *, required: bool = True) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > STRING_MAX_CHARS:
        raise _Refused(400, "invalid_offer", name)
    return value


def _provider_id(value: object) -> str:
    # A JSON number in offers, a string in notifications; stored as decimal.
    if isinstance(value, bool):
        raise _Refused(400, "invalid_offer", "providerId")
    if isinstance(value, int) and 0 <= value < 10 ** 18:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
        return str(int(value))
    raise _Refused(400, "invalid_offer", "providerId")


@dataclass(frozen=True)
class _Offer:
    share_with: str
    name: str
    provider_id: str
    owner: str
    shared_by: str
    secret: str
    uri: str | None


def _parse_offer(raw: bytes) -> _Offer:
    if len(raw) > OFFER_MAX_BYTES:
        raise _Refused(400, "invalid_offer")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Refused(400, "invalid_offer") from None
    if not isinstance(body, dict):
        raise _Refused(400, "invalid_offer")
    for name in _FIXED_FIELDS:
        if name not in body:
            raise _Refused(400, "invalid_offer", name, "missing")
    if body.get("resourceType") != "file":
        raise _Refused(400, "invalid_offer", "resourceType")
    if body.get("shareType") != "user":
        raise _Refused(400, "invalid_offer", "shareType")
    for name in _OPTIONAL_STRINGS:
        _string(body, name, required=False)
    protocol = body.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("name") != "webdav":
        raise _Refused(400, "invalid_offer", "protocol")
    options = protocol.get("options")
    if not isinstance(options, dict):
        raise _Refused(400, "invalid_offer", "protocol")
    secret = options.get("sharedSecret")
    if not isinstance(secret, str) or not secret or len(secret) > STRING_MAX_CHARS:
        raise _Refused(400, "invalid_offer", "sharedSecret")
    uri = options.get("uri")
    if uri is not None and (not isinstance(uri, str) or not uri or len(uri) > STRING_MAX_CHARS):
        raise _Refused(400, "invalid_offer", "uri")
    return _Offer(
        share_with=_string(body, "shareWith"),
        name=_string(body, "name"),
        provider_id=_provider_id(body.get("providerId")),
        owner=_string(body, "owner"),
        shared_by=_string(body, "sharedBy"),
        secret=secret,
        uri=uri,
    )


# ------------------------------------------------------------------- the sender

def _origin(cloud: CloudId) -> str:
    return f"{cloud.scheme or 'https'}://{cloud.host}{cloud.path}"


def fetch_discovery(config, cloud: CloudId, *, resolve=None) -> dict | None:
    """The sender's ``/ocm-provider/`` document, or None for any failure.

    Through the guarded client: no redirect, 5 s, 64 KiB, plain http and a
    private address only for a ``[sharing] private_hosts`` host.
    """
    from istota.sandbox import guarded_http

    url = _origin(cloud) + "/ocm-provider/"
    with guarded_http.new_client(timeout=DISCOVERY_TIMEOUT_SECONDS) as client:
        try:
            response = guarded_http.guarded_send(
                client, "GET", url,
                private_hosts=config.sharing.private_hosts,
                max_bytes=DISCOVERY_MAX_BYTES,
                timeout=DISCOVERY_TIMEOUT_SECONDS,
                max_redirects=0,
                headers={"Accept": "application/json"},
                resolve=resolve,
            )
        except guarded_http.GuardRefused as exc:
            logger.info("ocm discovery refused host=%s code=%s", cloud.host, exc.code)
            return None
    if response.status_code != 200:
        logger.info("ocm discovery host=%s status=%s", cloud.host, response.status_code)
        return None
    try:
        document = json.loads(response.content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _verified_document(config, cloud: CloudId, document: dict | None) -> dict:
    if document is None:
        raise _Refused(403, "sender_unverified")
    version = document.get("apiVersion")
    end = document.get("endPoint")
    if not isinstance(version, str) or not version.strip():
        raise _Refused(403, "sender_unverified")
    if not isinstance(end, str) or not end:
        raise _Refused(403, "sender_unverified")
    try:
        end_host = (urlsplit(end).netloc or "").lower()
    except ValueError:
        raise _Refused(403, "sender_unverified") from None
    if end_host != cloud.host:
        raise _Refused(403, "sender_unverified")
    return document


def _public_key(document: dict) -> str | None:
    """The published PEM, None when nothing is published; a malformed one refuses."""
    key = document.get("publicKey")
    if key is None:
        return None
    if not isinstance(key, dict) or not isinstance(key.get("publicKeyPem"), str) \
            or not key["publicKeyPem"].strip():
        raise _Refused(403, "sender_unverified")
    return key["publicKeyPem"]


def _webdav_base(cloud: CloudId, document: dict) -> str:
    for resource in document.get("resourceTypes") or ():
        if not isinstance(resource, dict) or resource.get("name") != "file":
            continue
        protocols = resource.get("protocols")
        path = protocols.get("webdav") if isinstance(protocols, dict) else None
        if not isinstance(path, str) or not path:
            break
        if "://" in path:
            parts = urlsplit(path)
            if parts.netloc.lower() != cloud.host or parts.scheme not in ("http", "https"):
                break
            path = parts.path
        if not path.startswith("/") or ".." in path.split("/") or "?" in path or "#" in path:
            break
        return f"{cloud.scheme or 'https'}://{cloud.host}{path.rstrip('/')}/"
    raise _Refused(403, "sender_unverified")


def _share_path(base: str, uri: str | None) -> str:
    """The share's path under the base: "" for the root, which is Nextcloud's case."""
    if uri is None:
        return ""
    if "://" in uri:
        parts = urlsplit(uri)
        base_parts = urlsplit(base)
        if parts.netloc.lower() != base_parts.netloc.lower() or not parts.path.startswith(base_parts.path):
            raise _Refused(400, "invalid_offer", "uri")
        uri = parts.path[len(base_parts.path):]
    segments = [s for s in uri.strip("/").split("/") if s]
    if any(s in (".", "..") for s in segments) or any(ch in uri for ch in "?#\\"):
        raise _Refused(400, "invalid_offer", "uri")
    return "/".join(segments)


# ------------------------------------------------------------------ the store

def _pending_count(conn: sqlite3.Connection, user_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM shared_sources WHERE user_id = ? AND kind = ? AND state = 'pending'",
        (user_id, KIND),
    ).fetchone()[0]


def _store(conn, config, user_id: str, cloud: CloudId, offer: _Offer, base: str,
           share_path: str) -> str:
    """Write the row and its held secret; the outcome, for the log and the bell."""
    from istota.credentials import store as secrets_store
    from istota.sharing import sources

    remote_key = f"{cloud.host}|{offer.provider_id}"
    extras = {
        "name": offer.name,
        "owner": offer.owner,
        "shared_by": offer.shared_by,
        "provider_id": offer.provider_id,
        "webdav": base,
        "share_path": share_path,
    }
    cap = int(config.sharing.ocm.max_pending_per_user)
    existing = sources.find_source(conn, user_id, KIND, remote_key)
    if existing is None:
        if _pending_count(conn, user_id) >= cap:
            raise _Refused(429, "too_many_pending")
        source_id = sources.create_source(
            conn, user_id=user_id, kind=KIND, state="pending", remote_host=cloud.host,
            remote_key=remote_key, access="read", extras=extras,
        )
        key, outcome = str(source_id), "new"
    elif existing.state == "declined":
        return "declined"
    elif existing.state == "revoked":
        if _pending_count(conn, user_id) >= cap:
            raise _Refused(429, "too_many_pending")
        sources.set_state(conn, existing.id, user_id, "pending")
        sources.set_details(conn, existing.id, user_id, extras=extras)
        key, outcome = str(existing.id), "reopened"
    elif existing.state == "pending":
        sources.set_details(conn, existing.id, user_id, extras={**existing.extras, **extras})
        key, outcome = str(existing.id), "repeat"
    else:
        # Active, or active in error: the live share stays on its credential,
        # and the new secret is held apart until the user accepts it.
        sources.set_details(conn, existing.id, user_id,
                            extras={**existing.extras, "reoffer_pending": True})
        key, outcome = f"{existing.id}:reoffer", "reoffer"
    secrets_store.set_secret(None, user_id, sources.SECRET_SERVICE, key, offer.secret,
                             connection=conn)
    return outcome


# --------------------------------------------------------------- the receiver

def _sender_key(offer_owner: str) -> str:
    cloud = parse_cloud_id(offer_owner)
    return cloud.host if cloud is not None else "unparsed:" + offer_owner[:64]


def _recipient(config, raw: str) -> str:
    cloud = parse_cloud_id(raw)
    site = (config.site.hostname or "").strip().lower()
    if cloud is None or not site or cloud.host != site or cloud.path:
        raise _Refused(404, "recipient_not_found", "shareWith", "not_found")
    user_id = config.resolve_user_id(cloud.user)
    if user_id is None:
        raise _Refused(404, "recipient_not_found", "shareWith", "not_found")
    return user_id


def receive_share(
    conn: sqlite3.Connection,
    config,
    body: bytes,
    headers: Mapping[str, str],
    *,
    client_address: str | None = None,
    now: float | None = None,
    resolve=None,
) -> OcmAnswer:
    """Take one ``POST /ocm/shares``; the answer to send. Commits its own write.

    ``headers`` is keyed in lower case. ``client_address`` is the client's
    address when a trusted proxy names it, and None otherwise.
    """
    from istota.credentials import store as secrets_store

    try:
        if not config.sharing.ocm.enabled:
            return OcmAnswer(404, {"message": "not_found", "validationErrors": []})
        offer = _parse_offer(body)
        if not _admit(config, client_address, _sender_key(offer.owner), time.monotonic()):
            raise _Refused(429, "rate_limited")

        owner = parse_cloud_id(offer.owner)
        shared_by = parse_cloud_id(offer.shared_by)
        if owner is None or shared_by is None or shared_by.host != owner.host:
            raise _Refused(403, "sender_unverified")
        document = _verified_document(config, owner, fetch_discovery(config, owner, resolve=resolve))
        key_pem = _public_key(document)
        if key_pem is not None:
            refusal = verify_signature("POST", "/ocm/shares", headers, body, key_pem, owner.host,
                                       time.time() if now is None else now)
            if refusal is not None:
                logger.info("ocm offer refused host=%s reason=%s", owner.host, refusal)
                raise _Refused(403, "sender_unverified")

        user_id = _recipient(config, offer.share_with)
        base = _webdav_base(owner, document)
        share_path = _share_path(base, offer.uri)
        if not secrets_store.secret_key_available():
            raise _Refused(503, "unavailable")

        from istota.notifications.resolvers import shared_source
        from istota.notifications.store import hand_to_daemon

        try:
            conn.execute("BEGIN IMMEDIATE")
            outcome = _store(conn, config, user_id, owner, offer, base, share_path)
            if outcome != "declined":
                hand_to_daemon(conn, [shared_source.write(conn, user_id)])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        logger.info("ocm offer received user=%s host=%s outcome=%s", user_id, owner.host, outcome)
        profile = config.users.get(user_id)
        name = (profile.display_name if profile is not None else "") or user_id
        return OcmAnswer(201, {"recipientDisplayName": name})
    except _Refused as refused:
        return refused.answer()
