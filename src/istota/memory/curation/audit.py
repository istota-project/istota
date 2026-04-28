"""Audit log for op-based USER.md curation.

Sidecar `USER.md.audit.jsonl` next to USER.md. Append-only JSONL with one entry
per night that produced ops. No rotation in v1.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ...storage import _get_mount_path, get_user_memory_path

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger("istota.memory.curation.audit")


def get_curation_audit_path(config: "Config", user_id: str) -> Path:
    user_md = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    return user_md.parent / "USER.md.audit.jsonl"


def write_audit_log(
    config: "Config",
    user_id: str,
    applied: list[dict],
    rejected: list[dict],
    user_md_size_bytes: int | None = None,
) -> None:
    """Append a single JSONL entry. No-op when both lists are empty.

    `user_md_size_bytes`, when provided, records USER.md size at the time of
    the curation run so growth curves are inspectable from the audit alone.
    """
    if not applied and not rejected:
        return

    path = get_curation_audit_path(config, user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": user_id,
            "applied": applied,
            "rejected": rejected,
        }
        if user_md_size_bytes is not None:
            entry["user_md_size_bytes"] = user_md_size_bytes
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("Failed to write curation audit log for %s: %s", user_id, e)
