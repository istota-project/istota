"""A limited assignment wake, run by the shipped scheduler in the shipped image.

`tests/test_assignment_skill.py` drives the CLI in-process and sets the withheld
scopes by hand. Only here does the scheduler create the wake, the executor work
out its reach, and the live skill proxy put that set in the CLI's environment
(ISSUE-914).

The wake has to be limited for this to mean anything, so the same call that uses
the assignment verbs also asks for an `external` skill and requires the proxy's
refusal. The verbs answering would prove nothing on a wake that had lost nothing.

Recorded with `_gate` reading `ctx["withheld"]` again: `show` and `next` answer
`not_a_user_turn`, while `external` still answers `egress_not_approved`.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from testbed.stack import CONTAINER_CONFIG

pytestmark = pytest.mark.smoke

USER = "testuser"
SLUG = "smoke-limited-wake"
#: The closing line of every wake's user half, in no other task's request.
ROUTE = "Carry this assignment forward"
EXTERNAL_SKILL = "wordpress"

SEED = f"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from istota import db
from istota.assignments import charter, store
from istota.config import load_config

config = load_config(Path({CONTAINER_CONFIG!r}))
due = datetime.now(timezone.utc) - timedelta(minutes=1)
with db.get_db(config.db_path) as conn:
    assignment_id = store.insert_assignment(
        conn, user_id={USER!r}, slug={SLUG!r}, title="Smoke wake", goal="Answer a wake.",
        budget={{"max_cost_usd": 5.0, "max_wakes": 5}}, dir_relpath=charter.dir_relpath({SLUG!r}),
    )
    store.set_state(conn, assignment_id, "sleeping")
    store.update_assignment(conn, assignment_id, next_wake_at=due.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert charter.create_directory(config, conn, store.get_assignment(conn, assignment_id))
    conn.commit()
"""

PROBE = f"""
ask() {{ istota-skill "$@" 2>/dev/null | tr '\\n' ' ' | cut -c1-400; }}
echo "show=$(ask assignment show {SLUG})"
echo "next=$(ask assignment next --wait user --progress 'smoke progress')"
echo "external=$(ask {EXTERNAL_SKILL} --help)"
"""


def _status(raw: str) -> str | None:
    found = re.search(r'"status":\s*"(\w+)"', raw)
    return found[1] if found else None


@pytest.mark.profile("base")
def test_a_limited_wake_uses_its_verbs_and_not_its_external_skills(stack):
    stack.script([{"when": ROUTE, "turns": [
        {"tool_calls": [{"id": "wake-probe", "name": "Bash", "arguments": {"command": PROBE}}]},
        {"text": "wake done"},
    ]}])
    mark = stack.probe.query("SELECT COALESCE(MAX(id), 0) AS id FROM tasks")[0]["id"]
    seeded = stack.exec(["uv", "run", "python", "-c", SEED], timeout=120)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr

    deadline = time.monotonic() + 240
    task = None
    while time.monotonic() < deadline and task is None:
        rows = stack.probe.query(
            "SELECT * FROM tasks WHERE id > ? AND source_type = 'assignment' "
            "AND status IN ('completed', 'failed') ORDER BY id LIMIT 1", [mark],
        )
        task = rows[0] if rows else None
        time.sleep(2)
    assert task is not None, f"the wake never finished\n{stack.logs(120)}"
    assert task["status"] == "completed", stack.diagnostics(task)

    output = stack.endpoint.tool_results_by_id().get("wake-probe")
    assert output is not None, stack.diagnostics(task)
    facts = dict(re.findall(r"^(\w+)=(.*)$", output, re.M))
    report = json.dumps(facts, indent=1)

    assert "egress_not_approved" in facts["external"], report
    assert _status(facts["show"]) == "ok", report
    assert _status(facts["next"]) == "ok", report

    # The worker finishes the wake after it commits the task's status.
    deadline = time.monotonic() + 60
    state = "active"
    while time.monotonic() < deadline and state == "active":
        state = stack.probe.query("SELECT state FROM assignments WHERE slug = ?", [SLUG])[0]["state"]
        time.sleep(1)
    assert state == "waiting", stack.diagnostics(task)
