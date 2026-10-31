"""An approved public job, run by the shipped scheduler in the shipped image.

`tests/test_egress_reach.py` computes a public job's reach, and the default suite
checks argv. Nothing there runs one: the approval through the operator's CLI, the
posture written on the row, the namespace it gets, the live skill proxy, the
deferred drain and a subtask's inheritance.

The job starts disabled, so it is approved before any occurrence exists, and is
then enabled with the same text, since a schedule edit keeps an approval.

Every absence has a witness that it was there to be absent. An attended turn runs
the same probe first and requires the opposite answers, so a missing file is the
posture's doing and not a stack that never had it. The sentinels are in the files
and never in the probe's text, so an echoed command cannot answer for them.

Recorded with `_public_withheld` answering nothing: the job's probe finds the
user's files. With `_public_job_kept_suffixes` answering None: the job's deferred
KV write is applied.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from testbed.stack import CONTAINER_CONFIG

pytestmark = pytest.mark.smoke

USER = "testuser"
USER_DIR = f"/data/workspace/Users/{USER}"
CONFIG_DIR = f"{USER_DIR}/istota/config"
CRON_PATH = f"{CONFIG_DIR}/CRON.md"
JOB = "public-probe"
SUBJECT = f"cron:{JOB}"
MARKER = "[e2e:public-job]"
SUBTASK_MARKER = "[e2e:public-subtask]"
MEMORY_WORD = "SENTINEL" + "-USERMD"
KV_KEY = "public-job-deferred"
CONTROL_KV_KEY = "public-job-control"
REFUSED_ON_THE_ISOLATED_PROFILE = "isolated_browser_operation_refused"

SEED = f"""
set -e
mkdir -p {CONFIG_DIR}
echo '{MEMORY_WORD} from the notes' > {CONFIG_DIR}/USER.md
echo 'ordinary file' > {USER_DIR}/public-probe-notes.txt
"""

PROBE = f"""
present() {{ if [ -e "$1" ]; then echo yes; else echo no; fi; }}
echo "user_dir=$(present {USER_DIR})"
echo "notes=$(present {USER_DIR}/public-probe-notes.txt)"
echo "user_md=$(present {CONFIG_DIR}/USER.md)"
echo "email=$(istota-skill email accounts 2>&1 | tr '\\n' ' ' | cut -c1-300)"
echo "kv=$(istota-skill kv namespaces 2>&1 | tr '\\n' ' ' | cut -c1-300)"
echo "skills=$(istota-skill skills show browse 2>&1 | head -c 40 | tr '\\n' ' ')"
echo "browse_state=$(istota-skill browse state --help 2>&1 | tr '\\n' ' ' | cut -c1-300)"
"""

def _kv_op(key: str) -> str:
    """A deferred KV write, which the control's drain must apply and a public job's drop."""
    return f"""
cat > "$ISTOTA_DEFERRED_DIR/task_${{ISTOTA_TASK_ID}}_kv_ops.json" <<'EOF'
[{{"op": "set", "namespace": "public-job", "key": "{key}", "value": "\\"planted\\""}}]
EOF
"""


#: The subtask stays while the op beside it goes.
SUBTASK = f"""
cat > "$ISTOTA_DEFERRED_DIR/task_${{ISTOTA_TASK_ID}}_subtasks.json" <<'EOF'
[{{"prompt": "{SUBTASK_MARKER} carry on"}}]
EOF
"""


def _bash(call_id: str, command: str) -> dict:
    return {"tool_calls": [{"id": call_id, "name": "Bash", "arguments": {"command": command}}]}


def _facts(stack, call_id: str) -> dict[str, str]:
    output = stack.endpoint.tool_results_by_id().get(call_id)
    assert output is not None, f"{call_id} never returned\n{stack.tool_output()}"
    return dict(re.findall(r"^(\w+)=(.*)$", output, re.M))


def _kv_keys(stack, key: str) -> list[dict]:
    return stack.probe.query(
        "SELECT key FROM istota_kv WHERE user_id = ? AND key = ?", [USER, key],
    )


def _requests_naming(stack, *markers: str) -> list[str]:
    """Each model request of these tasks, whole, system half included."""
    bodies = [json.dumps(body) for body in list(stack.endpoint.requests)]
    return [body for body in bodies if any(marker in body for marker in markers)]


def _write_job(stack, *, enabled: bool) -> None:
    flag = "true" if enabled else "false"
    body = (
        "```toml\n[[jobs]]\n"
        f'name = "{JOB}"\ncron = "* * * * *"\nreach = "public"\nenabled = {flag}\n'
        f'prompt = "{MARKER} read the news"\n```\n'
    )
    stack.exec(["sh", "-c", f"cat > {CRON_PATH} <<'EOF'\n{body}EOF\n"])


def _wait(what: str, stack, read, timeout: float = 180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = read()
        if found:
            return found
        time.sleep(2)
    raise AssertionError(f"{what} within {timeout}s\n{stack.logs(120)}")


@pytest.mark.profile("public-job")
def test_an_approved_public_job_holds_nothing_and_its_subtask_neither(stack):
    stack.exec(["sh", "-c", SEED])

    stack.script([_bash("control-probe", PROBE + _kv_op(CONTROL_KV_KEY)), {"text": "control done"}])
    control_id = stack.submit("look around", user_id=USER)
    stack.probe.wait_for_task(status="completed", task_id=control_id, timeout=180)
    control = _facts(stack, "control-probe")
    assert control["user_dir"] == "yes", control
    assert control["notes"] == "yes", control
    assert control["user_md"] == "yes", control
    assert MEMORY_WORD in stack.endpoint.transcript(), "the control's prompt carried no USER.md"
    assert "Unknown skill" not in control["email"], control
    assert "Unknown skill" not in control["kv"], control
    assert REFUSED_ON_THE_ISOLATED_PROFILE not in control["browse_state"], control
    assert _kv_keys(stack, CONTROL_KV_KEY), "the control's deferred kv op was not applied"

    stack.script([
        {"when": MARKER, "turns": [
            _bash("public-probe", PROBE + _kv_op(KV_KEY) + SUBTASK), {"text": "public done"},
        ]},
        {"when": SUBTASK_MARKER, "turns": [
            _bash("subtask-probe", PROBE), {"text": "subtask done"},
        ]},
    ])
    mark = stack.probe.query("SELECT COALESCE(MAX(id), 0) AS id FROM tasks")[0]["id"]
    _write_job(stack, enabled=False)
    try:
        _wait("the job never synced", stack, lambda: stack.probe.query(
            "SELECT reach FROM scheduled_jobs WHERE user_id = ? AND name = ?", [USER, JOB]))
        approved = stack.exec([
            "uv", "run", "istota", "-c", CONTAINER_CONFIG,
            "egress", "grant", USER, SUBJECT, "public",
        ], timeout=120)
        assert approved.returncode == 0, approved.stdout + approved.stderr
        _write_job(stack, enabled=True)

        parent = _wait("the job never ran", stack, lambda: stack.probe.query(
            "SELECT * FROM tasks WHERE id > ? AND source_type = 'scheduled' AND prompt LIKE ? "
            "AND status IN ('completed', 'failed') ORDER BY id LIMIT 1",
            [mark, f"%{MARKER}%"],
        ))[0]
        child = _wait("the subtask never ran", stack, lambda: stack.probe.query(
            "SELECT * FROM tasks WHERE parent_task_id = ? AND status IN ('completed', 'failed')",
            [parent["id"]],
        ))[0]
    finally:
        stack.exec(["rm", "-f", CRON_PATH])

    assert parent["status"] == "completed", stack.diagnostics(parent)
    assert parent["egress_posture"] == "public", parent
    assert child["status"] == "completed", stack.diagnostics(child)

    for call in ("public-probe", "subtask-probe"):
        facts = _facts(stack, call)
        assert facts["user_dir"] == "no", (call, facts)
        assert facts["notes"] == "no", (call, facts)
        assert facts["user_md"] == "no", (call, facts)
        assert "Unknown skill: 'email'" in facts["email"], (call, facts)
        assert "Unknown skill: 'kv'" in facts["kv"], (call, facts)
        assert "Unknown skill" not in facts["skills"], (call, facts)
        assert REFUSED_ON_THE_ISOLATED_PROFILE in facts["browse_state"], (call, facts)

    public_requests = _requests_naming(stack, MARKER, SUBTASK_MARKER)
    assert public_requests, "no public prompt reached the endpoint"
    assert not any(MEMORY_WORD in body for body in public_requests), (
        "USER.md reached a public job's prompt"
    )
    assert _kv_keys(stack, KV_KEY) == [], "a public job's deferred kv op was applied"
