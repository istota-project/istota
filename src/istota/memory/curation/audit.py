"""Audit log for op-based USER.md curation.

Sidecar `USER.md.audit.jsonl` next to USER.md. Append-only JSONL with one entry
per write event:
  - nightly curator run (one entry batches all that night's ops)
  - runtime CLI invocation (one entry per CLI call, typically one op)
  - synthetic `legacy` entry when the bypass detector notices an unexplained
    USER.md mtime/size change

Each entry carries:
  - `source`: "nightly" | "runtime" | "cli" | "legacy"
  - `entry_kind`: "batch" (default), "lint_candidate", "aborted", "legacy_detected"

A second sidecar, `USER.md.last_seen.json`, stores the file's last-known
size + sha256 so the nightly curator can detect writes that bypassed the
ops engine.

No rotation in v1.
"""

from __future__ import annotations

import hashlib
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


def get_user_md_last_seen_path(config: "Config", user_id: str) -> Path:
    user_md = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    return user_md.parent / "USER.md.last_seen.json"


def write_audit_log(
    config: "Config",
    user_id: str,
    applied: list[dict],
    rejected: list[dict],
    user_md_size_bytes: int | None = None,
    source: str = "nightly",
    entry_kind: str = "batch",
    extra: dict | None = None,
) -> None:
    """Append a single JSONL entry. No-op when both lists are empty AND
    no `extra` payload is supplied.

    `user_md_size_bytes`, when provided, records USER.md size at the time of
    the curation run so growth curves are inspectable from the audit alone.

    `source` distinguishes "nightly" curator entries from "runtime" CLI
    entries from operator "cli" entries from synthetic "legacy" entries.

    `entry_kind` is "batch" by default; lint Phase A logging uses
    "lint_candidate", aborted curator runs use "aborted", and bypass
    detection uses "legacy_detected".

    `extra` is merged into the entry as additional top-level keys
    (e.g. `lint_candidates`, `aborted_reason`, `legacy_signal`).
    """
    if not applied and not rejected and not extra:
        return

    path = get_curation_audit_path(config, user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": user_id,
            "source": source,
            "entry_kind": entry_kind,
            "applied": applied,
            "rejected": rejected,
        }
        if user_md_size_bytes is not None:
            entry["user_md_size_bytes"] = user_md_size_bytes
        if extra:
            for k, v in extra.items():
                if k not in entry:
                    entry[k] = v
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("Failed to write curation audit log for %s: %s", user_id, e)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_last_seen(config: "Config", user_id: str) -> dict | None:
    """Return the last-known USER.md fingerprint, or None on first sight /
    on any read error. Never raises."""
    path = get_user_md_last_seen_path(config, user_id)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_last_seen(
    config: "Config", user_id: str, *, size_bytes: int, sha256: str
) -> None:
    """Update the USER.md.last_seen.json sidecar. Best-effort; never raises."""
    path = get_user_md_last_seen_path(config, user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                }
            )
        )
    except OSError as e:
        logger.warning("Failed to write USER.md.last_seen for %s: %s", user_id, e)


def detect_bypass_write(
    config: "Config", user_id: str, current_text: str
) -> dict | None:
    """Detect whether USER.md changed since last seen WITHOUT a recorded
    audit entry. Returns the bypass-signal dict if a bypass is suspected,
    else None.

    Caller responsibilities:
      - Pass the current contents of USER.md (already read once).
      - On a positive return, write a synthetic legacy entry via
        `write_audit_log(..., source="legacy", entry_kind="legacy_detected", extra=signal)`.
      - Always call `write_last_seen()` afterwards to update the fingerprint.

    Note: this function returns the signal but does NOT consult the
    audit log itself. The nightly run should only call this on its first
    pass through a user — after that, runtime writes will have updated
    the last_seen sidecar via `write_last_seen()` themselves.
    """
    last = read_last_seen(config, user_id)
    if last is None:
        return None  # First sight; baseline only.
    current_sha = _hash_text(current_text)
    current_size = len(current_text.encode("utf-8"))
    if last.get("sha256") == current_sha:
        return None
    return {
        "previous_size_bytes": last.get("size_bytes"),
        "previous_sha256": last.get("sha256"),
        "previous_ts": last.get("ts"),
        "current_size_bytes": current_size,
        "current_sha256": current_sha,
    }
