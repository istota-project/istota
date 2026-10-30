"""A CRON.md `command:` job, run by the shipped scheduler in the shipped image.

ISSUE-886. CRON.md is written by the model, so this file is what stands between
an injected admin task and host command execution. `tests/test_confined_command.py`
reads argv and, on the linux tier, runs the namespace; only here is `istota-skill`
the image's own, so only here does a job's route to the databases get exercised.

The job's output is the witness, and "it ran" says nothing about where. `fstype`
is what a skipped sandbox cannot produce: unconfined, `/data/db` is on the `data`
volume and holds the framework database; confined it is an empty tmpfs. The
control outside the sandbox requires the opposite, so a mask the image gained
some other way would turn this red.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.smoke

CONTAINER_DB_DIR = "/data/db"

JOB_NAME = "confined-probe"

PROBE = (
    f'echo "fstype=$(stat -f -c %T {CONTAINER_DB_DIR} 2>&1)"; '
    f'test -e {CONTAINER_DB_DIR}/istota.db && echo db=present || echo db=absent; '
    "istota-skill kv namespaces 2>&1 && echo skill=ran || echo skill=refused"
)


def _write_cron(stack, command: str) -> str:
    """Install a one-job CRON.md, returning the path it went to."""
    config_dir = "/data/workspace/Users/testuser/istota/config"
    stack.exec(["mkdir", "-p", config_dir])
    body = (
        "```toml\n"
        "[[jobs]]\n"
        f'name = "{JOB_NAME}"\n'
        'cron = "* * * * *"\n'
        f"command = '{command}'\n"
        "```\n"
    )
    path = f"{config_dir}/CRON.md"
    stack.exec(["sh", "-c", f"cat > {path} <<'EOF'\n{body}EOF\n"])
    return path


def _wait_for_job_result(stack, id_above: int, timeout: float = 150) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = stack.probe.query(
            "SELECT id, status, result, error FROM tasks "
            # `error` as well as the status: a failed attempt goes back to
            # `pending` for the retry ladder, a wait this test would lose.
            "WHERE command IS NOT NULL AND id > ? "
            "AND (status IN ('completed', 'failed') OR error IS NOT NULL) "
            "ORDER BY id DESC LIMIT 1",
            [id_above],
        )
        if rows:
            return rows[0]
        time.sleep(2)
    raise AssertionError(
        f"no command task finished within {timeout}s\n"
        f"--- daemon logs ---\n{stack.logs(120)}"
    )


@pytest.mark.profile("base")
def test_a_command_job_runs_in_the_sandbox_and_reaches_its_skills(stack):
    control = stack.exec(["sh", "-c", f"test -e {CONTAINER_DB_DIR}/istota.db && echo present"])
    assert control.stdout.strip() == "present", "the control saw no database to mask"

    mark = stack.probe.query("SELECT COALESCE(MAX(id), 0) AS id FROM tasks")[0]["id"]
    path = _write_cron(stack, PROBE)
    try:
        task = _wait_for_job_result(stack, mark)
    finally:
        stack.exec(["sh", "-c", f"rm -f {path}"])

    assert task["status"] == "completed", f"{task}\n{stack.logs(120)}"
    output = task["result"]
    assert "fstype=tmpfs" in output, output
    assert "db=absent" in output, output
    assert "skill=ran" in output, output
