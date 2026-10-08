"""Credential activity metadata; callers own the transaction and exclude values."""

import json

ACTIONS = ("import", "export", "backup_export", "backup_recipient_set", "backup_recipient_cleared",
           "reveal", "restore", "history_purge", "retire", "delete", "capture",
           "recovery_fill", "recovery_fill_approved", "migration", "step_up_locked")


def record(conn, user_id: str, *, action: str, actor: str, name: str | None = None,
           detail: dict | None = None) -> None:
    if action not in ACTIONS:
        raise ValueError("unknown credential audit action")
    if detail is not None:
        if not isinstance(detail, dict) or any(
            not isinstance(k, str) or type(v) not in (str, int, bool, type(None))
            for k, v in detail.items()
        ):
            raise ValueError("audit detail must be a flat object")
        detail = {k: v[:200] if isinstance(v, str) else v for k, v in detail.items()}
    conn.execute("INSERT INTO credential_audit (user_id, name, action, actor, detail_json) VALUES (?, ?, ?, ?, ?)",
                 (user_id, name, action, actor, json.dumps(detail) if detail is not None else None))


def recent(conn, user_id: str, *, limit: int = 100) -> list[dict]:
    rows = conn.execute("SELECT id, name, action, actor, detail_json, at FROM credential_audit "
                        "WHERE user_id=? ORDER BY at DESC, id DESC LIMIT ?", (user_id, max(0, min(limit, 100)))).fetchall()
    return [{"id": r[0], "name": r[1], "action": r[2], "actor": r[3],
             "detail": json.loads(r[4]) if r[4] else None, "at": r[5]} for r in rows]


def prune(conn, *, older_than_days: int) -> int:
    if older_than_days <= 0:
        raise ValueError("retention must be positive")
    return conn.execute("DELETE FROM credential_audit WHERE at < datetime('now', ?)",
                        (f"-{older_than_days} days",)).rowcount
