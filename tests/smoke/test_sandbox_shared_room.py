"""A shared room's reach, looked at from inside a live task in the shipped image.

A turn runs with its sender's reach (ISSUE-576). A member's turn in a room more
than one human reads binds what their private room does. What is withheld is
withheld from a guest's turn and from a task nobody asked in the room (here a
`cli` task, the same rule as a subtask): no `Users/{uid}` bind, a per-task
temp dir instead of the per-user one (`room-task-<id>`, or `emissary-task-<id>`
for a guest). Independently, a `Groups/<id>` bind for the groups the task
resolves (multiplayer Stages 9, 13, 23).

The lean stack uses local storage: attachments live under
`Channels/<token>/media/`, and every turn can read its own room's media, guests
included. No turn binds the flat `Talk/` directory used on a Nextcloud mount.
We seed it anyway to prove it stays hidden, alongside the other rooms' media.

A correspondent's turn is the same question asked by real mail on the `email`
profile: mail somebody else wrote runs as the host, withholding all but a short
allowlist, and what the `email` CLI refuses on that turn it refuses through the
live skill proxy, which the default suite mocks.

The default suite patches `_bwrap_available` and reads argv, so it has never
run any of that. This tier does. Every scenario reads the same probe from
inside a task, and the private-room control runs the same probe in the same
session and requires the opposite answers for personal paths, so an absence
there is the room's doing and not a stack that never had the files.

Rooms and tasks are seeded through a Python script run in the container
against the daemon's own database: there is no CLI that adds a room member or
creates a guest's turn, and the other turns still go in through `istota task`,
which is the shipped path.
"""

from __future__ import annotations

import json
import re

import pytest

from testbed.services import mail
from tests.support import email_flow as flow
from tests.support import email_people as people
from tests.support import parity

pytestmark = pytest.mark.smoke

#: the testbed's config: `workspace_path`, `temp_dir`, and the user and bot name
#: the lean profile renders. Restated so a scenario reads as one thing; the
#: control below fails if any of them is not where the daemon put it.
WORKSPACE = "/data/workspace"
TEMP = "/data/tmp"
USER = "testuser"
BOT_DIR = "istota"
CONFIG = "/data/config/config.toml"

USER_DIR = f"{WORKSPACE}/Users/{USER}"
BOT = f"{USER_DIR}/{BOT_DIR}"
GROUPS = f"{WORKSPACE}/Groups"

#: Rooms. `shared` and `guest` are read by testuser and bob, both members of
#: `fam`; `outsider` is read by testuser and carol, who is in no group;
#: `private` is testuser's alone. testuser is also in `other`, which bob is
#: not, so only the private room can load it.
SHARED = "sr-shared"
OUTSIDER = "sr-outsider"
GUEST = "sr-guest"
PRIVATE = "sr-private"
REQUEST_MARK = "[e2e:shared-room-probe]"

#: The seed. Idempotent, because the stack is shared for the session and each
#: test reseeds what it needs.
SEED = f"""
import json, sys
from pathlib import Path
from istota import db
from istota.rooms import policy as room_policy
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
        {SHARED!r}: ("bob",), {OUTSIDER!r}: ("carol",), {GUEST!r}: ("bob",),
        {PRIVATE!r}: (),
    }}
    for token, others in rooms.items():
        if db.get_room(conn, token) is None:
            db.register_room(conn, token, "testuser", origin="web", name=token)
            db.add_room_member(conn, token, "testuser")
            for other in others:
                db.add_room_member(conn, token, other)
    policy = room_policy.ensure_policy(conn, {GUEST!r})
    conn.execute("UPDATE room_policy SET guest_reply = 'direct' WHERE room_token = ?",
                 ({GUEST!r},))
    pid = db.upsert_room_participant(
        conn, room_token={GUEST!r}, surface="talk", surface_ref="guests/probe",
        kind="guest", display_name="Probe Guest",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "guest-task":
        out["task_id"] = db.create_task(
            conn, user_id="testuser", source_type="cli", prompt={f"{REQUEST_MARK} guest probe"!r},
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
for room in {SHARED} {OUTSIDER} {GUEST} {PRIVATE}; do
  mkdir -p {WORKSPACE}/Channels/"$room"/media/1
  echo "$room" > {WORKSPACE}/Channels/"$room"/media/1/attachment.txt
  chown -R "$owner" {WORKSPACE}/Channels/"$room"
done
"""

MARK = "SHARED_ROOM_PROBE"

#: Facts about the task's view, each a line.
FACTS = f"""
S=SENTINEL
present() {{ if [ -e "$1" ]; then echo yes; else echo no; fi; }}
echo "user_dir=$(present {USER_DIR})"
echo "talk_dir=$(present {WORKSPACE}/Talk)"
echo "room_media=[$(for room in {SHARED} {OUTSIDER} {GUEST} {PRIVATE}; do
  cat {WORKSPACE}/Channels/"$room"/media/1/attachment.txt 2>/dev/null
done | tr '\\n' ' ')]"
echo "deferred=$(basename "${{ISTOTA_DEFERRED_DIR:-unset}}")"
echo "temp_entries=[$(ls -A {TEMP}/{USER} 2>&1 | tr '\\n' ' ')]"
echo "config_entries=[$(ls -A {BOT}/config 2>&1 | tr '\\n' ' ')]"
if grep -rqs "${{S}}-MEM" {BOT}/config {USER_DIR}/memories; then
  echo "memory_readable=yes"
else
  echo "memory_readable=no"
fi
if touch {USER_DIR}/probe-note 2>/dev/null; then
  echo "files_writable=yes"; rm -f {USER_DIR}/probe-note
else
  echo "files_writable=no"
fi
echo "groups_entries=[$(ls -A {GROUPS} 2>&1 | tr '\\n' ' ')]"
if touch {GROUPS}/fam/probe 2>/dev/null; then
  echo "fam_writable=yes"; rm -f {GROUPS}/fam/probe
else
  echo "fam_writable=no"
fi
echo "other_group=$(present {GROUPS}/other)"
"""

PROBE = f"""
M={MARK}
echo "${{M}}_BEGIN"
{FACTS}
echo "${{M}}_END"
"""

# A background memory task must not consume the probe's model responses.
SCRIPT = [{
    "when": REQUEST_MARK,
    "turns": [
        {"tool_calls": [{"id": "call-1", "name": "Bash", "arguments": {"command": PROBE}}]},
        {"text": "I looked around"},
    ],
}]


def _seed(stack, *args: str) -> dict:
    files = stack.exec(["sh", "-c", FILES_SEED])
    assert files.returncode == 0, f"seeding the files failed\n{files.stderr}"
    rows = stack.exec(["uv", "run", "python", "-c", SEED, *args], timeout=120)
    assert rows.returncode == 0, f"seeding the rooms failed\n{rows.stderr}"
    return json.loads(rows.stdout.strip().splitlines()[-1])


def _task(stack, token: str, source_type: str) -> int:
    """A task in ``token``: ``web`` is a member's turn, ``cli`` one nobody
    asked in the room."""
    result = stack.exec(
        ["uv", "run", "istota", "-c", CONFIG, "task", f"{REQUEST_MARK} probe the room",
         "-u", USER, "--source-type", source_type, "-t", token],
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r"Task created:\s*(\d+)", result.stdout)
    assert match, result.stdout
    return int(match.group(1))


def _observe(stack, task_id: int, mark: str = MARK) -> dict[str, str]:
    task = stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
    transcript = stack.endpoint.transcript()
    begin = transcript.find(f"{mark}_BEGIN")
    end = transcript.find(f"{mark}_END", begin + 1)
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
    stack.exec(["sh", "-c", f"rm -rf {USER_DIR}/.moved-bot; true"])


class TestAMembersTurnInASharedRoom:
    """The regression for ISSUE-576, in a namespace: nothing is withheld."""

    @pytest.mark.script(SCRIPT)
    def test_the_workspace_room_media_and_per_user_temp_are_there(self, shared_rooms):
        facts = _observe(shared_rooms, _task(shared_rooms, SHARED, "web"))
        report = _show(facts)
        assert facts["user_dir"] == "yes", report
        assert facts["files_writable"] == "yes", report
        assert facts["talk_dir"] == "no", report
        assert facts["room_media"].strip("[] ") == SHARED, report
        assert facts["deferred"] == USER, report
        # Out of the prompt, not out of reach: the files are on disk.
        assert facts["memory_readable"] == "yes", report

    @pytest.mark.script(SCRIPT)
    def test_a_group_every_reader_is_in_is_bound_and_no_other(self, shared_rooms):
        facts = _observe(shared_rooms, _task(shared_rooms, SHARED, "web"))
        report = _show(facts)
        assert facts["room_media"].strip("[] ") == SHARED, report
        assert facts["groups_entries"].strip("[] ") == "fam", report
        assert facts["fam_writable"] == "yes", report
        assert facts["other_group"] == "no", report

    @pytest.mark.script(SCRIPT)
    def test_a_room_with_a_reader_outside_the_group_binds_no_group(self, shared_rooms):
        facts = _observe(shared_rooms, _task(shared_rooms, OUTSIDER, "web"))
        assert facts["room_media"].strip("[] ") == OUTSIDER, _show(facts)
        assert facts["user_dir"] == "yes", _show(facts)
        assert "No such file" in facts["groups_entries"], _show(facts)


class TestATaskNobodyAskedInTheRoom:
    """A `cli` task, like a subtask, lands its answer in the room with no
    member asking in it, so it runs at room-safe reach."""

    @pytest.mark.script(SCRIPT)
    def test_personal_workspace_and_per_user_temp_are_not_there(self, shared_rooms):
        facts = _observe(shared_rooms, _task(shared_rooms, SHARED, "cli"))
        report = _show(facts)
        assert facts["user_dir"] == "no", report
        assert facts["talk_dir"] == "no", report
        assert facts["room_media"].strip("[] ") == SHARED, report
        assert facts["deferred"] == f"room-task-{facts['_task_id']}", report
        assert "planted-sibling.txt" not in facts["temp_entries"], report
        assert facts["memory_readable"] == "no", report


@parity.witness(1)
class TestAGuestsTurn:
    @pytest.mark.script(SCRIPT)
    def test_it_reaches_no_workspace_and_no_group(self, shared_rooms):
        task_id = _seed(shared_rooms, "guest-task")["task_id"]
        facts = _observe(shared_rooms, task_id)
        report = _show(facts)
        assert facts["user_dir"] == "no", report
        assert facts["talk_dir"] == "no", report
        assert facts["room_media"].strip("[] ") == GUEST, report
        assert facts["deferred"] == f"emissary-task-{task_id}", report
        assert "No such file" in facts["groups_entries"], report
        assert facts["memory_readable"] == "no", report


class TestThePrivateRoomControl:
    """The same probe, in testuser's own room, in the same session.

    Every personal-path absence above is equally true of a stack that never had the files or
    a task whose sandbox bound none of them. Here the same paths are present
    and readable. Flat Talk stays absent on this local workspace.
    """

    @pytest.mark.script(SCRIPT)
    def test_everything_withheld_above_is_reachable_here(self, shared_rooms):
        facts = _observe(shared_rooms, _task(shared_rooms, PRIVATE, "cli"))
        report = _show(facts)
        assert facts["user_dir"] == "yes", report
        assert facts["talk_dir"] == "no", report
        assert facts["memory_readable"] == "yes", report
        assert facts["room_media"].strip("[] ") == PRIVATE, report
        assert "USER.md" in facts["config_entries"], report
        assert "planted-sibling.txt" in facts["temp_entries"], report
        assert "fam" in facts["groups_entries"] and facts["other_group"] == "yes", report


#: A group for the host, so a turn that binds `Groups/` has something to bind.
#: No rooms: `SEED`'s private room would change where every email note on this
#: session's `email` stack goes.
GROUP_SEED = f"""
from pathlib import Path
from istota import db
from istota.config import load_config

config = load_config(Path({CONFIG!r}))
with db.get_db(config.db_path) as conn:
    if db.get_group(conn, "fam", include_archived=True) is None:
        db.create_group(conn, "fam", kind="family", display_name="fam", created_by="test")
        db.add_group_member(conn, "fam", {USER!r}, added_by="test")
"""

#: One pass of what the scheduler's cron tick does for this user, so "no such
#: job" does not depend on having waited out a tick.
CRON_SYNC = f"""
from pathlib import Path
from istota import db
from istota.config import load_config
from istota.cron_loader import load_cron_jobs, sync_cron_jobs_to_db

config = load_config(Path({CONFIG!r}))
jobs = load_cron_jobs(config, {USER!r})
if jobs is not None:
    with db.get_db(config.db_path) as conn:
        sync_cron_jobs_to_db(conn, {USER!r}, jobs, is_admin=False)
"""

CRON_MD = f"{BOT}/config/CRON.md"


def _mail_probe(nonce: str, job: str) -> str:
    """`FACTS`, then every CLI the turn might reach, then a CRON.md write.

    The write goes last and to every CRON.md the task can find, because its
    `mkdir` can make the path exist inside the namespace, which the presence
    facts must not see. Whether it reached anything is read from outside the
    sandbox, by the job it would have made (#873).
    """
    return f"""
M=CORR{nonce}
echo "${{M}}_BEGIN"
{FACTS}
refusal() {{ tr '\\n' ' ' | grep -o "$1" | head -1; }}
echo "email_list=$(istota-skill email list 2>&1 | refusal not_owner_turn)"
echo "email_send=$(istota-skill email send --to outsider@example.com --subject s --body b 2>&1 | refusal not_owner_turn)"
echo "kv=$(istota-skill kv namespaces 2>&1 | refusal "Unknown skill")"
istota-skill email output --body 'answered {nonce}' >/dev/null 2>&1
echo "email_output=$?"
mkdir -p {BOT}/config 2>/dev/null
for f in {CRON_MD} $(find {WORKSPACE} -name CRON.md 2>/dev/null); do
  cat > "$f" 2>/dev/null <<'CRON'
```toml
[[jobs]]
name = "{job}"
cron = "0 3 1 1 *"
prompt = "publish"
publish_shared_kv = "{job}"
```
CRON
done
echo "${{M}}_END"
"""


def _mail_turn(stack, sender: flow.Correspondent, to: str, nonce: str) -> dict[str, str]:
    """One real mail whose task runs the probe, and the facts it reported."""
    files = stack.exec(["sh", "-c", FILES_SEED])
    assert files.returncode == 0, files.stderr
    groups = stack.exec(["uv", "run", "python", "-c", GROUP_SEED], timeout=120)
    assert groups.returncode == 0, groups.stderr
    job = f"witness-{nonce}"
    stack.script([flow.route(nonce, [
        {"tool_calls": [{"id": f"call-probe-{nonce}", "name": "Bash",
                         "arguments": {"command": _mail_probe(nonce, job)}}]},
        {"text": "I looked around"},
    ])])
    subject = f"Probe {nonce}"
    sent = flow.send(
        stack, sender, to=[to], subject=subject, text=f"Please look {nonce}", marker=nonce,
    )
    row = flow.filed_row(stack, sent)
    assert row["task_id"] is not None, row
    facts = _observe(stack, row["task_id"], mark=f"CORR{nonce}")
    assert flow.worker_done(stack, row["task_id"])
    reply = flow.wait_for_reply(stack, after_uid=sent.outbox_uid, subject=f"Re: {subject}")
    facts["_job"] = job
    facts["_reply"] = reply.body_text or ""
    return facts


def _job_rows(stack, job: str) -> list[dict]:
    sync = stack.exec(["uv", "run", "python", "-c", CRON_SYNC], timeout=120)
    assert sync.returncode == 0, f"{sync.stderr}\n{stack.logs(200)}"
    return stack.probe.query(
        "SELECT name, publish_shared_kv FROM scheduled_jobs WHERE user_id = ? AND name = ?",
        [USER, job],
    )


@pytest.mark.profile("email")
class TestACorrespondentsTurn:
    """A trusted sender at the host's plus-address, through the real intake."""

    def _turn(self, stack, email_people) -> dict[str, str]:
        assert email_people.host_id == USER
        nonce = flow.new_nonce()
        return _mail_turn(
            stack, flow.person("trusted", nonce), mail.tagged(USER), nonce,
        )

    def test_it_reaches_no_workspace_no_group_and_no_other_room(self, stack, email_people):
        facts = self._turn(stack, email_people)
        report = _show(facts)
        assert facts["user_dir"] == "no", report
        assert facts["memory_readable"] == "no", report
        assert facts["files_writable"] == "no", report
        assert facts["talk_dir"] == "no", report
        assert facts["room_media"].strip("[] ") == "", report
        assert facts["deferred"] == f"room-task-{facts['_task_id']}", report
        assert "planted-sibling.txt" not in facts["temp_entries"], report
        assert "No such file" in facts["groups_entries"], report

    def test_a_cron_md_it_writes_makes_no_job(self, stack, email_people):
        """The #873 witness: the file would run as the host, with no turn to check."""
        facts = self._turn(stack, email_people)
        on_disk = stack.exec(["sh", "-c", f"cat {CRON_MD} 2>/dev/null; true"])
        assert facts["_job"] not in on_disk.stdout, _show(facts)
        assert _job_rows(stack, facts["_job"]) == [], _show(facts)

    def test_the_email_cli_answers_only_output_through_the_live_proxy(self, stack, email_people):
        facts = self._turn(stack, email_people)
        report = _show(facts)
        assert facts["email_list"] == "not_owner_turn", report
        assert facts["email_send"] == "not_owner_turn", report
        assert facts["kv"] == "Unknown skill", report
        assert facts["email_output"] == "0", report
        assert "answered" in facts["_reply"], report

    def test_a_widened_skill_reaches_the_proxy(self, stack, email_people):
        people.run_istota(stack, "user", "ensure", "--name", USER, "--correspondent-scope", "kv")
        try:
            facts = self._turn(stack, email_people)
        finally:
            people.run_istota(stack, "user", "ensure", "--name", USER, "--clear-correspondent-scopes")
        report = _show(facts)
        assert facts["kv"] == "", report
        assert facts["user_dir"] == "no", report
        assert facts["email_list"] == "not_owner_turn", report

    def test_the_hosts_own_mail_gets_the_opposite_answers(self, stack, email_people):
        """The control. Every absence above is equally true of a probe that
        never ran its write, or a stack with no `Users/testuser`."""
        assert email_people.host_id == USER
        nonce = flow.new_nonce()
        facts = _mail_turn(stack, flow.person("host", nonce), mail.BOT_ADDRESS, nonce)
        report = _show(facts)
        try:
            assert facts["user_dir"] == "yes", report
            assert facts["memory_readable"] == "yes", report
            assert facts["files_writable"] == "yes", report
            assert facts["deferred"] == USER, report
            assert "fam" in facts["groups_entries"], report
            assert facts["email_list"] == "", report
            assert facts["email_send"] == "", report
            assert facts["kv"] == "", report
            assert _job_rows(stack, facts["_job"]) == [
                {"name": facts["_job"], "publish_shared_kv": facts["_job"]},
            ], report
        finally:
            empty = stack.exec(["sh", "-c", f"printf '```toml\\n```\\n' > {CRON_MD}"])
            assert empty.returncode == 0, empty.stderr
            assert _job_rows(stack, facts["_job"]) == [], report
