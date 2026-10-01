"""The discovery cache: what `describe` learned about one site, for an hour.

Kept in the reserved KV namespace ``_wordpress``, so the model can neither read
nor overwrite it through `istota-skill kv` (`kv_namespaces.py`), and written with
`db.kv_set` directly as the `briefing` skill does: a `kv` CLI write is deferred
until the task succeeds and would not be visible to the next call in the same
task.

Keys are scoped by site name, blog and a digest of the base URL, so pointing a
site's vault entry at another address starts a fresh cache rather than serving
the old site's types for the rest of the hour.

A cache is an optimisation, so it never fails a call: an absent database, a
busy lock or a malformed row reads as a miss and is logged at debug.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from istota import db

log = logging.getLogger(__name__)

NAMESPACE = "_wordpress"
TTL_SECONDS = 3600
_BUSY_TIMEOUT_MS = 2000


class Cache:
    def __init__(self, db_path, user_id: str, *, site: str, blog: str | None, base: str,
                 ttl: int = TTL_SECONDS, now=time.time) -> None:
        self._db_path = Path(db_path) if db_path else None
        self._user_id = user_id
        digest = hashlib.sha256(base.encode()).hexdigest()[:12]
        self.scope = f"{site}|{blog or '-'}|{digest}|"
        self._ttl = ttl
        self._now = now

    def _usable(self) -> bool:
        return bool(self._user_id) and db.database_present(self._db_path)

    def get(self, key: str):
        if not self._usable():
            return None
        try:
            with db.get_db(self._db_path, busy_timeout_ms=_BUSY_TIMEOUT_MS) as conn:
                row = db.kv_get(conn, self._user_id, NAMESPACE, self.scope + key)
            if row is None:
                return None
            stored = json.loads(row["value"])
            if self._now() - float(stored["at"]) > self._ttl:
                return None
            return stored["data"]
        except Exception as exc:  # noqa: BLE001 — a cache miss, never a failure
            log.debug("wordpress cache read failed: %s", type(exc).__name__)
            return None

    def put(self, key: str, data) -> None:
        if not self._usable():
            return
        try:
            value = json.dumps({"at": self._now(), "data": data})
            with db.get_db(self._db_path, busy_timeout_ms=_BUSY_TIMEOUT_MS) as conn:
                db.kv_set(conn, self._user_id, NAMESPACE, self.scope + key, value)
        except Exception as exc:  # noqa: BLE001
            log.debug("wordpress cache write failed: %s", type(exc).__name__)

    def drop(self) -> None:
        """Forget everything cached for this site and blog."""
        if not self._usable():
            return
        try:
            with db.get_db(self._db_path, busy_timeout_ms=_BUSY_TIMEOUT_MS) as conn:
                for row in db.kv_list(conn, self._user_id, NAMESPACE):
                    if row["key"].startswith(self.scope):
                        db.kv_delete(conn, self._user_id, NAMESPACE, row["key"])
        except Exception as exc:  # noqa: BLE001
            log.debug("wordpress cache drop failed: %s", type(exc).__name__)
