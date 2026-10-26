"""Assignments end to end on the local-mode stack: default, charter, wakes, close.

The default suite holds each half of an assignment against a fake clock and a
fake brain. Here every seam is the stack's own: the `[assignments]` section goes
in through `istota-stack apply` and the restart it decides, the user's turns are
web chat messages in a private room the case creates, the approval is the web
confirm route, the wakes are created by the scheduler's `assignments` gate and
run by the native brain in the sandbox, and the scripted model on the Mac
answers each turn with one Bash call. Nothing inside the stack is patched; the
case reads the framework DB from inside the daemon container.

1. With no experimental flag, `istota-skill assignment list` answers `ok`, and
   the migration has run (the two `assignment_id` columns, and the request
   ledger's `kind` CHECK admitting `assignment_activation`).
2. A one-minute wake floor, so the inbox wake is not held for fifteen, through
   `istota-stack apply`.
3. A user turn runs `create`, two `item add`s (a `contains` check on a
   deliverable, a confirmed item) and `activate`; the task parks on the charter
   preview; the web confirm approves it; the assignment is `sleeping`, and the
   re-run's `activate` answers `approved`.
4. Wake 1 (the gate, `fired_by = time`) writes the deliverable and the
   scratchpad, hands a step to the user and asks to wait for them. The wake
   ends, the item passes, the state is `waiting`, a `handoff` notice is open,
   and the wake put nothing in any room.
5. A user turn reports the hand-off done; wake 2 (`fired_by = inbox`) proposes
   completion; the state waits on `completion` with a `completion` notice; a
   user turn closes it `--completed`, and a `closed` notice is written.
6. The sandbox: the wake's mounts are the private task's, less the per-room
   and per-task paths, and the framework DB's directory is the empty mask.

The `[assignments]` section is put back as it was at the end.
Each session's assignment has a fresh slug; old ones stay in the VM's DB, which
is why the case raises `max_active_per_user` while it runs.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid

import pytest

from . import lima
from .web import API, WebSession, db_read, sign_in

pytestmark = pytest.mark.vm

#: The gate runs on `briefing_check_interval` (60s); a wake is one Bash call.
WAKE_SECONDS = 360
TASK_SECONDS = 300
#: What the case sets in `[assignments]` while it runs.
ASSIGNMENTS = {"min_wake_interval_minutes": 1, "max_active_per_user": 50}

#: Read from inside a task: whether the framework DB is reachable, and every
#: mount point the namespace has.
SANDBOX_PROBE = """
echo PROBE_BEGIN
if [ -e /data/db/istota.db ]; then echo framework_db=visible; else echo framework_db=absent; fi
echo db_dir_entries=$(ls -A /data/db 2>/dev/null | wc -l)
echo mounts=$(awk '{ print $5 }' /proc/self/mountinfo | sort -u | tr '\\n' ' ')
echo PROBE_END
"""


def _config(vm: lima.Vm) -> dict:
    return json.loads(vm.out(
        f"python3 -c 'import json,tomllib;print(json.dumps(tomllib.load("
        f"open(\"{lima.STACK}/config/config.toml\",\"rb\"))))'"
    ))


def _set_assignments(vm: lima.Vm) -> None:
    lima.apply_config(vm, {"assignments": ASSIGNMENTS})
    lima.wait_healthy(vm)


def _restore(vm: lima.Vm, original: dict) -> None:
    delta = {key: original[key] for key in ("assignments",) if key in original}
    drop = tuple(key for key in ("assignments",) if key not in original)
    lima.apply_config(vm, delta, drop=drop)
    lima.wait_healthy(vm)


def _row(vm: lima.Vm, sql: str, *args) -> dict:
    """One row as a dict, through json_object so a single column carries it."""
    raw = db_read(vm, sql, *[str(a) for a in args])
    return json.loads(raw) if raw else {}


def _task_status(vm: lima.Vm, task_id: int) -> str:
    return db_read(vm, "SELECT status FROM tasks WHERE id = ?", str(task_id))


def _wait_task(vm: lima.Vm, task_id: int, statuses: tuple[str, ...], *, timeout: float = TASK_SECONDS) -> str:
    seen = {"status": ""}

    def done() -> bool:
        seen["status"] = _task_status(vm, task_id)
        return seen["status"] in statuses

    try:
        lima.wait_for(done, timeout=timeout, what=f"task {task_id} to reach {statuses}")
    except lima.VmError:
        logs = db_read(vm, "SELECT group_concat(level || ': ' || message, char(10)) FROM task_logs "
                       "WHERE task_id = ?", str(task_id))
        error = db_read(vm, "SELECT error FROM tasks WHERE id = ?", str(task_id))
        raise AssertionError(f"task {task_id} is {seen['status']!r}, not {statuses}; error {error!r}\n{logs}")
    return seen["status"]


def _bash(command: str) -> dict:
    return {"tool_calls": [{"id": f"call-{uuid.uuid4().hex[:8]}", "name": "Bash",
                            "arguments": {"command": command}}]}


def _between(text: str, begin: str, end: str) -> str:
    start = text.find(begin)
    stop = text.find(end, start + len(begin))
    assert start >= 0 and stop >= 0, f"no {begin}..{end} in:\n{text[:3000]}"
    return text[start + len(begin):stop].strip()


def _json_lines(text: str) -> list[dict]:
    """Every JSON object a skill call printed; an error is one line, an answer is indented."""
    decoder = json.JSONDecoder()
    found = []
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return found
        if start and text[start - 1] != "\n":
            position = start + 1
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            position = start + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        position = end


def _normalized_mounts(probe: dict[str, str], *, room_token: str = "") -> set[str]:
    """Mount points with task ids taken out, less the room's own channel directory."""
    out = set()
    for path in probe["mounts"].split():
        if room_token and path.endswith(f"/Channels/{room_token}"):
            continue
        out.add(re.sub(r"\d+", "N", path))
    return out


class Scenario:
    """What one session's run has established, step by step."""

    def __init__(self, vm: lima.Vm, model, web: WebSession, room: dict) -> None:
        self.vm = vm
        self.model = model
        self.web = web
        self.room = room
        self.nonce = uuid.uuid4().hex[:10]
        self.slug = f"vm-tier-{self.nonce}"
        self.title = f"VM tier {self.nonce}"
        #: Written by wake 1, checked by the `contains` item.
        self.token = f"answer-{self.nonce}"
        #: Reported with the hand-off; only wake 2's inbox carries it.
        self.wake2 = f"wake-two-{self.nonce}"
        self.assignment_id = 0
        self.action_id = 0
        self.wake_tasks: list[int] = []
        self.results: dict[str, list[str]] = {}
        self.seen_calls: set[tuple[str, str]] = set()
        self.probes: dict[str, dict[str, str]] = {}
        self.system_prompts: dict[str, str] = {}
        self.failed = ""

    # -- the script

    def marker(self, name: str) -> str:
        return f"[e2e:{name}-{self.nonce}]"

    def directory(self) -> str:
        return (f'"$ISTOTA_WORKSPACE_PATH/Users/$ISTOTA_USER_ID/$ISTOTA_BOT_DIR_NAME/'
                f'assignments/{self.slug}"')

    def routes(self) -> list[dict]:
        cli = "istota-skill assignment"
        charter = (
            f"if ! {cli} show {self.slug} >/dev/null 2>&1; then\n"
            f"  {cli} create --title {shlex.quote(self.title)}"
            f" --goal 'Write the answer into result.md and have the user check it.'"
            f" --autonomy 'May write files in this assignment folder.'"
            f" --budget-usd 5 --max-wakes 10\n"
            f"  {cli} item add {self.slug} --text 'result.md carries the answer' --kind mechanical"
            f" --check contains --file result.md --text-match {self.token}\n"
            f"  {cli} item add {self.slug} --text 'The user has read result.md' --kind confirmed\n"
            f"fi\n"
            f"echo ACTIVATE_BEGIN; {cli} activate {self.slug}; echo ACTIVATE_END\n"
            + SANDBOX_PROBE
        )
        wake1 = (
            f"set -e\nd={self.directory()}\n"
            f"printf 'The answer is {self.token}.\\n' > \"$d/result.md\"\n"
            f"printf '\\n- wake 1 wrote result.md\\n' >> \"$d/SCRATCHPAD.md\"\n"
            f"echo HANDOFF_BEGIN; {cli} handoff 'Read result.md and say whether it reads right'"
            f" --report 'whether it reads right'; echo HANDOFF_END\n"
            f"echo NEXT_BEGIN; {cli} next --wait user --progress 'result.md written'; echo NEXT_END\n"
            + SANDBOX_PROBE
        )
        wake2 = (
            f"echo PAUSE_BEGIN; {cli} pause {self.slug}; echo PAUSE_END\n"
            f"echo COMPLETE_BEGIN; {cli} complete --evidence 'result.md carries the answer'; echo COMPLETE_END\n"
        )
        done = (f"{cli} handoff done {self.slug} {self.action_id or 0}"
                f" --report {shlex.quote(self.wake2 + ' it reads right')}")
        close = f"{cli} close {self.slug} --completed"
        return [
            {"when": self.marker("charter"), "turns": [_bash(charter), {"text": "Approve the charter above."}]},
            # A heading only a wake's user half has; the charter preview a
            # confirmed re-run may carry has `## Done when`, never this.
            {"when": "## Open hand-offs", "turns": [_bash(wake1), {"text": f"wake one finished {self.nonce}"}]},
            # Furthest right wins: the hand-off report sits in `## Inbox`, after the board.
            {"when": self.wake2, "turns": [_bash(wake2), {"text": f"wake two finished {self.nonce}"}]},
            {"when": self.marker("done"), "turns": [_bash(done), {"text": "Reported it done."}]},
            {"when": self.marker("close"), "turns": [_bash(close), {"text": "Closed it."}]},
        ]

    def rescript(self) -> None:
        self.collect()
        self.model.rescript(self.routes())

    def tool_outputs(self) -> list[tuple[tuple[str, str], str]]:
        """Every Bash result, keyed by call id and content.

        Not `tool_results_by_id`: a confirmed re-run replays its route from the
        first turn, so the same call id answers twice with different output.
        """
        out = []
        for body in list(self.model.requests):
            for message in body.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "tool":
                    content = str(message.get("content"))
                    out.append(((str(message.get("tool_call_id")), content), content))
        return out

    def collect(self) -> None:
        """Keep what the endpoint saw so far; a rescript clears it."""
        for key, result in self.tool_outputs():
            if key in self.seen_calls:
                continue
            self.seen_calls.add(key)
            for name in ("ACTIVATE", "HANDOFF", "NEXT", "PAUSE", "COMPLETE"):
                if f"{name}_BEGIN" in result:
                    self.results.setdefault(name, []).append(_between(result, f"{name}_BEGIN", f"{name}_END"))
            if "PROBE_BEGIN" in result:
                key = "wake" if "HANDOFF_BEGIN" in result else "user"
                self.probes.setdefault(key, lima.marked_block(result))
        for prompt in self.model.messages_by_role("system"):
            if "## Assignment" in prompt:
                self.system_prompts.setdefault("wake", prompt)

    # -- the user's side

    def say(self, text: str) -> int:
        response = self.web.post(f"{API}/chat/rooms/{self.room['id']}/messages", json={"text": text})
        assert response.status_code == 200, response.text[:2000]
        task_id = response.json().get("task_id")
        assert task_id, response.text[:2000]
        return int(task_id)

    def assignment(self) -> dict:
        return _row(self.vm, "SELECT json_object('id', id, 'state', state, 'state_reason', state_reason, "
                    "'waiting_on', waiting_on, 'next_wake_at', next_wake_at) FROM assignments "
                    "WHERE user_id = ? AND slug = ?", lima.USER, self.slug)

    def wakes(self) -> list[dict]:
        raw = db_read(self.vm, "SELECT json_group_array(json_object('id', id, 'seq', seq, 'task_id', task_id, "
                      "'fired_by', fired_by, 'ended_at', ended_at, 'outcome', outcome, "
                      "'progress_signals', progress_signals)) FROM (SELECT * FROM assignment_wakes "
                      "WHERE assignment_id = ? ORDER BY seq)", str(self.assignment_id))
        return json.loads(raw or "[]")

    def notices(self) -> dict[str, str]:
        raw = db_read(self.vm, "SELECT json_group_object(dedup_key, state) FROM notifications "
                      "WHERE user_id = ? AND source = 'assignment' AND dedup_key LIKE ?",
                      lima.USER, f"%:{self.assignment_id}%")
        return json.loads(raw or "{}")

    def wait_wake(self, seq: int) -> dict:
        def ended() -> bool:
            wakes = self.wakes()
            return len(wakes) >= seq and bool(wakes[seq - 1]["ended_at"])

        try:
            lima.wait_for(ended, timeout=WAKE_SECONDS, interval=5, what=f"wake {seq} to end")
        except lima.VmError:
            raise AssertionError(
                f"wake {seq} did not end: assignment {self.assignment()}, wakes {self.wakes()}, "
                f"unmatched model requests {self.model.marked_unmatched() or self.model.unmatched}")
        wake = self.wakes()[seq - 1]
        self.wake_tasks.append(int(wake["task_id"]))
        return wake

    def messages_of(self, task_id: int) -> int:
        return int(db_read(self.vm, "SELECT COUNT(*) FROM messages WHERE task_id = ?", str(task_id)) or 0)

    def room_messages(self) -> int:
        return int(db_read(self.vm, "SELECT COUNT(*) FROM messages WHERE room_token = ?",
                           self.room["token"]) or 0)


@pytest.fixture(scope="module")
def stack(local_stack):
    vm = local_stack
    original = _config(vm)
    try:
        yield vm
    finally:
        _restore(vm, original)


@pytest.fixture(scope="module")
def scenario(stack, model):
    web = WebSession()
    try:
        sign_in(stack, web)
        created = web.post(f"{API}/chat/rooms", json={"name": f"vm tier assignments {uuid.uuid4().hex[:6]}"})
        assert created.status_code == 200, created.text[:2000]
        yield Scenario(stack, model, web, created.json())
    finally:
        web.close()


def _step(scenario: Scenario, name: str):
    if scenario.failed:
        pytest.skip(f"an earlier step failed: {scenario.failed}")

    class _Guard:
        def __enter__(self):
            return self

        def __exit__(self, kind, value, tb):
            if kind is not None and not issubclass(kind, pytest.skip.Exception):
                scenario.failed = name
            return False

    return _Guard()


def test_with_no_flag_the_cli_answers_and_the_migration_has_run(scenario):
    with _step(scenario, "default"):
        vm = scenario.vm
        for table in ("tasks", "task_usage"):
            assert db_read(vm, f"SELECT COUNT(*) FROM pragma_table_info('{table}') "
                               "WHERE name = 'assignment_id'") == "1", f"{table}.assignment_id is missing"
        ledger = db_read(vm, "SELECT sql FROM sqlite_master WHERE name = 'whatsapp_skill_requests'")
        assert "assignment_activation" in ledger, ledger
        assert db_read(vm, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'assignment_wakes'") == "1"
        printed = lima.run_probe_task(vm, scenario.model, "istota-skill assignment list")
        answers = _json_lines(printed)
        assert answers and answers[-1].get("status") == "ok", printed


def test_an_admin_charters_and_activates_and_the_approval_starts_it(scenario):
    with _step(scenario, "charter"):
        vm = scenario.vm
        _set_assignments(vm)
        assert _config(vm)["assignments"]["min_wake_interval_minutes"] == 1
        scenario.rescript()
        task_id = scenario.say(f"{scenario.marker('charter')} Draft the assignment and start it.")
        _wait_task(vm, task_id, ("pending_confirmation", "completed", "failed", "cancelled"))
        assert _task_status(vm, task_id) == "pending_confirmation"
        held = scenario.assignment()
        assert held["state"] == "draft", held
        scenario.assignment_id = int(held["id"])
        prompt = db_read(vm, "SELECT confirmation_prompt FROM tasks WHERE id = ?", str(task_id))
        assert scenario.title in prompt and scenario.token in prompt, prompt
        kind = db_read(vm, "SELECT r.kind FROM whatsapp_skill_requests r JOIN tasks t "
                           "ON t.whatsapp_confirmation_request_id = r.id WHERE t.id = ?", str(task_id))
        assert kind == "assignment_activation", kind

        confirmed = scenario.web.post(f"{API}/chat/tasks/{task_id}/confirm", json={"room": scenario.room["token"]})
        assert confirmed.status_code == 200, confirmed.text[:2000]
        _wait_task(vm, task_id, ("completed", "failed", "cancelled"))
        assert _task_status(vm, task_id) == "completed"
        scenario.collect()
        activations = [_json_lines(text)[-1] for text in scenario.results.get("ACTIVATE", [])]
        assert [a.get("status") for a in activations][-2:] == ["held", "approved"], activations
        # Sleeping, or already active if the gate has picked it up.
        assert scenario.assignment()["state"] in ("sleeping", "active", "waiting"), scenario.assignment()
        assert db_read(vm, "SELECT COUNT(*) FROM assignment_events WHERE assignment_id = ? "
                           "AND kind = 'activated'", str(scenario.assignment_id)) == "1"


def test_wake_one_hands_off_and_waits_with_nothing_delivered(scenario):
    with _step(scenario, "wake 1"):
        vm = scenario.vm
        before = scenario.room_messages()
        wake = scenario.wait_wake(1)
        scenario.collect()
        task_id = scenario.wake_tasks[0]
        task = _row(vm, "SELECT json_object('source_type', source_type, 'queue', queue, 'status', status, "
                        "'assignment_id', assignment_id, 'conversation_token', conversation_token, "
                        "'output_target', output_target) FROM tasks WHERE id = ?", task_id)
        assert task == {"source_type": "assignment", "queue": "assignment", "status": "completed",
                        "assignment_id": scenario.assignment_id, "conversation_token": None,
                        "output_target": None}, task
        assert wake["fired_by"] == "time", wake
        assert wake["outcome"] == "next", wake
        handoff = _json_lines(scenario.results["HANDOFF"][-1])[-1]
        assert handoff.get("status") == "ok", handoff
        scenario.action_id = int(db_read(vm, "SELECT id FROM assignment_actions WHERE assignment_id = ? "
                                             "AND kind = 'handoff' AND status = 'open'", str(scenario.assignment_id)))
        items = db_read(vm, "SELECT group_concat(kind || '=' || status, ' ') FROM "
                            "(SELECT * FROM assignment_items WHERE assignment_id = ? ORDER BY position)",
                        str(scenario.assignment_id))
        assert items == "mechanical=passing confirmed=open", items
        state = scenario.assignment()
        assert state["state"] == "waiting" and "user" in (state["waiting_on"] or ""), state
        notices = scenario.notices()
        assert notices.get(f"handoff:{scenario.assignment_id}:{scenario.action_id}") == "open", notices
        assert "item_passing" in wake["progress_signals"] or "deliverable_changed" in wake["progress_signals"], wake
        # Nothing reached a room: not the wake's answer, not a mirror of it.
        assert scenario.messages_of(task_id) == 0
        assert scenario.room_messages() == before
        assert "## Assignment" in scenario.system_prompts.get("wake", ""), "the wake had no assignment section"


def test_the_wake_runs_in_a_private_tasks_sandbox_without_the_db(scenario):
    # Not a step: the close below does not depend on it.
    if not scenario.probes:
        pytest.skip(f"no probe ran: {scenario.failed}")
    wake, user = scenario.probes.get("wake"), scenario.probes.get("user")
    assert wake and user, scenario.probes
    for name, probe in (("wake", wake), ("user turn", user)):
        assert probe["framework_db"] == "absent", (name, probe)
        assert probe["db_dir_entries"] == "0", (name, probe)
    # The one difference: a room turn binds its room's channel directory,
    # and a wake has no room.
    token = scenario.room["token"]
    assert any(p.endswith(f"/Channels/{token}") for p in user["mounts"].split()), user["mounts"]
    assert not any("/Channels/" in p for p in wake["mounts"].split()), wake["mounts"]
    wake_mounts, user_mounts = _normalized_mounts(wake), _normalized_mounts(user, room_token=token)
    assert any("/Users/" in p for p in wake_mounts), sorted(wake_mounts)
    assert wake_mounts == user_mounts, (
        f"only the wake: {sorted(wake_mounts - user_mounts)}\n"
        f"only the user turn: {sorted(user_mounts - wake_mounts)}")


def test_a_reported_handoff_wakes_it_and_a_confirmed_completion_closes_it(scenario):
    with _step(scenario, "wake 2 and close"):
        vm = scenario.vm
        scenario.rescript()  # the hand-off's id is in the script now
        task_id = scenario.say(f"{scenario.marker('done')} I read result.md; mark the assignment step done.")
        _wait_task(vm, task_id, ("completed", "failed", "cancelled"))
        scenario.collect()
        assert _task_status(vm, task_id) == "completed"
        assert db_read(vm, "SELECT status FROM assignment_actions WHERE id = ?", str(scenario.action_id)) == "done"

        wake = scenario.wait_wake(2)
        scenario.collect()
        assert wake["fired_by"] == "inbox", wake
        assert "handoff_done" in wake["progress_signals"], wake
        paused = _json_lines(scenario.results["PAUSE"][-1])[-1]
        assert paused.get("error") == "not_a_user_turn", paused
        completed = _json_lines(scenario.results["COMPLETE"][-1])[-1]
        assert completed.get("status") == "ok", completed
        assert wake["outcome"] == "complete_proposed", wake
        state = scenario.assignment()
        assert state["state"] == "waiting" and "completion" in (state["waiting_on"] or ""), state
        assert scenario.notices().get(f"completion:{scenario.assignment_id}") == "open", scenario.notices()
        assert scenario.messages_of(scenario.wake_tasks[1]) == 0

        task_id = scenario.say(f"{scenario.marker('close')} It is done; close the assignment as completed.")
        _wait_task(vm, task_id, ("completed", "failed", "cancelled"))
        assert _task_status(vm, task_id) == "completed"
        assert scenario.assignment()["state"] == "completed", scenario.assignment()
        notices = scenario.notices()
        assert f"closed:{scenario.assignment_id}" in notices, notices
        assert notices.get(f"completion:{scenario.assignment_id}") != "open", notices
        assert notices.get(f"handoff:{scenario.assignment_id}:{scenario.action_id}") != "open", notices
        time.sleep(1)
        assert len(scenario.wakes()) == 2, scenario.wakes()
