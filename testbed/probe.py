"""Reading the framework DB back, from wherever it happens to live.

Two access modes, because the two callers genuinely differ. A local path is
direct `sqlite3` and is what a host-side or bind-mounted deployment gets — the
in-process wire suite reads that way, with no container at all. The lean stack
keeps its DB on a named volume (so the upgrade tier can retain it across an
upgrade), and a named volume has no host path, so those rows come back through
`docker compose exec`.

The remote reader is `python -c`, not the `sqlite3` CLI. `sqlite3` is not
installed in the shipped image and adding it would mean changing the artifact in
order to test it; Python with the stdlib `sqlite3` module is guaranteed, because
the daemon is written in it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "/data/db/istota.db"
QUERY_TIMEOUT = 60
POLL_INTERVAL = 0.5

# Read-only, and via a URI so SQLite enforces it. The daemon is writing this
# file concurrently; a probe that took a write lock could stall the thing it is
# observing and turn an assertion into a timeout somewhere else entirely.
_REMOTE_READER = """
import json, sqlite3, sys
connection = sqlite3.connect("file:%s?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(sys.argv[1], json.loads(sys.argv[2])).fetchall()
print(json.dumps([dict(row) for row in rows]))
"""


@dataclass
class Probe:
    """Assertions against a framework DB, local or inside a container."""

    db_path: str = DEFAULT_DB_PATH
    local: Path | None = None
    compose_args: list[str] | None = None
    service: str = "istota"

    def __post_init__(self) -> None:
        if self.local is None and self.compose_args is None:
            raise ValueError("Probe needs either a local path or compose args")

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        params = params or []
        if self.local is not None:
            connection = sqlite3.connect(f"file:{self.local}?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                return [dict(row) for row in connection.execute(sql, params).fetchall()]
            finally:
                connection.close()

        result = subprocess.run(
            (self.compose_args or [])
            + [
                "exec",
                "-T",
                self.service,
                "python",
                "-c",
                _REMOTE_READER % self.db_path,
                sql,
                json.dumps(params),
            ],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"probe query failed ({result.returncode})\n{sql}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        return json.loads(result.stdout or "[]")

    def tasks(
        self,
        *,
        task_id: int | None = None,
        user_id: str | None = None,
        status: str | None = None,
        source_type: str | None = None,
    ) -> list[dict]:
        """Rows from `tasks`, narrowed by whichever filters are given.

        `task_id` exists because the others are not selective enough on a
        running daemon: the scheduler queues work of its own for the same user
        at startup (a feeds poll, a sleep cycle), so `user_id=` alone returns
        whichever task finished first — which is how the smoke tests first came
        back asserting against `source_type='scheduled'`.
        """
        clauses, params = [], []
        for column, value in (
            ("id", task_id),
            ("user_id", user_id),
            ("status", status),
            ("source_type", source_type),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(f"SELECT * FROM tasks{where} ORDER BY id", params)

    def task_logs(self, task_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY id", [task_id]
        )

    def wait_for_task(self, *, status: str, timeout: float = 60, **filters) -> dict:
        """Block until one task reaches `status`, and return it.

        The terminal statuses are watched alongside the requested one so that
        waiting for `completed` on a task that already failed returns
        immediately with the failure, rather than spending the whole timeout and
        then reporting "no task reached completed" — which says nothing about
        why. The caller's assertion on `status` is what still fails.

        `pending_confirmation` is in that set even though it is a *suspended*
        rather than a finished state: the task is parked waiting for a human and
        will not move on its own, so treating it as non-terminal reintroduces
        exactly the full-timeout-with-no-explanation this exists to prevent. The
        full list is in AGENTS.md under "Task Status".
        """
        terminal = {status, "completed", "failed", "cancelled", "pending_confirmation"}
        deadline = time.monotonic() + timeout
        seen: list[dict] = []
        while time.monotonic() < deadline:
            seen = self.tasks(**filters)
            for task in seen:
                if task.get("status") in terminal:
                    return task
            time.sleep(POLL_INTERVAL)

        raise TimeoutError(
            f"no task reached {status!r} within {timeout}s; "
            f"saw {[(t.get('id'), t.get('status')) for t in seen]}"
        )
