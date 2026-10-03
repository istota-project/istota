"""The code reviewer's file boundary, from inside a live review in the image.

`code_review run` hands its one reviewer `Read`, `Grep` and `Glob` over a
snapshot of the reviewed commit (`{temp_dir}/.review/{user}/run-<hex>/`). The
unit tier checks the request: `allowed_tools`, `fs_read_roots=[run_dir]`, a
`sandbox_wrap` built with every scope withheld. None of that says what a tool
call actually returns once the daemon runs it, which is what this file asks.

A task commits a change in its own repos subtree and runs the CLI; the CLI's
reviewer is scripted to make three reads in one turn and then answer:

1. the changed file, by a path relative to `tree/`, which must come back with
   the committed content;
2. a file planted in the user's workspace and one planted beside the framework
   database, which must both come back as tool errors without their content;
3. a fixed JSON answer, which must arrive in the envelope the task reads.

**Which boundary this witnesses depends on the brain, and the lean stack runs
the native one.** `NativeBrain` ignores `sandbox_wrap` (that is the Claude
CLI's namespace, ISSUE-389) and is given no `native_sandbox_wrap` on this path,
as in `health/_brain_call.py`; its tool server is confined by `fs_read_roots`,
and the refusals below are that rule's ("path is outside the allowed
workspace"). So this proves the reviewer's reads are bounded to the snapshot
in the shipped image on the native brain. It does not exercise the namespace
`build_daemon_sandbox(withheld_scopes=...)` builds, which only the Claude
brains enter and which nothing in this tier can run: the scripted endpoint
speaks the OpenAI wire format and the image ships no `claude`. The recorded
negative controls are in the module's commit message.

**The reads are relative to `tree/`**, not absolute as `reviewer.md` asks,
because the run directory's name is random and a script is fixed before the
run. On the native brain the tool server's working directory is `req.cwd`,
which the CLI sets to the snapshot's `tree/`.

Runs on the `forge` profile, not `base`: the CLI refuses before any model call
without `developer.enabled` and a `repos_dir`, and `forge` is the profile that
renders both. The gitlab stub is not used.
"""

from __future__ import annotations

import json

import pytest

from testbed import profiles

pytestmark = pytest.mark.smoke

#: `db_path` is `/data/db/istota.db` and `workspace_path` is `/mnt/shared` on
#: the lean shape (`docker/istota/render-config.sh`), the same literals
#: `test_sandbox_in_stack.py` and `test_sandbox_shared_room.py` restate.
DB_DIR = "/data/db"
WORKSPACE = "/mnt/shared/Users/testuser"

#: Content no other path in the stack carries. The workspace and database ones
#: are written by the fixture through `docker compose exec`; the tree one is
#: assembled by the task's own command (see `TASK_COMMAND`).
TREE_SENTINEL = "review-witness-tree-c41d"
WORKSPACE_SENTINEL = "review-witness-workspace-9b2e"
DB_SENTINEL = "review-witness-db-5f07"

PLANTED_NAME = "review-witness.txt"
WORKSPACE_FILE = f"{WORKSPACE}/{PLANTED_NAME}"
DB_FILE = f"{DB_DIR}/{PLANTED_NAME}"

REPO_NAME = "review-witness"
CHANGED_FILE = "reviewed.txt"

#: The task's one Bash call. No `set -x`: xtrace would echo the commands into
#: the Bash tool result. The tree sentinel is assembled by `printf` from two
#: halves, so the joined string is in no command text sent to the model.
TASK_COMMAND = f"""
set -eu
test -n "$DEVELOPER_REPOS_DIR"
cd "$DEVELOPER_REPOS_DIR"
rm -rf {REPO_NAME}
git init -q -b main {REPO_NAME}
cd {REPO_NAME}
echo "first line" > {CHANGED_FILE}
git -c user.email=smoke@example.com -c user.name=Smoke add {CHANGED_FILE}
git -c user.email=smoke@example.com -c user.name=Smoke commit -q -m "Add {CHANGED_FILE}"
printf "%s-%s\\n" {TREE_SENTINEL.rsplit("-", 1)[0]} {TREE_SENTINEL.rsplit("-", 1)[1]} >> {CHANGED_FILE}
git -c user.email=smoke@example.com -c user.name=Smoke commit -q -am "Extend {CHANGED_FILE}"
istota-skill code_review run --worktree "$PWD" --base HEAD~1 --intent "smoke witness"
"""

READ_TREE = "review-read-tree"
READ_WORKSPACE = "review-read-workspace"
READ_DB = "review-read-db"

FINDING_CLAIM = "The scripted reviewer's one finding"

REVIEWER_ANSWER = json.dumps({
    "findings": [{
        "severity": "high",
        "file": CHANGED_FILE,
        "line": 2,
        "claim": FINDING_CLAIM,
        "evidence": "scripted",
        "action": "none",
        "unverified": False,
    }],
    "ruled_out": [],
})

#: Four turns over two conversations on one endpoint, which routes by call
#: order alone: the task's Bash call, then the reviewer's two turns while that
#: call is still running inside the CLI, then the task's closing answer.
SCRIPT = [
    {"tool_calls": [{
        "id": "task-bash",
        "name": "Bash",
        "arguments": {"command": TASK_COMMAND},
    }]},
    {"tool_calls": [
        {"id": READ_TREE, "name": "Read", "arguments": {"file_path": CHANGED_FILE}},
        {"id": READ_WORKSPACE, "name": "Read", "arguments": {"file_path": WORKSPACE_FILE}},
        {"id": READ_DB, "name": "Read", "arguments": {"file_path": DB_FILE}},
    ]},
    {"text": REVIEWER_ANSWER},
    {"text": "reviewed"},
]

REFUSAL = "outside the allowed workspace"


def tool_result(stack, call_id: str) -> str:
    """What the tool call `call_id` returned, as the endpoint was sent it.

    Keyed on the id rather than searched for in the whole transcript, so a
    sentinel echoed anywhere else (a command, a prompt) cannot stand in for a
    tool result.
    """
    for body in list(stack.endpoint.requests):
        for message in body.get("messages") or []:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
                return str(message.get("content"))
    raise AssertionError(
        f"no tool result for {call_id!r} reached the endpoint, so the reviewer "
        "never ran its tools or the run stopped before its second turn\n"
        f"--- tool results ---\n{stack.endpoint.tool_results()}\n"
        f"--- daemon logs ---\n{stack.logs(120)}"
    )


def envelope(stack) -> dict:
    """The CLI's one-line JSON envelope, out of the task's Bash result."""
    output = tool_result(stack, "task-bash")
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{") and '"status"' in line:
            return json.loads(line)
    raise AssertionError(
        f"the Bash call printed no envelope\n--- output ---\n{output}\n"
        f"--- daemon logs ---\n{stack.logs(120)}"
    )


@pytest.fixture
def planted(stack):
    """The two files the reviewer must not read."""
    result = stack.exec([
        "sh", "-c",
        f"mkdir -p {WORKSPACE} && "
        f"echo {WORKSPACE_SENTINEL} > {WORKSPACE_FILE} && "
        f"echo {DB_SENTINEL} > {DB_FILE}",
    ])
    assert result.returncode == 0, (
        f"planting the witness files failed, so every absence below would be "
        f"about a file that was never written\n{result.stderr}"
    )
    yield stack
    stack.exec([
        "sh", "-c",
        f"rm -f {WORKSPACE_FILE} {DB_FILE}; "
        f"rm -rf /data/repos/testuser/{REPO_NAME}",
    ])


@pytest.mark.profile(profiles.FORGE.name)
class TestTheReviewerReadsOnlyItsSnapshot:
    @pytest.mark.script(SCRIPT)
    def test_the_tree_is_readable_and_nothing_else_is(self, planted):
        stack = planted
        task_id = stack.submit("review the change")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=240)

        tree = tool_result(stack, READ_TREE)
        assert TREE_SENTINEL in tree, (
            "the changed file did not come back from tree/, so the refusals "
            "below could be a reviewer that can read nothing at all\n"
            f"--- result ---\n{tree}\n--- daemon logs ---\n{stack.logs(120)}"
        )

        workspace = tool_result(stack, READ_WORKSPACE)
        assert WORKSPACE_SENTINEL not in workspace, (
            f"the reviewer read {WORKSPACE_FILE}: the user's workspace is "
            f"inside its file boundary\n--- result ---\n{workspace}"
        )
        assert REFUSAL in workspace, (
            f"reading {WORKSPACE_FILE} failed for some other reason than the "
            f"boundary\n--- result ---\n{workspace}"
        )

        db = tool_result(stack, READ_DB)
        assert DB_SENTINEL not in db, (
            f"the reviewer read {DB_FILE}: the directory holding the framework "
            f"database is inside its file boundary\n--- result ---\n{db}"
        )
        assert REFUSAL in db, (
            f"reading {DB_FILE} failed for some other reason than the "
            f"boundary\n--- result ---\n{db}"
        )

        answer = envelope(stack)
        assert answer["status"] == "ok", answer
        assert answer["reviewer"]["tools"] is True, (
            "the review fell back to text-only, so the reads above were not "
            f"the tooled reviewer's\n{answer}"
        )
        assert [f["claim"] for f in answer["findings"]] == [FINDING_CLAIM], answer

    def test_the_planted_files_are_there_to_be_refused(self, planted):
        """The control: the daemon's own view reads both planted files.

        Without it, a refusal above could be a file that was never written or
        a path that does not exist in the container.
        """
        result = planted.exec(["cat", WORKSPACE_FILE, DB_FILE])

        assert result.returncode == 0, result.stderr
        assert WORKSPACE_SENTINEL in result.stdout, result.stdout
        assert DB_SENTINEL in result.stdout, result.stdout
