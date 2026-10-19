"""Public discovery for the generic iOS shell.

Only fixed protocol values and deployment capability flags leave this route.
Browser authentication and the session receipt are separate protocol steps.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from istota.webui.origin import external_origin


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
    direct_push = (
        push.enabled and push.dispatch_interval_seconds > 0
        and bool(push.apns_key_path.strip()) and bool(push.apns_key_id.strip())
        and bool(push.apns_team_id.strip()) and bool(push.apns_topic.strip())
    )
    # No config strings are serialized: both capability branches stay below
    # 512 bytes, well within the client's 64 KiB discovery limit.
    return JSONResponse({
        "product": "istota",
        "mobile_protocol": 1,
        "minimum_app_version": "0.12.0",
        "web_base_path": "/istota/",
        "auth": {"mode": "browser_handoff", "version": 1},
        "location_ingest_path": "/webhooks/location" if config.location.enabled else None,
        "push": {"delivery": "direct" if direct_push else "unavailable"},
    }, headers={"Cache-Control": "no-store"})
