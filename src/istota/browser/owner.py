"""Browser profile identity and task hints for capacity scheduling."""

import json
import os
from uuid import uuid4

BRIEFINGS_IDENTITY = "__briefings__"


def require_briefings_profile(api_url: str) -> None:
    """An older browser must refuse research before opening or reading a tab."""
    from istota.browser.admission import browser_call

    response = browser_call("get", f"{api_url}/health", timeout=10.0,
                            headers=browser_headers(BRIEFINGS_IDENTITY))
    if response.status_code != 200 or response.json().get("briefing_task_isolation") is not True:
        raise ValueError("briefing_browser_isolation_unavailable")


def with_browser_owner(payload: dict) -> dict:
    """Attach a task's identity; callers without a task remain anonymous."""
    user_id = os.environ.get("ISTOTA_USER_ID")
    task_id = os.environ.get("ISTOTA_TASK_ID")
    if user_id and task_id:
        return {**payload, "owner": json.dumps([user_id, task_id], separators=(",", ":"))}
    return payload


def browser_headers(user_id: str | None = None) -> dict[str, str]:
    """Carry the original principal; refuse identities HTTP cannot represent.

    A skill CLI takes the principal from its task's environment. The daemon
    has no task environment, so a daemon-side caller such as a briefing
    source passes the user it is acting for.
    """
    daemon_call = user_id is not None
    if user_id is None:
        user_id = os.environ.get("ISTOTA_USER_ID")
    if not user_id:
        raise ValueError("ISTOTA_USER_ID is required for browser requests")
    # Raw header values are ASCII. Do not strip or encode a principal into a
    # different one; the container uses this exact value as its profile id.
    if (not user_id.isascii() or user_id != user_id.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in user_id)):
        raise ValueError("ISTOTA_USER_ID cannot be represented safely as an HTTP header")
    headers = {"X-Istota-User": user_id}
    if user_id == BRIEFINGS_IDENTITY:
        owner = str(uuid4()) if daemon_call else os.environ.get("ISTOTA_TASK_ID")
        if not owner:
            raise ValueError("Briefings browser requires a task owner")
        headers["X-Istota-Owner"] = owner
    return headers
