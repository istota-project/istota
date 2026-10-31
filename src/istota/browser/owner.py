"""Browser profile identity and task hints for capacity scheduling."""

import json
import os
from uuid import uuid4

#: The task identity research runs under. Never a browser profile: it maps to
#: `ISOLATED_PROFILE` on the way out.
BRIEFINGS_IDENTITY = "__briefings__"
#: One profile for every browse use that should hold no user data, reused across
#: tasks and users. `docker/browser/browse_api.py` writes the name too, and
#: `tests/test_browser_profile.py` holds the two equal.
ISOLATED_PROFILE = "__isolated__"
#: Which profile a skill CLI's requests name, chosen from `Reach.isolated_browser`
#: and set by the skill proxy, so a task cannot choose it.
BROWSER_PROFILE_VAR = "ISTOTA_BROWSER_PROFILE"


def browser_profile() -> str | None:
    """The profile this process acts in. A CLI with no skill proxy (a CRON
    `skill:` job) has no variable, and its user's profile is the one it had."""
    profile = os.environ.get(BROWSER_PROFILE_VAR)
    if profile:
        return profile
    user_id = os.environ.get("ISTOTA_USER_ID")
    return ISOLATED_PROFILE if user_id == BRIEFINGS_IDENTITY else user_id


def require_isolated_profile(api_url: str) -> None:
    """An older browser must refuse before opening or reading a tab."""
    from istota.browser.admission import browser_call

    response = browser_call("get", f"{api_url}/health", timeout=10.0,
                            headers=browser_headers(ISOLATED_PROFILE))
    if response.status_code != 200 or response.json().get("isolated_browser_profile") is not True:
        raise ValueError("isolated_browser_unavailable")


def with_browser_owner(payload: dict) -> dict:
    """Attach a task's identity; callers without a task remain anonymous."""
    profile = browser_profile()
    task_id = os.environ.get("ISTOTA_TASK_ID")
    if profile and task_id:
        return {**payload, "owner": json.dumps([profile, task_id], separators=(",", ":"))}
    return payload


def browser_headers(user_id: str | None = None) -> dict[str, str]:
    """Carry the profile to act in; refuse identities HTTP cannot represent.

    A skill CLI takes it from its task's environment. The daemon has no task
    environment, so a daemon-side caller such as a briefing source passes it.
    """
    daemon_call = user_id is not None
    if user_id is None:
        user_id = browser_profile()
    if not user_id:
        raise ValueError("ISTOTA_USER_ID is required for browser requests")
    # Raw header values are ASCII. Do not strip or encode a principal into a
    # different one; the container uses this exact value as its profile id.
    if (not user_id.isascii() or user_id != user_id.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in user_id)):
        raise ValueError("ISTOTA_USER_ID cannot be represented safely as an HTTP header")
    headers = {"X-Istota-User": user_id}
    if user_id == ISOLATED_PROFILE:
        owner = str(uuid4()) if daemon_call else os.environ.get("ISTOTA_TASK_ID")
        if not owner:
            raise ValueError("The isolated browser profile requires a task owner")
        headers["X-Istota-Owner"] = owner
    return headers
