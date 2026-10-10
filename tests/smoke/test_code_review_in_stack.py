"""The code reviewer's file boundary, from inside a live review in the image.

`code_review run` hands its one reviewer `Read`, `Grep` and `Glob` over a
snapshot of the reviewed commit (`{temp_dir}/.review/{user}/run-<hex>/`). The
unit tier checks the request: `allowed_tools`, `fs_read_roots=[run_dir]`, a
`sandbox_wrap` built with every scope withheld. None of that says what a tool
call actually returns once the daemon runs it, which is what this file asks.

In the native class, a task commits a change in its own repos subtree and runs
the CLI; the CLI's reviewer is scripted to make three reads in one turn and then
answer (the claude class's shape is described on the class and its script):

1. the changed file, by a path relative to `tree/`, which must come back with
   the committed content;
2. a file planted in the user's workspace and one planted beside the framework
   database, which must both come back as tool errors without their content;
3. a fixed JSON answer, which must arrive in the envelope the task reads.

**Which boundary this witnesses depends on the brain, so there are two
scenarios.** `NativeBrain` ignores `sandbox_wrap` (that is the Claude CLI's
namespace, ISSUE-389) and is given no `native_sandbox_wrap` on this path, as in
`health/_brain_call.py`; its tool server is confined by `fs_read_roots`, and
the refusals in the first class are that rule's ("path is outside the allowed
workspace"). That class does not exercise the namespace
`build_daemon_sandbox(withheld_scopes=...)` builds, which only the Claude
brains enter. The second class does (ISSUE-614): it runs the same CLI on the
`claude_code` brain, with the image's own `claude` talking to the scripted
endpoint's Anthropic half, so the reviewer's reads are refused by bubblewrap
and by nothing else. The recorded negative controls are in the commit messages
of the two classes.

**The native class's reads are relative to `tree/`**, not absolute as `reviewer.md` asks,
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
from testbed.services.model_endpoint import ERROR_PREFIX

pytestmark = pytest.mark.smoke

#: `db_path` is `/data/db/istota.db` and `workspace_path` is `/data/workspace` on
#: the lean shape (`testbed.stack.LEAN_BASE_CONFIG`), the same literals
#: `test_sandbox_in_stack.py` and `test_sandbox_shared_room.py` restate.
DB_DIR = "/data/db"
WORKSPACE = "/data/workspace/Users/testuser"

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


# -- The same reviewer on the claude_code brain (ISSUE-614) ------------------

#: The daemon's per-user temp dir (`temp_dir` is `/data/tmp` on the lean
#: shape). Every task's downloads and deferred-op files live there, which is
#: why a reviewer with scopes withheld gets a fresh scratch work dir instead;
#: a file planted here is the witness for that half.
TEMP_DIR = "/data/tmp/testuser"
TEMP_SENTINEL = "review-witness-temp-3a81"
TEMP_FILE = f"{TEMP_DIR}/{PLANTED_NAME}"

CLAUDE_REPO = "review-witness-claude"
CLAUDE_CONFIG = "/tmp/review-witness-claude.toml"

READ_TEMP = "review-read-temp"

#: What the `claude` CLI's Read answers for a path that is not there.
CLAUDE_NOT_FOUND = "File does not exist."

#: The reviewer's own budget, passed as `--timeout` so a hung review ends
#: inside the exec bound below rather than as a `TimeoutExpired` with a
#: `claude` process still running in the container, where no reset sees it.
CLAUDE_REVIEW_TIMEOUT = 120
CLAUDE_EXEC_TIMEOUT = 300

#: Where `build_snapshot` puts run dirs on the lean shape
#: (`{temp_dir}/.review/{user}/run-<hex>/`).
REVIEW_ROOT = "/data/tmp/.review/testuser"

#: Both turns are the reviewer's: there is no task here, so nothing precedes
#: or follows them on the endpoint. The tree witness is a `Grep` over the
#: review root rather than a `Read`, because the run dir's name is random and
#: a relative path does not work here: inside the wrap the CLI's working
#: directory is the scratch work dir, not `tree/` (bwrap's `--chdir` wins over
#: `req.cwd`, which is why `reviewer.md` asks for absolute paths). The pattern
#: is the sentinel's first half, so the whole sentinel can only come back from
#: the file.
CLAUDE_SCRIPT = [
    {"tool_calls": [
        {"id": READ_TREE, "name": "Grep", "arguments": {
            "pattern": TREE_SENTINEL.rsplit("-", 1)[0],
            "path": REVIEW_ROOT,
            "output_mode": "content",
        }},
        {"id": READ_WORKSPACE, "name": "Read", "arguments": {"file_path": WORKSPACE_FILE}},
        {"id": READ_DB, "name": "Read", "arguments": {"file_path": DB_FILE}},
        {"id": READ_TEMP, "name": "Read", "arguments": {"file_path": TEMP_FILE}},
    ]},
    {"text": REVIEWER_ANSWER},
]


def claude_review_command(stack) -> str:
    """Build a repo, then run the reviewer on the `claude_code` brain.

    Through `docker compose exec` rather than a task's Bash call, so the daemon
    keeps its native brain and no task turn shares the script. The CLI reads a
    copy of the rendered config with `[brain] kind` switched; the daemon's own
    is untouched. `ANTHROPIC_BASE_URL` and the key reach the `claude` process
    through `build_model_cli_env`'s top-up from this process's environment, the
    route the skill proxy feeds in production. The proxy's own env assembly is
    the native scenario's to cover, not this one's.

    This is outside testbed rule 1 on purpose. That rule is about how a service
    points the *daemon* at itself; the daemon never reads this copy, and its
    brain has to stay native so no task turn competes for the script.
    """
    repo = f"/data/repos/testuser/{CLAUDE_REPO}"
    git = "git -c user.email=smoke@example.com -c user.name=Smoke"
    head, tail = TREE_SENTINEL.rsplit("-", 1)
    return f"""
set -eu
cp /data/config/config.toml {CLAUDE_CONFIG}
sed -i '0,/^kind = "native"$/s//kind = "claude_code"/' {CLAUDE_CONFIG}
grep -qx 'kind = "claude_code"' {CLAUDE_CONFIG}
mkdir -p /data/repos/testuser
rm -rf {repo}
git init -q -b main {repo}
cd {repo}
echo "first line" > {CHANGED_FILE}
{git} add {CHANGED_FILE}
{git} commit -q -m "Add {CHANGED_FILE}"
printf "%s-%s\\n" {head} {tail} >> {CHANGED_FILE}
{git} commit -q -am "Extend {CHANGED_FILE}"
cd /app
env ISTOTA_CONFIG_PATH={CLAUDE_CONFIG} ISTOTA_USER_ID=testuser \\
    DEVELOPER_REPOS_DIR=/data/repos/testuser \\
    ANTHROPIC_BASE_URL={stack.endpoint.anthropic_container_url} \\
    ANTHROPIC_API_KEY=unused-by-the-scripted-endpoint \\
    uv run python -m istota.skills.code_review run \\
    --worktree {repo} --base HEAD~1 --intent "smoke witness" \\
    --timeout {CLAUDE_REVIEW_TIMEOUT}
"""


@pytest.fixture
def planted_with_temp(planted):
    """`planted`, plus a file in the per-user temp dir."""
    result = planted.exec([
        "sh", "-c", f"mkdir -p {TEMP_DIR} && echo {TEMP_SENTINEL} > {TEMP_FILE}",
    ])
    assert result.returncode == 0, result.stderr
    yield planted
    planted.exec([
        "sh", "-c",
        f"rm -f {TEMP_FILE} {CLAUDE_CONFIG}; "
        f"rm -rf /data/repos/testuser/{CLAUDE_REPO}",
    ])


@pytest.mark.profile(profiles.FORGE.name)
class TestTheClaudeReviewerIsConfinedByItsNamespace:
    """The reviewer's `claude` process, inside `build_daemon_sandbox`'s wrap.

    The CLI runs with `--dangerously-skip-permissions` and ignores
    `fs_read_roots`, so every refusal here is bubblewrap's: the workspace is
    unbound because `files` is withheld, the database directory is masked, and
    the per-user temp dir is unbound because a withheld scope moves the work
    dir to a fresh scratch dir. The tree read coming back shows the namespace
    binds the snapshot and that the CLI could read at all.
    """

    @pytest.mark.script(CLAUDE_SCRIPT)
    def test_only_the_snapshot_is_readable(self, planted_with_temp):
        stack = planted_with_temp
        result = stack.exec(
            ["sh", "-c", claude_review_command(stack)], timeout=CLAUDE_EXEC_TIMEOUT
        )

        answer = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{") and '"status"' in line:
                answer = json.loads(line)
        assert answer is not None, (
            f"the CLI printed no envelope (exit {result.returncode})\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr[-4000:]}"
        )

        results = stack.endpoint.tool_results_by_id()
        missing = {READ_TREE, READ_WORKSPACE, READ_DB, READ_TEMP} - results.keys()
        assert not missing, (
            f"no result for {sorted(missing)}: the reviewer never ran its tools "
            f"on the claude_code brain\n{answer}\n"
            f"--- stderr ---\n{result.stderr[-4000:]}"
        )

        # The tree line, not just the sentinel: `meta/diff.patch` carries it too.
        assert f"/tree/{CHANGED_FILE}:2:{TREE_SENTINEL}" in results[READ_TREE], (
            "the changed file did not come back from tree/, so the refusals "
            "below could be a namespace that binds nothing\n"
            f"--- result ---\n{results[READ_TREE]}"
        )
        for call_id, sentinel, path in (
            (READ_WORKSPACE, WORKSPACE_SENTINEL, WORKSPACE_FILE),
            (READ_DB, DB_SENTINEL, DB_FILE),
            (READ_TEMP, TEMP_SENTINEL, TEMP_FILE),
        ):
            content = results[call_id]
            assert sentinel not in content, (
                f"the claude reviewer read {path}: it is inside the reviewer's "
                f"namespace\n--- result ---\n{content}"
            )
            # "Does not exist" is the namespace's answer: an unbound path and
            # the database mask both read as absent. A CLI-side refusal would
            # be an error with a different text.
            assert content.startswith(ERROR_PREFIX + CLAUDE_NOT_FOUND), (
                f"reading {path} did not fail as a path absent from the "
                f"namespace\n--- result ---\n{content}"
            )

        assert answer["status"] == "ok", answer
        assert answer["reviewer"]["tools"] is True, answer
        assert [f["claim"] for f in answer["findings"]] == [FINDING_CLAIM], answer

    def test_the_planted_files_are_there_to_be_refused(self, planted_with_temp):
        result = planted_with_temp.exec(["cat", WORKSPACE_FILE, DB_FILE, TEMP_FILE])

        assert result.returncode == 0, result.stderr
        for sentinel in (WORKSPACE_SENTINEL, DB_SENTINEL, TEMP_SENTINEL):
            assert sentinel in result.stdout, result.stdout
