"""Token-authenticated APNs requests; optional HTTP/2, no delivery retries.

Apple requires JWS's raw r || s signature, not cryptography's DER encoding.
The process reuses one HTTP/2 connection pool and a 50-minute provider token.
Only an expired provider token can cause one retry, subject to Apple's
20-minute mint floor. A newly minted token rejected as expired is an operator
problem; minting again for each device would rate-limit the whole deployment.
"""

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from istota.config import PushConfig

logger = logging.getLogger(__name__)
_TOKEN_TTL_SECONDS = 3000
_TOKEN_MIN_SECONDS = 1200
_MAX_PAYLOAD_BYTES = 4096
_HOSTS = {"production": "api.push.apple.com", "sandbox": "api.sandbox.push.apple.com"}
# Never persist arbitrary response text, even if it resembles a reason word.
_REASONS = frozenset({
    "BadCollapseId", "BadDeviceToken", "BadExpirationDate", "BadMessageId",
    "BadPriority", "BadTopic", "DeviceTokenNotForTopic", "DuplicateHeaders",
    "IdleTimeout", "InvalidPushType", "MissingDeviceToken", "MissingTopic",
    "PayloadEmpty", "TopicDisallowed", "BadCertificate", "BadCertificateEnvironment",
    "ExpiredProviderToken", "Forbidden", "InvalidProviderToken", "MissingProviderToken",
    "BadPath", "MethodNotAllowed", "Unregistered", "PayloadTooLarge",
    "TooManyProviderTokenUpdates", "TooManyRequests", "InternalServerError",
    "ServiceUnavailable", "Shutdown",
})
_TOKEN = re.compile(r"[0-9a-fA-F]+\Z")
_lock = threading.RLock()
_http_client: httpx.Client | None = None
_cached_credentials = None
_cached_token: str | None = None
_cached_at = 0.0


@dataclass(frozen=True)
class ApnsCredentials:
    key_path: str
    key_id: str
    team_id: str
    topic: str


@dataclass(frozen=True)
class ApnsResult:
    status: int = 0
    reason: str = ""
    timestamp: int | None = None  # APNs invalidation time, milliseconds since epoch
    badge: int | None = None
    device_created_at: str | None = None

    @property
    def success(self) -> bool:
        return self.status == 200


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def provider_token(config: PushConfig, *, force_refresh: bool = False) -> str | None:
    """Return a cached ES256 JWT; an unavailable credential never escapes."""
    global _cached_credentials, _cached_token, _cached_at
    try:
        if not config.enabled:
            return None
        credentials = ApnsCredentials(
            config.apns_key_path, config.apns_key_id, config.apns_team_id, config.apns_topic,
        )
        if not all((credentials.key_path, credentials.key_id, credentials.team_id, credentials.topic)):
            raise ValueError("incomplete APNs configuration")
        with _lock:
            now = time.time()
            age = now - _cached_at
            if _cached_credentials == credentials and _cached_token:
                if age < _TOKEN_MIN_SECONDS or (not force_refresh and age < _TOKEN_TTL_SECONDS):
                    return _cached_token
            private_key = serialization.load_pem_private_key(
                Path(credentials.key_path).read_bytes(), password=None,
            )
            if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
                raise ValueError("APNs requires a P-256 private key")
            header = _encode(json.dumps({"alg": "ES256", "kid": credentials.key_id}, separators=(",", ":")).encode())
            claims = _encode(json.dumps({"iss": credentials.team_id, "iat": int(now)}, separators=(",", ":")).encode())
            signing_input = f"{header}.{claims}".encode("ascii")
            r, s = decode_dss_signature(private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
            signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
            _cached_token = f"{header}.{claims}.{_encode(signature)}"
            _cached_at = now
            _cached_credentials = credentials
            return _cached_token
    except Exception as exc:
        logger.warning("APNs provider token unavailable: %s", type(exc).__name__)
        return None


def _client() -> httpx.Client:
    global _http_client
    with _lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(http2=True, timeout=10.0)
        return _http_client


def send(
    config: PushConfig, device: dict, payload: dict, *, push_type: str = "alert",
    priority: int = 10, collapse_id: str | None = None, expiration: int = 0,
    transport: httpx.BaseTransport | None = None,
) -> ApnsResult:
    """Send once, except for one bounded provider-token refresh.

    ``transport`` is the HTTP boundary for offline tests; production reuses
    ``_client()``. Results carry the registration generation so late responses
    cannot remove or update a row that was registered again during the send.
    """
    created_at = None
    badge = None
    try:
        created_at = device.get("created_at")
        badge = payload.get("aps", {}).get("badge")
        if not config.enabled:
            return ApnsResult(reason="PushDisabled", device_created_at=created_at)
        token = device["token"]
        if not isinstance(token, str) or not _TOKEN.fullmatch(token) or len(token) % 2:
            raise ValueError("invalid device token")
        host = _HOSTS[device["environment"]]
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > _MAX_PAYLOAD_BYTES:
            return ApnsResult(reason="PayloadTooLarge", device_created_at=created_at)
        jwt = provider_token(config)
        if not jwt:
            return ApnsResult(reason="ProviderTokenUnavailable", device_created_at=created_at)
        headers = {
            "authorization": f"bearer {jwt}", "content-type": "application/json",
            "apns-topic": config.apns_topic, "apns-push-type": push_type,
            "apns-priority": str(priority), "apns-expiration": str(expiration),
        }
        if collapse_id is not None:
            if len(collapse_id.encode("utf-8")) > 64:
                raise ValueError("collapse id too long")
            headers["apns-collapse-id"] = collapse_id
        client = httpx.Client(transport=transport, http2=True, timeout=10.0) if transport else _client()
        try:
            for attempt in range(2):
                response = client.post(f"https://{host}/3/device/{token}", content=body, headers=headers)
                status = response.status_code
                reason = ""
                timestamp = None
                if status != 200:
                    try:
                        data = response.json()
                        raw_reason = data.get("reason", "")
                        reason = raw_reason if isinstance(raw_reason, str) and raw_reason in _REASONS else "UnknownResponse"
                        raw_timestamp = data.get("timestamp")
                        if type(raw_timestamp) is int and raw_timestamp >= 0:
                            timestamp = raw_timestamp
                    except (ValueError, AttributeError):
                        reason = "InvalidResponse"
                if status == 403 and reason == "ExpiredProviderToken" and attempt == 0:
                    refreshed = provider_token(config, force_refresh=True)
                    if refreshed and refreshed != jwt:
                        headers["authorization"] = f"bearer {refreshed}"
                        continue
                result = ApnsResult(status, reason, timestamp, badge, created_at)
                if not result.success:
                    level = logging.ERROR if reason in {"BadTopic", "InvalidProviderToken"} else logging.WARNING
                    logger.log(level, "APNs device %s (...%s): %s %s", device.get("id"), token[-6:], status, reason)
                return result
        finally:
            if transport is not None:
                client.close()
    except Exception as exc:
        # HTTP exceptions contain the URL, whose path contains the full token.
        logger.warning("APNs send failed: %s", type(exc).__name__)
        return ApnsResult(reason=type(exc).__name__, device_created_at=created_at)
    return ApnsResult(reason="ExpiredProviderToken", device_created_at=created_at)
