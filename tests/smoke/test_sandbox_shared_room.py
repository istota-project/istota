"""A shared room's reach, looked at from inside a live task in the shipped image.

A room more than one human reads withholds the sender's ungranted scopes, and
the filesystem half of that is the sandbox mount plan: no `Users/{uid}` bind
without `files`, a per-task `room-task-<id>` temp dir instead of the per-user
one, no flat Talk directory, read-only tmpfs masks over the memory directories
when `files` is granted without `memory` (made first when absent, put on the
target when a symlink), the bot directory bound onto itself so it cannot be
renamed out from under those masks and checked by inode before the command
runs, and a `Groups/<id>` bind for the groups
the task resolves (multiplayer Stages 9, 13, 16, 23 and 28).

The default suite patches `_bwrap_available` and reads argv, so it has never
run any of that. This tier does. Every scenario reads the same probe from
inside a task, and the private-room control runs the same probe in the same
session and requires the opposite answers, so an absence here is the room's
doing and not a stack that never had the files.

Rooms and tasks are seeded through a Python script run in the container
against the daemon's own database: there is no CLI that adds a room member,
writes a grant or creates a guest's turn, and the principal turns still go in
through `istota task`, which is the shipped path.
"""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.smoke

#: `render-config.sh`: `workspace_path`, `temp_dir`, and the user and bot name
#: the lean profile renders. Restated so a scenario reads as one thing; the
#: control below fails if any of them is not where the daemon put it.
WORKSPACE = "/mnt/shared"
TEMP = "/data/tmp"
USER = "testuser"
BOT_DIR = "istota"
CONFIG = "/data/config/config.toml"

USER_DIR = f"{WORKSPACE}/Users/{USER}"
BOT = f"{USER_DIR}/{BOT_DIR}"
GROUPS = f"{WORKSPACE}/Groups"

#: Rooms. `restricted` and `guest` are read by testuser and bob, both members of
#: `fam`; `files` is read by testuser and carol, who is in no group; `private`
#: is testuser's alone. testuser is also in `other`, which bob is not, so only
#: the private room can load it.
RESTRICTED = "sr-restricted"
FILES = "sr-files"
GUEST = "sr-guest"
PRIVATE = "sr-private"

#: The seed. Idempotent, because the stack is shared for the session and each
#: test reseeds what it needs.
SEED = f"""
import json, sys
from pathlib import Path
from istota import db, room_policy
from istota.config import load_config

config = load_config(Path({CONFIG!r}))
out = {{}}
with db.get_db(config.db_path) as conn:
    for gid, members in (("fam", ("testuser", "bob")), ("other", ("testuser",))):
        if db.get_group(conn, gid, include_archived=True) is None:
            db.create_group(conn, gid, kind="family", display_name=gid, created_by="test")
            for m in members:
                db.add_group_member(conn, gid, m, added_by="test")
    rooms = {{
        {RESTRICTED!r}: ("bob",), {FILES!r}: ("carol",), {GUEST!r}: ("bob",),
        {PRIVATE!r}: (),
    }}
    for token, others in rooms.items():
        if db.get_room(conn, token) is None:
            db.register_room(conn, token, "testuser", origin="web", name=token)
            db.add_room_member(conn, token, "testuser")
            for other in others:
                db.add_room_member(conn, token, other)
    conn.execute(
        "INSERT OR IGNORE INTO room_data_grants (room_token, user_id, scope) "
        "VALUES (?, 'testuser', 'files')", ({FILES!r},),
    )
    policy = room_policy.ensure_policy(conn, {GUEST!r})
    conn.execute("UPDATE room_policy SET guest_reply = 'direct' WHERE room_token = ?",
                 ({GUEST!r},))
    pid = db.upsert_room_participant(
        conn, room_token={GUEST!r}, surface="talk", surface_ref="guests/probe",
        kind="guest", display_name="Probe Guest",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "guest-task":
        out["task_id"] = db.create_task(
            conn, user_id="testuser", source_type="cli", prompt="guest probe",
            conversation_token={GUEST!r}, guest_participant_id=pid, audience="mixed",
        )
print(json.dumps(out))
"""

#: The files every scenario looks for. The sentinel is in the files and never
#: in the probe's command text, so a probe echoed into the transcript cannot
#: answer for the files.
FILES_SEED = f"""
set -e
mkdir -p {BOT}/config {USER_DIR}/memories {GROUPS}/fam {GROUPS}/other {WORKSPACE}/Talk
echo 'SENTINEL-MEM user notes' > {BOT}/config/USER.md
echo 'SENTINEL-MEM dated' > {USER_DIR}/memories/2026-09-01.md
echo 'ordinary file' > {USER_DIR}/notes.txt
echo 'a talk attachment' > {WORKSPACE}/Talk/attachment.txt
mkdir -p {TEMP}/{USER}
echo 'another task left this' > {TEMP}/{USER}/planted-sibling.txt
owner=$(stat -c %u:%g {TEMP})
chown -R "$owner" {USER_DIR} {GROUPS} {WORKSPACE}/Talk {TEMP}/{USER}
"""

MARK = "SHARED_ROOM_PROBE"

#: Facts about the task's view, each a line. `memory_readable` and
#: `config_entries` are the discriminators for the masks (the workspace volume
#: may itself be a tmpfs, so `*_fs` is a diagnosis rather than a verdict).
#: `rename` moves the bot directory and puts it back when that worked.
PROBE = f"""
M={MARK}
S=SENTINEL
echo "${{M}}_BEGIN"
present() {{ if [ -e "$1" ]; then echo yes; else echo no; fi; }}
echo "user_dir=$(present {USER_DIR})"
echo "talk_dir=$(present {WORKSPACE}/Talk)"
echo "deferred=$(basename "${{ISTOTA_DEFERRED_DIR:-unset}}")"
echo "temp_entries=[$(ls -A {TEMP}/{USER} 2>&1 | tr '\\n' ' ')]"
echo "memories_fs=$(stat -f -c %T {USER_DIR}/memories 2>&1)"
echo "config_fs=$(stat -f -c %T {BOT}/config 2>&1)"
echo "playbooks_fs=$(stat -f -c %T {BOT}/playbooks 2>&1)"
echo "config_entries=[$(ls -A {BOT}/config 2>&1 | tr '\\n' ' ')]"
echo "archive_entries=[$(ls -A {USER_DIR}/archive/mem 2>&1 | tr '\\n' ' ')]"
if grep -rqs "${{S}}-MEM" {BOT}/config {USER_DIR}/memories {USER_DIR}/archive; then
  echo "memory_readable=yes"
else
  echo "memory_readable=no"
fi
if touch {USER_DIR}/probe-note 2>/dev/null; then
  echo "files_writable=yes"; rm -f {USER_DIR}/probe-note
else
  echo "files_writable=no"
fi
if touch {BOT}/playbooks/probe 2>/dev/null; then
  echo "playbook_writable=yes"; rm -f {BOT}/playbooks/probe
else
  echo "playbook_writable=no"
fi
if out=$(mv {BOT} {USER_DIR}/.moved-bot 2>&1); then
  echo "rename=ok"; mv {USER_DIR}/.moved-bot {BOT}
else
  echo "rename=refused [$out]"
fi
echo "groups_entries=[$(ls -A {GROUPS} 2>&1 | tr '\\n' ' ')]"
if touch {GROUPS}/fam/probe 2>/dev/null; then
  echo "fam_writable=yes"; rm -f {GROUPS}/fam/probe
else
  echo "fam_writable=no"
fi
echo "other_group=$(present {GROUPS}/other)"
echo "${{M}}_END"
"""

SCRIPT = [
    {"tool_calls": [{"id": "call-1", "name": "Bash", "arguments": {"command": PROBE}}]},
    {"text": "I looked around"},
]


def _seed(stack, *args: str) -> dict:
    files = stack.exec(["sh", "-c", FILES_SEED])
    assert files.returncode == 0, f"seeding the files failed\n{files.stderr}"
    rows = stack.exec(["uv", "run", "python", "-c", SEED, *args], timeout=120)
    assert rows.returncode == 0, f"seeding the rooms failed\n{rows.stderr}"
    return json.loads(rows.stdout.strip().splitlines()[-1])


def _principal_task(stack, token: str) -> int:
    result = stack.exec(
        ["uv", "run", "istota", "-c", CONFIG, "task", "probe the room",
         "-u", USER, "--source-type", "cli", "-t", token],
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r"Task created:\s*(\d+)", result.stdout)
    assert match, result.stdout
    return int(match.group(1))


def _observe(stack, task_id: int) -> dict[str, str]:
    task = stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
    transcript = stack.endpoint.transcript()
    begin = transcript.find(f"{MARK}_BEGIN")
    end = transcript.find(f"{MARK}_END", begin + 1)
    if begin < 0 or end < 0:
        raise AssertionError(
            f"the probe's output never reached the model (task {task_id} is "
            f"{task.get('status')}), so this says nothing about the binds\n"
            f"--- daemon logs ---\n{stack.logs(150)}"
        )
    facts = {}
    for line in transcript[begin:end].splitlines():
        key, sep, value = line.partition("=")
        if sep:
            facts[key.strip()] = value.strip()
    facts["_task_id"] = str(task_id)
    return facts


def _show(facts) -> str:
    return "\n".join(f"{k}={v}" for k, v in facts.items())


@pytest.fixture
def shared_rooms(stack):
    _seed(stack)
    yield stack
    stack.exec(["sh", "-c",
                f"rm -rf {USER_DIR}/archive {USER_DIR}/.moved-bot; "
                f"[ -L {USER_DIR}/memories ] && rm {USER_DIR}/memories; true"])


class TestASharedRoomWithNothingGranted:
    @pytest.mark.script(SCRIPT)
    def test_the_workspace_talk_and_per_user_temp_are_not_there(self, shared_rooms):
        facts = _observe(shared_rooms, _principal_task(shared_rooms, RESTRICTED))
        report = _show(facts)
        assert facts["user_dir"] == "no", report
        assert facts["talk_dir"] == "no", report
        assert facts["deferred"] == f"room-task-{facts['_task_id']}", report
        assert "planted-sibling.txt" not in facts["temp_entries"], report
        assert facts["memory_readable"] == "no", report

    @pytest.mark.script(SCRIPT)
    def test_a_group_every_reader_is_in_is_bound_and_no_other(self, shared_rooms):
        facts = _observe(shared_rooms, _principal_task(shared_rooms, RESTRICTED))
        report = _show(facts)
        assert facts["groups_entries"].strip("[] ") == "fam", report
        assert facts["fam_writable"] == "yes", report
        assert facts["other_group"] == "no", report


class TestFilesGrantedWithoutMemory:
    @pytest.mark.script(SCRIPT)
    def test_the_workspace_is_there_and_its_memory_is_not(self, shared_rooms):
        facts = _observe(shared_rooms, _principal_task(shared_rooms, FILES))
        report = _show(facts)
        assert facts["user_dir"] == "yes", report
        assert facts["files_writable"] == "yes", report
        assert facts["memory_readable"] == "no", report
        assert facts["config_entries"] == "[]", report
        # Absent before the task: made by the daemon and masked read-only.
        assert facts["playbooks_fs"] == "tmpfs", report
        assert facts["playbook_writable"] == "no", report

    @pytest.mark.script(SCRIPT)
    def test_the_bot_directory_cannot_be_renamed_out_from_under_the_masks(
        self, shared_rooms,
    ):
        """Stage 16's open question, answered live: rename(2) refuses only a
        mountpoint, and the self-bind is what makes the bot directory one."""
        facts = _observe(shared_rooms, _principal_task(shared_rooms, FILES))
        assert facts["rename"].startswith("refused"), _show(facts)
        assert "busy" in facts["rename"].lower(), _show(facts)

    @pytest.mark.script(SCRIPT)
    def test_a_symlinked_memory_directory_is_masked_where_it_points(self, shared_rooms):
        moved = shared_rooms.exec(["sh", "-c", (
            f"mkdir -p {USER_DIR}/archive && mv {USER_DIR}/memories {USER_DIR}/archive/mem "
            f"&& ln -s {USER_DIR}/archive/mem {USER_DIR}/memories "
            f"&& chown -h $(stat -c %u:%g {TEMP}) {USER_DIR}/memories {USER_DIR}/archive"
        )])
        assert moved.returncode == 0, moved.stderr
        try:
            facts = _observe(shared_rooms, _principal_task(shared_rooms, FILES))
        finally:
            shared_rooms.exec(["sh", "-c", (
                f"rm {USER_DIR}/memories && mv {USER_DIR}/archive/mem {USER_DIR}/memories "
                f"&& rm -rf {USER_DIR}/archive"
            )])
        report = _show(facts)
        assert facts["archive_entries"] == "[]", report
        assert facts["memory_readable"] == "no", report

    @pytest.mark.script(SCRIPT)
    def test_a_room_with_a_reader_outside_the_group_binds_no_group(self, shared_rooms):
        facts = _observe(shared_rooms, _principal_task(shared_rooms, FILES))
        assert "No such file" in facts["groups_entries"], _show(facts)


class TestTheIdentityGuard:
    """The self-bind's source can be swapped for a symlink between the plan
    and bwrap's mount, by another task of the same user. What runs the
    command is a check, after every mount, that the path is the inode the plan
    saw. The files tests above pass through it in a real namespace; this is
    its refusal, run in the image's own shell and `stat`."""

    def test_it_runs_the_command_only_for_the_planned_inode(self, shared_rooms):
        from istota.sandbox_plan import IDENTITY_GUARD

        ident = shared_rooms.exec(["stat", "-c", "%d:%i", BOT]).stdout.strip()
        assert re.fullmatch(r"\d+:\d+", ident), ident
        guarded = ["/bin/sh", "-c", IDENTITY_GUARD, "sh", BOT]
        ran = shared_rooms.exec([*guarded, ident, "--", "echo", "GUARD_RAN"])
        assert ran.returncode == 0 and "GUARD_RAN" in ran.stdout, ran.stderr
        other = shared_rooms.exec(["stat", "-c", "%d:%i", USER_DIR]).stdout.strip()
        refused = shared_rooms.exec([*guarded, other, "--", "echo", "GUARD_RAN"])
        assert refused.returncode == 125, (refused.returncode, refused.stderr)
        assert "GUARD_RAN" not in refused.stdout
        assert "not the directory that was planned" in refused.stderr


class TestAGuestsTurn:
    @pytest.mark.script(SCRIPT)
    def test_it_reaches_no_workspace_and_no_group(self, shared_rooms):
        task_id = _seed(shared_rooms, "guest-task")["task_id"]
        facts = _observe(shared_rooms, task_id)
        report = _show(facts)
        assert facts["user_dir"] == "no", report
        assert facts["deferred"] == f"emissary-task-{task_id}", report
        assert "No such file" in facts["groups_entries"], report
        assert facts["memory_readable"] == "no", report


class TestThePrivateRoomControl:
    """The same probe, in testuser's own room, in the same session.

    Every assertion above is an absence, and each is equally true of a stack
    that never had the files or a task whose sandbox bound none of them. Here
    the same paths are present, readable, and the bot directory renames.
    """

    @pytest.mark.script(SCRIPT)
    def test_everything_withheld_above_is_reachable_here(self, shared_rooms):
        facts = _observe(shared_rooms, _principal_task(shared_rooms, PRIVATE))
        report = _show(facts)
        assert facts["user_dir"] == "yes", report
        assert facts["talk_dir"] == "yes", report
        assert facts["memory_readable"] == "yes", report
        assert "USER.md" in facts["config_entries"], report
        assert "planted-sibling.txt" in facts["temp_entries"], report
        assert facts["rename"] == "ok", report
        assert "fam" in facts["groups_entries"] and facts["other_group"] == "yes", report
