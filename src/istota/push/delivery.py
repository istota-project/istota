"""The send boundary: what one device is sent for one logical alert, and the send.

The dispatcher and `istota push test` describe an alert once, as a
`LogicalPush`; `build_delivery` turns it into the exact APNs body for one
device in that device's content mode, and `send_prepared` is the only caller of
`apns.send`. The three modes:

- `legacy`: today's plaintext payload, from the helpers below, unchanged.
- `generic`: a private deployment and a device without an encrypted
  enrollment, or one enrolled under an older signing key. A fixed alert.
- `encrypted`: `envelope.seal`, the text and routing inside the envelope.

A private deployment never falls back to plaintext. Nothing here logs content.
"""

import copy
import json
import unicodedata
from dataclasses import dataclass

from istota.config import Config
from istota.push import apns, devices, envelope

_ALERT_TITLE_MAX = 80
_ALERT_BODY_MAX = 180
_MAX_PAYLOAD_BYTES = 4096


class DeliveryError(Exception):
    """A private payload that could not be built; `reason` is what the row records."""

    def __init__(self, reason: str, cause: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


@dataclass(frozen=True)
class LogicalPush:
    """One alert or badge update, before any device's content mode applies.

    `thread` is the legacy `thread-id`; `group` is the logical collapse group
    (`room:<token>`, or None for an alert that does not collapse). Text is raw:
    each mode sanitizes and caps it.
    """
    kind: str
    badge: int | None
    title: str = ""
    body: str = ""
    thread: str | None = None
    group: str | None = None
    target: dict | None = None


@dataclass(frozen=True)
class PreparedPush:
    payload: bytes
    collapse_id: str | None
    priority: int
    kind: str
    badge: int | None
    event_id: str | None


def alert(title: str, body: str, badge: int | None, *, thread: str, group: str | None,
          target: dict | None) -> LogicalPush:
    return LogicalPush("alert", badge, title, body, thread, group, target)


def badge_only(badge: int) -> LogicalPush:
    return LogicalPush("badge", badge)


# --- Legacy payload helpers -----------------------------------------------------------


def _sanitize(text: str, limit: int) -> str:
    # Keep whitespace as separators before removing other control characters.
    clean = "".join(" " if char.isspace() else char for char in text
                    if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    return " ".join(clean.split())[:limit]


def _fit_payload(payload: dict) -> dict:
    """Keep routing data intact; refuse an envelope that cannot fit alone."""
    def size():
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    alert = payload["aps"]["alert"]
    if size() > _MAX_PAYLOAD_BYTES:
        alert["body"] = ""
    while size() > _MAX_PAYLOAD_BYTES and alert["title"]:
        alert["title"] = alert["title"][:-1]
    if size() > _MAX_PAYLOAD_BYTES:
        raise ValueError("Push routing envelope exceeds APNs limit")
    return payload


def _alert_payload(title: str, body: str, badge: int, thread: str, target: dict) -> dict:
    return _fit_payload({
        "aps": {"alert": {"title": _sanitize(title, _ALERT_TITLE_MAX),
                          "body": _sanitize(body, _ALERT_BODY_MAX)},
                "badge": badge, "sound": "default", "thread-id": thread,
                "interruption-level": "active"},
        "istota": target,
    })


def _for_device(payload: dict, device: dict) -> dict:
    """Name the enrolling connection in a generic-shell registration's routing.

    The app refuses a tap whose binding is not its current connection, since a
    room ID alone is not unique across servers. Legacy rows get the payload as is.
    """
    binding = device.get("connection_binding")
    if binding is None or "istota" not in payload:
        return payload
    routed = copy.deepcopy(payload)
    routed["istota"]["connection_binding"] = binding
    routed["istota"]["connection_generation"] = device["connection_generation"]
    return _fit_payload(routed)


def legacy_payload(logical: LogicalPush, device: dict) -> dict:
    """The plaintext payload a legacy installation sends this device."""
    if logical.kind == "badge":
        return {"aps": {"badge": logical.badge}}
    payload = _alert_payload(logical.title, logical.body, logical.badge, logical.thread, logical.target)
    return _for_device(payload, device)


def _serialize(payload: dict) -> bytes:
    # What apns.send produced from a dict before the seam; the snapshot holds it.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


# --- Content modes -----------------------------------------------------------------------


def effective_content(config: Config, device: dict, sender_key_id: str | None) -> str:
    """`legacy`, `generic` or `encrypted`, by the rule the device list shows."""
    content, _stale = devices.device_content(config.push.content, device, sender_key_id)
    return content


def _generic_payload(config: Config, badge: int | None) -> bytes:
    aps = {"alert": {"title": _sanitize(config.bot_name, _ALERT_TITLE_MAX), "body": envelope.GENERIC_BODY},
           "sound": "default"}
    if badge is not None:
        aps["badge"] = badge
    return _serialize({"aps": aps})


def _sealed(config: Config, device: dict, logical: LogicalPush, now: float, signing_key) -> PreparedPush:
    if logical.target is None:
        target = dict(envelope.INBOX_TARGET)
    else:
        # The routing object exactly as `_for_device` extends it for a bound legacy row.
        target = copy.deepcopy(logical.target)
        target["connection_binding"] = device["connection_binding"]
        target["connection_generation"] = device["connection_generation"]
    mid = envelope.new_event_id()
    try:
        device_public_key = envelope.b64url_decode(device["encryption_public_key"])
        payload, collapse = envelope.seal(
            signing_key=signing_key, device_public_key=device_public_key,
            binding=device["connection_binding"], generation=device["push_generation"], now=now,
            alert_title=_sanitize(config.bot_name, _ALERT_TITLE_MAX),
            title=_sanitize(logical.title, _ALERT_TITLE_MAX), body=_sanitize(logical.body, _ALERT_BODY_MAX),
            badge=None if logical.badge is None else min(logical.badge, envelope.MAX_BADGE),
            logical_group=logical.group, target=target, mid=mid,
        )
    except envelope.PayloadTooLarge as error:
        raise DeliveryError("payload_too_large", type(error).__name__) from error
    except (ValueError, TypeError, KeyError) as error:
        # EnvelopeError is a ValueError; a stored key that is not canonical base64url lands here too.
        raise DeliveryError("private_payload", type(error).__name__) from error
    return PreparedPush(payload, collapse, 10, "alert", logical.badge, mid)


def build_delivery(config: Config, device: dict, logical: LogicalPush, *, now: float,
                   signing_key=None) -> PreparedPush:
    """The exact APNs body for one device.

    A legacy envelope that cannot fit raises ValueError, as `_fit_payload`
    always has. A private payload that cannot be built raises DeliveryError.
    A private deployment without its signing key sends every device the
    generic alert, which needs no key and carries no text.
    """
    if logical.kind == "badge":
        return PreparedPush(_serialize({"aps": {"badge": logical.badge}}), "badge", 5, "badge", logical.badge, None)
    if config.push.content != "private":
        return PreparedPush(_serialize(legacy_payload(logical, device)), logical.group, 10, "alert",
                            logical.badge, None)
    sender_key_id = envelope.key_id(signing_key.public_key().public_bytes_raw()) if signing_key else None
    if signing_key is not None and effective_content(config, device, sender_key_id) == "encrypted":
        return _sealed(config, device, logical, now, signing_key)
    return PreparedPush(_generic_payload(config, logical.badge), None, 10, "alert", logical.badge, None)


def send_prepared(config: Config, device: dict, prepared: PreparedPush) -> apns.ApnsResult:
    """Send the measured bytes; the one place anything calls `apns.send`."""
    return apns.send(config.push, device, prepared.payload, collapse_id=prepared.collapse_id,
                     priority=prepared.priority, expiration=0)
