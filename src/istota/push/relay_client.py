"""The relay transport: one prepared push sent through the push relay.

`send` is `delivery.send_prepared`'s branch for `[push] delivery = "relay"`.
The request is istota-relay's `POST {relay_url}/v1/routes/{route_id}/send`
(that repository's `src/relay/send.py` is authoritative): the device's send
capability as the bearer, and a body of `version` 1, a `request_id`, `kind`
and either the sealed `envelope` (the `istota_private` object `build_delivery`
measured) or the `badge`. The relay only forwards signed envelopes, so a
generic alert is refused here rather than sent.

`request_id` is the envelope's `mid` for an alert, so the push and its ID are
one; a badge update has none and gets a fresh 16-byte ID. Nothing resends: a
lost answer is `delivery_unknown` and stays lost, as the dispatcher's alerts
are best effort.

The relay URL is the operator's configured value, not one a user chose, so
this is a plain `httpx` client with redirects off, as `apns.py` is, rather
than `sandbox/guarded_http`. The answer is read to at most 4096 bytes and held
to the relay's finite result schema, each result at its own HTTP status;
anything else, a redirect included, is `delivery_unknown`. Results become
`ApnsResult`s so `devices.record_result` applies them: `invalid_device`
deletes like an APNs 410, the route results in `devices.REPAIR_REASONS` mark
the device `repair`, and the rest are transient.

Never logged: the capability, the route ID, the envelope or an exception's
text, which can carry the URL.
"""

import logging
import threading
import time

import httpx

from istota.config import Config
from istota.push import apns, devices, envelope
from istota.push.apns import ApnsResult

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_RESPONSE_BYTES = 4096

# Each result and the HTTP statuses the relay answers it with.
_STATUSES = {
    "accepted": {200},
    "invalid_payload": {400, 409, 413},
    "unauthorized_route": {401},
    "expired_route": {403},
    "invalid_device": {410},
    "rate_limited": {429},
    "provider_unavailable": {502, 503},
    "delivery_unknown": {504},
}
_KEYS = {"result", "apns_status", "apns_reason", "invalidated_at", "retry_after"}
_INVALID_DEVICE_REASONS = {"BadDeviceToken", "DeviceTokenNotForTopic"}

UNKNOWN = "delivery_unknown"
UNREACHABLE = "relay_unreachable"
NEEDS_ENVELOPE = "relay_needs_envelope"

_lock = threading.Lock()
_http_client: httpx.Client | None = None


def _client() -> httpx.Client:
    global _http_client
    with _lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(follow_redirects=False)
        return _http_client


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_id(text) -> bool:
    try:
        return isinstance(text, str) and len(envelope.b64url_decode(text)) == 16
    except ValueError:
        return False


def _request_body(prepared) -> dict | None:
    """The relay's request body for a prepared push, or None for one it cannot carry."""
    if prepared.kind == "badge":
        return {"version": PROTOCOL_VERSION, "request_id": envelope.new_event_id(), "kind": "badge",
                "badge": max(0, min(int(prepared.badge or 0), envelope.MAX_BADGE))}
    try:
        private = envelope._strict_json(prepared.payload, "malformed").get("istota_private")
    except (ValueError, AttributeError):
        return None
    if not isinstance(private, dict):
        return None
    request_id = prepared.event_id if _is_id(prepared.event_id) else envelope.new_event_id()
    return {"version": PROTOCOL_VERSION, "request_id": request_id, "kind": "alert", "envelope": private}


def _answer(status: int, data: bytes) -> dict | None:
    """The relay's answer if it is one the schema allows at this status, else None."""
    try:
        answer = envelope._strict_json(data, "malformed")
    except ValueError:
        return None
    if not isinstance(answer, dict) or not set(answer) <= _KEYS:
        return None
    if status not in _STATUSES.get(answer.get("result"), ()):
        return None
    for name in ("apns_status", "invalidated_at", "retry_after"):
        if name in answer and (not _is_int(answer[name]) or answer[name] < 0):
            return None
    if "apns_reason" in answer and not isinstance(answer["apns_reason"], str):
        return None
    return answer


def _result(status: int, answer: dict, badge: int | None, created_at: str | None) -> ApnsResult:
    name = answer["result"]
    reason = answer.get("apns_reason")
    if reason not in apns._REASONS:
        reason = None
    if name == "accepted":
        return ApnsResult(200, "", None, badge, created_at)
    if name == "invalid_device":
        if answer.get("apns_status") == 400 and reason in _INVALID_DEVICE_REASONS:
            return ApnsResult(400, reason, None, badge, created_at)
        # The relay has revoked the route either way; with no APNs time, invalid as of now.
        invalidated = answer.get("invalidated_at", int(time.time() * 1000))
        return ApnsResult(410, "Unregistered", invalidated, badge, created_at)
    if name == "rate_limited" and answer.get("apns_status") == 429 and reason == "TooManyRequests":
        return ApnsResult(429, reason, None, badge, created_at)
    return ApnsResult(status, name, None, badge, created_at)


def _read_bounded(response: httpx.Response) -> bytes | None:
    data = bytearray()
    for chunk in response.iter_bytes():
        data += chunk
        if len(data) > MAX_RESPONSE_BYTES:
            return None
    return bytes(data)


def send(config: Config, device: dict, prepared, *, client: httpx.Client | None = None) -> ApnsResult:
    """Send one prepared push to the relay for this device; one request at most, never resent."""
    created_at = device.get("created_at")
    badge = prepared.badge
    route_id = device.get("relay_route_id")
    capability = None
    if _is_id(route_id):
        capability = devices.read_relay_capability(devices.relay_credentials_dir(config),
                                                   device.get("relay_credential_ref"))
    if capability is None:
        logger.warning("Push relay device %s has no usable route or credential; marking it for repair",
                       device.get("id"))
        return ApnsResult(0, devices.CREDENTIAL_MISSING, None, badge, created_at)
    body = _request_body(prepared)
    if body is None:
        return ApnsResult(0, NEEDS_ENVELOPE, None, badge, created_at)
    url = f"{config.push.relay_url}/v1/routes/{route_id}/send"
    headers = {"authorization": f"Bearer {capability}", "content-type": "application/json"}
    content = envelope.encode_json(body)
    try:
        with (client or _client()).stream(
            "POST", url, content=content, headers=headers,
            timeout=config.push.relay_timeout_seconds, follow_redirects=False,
        ) as response:
            status = response.status_code
            data = _read_bounded(response)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        logger.warning("Push relay unreachable for device %s: %s", device.get("id"), type(exc).__name__)
        return ApnsResult(0, UNREACHABLE, None, badge, created_at)
    except Exception as exc:
        # The request may have left; the relay may have handed it to APNs.
        logger.warning("Push relay send for device %s ended unknown: %s", device.get("id"), type(exc).__name__)
        return ApnsResult(0, UNKNOWN, None, badge, created_at)
    answer = None if data is None else _answer(status, data)
    if answer is None:
        logger.warning("Push relay answered device %s outside its schema (HTTP %s)", device.get("id"), status)
        return ApnsResult(0, UNKNOWN, None, badge, created_at)
    result = _result(status, answer, badge, created_at)
    if not result.success:
        logger.warning("Push relay device %s: %s %s", device.get("id"), result.status, result.reason)
    return result
