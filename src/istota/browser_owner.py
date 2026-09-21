"""Browser profile identity and task hints for capacity scheduling."""

import json
import os


def with_browser_owner(payload: dict) -> dict:
    """Attach a task's identity; callers without a task remain anonymous."""
    user_id = os.environ.get("ISTOTA_USER_ID")
    task_id = os.environ.get("ISTOTA_TASK_ID")
    if user_id and task_id:
        return {**payload, "owner": json.dumps([user_id, task_id], separators=(",", ":"))}
    return payload


def browser_headers() -> dict[str, str]:
    """Carry the original principal; refuse identities HTTP cannot represent."""
    user_id = os.environ.get("ISTOTA_USER_ID")
    if not user_id:
        raise ValueError("ISTOTA_USER_ID is required for browser requests")
    # Raw header values are ASCII. Do not strip or encode a principal into a
    # different one; the container uses this exact value as its profile id.
    if (not user_id.isascii() or user_id != user_id.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in user_id)):
        raise ValueError("ISTOTA_USER_ID cannot be represented safely as an HTTP header")
    return {"X-Istota-User": user_id}
