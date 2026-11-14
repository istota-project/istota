"""Public mobile discovery and bounded handoff parsing.

The auth router in app.py owns browser sessions, forms and continuations.
"""

import json
import re
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from istota.webui.origin import external_origin
from istota.webui.request_guards import BodyError, origin_key, read_bounded_body


router = APIRouter()


@router.get("/config")
async def mobile_config(request: Request) -> JSONResponse:
    config = getattr(request.app.state, "istota_config", None)
    try:
        _, scheme = external_origin(config)
    except ValueError:
        scheme = None
    if scheme != "https":
        return JSONResponse(
            {"error": "mobile_configuration_unavailable"}, status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    push = config.push
    # The web container need not hold the daemon's signing key. Discovery
    # reports configuration, without opening the key or contacting APNs.
    dispatching = push.dispatching
    direct_push = (
        dispatching and push.delivery == "direct"
        and bool(push.apns_key_path.strip()) and bool(push.apns_key_id.strip())
        and bool(push.apns_team_id.strip()) and bool(push.apns_topic.strip())
    )
    push_capabilities = {"delivery": "direct" if direct_push else "unavailable"}
    # A separate key, never a third `delivery` value: shells 0.12 to 0.14 refuse one.
    if direct_push and push.content == "private" and push.signing_key_path.strip():
        push_capabilities["private"] = {"versions": [1]}
    # The load normalized the URL and held it to private content and a key.
    # The app compares it, path included, with its compiled-in relay. Relay
    # delivery is private only: `private` is the envelope, `relay` the
    # transport. The web page enrolls privately only on `delivery == "direct"`,
    # and the 0.15 shell enrolls only when the page asks, so an older app does
    # not enroll on these keys.
    if dispatching and push.delivery == "relay":
        push_capabilities["private"] = {"versions": [1]}
        push_capabilities["relay"] = {"versions": [1], "url": push.relay_url}
    # The only config string serialized is the relay URL, bounded at load, so
    # every branch stays well within the client's 64 KiB discovery limit.
    return JSONResponse({
        "product": "istota",
        "mobile_protocol": 1,
        "minimum_app_version": "0.12.0",
        "web_base_path": "/istota/",
        "auth": {"mode": "browser_handoff", "version": 1},
        "location_ingest_path": "/webhooks/location" if config.location.enabled else None,
        "push": push_capabilities,
    }, headers={"Cache-Control": "no-store"})


def _unique_fields(pairs):
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError("duplicate field")
        fields[name] = value
    return fields


async def read_handoff_fields(request: Request, config, *, json_body: bool, names: set[str]) -> dict:
    """Admit headers before reading a bounded, strictly decoded native proof."""
    expected = "application/json" if json_body else "application/x-www-form-urlencoded"
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].lower() not in (expected, expected + "; charset=utf-8"):
        raise BodyError(415, "unsupported content type")
    origins = request.headers.getlist("origin")
    try:
        host, scheme = external_origin(config)
        if origins:
            origin = urlsplit(origins[0])
            seen = origin_key(origins[0])
            if (len(origins) != 1 or origin.username is not None or origin.password is not None
                    or origin.path or origin.query or origin.fragment
                    or seen is None or seen != origin_key(f"{scheme}://{host}")):
                raise ValueError("origin mismatch")
    except ValueError:
        raise BodyError(403, "origin refused") from None
    raw = await read_bounded_body(request, 4096, require_length=False)
    try:
        text = raw.decode("utf-8", errors="strict")
        if json_body:
            fields = json.loads(text, object_pairs_hook=_unique_fields)
        else:
            if re.search(r"%(?![0-9a-fA-F]{2})", text):
                raise ValueError("invalid escape")
            fields = _unique_fields(parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                                             encoding="utf-8", errors="strict", max_num_fields=len(names)))
        if not isinstance(fields, dict) or set(fields) != names:
            raise ValueError("invalid fields")
        if any(not isinstance(value, str) for value in fields.values()):
            raise ValueError("invalid value")
        return fields
    except (ValueError, UnicodeError, RecursionError):
        raise BodyError(400, "invalid handoff request") from None
