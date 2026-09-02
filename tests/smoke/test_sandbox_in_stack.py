"""A Bash tool call, run through the real sandbox, inside the shipped image.

`test_lean_stack.py` proves a task reaches the model and comes back. It never
runs a tool, so it never touches bubblewrap — and every forge scenario does,
because the wrapper is something the model *execs*. That gap is what this file
closes, and it is worth its own file because when it fails, nothing in
`test_forge_e2e.py` can pass and none of those failures name the reason.

The scripted model issues one `Bash` call and then answers. What is asserted is
that the command's output came back, which is only true if bwrap built a
namespace, mounted the task's view, and ran the shell inside it.

**"The output came back" is not a witness that the sandbox ran, and this file
used to claim it was.** A task whose sandbox was skipped runs the same command
through the same shell and returns the same bytes, so the first two scenarios
below pass identically with bwrap disabled — which is exactly the state the
deployment was in when Stage 7 measured it (`_bwrap_available` probed without
`--unshare-user`, which is the one thing that works as root in a container
without CAP_SYS_ADMIN). What distinguishes the two is `TestTheDatabaseMasks`:
`build_bwrap_cmd` ends by covering `db_path.parent` with an empty read-only
tmpfs, and *that* is visible from inside the task and cannot be produced by a
task running unconfined.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

# One turn that runs a command, then one that answers with what it saw. The
# agent loop needs the second: a turn ending in `tool_calls` is a request for
# another round, and the scripted endpoint answers an unscripted round with an
# error frame rather than replaying.
SANDBOX_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": "echo SANDBOX_RAN_THIS"},
            }
        ]
    },
    {"text": "the command produced SANDBOX_RAN_THIS"},
]

#: `db_path` is `/data/db/istota.db` on both container shapes
#: (`render-config.sh:121`), so the directory `build_bwrap_cmd` masks is this.
#: `module_db_root()` derives as `{db_path.parent}/modules` and is therefore
#: already covered — `_mask_dir` skips a candidate an earlier mask contains,
#: because a nested mask makes bwrap fail every task rather than one directory.
CONTAINER_DB_DIR = "/data/db"

# Five facts about one directory, read from inside a live task. Not one: an
# `ls` that comes back empty is equally true of a directory that is masked, a
# directory that is empty, and a path that is not in the namespace at all — and
# the first version of this probe would have passed on the third.
#
# `stat -f -c %T` is the positive half. Unmasked, `/data/db` is the
# `istota_test_db` named volume and reports `ext2/ext3`; masked it reports
# `tmpfs`, because the mask *is* a tmpfs. So the probe fails loudly if the path
# vanished (stat prints an error, and `fstype=` carries it into the assertion)
# rather than reading an absence as a boundary.
#
# `writable=` is the other half of `--remount-ro`, and it is what tells a real
# mask from a `--tmpfs` somebody forgot to remount: on a writable mask a
# `sqlite3` probe *creates* a zero-byte file and then answers "no such table",
# which reads as a corrupt database rather than as a boundary.
#
# Markers around the block because the tool result reaches the assertion inside
# a whole request transcript, and a bare `tmpfs` substring would match anything.
MASK_PROBE = f"""
echo MASK_PROBE_BEGIN
echo "fstype=$(stat -f -c %T {CONTAINER_DB_DIR} 2>&1)"
echo "entries=[$(ls -A {CONTAINER_DB_DIR} 2>&1 | tr '\\n' ' ')]"
if cat {CONTAINER_DB_DIR}/istota.db > /dev/null 2>&1; then
  echo "framework_db=readable"
else
  echo "framework_db=unreadable"
fi
if touch {CONTAINER_DB_DIR}/mask-probe 2>/dev/null; then
  echo "writable=yes"
  rm -f {CONTAINER_DB_DIR}/mask-probe
else
  echo "writable=no"
fi
echo MASK_PROBE_END
"""

MASK_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": MASK_PROBE},
            }
        ]
    },
    {"text": "I looked at the database directory"},
]


def probe_output(stack) -> str:
    """The marked block out of the endpoint transcript, or a readable failure.

    Through `transcript()` rather than `requests`, for the reason the scenario
    below it gives: it takes the endpoint's lock, and the daemon's own tasks
    keep running after `wait_for_task` returns.
    """
    transcript = stack.endpoint.transcript()
    begin = transcript.find("MASK_PROBE_BEGIN")
    end = transcript.find("MASK_PROBE_END", begin + 1)
    if begin < 0 or end < 0:
        raise AssertionError(
            "the mask probe's output never reached the model, so the Bash tool "
            "did not run or its result was not sent back — this says nothing "
            "about the masks either way\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
    return transcript[begin:end]


class TestTheSandboxRunsInsideTheContainer:
    @pytest.mark.script(SANDBOX_SCRIPT)
    def test_a_bash_tool_call_completes(self, stack):
        task_id = stack.submit("run a command for me")

        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=180
        )

        assert task["status"] == "completed", (
            f"task {task_id} ended {task['status']!r}: {task.get('error')!r}\n"
            "If this says bwrap could not create a namespace, the compose file's "
            "`security_opt: seccomp:unconfined` is missing or was not applied.\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )

    @pytest.mark.script(SANDBOX_SCRIPT)
    def test_the_commands_output_came_back_to_the_model(self, stack):
        """The assertion that distinguishes "ran" from "reported success".

        A task completes whether or not the tool produced anything — the second
        scripted turn answers regardless. The proof is that the *tool result*
        the daemon sent back to the endpoint carries the command's stdout,
        which nothing but a real execution puts there.
        """
        task_id = stack.submit("run a command for me")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        # `transcript()` rather than iterating `requests`: it takes the
        # endpoint's lock, and the daemon's own tasks keep running after
        # `wait_for_task` returns, so handler threads may still be appending.
        # It also tolerates a body with no `messages` key, which indexing does
        # not.
        assert "SANDBOX_RAN_THIS" in stack.endpoint.transcript(), (
            "no request carried the command's output back; the Bash tool did "
            "not run, or its result never reached the model\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )


class TestTheDatabaseMasks:
    """The boundary, observed from inside a live task in the shipped artifact.

    `build_bwrap_cmd` ends by covering `db_path.parent` and `module_db_root()`
    with an empty, read-only tmpfs — the last mount operations, so no earlier
    bind shows through. Until now that was asserted two ways, neither of them
    in a deployment: as argv in the default suite (which patches
    `_bwrap_available` and never runs bwrap) and as namespace contents in
    `tests/linux/` (which needs a real kernel and is not the shipped image).

    The lean stack runs a real bwrap under `seccomp:unconfined`, so a scripted
    Bash call can read the directory the daemon says it masked. That is the
    difference between "the code would emit `--tmpfs`" and "the model cannot
    see the database".

    Nothing here asserts anything about *nested* user namespaces, and the
    reason changed under this file rather than going away. `--disable-userns`
    needs a writable `/proc/sys`, which a container does not have by default —
    but both shapes now grant `systempaths=unconfined`, without which bwrap
    cannot mount a procfs inside its own user namespace at all, and that grant
    makes `/proc/sys` writable as a side effect. So
    `executor._bwrap_supports_disable_userns` finds the flag supported here and
    it does reach the real argv. Nothing asserts on it either way: that is the
    spec's decision about scope, not a statement about what the argv contains.
    """

    @pytest.mark.script(MASK_SCRIPT)
    def test_the_database_directory_is_an_empty_read_only_tmpfs(self, stack):
        task_id = stack.submit("look at the database directory")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = probe_output(stack)

        # All four together, because each alone has a false pass. A `tmpfs`
        # that is not empty is a mask with something bound over it; an empty
        # directory that is not a tmpfs is a database that has not been created
        # yet; an unreadable file could be a permissions accident.
        assert "fstype=tmpfs" in observed, (
            f"{CONTAINER_DB_DIR} inside the task is not a tmpfs, so the "
            "sandbox's database mask is not in the namespace — either bwrap "
            "was skipped (check the daemon log for `Sandbox enabled but "
            "bubblewrap unavailable`) or the mask was not emitted.\n"
            f"--- probe ---\n{observed}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "entries=[]" in observed, (
            f"{CONTAINER_DB_DIR} is a tmpfs but is not empty; something is "
            f"mounted over the mask\n--- probe ---\n{observed}"
        )
        assert "framework_db=unreadable" in observed, (
            "the framework database is readable from inside the task\n"
            f"--- probe ---\n{observed}"
        )
        assert "writable=no" in observed, (
            "the mask is writable, so a `sqlite3` probe against it will create "
            "a zero-byte file and answer `no such table` — which reads as a "
            "corrupt database rather than as a boundary. `--remount-ro` was "
            f"not applied.\n--- probe ---\n{observed}"
        )

    def test_the_database_is_there_to_be_masked(self, stack):
        """The control, and it runs in the same session rather than by hand.

        Everything above is an absence, and an absence proves nothing without
        evidence that the thing was present to begin with. This reads the same
        directory through `docker compose exec`, which is *not* inside the
        sandbox: the daemon's own view has the framework database in it, on the
        named volume, readable. So the previous test is the difference the
        sandbox makes and not a statement about how the image is laid out.
        """
        result = stack.exec(["sh", "-c", MASK_PROBE])

        assert result.returncode == 0, result.stderr
        assert "fstype=tmpfs" not in result.stdout, (
            f"{CONTAINER_DB_DIR} is a tmpfs in the *daemon's* own view, which "
            "means the test above cannot tell a mask from the container's "
            f"ordinary layout\n{result.stdout}"
        )
        assert "framework_db=readable" in result.stdout, (
            "the framework database is not readable from the daemon's own "
            "view either, so the assertion above is not about the sandbox\n"
            f"{result.stdout}"
        )


# --------------------------------------------------------------------------
# The composed system prompt, read from inside a live task in the shipped image
# --------------------------------------------------------------------------

#: A string that exists only in the system half of the composed prompt
#: (`executor.build_rules_section`). Grepped for inside the task and looked for
#: in the endpoint's `role: system` message, which is what ties the file on
#: disk to what the model was actually sent.
COMPOSED_SENTINEL = "## Important rules"

#: Carried in the submitted request, so it is in the *user* half. The probe
#: proves it is absent from the system file: the split is only worth anything
#: if each half holds one thing.
REQUEST_SENTINEL = "SMOKE_COMPOSED_REQUEST_SENTINEL"

#: `ISTOTA_DEFERRED_DIR` is the task temp dir and `ISTOTA_TASK_ID` names the
#: task, both already in the sandbox environment — so this is the executor's
#: own naming convention read back from inside the namespace rather than a
#: path this test invented.
#:
#: Four facts, and each one is needed. `composed=` says the standing
#: instructions are on disk where the brain was told to find them.
#: `request_in_system=` says the user half did not leak into the permanent
#: message. `append=` is the boundary: the file lives inside the task's own
#: read-write temp directory, so only the later `--ro-bind` makes it refuse.
#: And `sibling=` is the control for that one — without it a refusal is
#: equally consistent with the whole directory having been made read-only,
#: which would break every task rather than protect one file.
SYSPROMPT_PROBE = f"""
COMPOSED="$ISTOTA_DEFERRED_DIR/task_${{ISTOTA_TASK_ID}}_system_prompt.txt"
SIBLING="$ISTOTA_DEFERRED_DIR/sysprompt-sibling-probe.txt"
echo SYSPROMPT_PROBE_BEGIN
echo "path=$COMPOSED"
if [ -f "$COMPOSED" ]; then echo "exists=yes"; else echo "exists=no"; fi
if grep -qF '{COMPOSED_SENTINEL}' "$COMPOSED" 2>/dev/null; then
  echo "composed=present"
else
  echo "composed=absent"
fi
if grep -qF '{REQUEST_SENTINEL}' "$COMPOSED" 2>/dev/null; then
  echo "request_in_system=yes"
else
  echo "request_in_system=no"
fi
if echo tampered >> "$COMPOSED" 2>/dev/null; then
  echo "append=accepted"
else
  echo "append=refused"
fi
if echo probe > "$SIBLING" 2>/dev/null; then
  echo "sibling=writable"
  rm -f "$SIBLING"
else
  echo "sibling=refused"
fi
echo SYSPROMPT_PROBE_END
"""

SYSPROMPT_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": SYSPROMPT_PROBE},
            }
        ]
    },
    {"text": "I looked at the composed system prompt"},
]


def marked_block(stack, begin: str, end: str, what: str) -> str:
    """The marked region of the endpoint transcript, or a readable failure."""
    transcript = stack.endpoint.transcript()
    start = transcript.find(begin)
    stop = transcript.find(end, start + 1)
    if start < 0 or stop < 0:
        raise AssertionError(
            f"{what} never reached the model, so the Bash tool did not run or "
            "its result was not sent back — this says nothing about the "
            "property either way\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
    return transcript[start:stop]


class TestTheComposedSystemPromptInTheStack:
    """`task_<id>_system_prompt.txt`, observed from inside a live task.

    Istota's standing instructions travel to the brain as a file so they land
    with system authority rather than as the first user message, which native
    compaction summarizes away (ISSUE-375). The file is written into the task's
    own read-write temp directory and re-bound read-only on top of it, which is
    a property no unit test can observe: the default suite patches
    `_bwrap_available` and asserts argv.

    Read the four probe answers together. Any one of them passes in states the
    others refuse — `append=refused` alone is equally true of a directory that
    was made read-only wholesale, and `composed=present` alone is equally true
    of a file nothing bound at all.
    """

    @pytest.mark.script(SYSPROMPT_SCRIPT)
    def test_the_composed_file_is_readable_but_not_writable(self, stack):
        task_id = stack.submit(
            f"{REQUEST_SENTINEL} look at your own system prompt file"
        )
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = marked_block(
            stack,
            "SYSPROMPT_PROBE_BEGIN",
            "SYSPROMPT_PROBE_END",
            "the composed system prompt probe's output",
        )

        assert "exists=yes" in observed, (
            "the executor wrote no composed system prompt for this task, or it "
            "is not at the path the brain was given\n"
            f"--- probe ---\n{observed}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "composed=present" in observed, (
            "the file exists but carries none of Istota's standing "
            f"instructions\n--- probe ---\n{observed}"
        )
        assert "request_in_system=no" in observed, (
            "the user's request is inside the system file, which would make "
            f"task material permanent rather than summarizable\n"
            f"--- probe ---\n{observed}"
        )
        assert "append=refused" in observed, (
            "the model can append to its own standing instructions — the "
            "later `--ro-bind` is missing, or bwrap was skipped entirely "
            "(check the daemon log for `Sandbox enabled but bubblewrap "
            f"unavailable`)\n--- probe ---\n{observed}"
        )
        assert "sibling=writable" in observed, (
            "a sibling file in the same directory is not writable either, so "
            "the refusal above is a read-only task directory rather than the "
            f"one-file carve-out\n--- probe ---\n{observed}"
        )

    @pytest.mark.script(SYSPROMPT_SCRIPT)
    def test_each_half_reached_the_model_on_its_own_channel(self, stack):
        """The other end of the handoff, from the endpoint's own view.

        The probe says what is on disk. This says what was sent — and the two
        together are the claim, because a file with the right contents that
        reached the model as a user message is the defect wearing a label.
        """
        task_id = stack.submit(
            f"{REQUEST_SENTINEL} look at your own system prompt file"
        )
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        systems = stack.endpoint.messages_by_role("system")
        users = stack.endpoint.messages_by_role("user")

        assert any(COMPOSED_SENTINEL in text for text in systems), (
            "no system message carried Istota's standing instructions, so the "
            "composed file was not passed with system authority\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert any(REQUEST_SENTINEL in text for text in users), (
            "the request never reached a user message\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        # The user turn carries the request and not the instructions. Asserted
        # against the *first* user message rather than all of them: a tool
        # result echoing the probe command is also a message, and it quotes
        # both sentinels by construction.
        first_user = users[0]
        assert REQUEST_SENTINEL in first_user
        assert COMPOSED_SENTINEL not in first_user, (
            "the standing instructions are still on the user turn, where "
            "compaction summarizes them away — the flip did not happen"
        )
