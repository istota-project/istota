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
proved at accept, by a PROPFIND with it against the verified host.

**Accepting** (`accept`) refuses first on an unsandboxed multi-user install
(`credentials.names.vault_isolation_refusal`), then PROPFINDs the share root
with the held secret and reads its access from ``oc:permissions``, then in one
transaction stores the secret as the local credential `sources.credential_name`
bound to the sender, with its broker grant, and sets the row active. A
re-offer of an active share replaces the credential's value the same way, and
only once its own PROPFIND has succeeded. Only a signed repeat is held at all
(`_store`): a PROPFIND proves a secret opens something on the sender's host,
not that it is the share the owner made. `decline`, `remove` and the scheduler's `expire_pending` take the
row to ``declined`` or ``revoked``, which deletes every secret it held.

**Telling the sender** (`notify_sender`) is an unsigned POST to the
``endPoint`` its discovery document names, made after the commit; a failure is
logged and undoes nothing. ``SHARE_DECLINED`` makes Nextcloud delete the
share, so declining a re-offer of an active share sends nothing: it would end
the live share too.

**Remote notifications** (`receive_notification`) act only when the
notification's ``sharedSecret`` is the named share's own, compared in constant
time, and, where the sender's discovery document publishes a key, when its
signature verifies. ``SHARE_UNSHARED`` revokes; ``RESHARE_CHANGE_PERMISSION``
re-reads ``oc:permissions`` (Nextcloud never sends it to a recipient, so the
``shares`` skill's own re-read is what keeps access current there); any other
type is 501, ``RESHARE_UNDO`` included.
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
DISCOVERY_DEADLINE_SECONDS = 10.0
STRING_MAX_CHARS = 255
DATE_SKEW_SECONDS = 300

KIND = "ocm_share"
# Per minute. A Nextcloud sends one request per share and retries a failure on
# the legacy OCS path rather than here, so these bound a flood, not real use.
ADDRESS_MAX_PER_MINUTE = 30
SENDER_MAX_PER_MINUTE = 20
# Every notification is checked against stored secrets before its sender is
# known, and each check decrypts; this caps that work across all callers.
SECRET_CHECKS_PER_MINUTE = 60
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
_secret_check_window = SlidingWindow(WINDOW_SECONDS, SECRET_CHECKS_PER_MINUTE, max_keys=1)
_discovery_window: SlidingWindow | None = None


def reset_limits() -> None:
    """Forget every recorded hit; for tests."""
    global _address_window, _sender_window, _secret_check_window, _discovery_window
    with _lock:
        _address_window = SlidingWindow(WINDOW_SECONDS, ADDRESS_MAX_PER_MINUTE)
        _sender_window = SlidingWindow(WINDOW_SECONDS, SENDER_MAX_PER_MINUTE)
        _secret_check_window = SlidingWindow(WINDOW_SECONDS, SECRET_CHECKS_PER_MINUTE, max_keys=1)
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


def _admit_secret_check(address: str | None, tick: float) -> bool:
    if address is not None and not _address_window.admit(address, tick):
        return False
    return _secret_check_window.admit("secret_check", tick)


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


def _provider_id(value: object, message: str = "invalid_offer") -> str:
    # A JSON number in offers, a string in notifications; stored as decimal.
    if isinstance(value, bool):
        raise _Refused(400, message, "providerId")
    if isinstance(value, int) and 0 <= value < 10 ** 18:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
        return str(int(value))
    raise _Refused(400, message, "providerId")


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
                deadline_seconds=DISCOVERY_DEADLINE_SECONDS,
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
           share_path: str, *, signed: bool) -> str:
    """Write the row and its held secret; the outcome, for the log and the bell.

    A repeat for a pending or live share replaces a secret only when the offer
    was signed: unsigned, anyone holding a secret the same host accepts (their
    own public link, say) could swap in a folder of their choosing, and the
    accept-time PROPFIND would pass. Such a repeat changes nothing.
    """
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
    elif not signed:
        return "repeat_unsigned"
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
            outcome = _store(conn, config, user_id, owner, offer, base, share_path,
                             signed=key_pem is not None)
            if outcome not in ("declined", "repeat_unsigned"):
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


# ------------------------------------------------------- the share's own root

PROPFIND_MAX_BYTES = 256 * 1024
REMOTE_TIMEOUT_SECONDS = 10.0
REMOTE_DEADLINE_SECONDS = 30.0
_OC_PERMISSIONS = "{http://owncloud.org/ns}permissions"
# `oc:permissions` letters that mean the share may be written: create (C),
# create folders (K), write (W). An edit share reads RGDNVCK, a read share RG.
_WRITE_LETTERS = frozenset("CKW")
_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    b"<d:prop><oc:permissions/></d:prop></d:propfind>"
)

_ACTION_MESSAGES = {
    "not_found": "That folder share was not found.",
    "not_waiting": "That folder offer is no longer waiting.",
    "not_active": "That folder share is not active.",
    "share_unopened": "This share could not be opened.",
    "share_unreachable": "The sharing server could not be reached.",
    "offer_changed": "The offer changed while it was being accepted. Try again.",
    "credential_refused": "The share's credential could not be stored.",
    "unavailable": "This server cannot store credentials until its credential store is set up.",
}


class ShareActionRefused(Exception):
    """A user's accept, decline or remove was refused; ``code`` says why, the text is fixed.

    From `share_access`, ``status`` is the share root's HTTP status when it
    answered one, and ``reason`` the fixed code a source's ``last_reason``
    takes (``http_<status>``, a guard code, ``invalid_response``), so the
    ``shares`` skill can tell a revoked share from a failing server.
    """

    def __init__(self, code: str, message: str | None = None, *,
                 status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message or _ACTION_MESSAGES[code])
        self.code = code
        self.status = status
        self.reason = reason or code


@dataclass(frozen=True)
class ActionResult:
    source_id: int
    state: str
    access: str
    notified: bool


def share_root_url(source) -> str:
    """The share root: the verified WebDAV base, and the share's path under it."""
    from urllib.parse import quote

    base = str(source.extras.get("webdav") or "")
    if not base.endswith("/"):
        raise ShareActionRefused("share_unreachable")
    path = str(source.extras.get("share_path") or "")
    if not path:
        return base
    return base + "/".join(quote(segment, safe="") for segment in path.split("/")) + "/"


def _access_from(permissions: str | None) -> str:
    return "write" if permissions and _WRITE_LETTERS & set(permissions) else "read"


def share_access(config, source, secret: str, *, resolve=None) -> str:
    """PROPFIND the share root with ``secret``; its access, ``read`` or ``write``.

    The proof that the secret opens the share on the verified host:
    `ShareActionRefused` ``share_unopened`` on a 401, 403 or 404, and
    ``share_unreachable`` on anything else that is not a multistatus.
    Nextcloud's public WebDAV endpoint takes the secret as the Basic user with
    an empty password.
    """
    from istota.lib import dav_xml
    from istota.sandbox import guarded_http

    url = share_root_url(source)
    with guarded_http.new_client(timeout=REMOTE_TIMEOUT_SECONDS) as client:
        try:
            response = guarded_http.guarded_send(
                client, "PROPFIND", url,
                private_hosts=config.sharing.private_hosts,
                max_bytes=PROPFIND_MAX_BYTES,
                timeout=REMOTE_TIMEOUT_SECONDS,
                deadline_seconds=REMOTE_DEADLINE_SECONDS,
                auth=(secret, ""),
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                content=_PROPFIND_BODY,
                resolve=resolve,
            )
        except guarded_http.GuardRefused as exc:
            logger.info("ocm share root refused source=%s code=%s", source.id, exc.code)
            raise ShareActionRefused("share_unreachable", reason=exc.code) from None
    status = response.status_code
    if status in (401, 403, 404):
        logger.info("ocm share root source=%s status=%s", source.id, status)
        raise ShareActionRefused("share_unopened", status=status, reason=f"http_{status}")
    if status != 207:
        logger.info("ocm share root source=%s status=%s", source.id, status)
        raise ShareActionRefused("share_unreachable", status=status, reason=f"http_{status}")
    try:
        rows = dav_xml.parse_multistatus(response.content)
    except dav_xml.DavXmlError:
        raise ShareActionRefused("share_unreachable", status=status,
                                 reason="invalid_response") from None
    if not rows:
        raise ShareActionRefused("share_unreachable", status=status, reason="invalid_response")
    element = rows[0][1].get(_OC_PERMISSIONS)
    return _access_from(element.text if element is not None else None)


# -------------------------------------------------------- telling the sender

_NOTIFICATION_MESSAGES = {
    "SHARE_ACCEPTED": "Recipient accepted the share",
    "SHARE_DECLINED": "Recipient declined the share",
}


def notify_sender(config, source, notification_type: str, secret: str, *, resolve=None) -> bool:
    """POST ``notification_type`` to the sender's ``endPoint/notifications``; whether it took.

    Unsigned: Istota signs nothing (see the module docstring). The endpoint
    comes from the sender's discovery document, verified as at the offer.
    Never raises; a failure is logged with the host and a code only.
    """
    from istota.sandbox import guarded_http

    owner = parse_cloud_id(source.extras.get("owner"))
    if owner is None or owner.host != source.remote_host:
        logger.info("ocm notify source=%s type=%s skipped=no_owner", source.id, notification_type)
        return False
    try:
        document = _verified_document(config, owner, fetch_discovery(config, owner, resolve=resolve))
    except _Refused:
        logger.info("ocm notify source=%s type=%s skipped=sender_unverified",
                    source.id, notification_type)
        return False
    body = json.dumps({
        "notificationType": notification_type,
        "resourceType": "file",
        "providerId": str(source.extras.get("provider_id") or source.remote_key.rpartition("|")[2]),
        "notification": {
            "sharedSecret": secret,
            "message": _NOTIFICATION_MESSAGES.get(notification_type, notification_type),
        },
    }).encode()
    url = document["endPoint"].rstrip("/") + "/notifications"
    try:
        with guarded_http.new_client(timeout=REMOTE_TIMEOUT_SECONDS) as client:
            response = guarded_http.guarded_send(
                client, "POST", url,
                private_hosts=config.sharing.private_hosts,
                max_bytes=DISCOVERY_MAX_BYTES,
                timeout=REMOTE_TIMEOUT_SECONDS,
                deadline_seconds=REMOTE_DEADLINE_SECONDS,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                content=body,
                resolve=resolve,
            )
    except guarded_http.GuardRefused as exc:
        logger.info("ocm notify source=%s type=%s code=%s", source.id, notification_type, exc.code)
        return False
    except Exception as exc:  # noqa: BLE001 -- never raises; the type only
        logger.warning("ocm notify source=%s type=%s failed: %s",
                       source.id, notification_type, type(exc).__name__)
        return False
    ok = 200 <= response.status_code < 300
    logger.info("ocm notify source=%s host=%s type=%s status=%s",
                source.id, source.remote_host, notification_type, response.status_code)
    return ok


# ------------------------------------------------- the user's accept, decline

def _waiting_key(source) -> str | None:
    """The held secret's key while the user has something to answer, else None."""
    if source.kind != KIND:
        return None
    if source.state == "pending":
        return str(source.id)
    if source.state in ("active", "error") and source.extras.get("reoffer_pending") is True:
        return f"{source.id}:reoffer"
    return None


def _held_secret(conn, user_id: str, key: str) -> str | None:
    from istota.credentials import store as secrets_store
    from istota.sharing import sources

    value = secrets_store.get_secret(None, user_id, sources.SECRET_SERVICE, key,
                                     connection=conn, touch=False)
    return value if isinstance(value, str) and value else None


def _credential_value(conn, user_id: str, source_id: int) -> str | None:
    from istota.credentials import store as secrets_store
    from istota.credentials.names import VAULT_ENTRY_SERVICE
    from istota.sharing import sources

    value = secrets_store.get_secret(None, user_id, VAULT_ENTRY_SERVICE,
                                     sources.credential_name(source_id),
                                     connection=conn, touch=False)
    return value if isinstance(value, str) and value else None


def _owned_share(conn, source_id: int, user_id: str):
    from istota.sharing import sources

    source = sources.get_source(conn, source_id, user_id)
    if source is None or source.kind != KIND:
        raise ShareActionRefused("not_found")
    return source


def _store_credential(conn, user_id: str, source, secret: str, allow_scheduled: bool) -> None:
    """Create the share's local credential, or replace a re-offered one's value."""
    from istota.credentials import local
    from istota.sharing import sources

    name = sources.credential_name(source.id)
    try:
        if source.state != "pending" and local.is_local(conn, user_id, name):
            # A re-offer: the value changes, the site and the user's grant stay.
            fields = local.stored_fields(conn, user_id, name)
            local.update(conn, user_id, name, value=secret, username=None, url=fields["url"],
                         extra_hosts=fields["extra_hosts"], headers="", revealable=False,
                         system_prefix="ocm_share_")
        else:
            local.create(
                conn, user_id,
                local.LocalCredential(name=name, value=secret, url=f"https://{source.remote_host}"),
                access={"scope_mode": "all", "allow_scheduled": allow_scheduled},
                system_prefix="ocm_share_",
            )
    except local.LocalCredentialError as exc:
        logger.warning("ocm accept source=%s credential refused field=%s", source.id, exc.field)
        raise ShareActionRefused("credential_refused") from None


def accept(
    conn: sqlite3.Connection,
    config,
    source_id: int,
    user_id: str,
    *,
    allow_scheduled: bool = False,
    sandboxed: bool | None = None,
    resolve=None,
) -> ActionResult:
    """Accept the user's waiting offer, or the re-offer of an active share. Commits.

    ``sandboxed`` is the daemon's published verdict in the web process
    (`credentials.names.tasks_sandboxed`, the default). ``allow_scheduled``
    is the accept dialog's checkbox, off unless the user ticks it; a
    re-offer keeps the grant the share already has. Refusals are
    `ShareActionRefused`; nothing is written before the PROPFIND succeeds.
    """
    from istota.credentials import names
    from istota.credentials import store as secrets_store
    from istota.sharing import sources

    if type(allow_scheduled) is not bool:
        raise ValueError("allow_scheduled must be true or false")
    if sandboxed is None:
        sandboxed = names.tasks_sandboxed(config)
    refusal = names.vault_isolation_refusal(config, user_id, sandboxed=sandboxed)
    if refusal is not None:
        raise ShareActionRefused("isolation", refusal)
    if not secrets_store.secret_key_available():
        raise ShareActionRefused("unavailable")

    source = _owned_share(conn, source_id, user_id)
    key = _waiting_key(source)
    secret = _held_secret(conn, user_id, key) if key is not None else None
    if key is None or secret is None:
        raise ShareActionRefused("not_waiting")
    access = share_access(config, source, secret, resolve=resolve)

    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _owned_share(conn, source_id, user_id)
        if _waiting_key(current) != key or _held_secret(conn, user_id, key) != secret:
            raise ShareActionRefused("offer_changed")
        _store_credential(conn, user_id, current, secret, allow_scheduled)
        secrets_store.delete_secret(None, user_id, sources.SECRET_SERVICE, key, connection=conn)
        if "reoffer_pending" in current.extras:
            extras = {k: v for k, v in current.extras.items() if k != "reoffer_pending"}
            sources.set_details(conn, source_id, user_id, extras=extras)
        sources.set_access(conn, source_id, user_id, access)
        if current.state != "active":
            sources.set_state(conn, source_id, user_id, "active")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    after = sources.get_source(conn, source_id, user_id)
    logger.info("ocm share accepted user=%s source=%s host=%s access=%s reoffer=%s",
                user_id, source_id, after.remote_host, access, key.endswith(":reoffer"))
    notified = notify_sender(config, after, "SHARE_ACCEPTED", secret, resolve=resolve)
    return ActionResult(source_id, after.state, after.access, notified)


def decline(conn: sqlite3.Connection, config, source_id: int, user_id: str, *,
            resolve=None) -> ActionResult:
    """Decline the user's waiting offer, or the re-offer of an active share. Commits.

    A pending offer becomes ``declined``, its secret deleted, and the sender
    is told (Nextcloud then deletes the share). A re-offer's held secret is
    deleted and the active share stays as it is, with nothing sent: the
    sender would read ``SHARE_DECLINED`` for that provider id as the end of
    the live share.
    """
    from istota.credentials import store as secrets_store
    from istota.sharing import sources

    try:
        conn.execute("BEGIN IMMEDIATE")
        source = _owned_share(conn, source_id, user_id)
        key = _waiting_key(source)
        if key is None:
            raise ShareActionRefused("not_waiting")
        secret = _held_secret(conn, user_id, key)
        if source.state == "pending":
            sources.set_state(conn, source_id, user_id, "declined", reason="declined")
        else:
            secrets_store.delete_secret(None, user_id, sources.SECRET_SERVICE, key, connection=conn)
            extras = {k: v for k, v in source.extras.items() if k != "reoffer_pending"}
            sources.set_details(conn, source_id, user_id, extras=extras)
            secret = None
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    after = sources.get_source(conn, source_id, user_id)
    logger.info("ocm offer declined user=%s source=%s host=%s state=%s",
                user_id, source_id, after.remote_host, after.state)
    notified = (notify_sender(config, after, "SHARE_DECLINED", secret, resolve=resolve)
                if secret else False)
    return ActionResult(source_id, after.state, after.access, notified)


def remove(conn: sqlite3.Connection, config, source_id: int, user_id: str, *,
           resolve=None) -> ActionResult:
    """Remove the user's active share: ``revoked``, the credential deleted, the sender told. Commits.

    Nextcloud deletes a share on ``SHARE_DECLINED``, so this is in practice
    a recipient-side unshare.
    """
    from istota.sharing import sources

    try:
        conn.execute("BEGIN IMMEDIATE")
        source = _owned_share(conn, source_id, user_id)
        if source.state not in ("active", "error"):
            raise ShareActionRefused("not_active")
        secret = _credential_value(conn, user_id, source_id)
        sources.revoke(conn, source_id, user_id, reason="removed")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    after = sources.get_source(conn, source_id, user_id)
    logger.info("ocm share removed user=%s source=%s host=%s", user_id, source_id, after.remote_host)
    notified = (notify_sender(config, after, "SHARE_DECLINED", secret, resolve=resolve)
                if secret else False)
    return ActionResult(source_id, after.state, after.access, notified)


def expire_pending(conn: sqlite3.Connection, config, now=None) -> int:
    """Decline every offer left unanswered for ``[sharing.ocm] pending_days``; the count.

    Posts nothing: an expired offer is not the user's answer. ``now`` is an
    aware datetime (UTC now when None). One transaction per offer, so one
    row's failure is logged and the rest still expire.
    """
    from datetime import datetime, timedelta, timezone

    from istota.sharing import sources

    moment = now or datetime.now(timezone.utc)
    cutoff = (moment.astimezone(timezone.utc)
              - timedelta(days=int(config.sharing.ocm.pending_days))).strftime("%Y-%m-%d %H:%M:%S")
    expired = 0
    for source in sources.pending_since(conn, KIND, cutoff):
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = sources.get_source(conn, source.id, source.user_id)
            if current is not None and current.state == "pending" and current.updated_at <= cutoff:
                sources.set_state(conn, source.id, source.user_id, "declined", reason="expired")
                expired += 1
            conn.commit()
        except Exception as exc:  # noqa: BLE001 -- one offer's failure leaves the rest
            conn.rollback()
            logger.warning("ocm expiry source=%s failed: %s", source.id, type(exc).__name__)
    if expired:
        logger.info("ocm offers expired count=%d", expired)
    return expired


# ------------------------------------------------------ remote notifications

NOTIFICATION_MAX_BYTES = 64 * 1024
_HANDLED_NOTIFICATIONS = ("SHARE_UNSHARED", "RESHARE_CHANGE_PERMISSION")


@dataclass(frozen=True)
class _Notification:
    kind: str
    resource_type: str
    provider_id: str
    secret: str


def _parse_notification(raw: bytes) -> _Notification:
    if len(raw) > NOTIFICATION_MAX_BYTES:
        raise _Refused(400, "invalid_notification")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Refused(400, "invalid_notification") from None
    if not isinstance(body, dict):
        raise _Refused(400, "invalid_notification")
    kind = body.get("notificationType")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise _Refused(400, "invalid_notification", "notificationType")
    resource_type = body.get("resourceType")
    if not isinstance(resource_type, str) or len(resource_type) > 64:
        raise _Refused(400, "invalid_notification", "resourceType")
    provider_id = _provider_id(body.get("providerId"), "invalid_notification")
    notification = body.get("notification")
    if not isinstance(notification, dict):
        raise _Refused(400, "invalid_notification", "notification")
    secret = notification.get("sharedSecret")
    if not isinstance(secret, str) or not secret or len(secret) > STRING_MAX_CHARS:
        raise _Refused(400, "invalid_notification", "sharedSecret")
    return _Notification(kind, resource_type, provider_id, secret)


def _matching_share(conn, provider_id: str, secret: str):
    """The share named ``provider_id`` whose own secret is ``secret``, or None.

    Every candidate is compared, in constant time, so the answer does not
    depend on which row matched.
    """
    import hmac

    from istota.sharing import sources

    found = None
    given = secret.encode()
    for source in sources.shares_for_provider(conn, provider_id):
        if source.state == "pending":
            stored = _held_secret(conn, source.user_id, str(source.id))
        else:
            stored = _credential_value(conn, source.user_id, source.id)
        if stored is not None and hmac.compare_digest(stored.encode(), given) and found is None:
            found = source
    return found


def receive_notification(
    conn: sqlite3.Connection,
    config,
    body: bytes,
    headers: Mapping[str, str],
    *,
    client_address: str | None = None,
    now: float | None = None,
    resolve=None,
) -> OcmAnswer:
    """Take one ``POST /ocm/notifications``; the answer to send. Commits its own write.

    In order: the body (400); the type (501 for anything not handled); the
    per-address and global limits on secret checks (429), since a check
    decrypts stored secrets before the sender is known; the secret against
    the named share's own (403, nothing changed); the sender and discovery
    limits (429); the sender's discovery document, and its signature where
    the document publishes a key (403); then the change.
    """
    from istota.sharing import sources

    try:
        if not config.sharing.ocm.enabled:
            return OcmAnswer(404, {"message": "not_found", "validationErrors": []})
        note = _parse_notification(body)
        if note.kind not in _HANDLED_NOTIFICATIONS or note.resource_type != "file":
            raise _Refused(501, "not_implemented")
        if not _admit_secret_check(client_address, time.monotonic()):
            raise _Refused(429, "rate_limited")
        source = _matching_share(conn, note.provider_id, note.secret)
        if source is None:
            logger.info("ocm notification refused type=%s reason=secret_mismatch", note.kind)
            raise _Refused(403, "forbidden")
        # The address was counted above.
        if not _admit(config, None, source.remote_host, time.monotonic()):
            raise _Refused(429, "rate_limited")
        owner = parse_cloud_id(source.extras.get("owner"))
        if owner is None or owner.host != source.remote_host:
            raise _Refused(403, "forbidden")
        document = _verified_document(config, owner, fetch_discovery(config, owner, resolve=resolve))
        key_pem = _public_key(document)
        if key_pem is not None:
            refusal = verify_signature("POST", "/ocm/notifications", headers, body, key_pem,
                                       owner.host, time.time() if now is None else now)
            if refusal is not None:
                logger.info("ocm notification refused source=%s reason=%s", source.id, refusal)
                raise _Refused(403, "forbidden")

        if note.kind == "SHARE_UNSHARED":
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = sources.get_source(conn, source.id, source.user_id)
                if current is not None and current.state in ("pending", "active", "error"):
                    sources.revoke(conn, source.id, source.user_id, reason="remote_unshared")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        elif source.state in ("active", "error"):
            secret = _credential_value(conn, source.user_id, source.id)
            try:
                access = share_access(config, source, secret, resolve=resolve) if secret else None
            except ShareActionRefused as refused:
                logger.info("ocm permission re-read source=%s code=%s", source.id, refused.code)
                access = None
            if access is not None:
                sources.set_access(conn, source.id, source.user_id, access)
                conn.commit()
        logger.info("ocm notification user=%s source=%s host=%s type=%s",
                    source.user_id, source.id, source.remote_host, note.kind)
        return OcmAnswer(201, [])
    except _Refused as refused:
        return refused.answer()
