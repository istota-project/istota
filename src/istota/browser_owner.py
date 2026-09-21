"""Caller hints for browser capacity scheduling; these do not authenticate requests."""

import json
import os


def with_browser_owner(payload: dict) -> dict:
    """Attach a task's identity; callers without a task remain anonymous."""
    user_id = os.environ.get("ISTOTA_USER_ID")
    task_id = os.environ.get("ISTOTA_TASK_ID")
    if user_id and task_id:
        return {**payload, "owner": json.dumps([user_id, task_id], separators=(",", ":"))}
    return payload
