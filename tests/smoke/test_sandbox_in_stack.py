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
# The task control directory, read from inside a live task in the shipped image
# --------------------------------------------------------------------------

#: `temp_dir` is the literal `/data/tmp` on both container shapes
#: (`render-config.sh:130`) and `Stack.submit` submits as `testuser`, so the
#: task temp dir — `ISTOTA_DEFERRED_DIR`, a read-write bind and therefore a
#: `native_fs_roots` write root — is this. It is also the sandbox's `--chdir`
#: target *on this stack*, which is a narrower claim than it looks:
#: `chdir_target = workspace_resolved or user_temp_dir.resolve()`
#: (`executor.py:3973`), so a deployment with a REPL workspace chdirs
#: somewhere else and `control_in_cwd` below would be asking about that
#: directory instead. The `cwd=` answer is what holds the two together.
#:
#: Named as a literal because the `Read` call further down has to carry an
#: absolute path, and a scripted turn is fixed before the task exists. Both
#: probes echo the variable back and the tests compare, so a layout change
#: says so here rather than surfacing as an unexplained "File not found" from
#: the Read tool.
TASK_TEMP_DIR = "/data/tmp/testuser"

#: `{temp_dir}/.control/{user_id}/task_{task_id}` — `executor
#: .get_task_control_dir`, with this stack's `temp_dir` and user id filled in.
#: The task id is appended by the test, because it is only known once the task
#: has been submitted. The probe derives the same path from
#: `$ISTOTA_DEFERRED_DIR` and `$ISTOTA_TASK_ID` rather than being told it, and
#: the test compares the two on a whole line: a probe that computed the wrong
#: directory would otherwise report every absence below as a boundary, and an
#: unanchored prefix match would accept `task_41` for task 4.
#:
#: The probe's derivation is *lexical* (`dirname` of an environment variable)
#: where the product resolves (`Path(temp_dir).resolve()`, `executor.py:449`),
#: so a symlink anywhere in `/data/tmp` would diverge the two. Nothing in
#: either container shape has one, and the divergence is loud rather than
#: silent — it lands on the `control=` comparison, not on a boundary answer.
CONTROL_DIR_PREFIX = "/data/tmp/.control/testuser/task_"

#: A control directory belonging to no task, planted in *this user's* control
#: subtree from the daemon's own view before the task is submitted, and
#: required to be absent from the namespace. This is the isolation half, and
#: without it nothing here would notice a bind widened from the task's own
#: directory to `{temp_dir}/.control` or `{temp_dir}/.control/{user_id}` —
#: every other answer in the probe is unchanged by that, while every other
#: task's assembled prompt (which carries `USER.md` and the channel context)
#: becomes readable. `tests/smoke/test_sandbox_repos_isolation.py` seeds
#: another user's tree the same way and for the same reason.
#:
#: The id is far above anything the stack will reach, so it can never collide
#: with a real task's directory.
NEIGHBOUR_TASK_DIR = "/data/tmp/.control/testuser/task_999999"

#: Written into the planted directory, so "absent" can be told from "present
#: but empty" and so the read half is asserted as well as the stat.
NEIGHBOUR_SENTINEL = "SMOKE_NEIGHBOUR_TASK_CONTROL_SENTINEL"

#: A string that exists only in the system half of the composed prompt
#: (`executor.build_rules_section`). Grepped for inside the task and looked for
#: in the endpoint's `role: system` message, which is what ties the file on
#: disk to what the model was actually sent.
COMPOSED_SENTINEL = "## Important rules"

#: Carried in the submitted request, so it is in the *user* half. The probe
#: proves it is absent from the system file: the split is only worth anything
#: if each half holds one thing.
REQUEST_SENTINEL = "SMOKE_COMPOSED_REQUEST_SENTINEL"

#: `ISTOTA_DEFERRED_DIR` is the task temp dir and the sandbox's `--chdir`
#: target, and `ISTOTA_TASK_ID` names the task; both are already in the
#: sandbox environment. The control directory is derived from them the way
#: `executor.get_task_control_dir` derives it — a sibling `.control` of the
#: per-user directories, then the user id, then `task_<id>` — so this is the
#: executor's own naming convention read back from inside the namespace rather
#: than a path this test invented. The derived path is echoed and the test
#: compares it against `CONTROL_DIR_PREFIX`, because a probe aimed at a
#: directory that does not exist reports every absence below as a boundary.
#:
#: The five the spec's `### The smoke witness` names, each of which passes in
#: a state the others refuse:
#:
#: - `exists=` / `composed=` say the standing instructions are on disk where
#:   the brain was told to find them. Read as one fact: `exists=` is the
#:   cheaper failure message, and it is the answer the first control below
#:   moved. Alone the pair is equally true of a file nothing bound.
#: - `append=` is half the boundary: a write to that same file is refused.
#: - `sibling=` is the control for `append=`. A file in the *per-user temp
#:   dir* stays writable, which is what tells a working control bind from a
#:   task directory made read-only wholesale — that would break every task
#:   rather than protect the framework's files.
#: - `control_in_cwd=` is the positive claim of the move: the model's working
#:   directory holds no prompt half any more, under the retired
#:   `task_<id>_prompt.txt` / `task_<id>_system_prompt.txt` spelling *or* the
#:   new bare one. It deliberately does not match `task_<id>_result.txt`,
#:   which the model writes and which stays there.
#: - `control_dir=` is the per-directory claim the old per-file assertion
#:   could not make: a file the daemon has never written is refused as well,
#:   so anything added to the directory later is covered without a new guard.
#:
#: Three more, each closing a gap the stage review found:
#:
#: - `user_half=` turns `control_in_cwd=` from an absence into a move. Without
#:   it, an `execute_task` that stopped writing `prompt.txt` at all leaves
#:   every answer here green.
#: - `cwd_glob_control=` is `control_in_cwd=`'s own control, run in the same
#:   command: plant the retired name, glob, require `yes`, remove it, glob
#:   again. `no` is otherwise equally true of a broken glob, a missing `ls`
#:   and the property holding, and neither negative control below moves it.
#: - `neighbour=` / `neighbour_readable=` are the isolation half; see
#:   `NEIGHBOUR_TASK_DIR`.
#:
#: `request_in_system=` is none of them. It rides along because the file is
#: open anyway and it is the disk-side half of the split ISSUE-375 made;
#: `test_each_half_reached_the_model_on_its_own_channel` is the other.
#:
#: Both write answers carry the error text rather than discarding it, because
#: `refused` and `unwritable` each collapse EROFS and ENOENT into one token —
#: which is exactly what made the first negative control below unreadable
#: until the probe was re-run by hand.
#:
#: The two sentinels are interpolated into single-quoted shell words, so
#: neither may contain an apostrophe; one that did would produce a malformed
#: command whose failure reads as `composed=absent`, i.e. as a product
#: regression.
SYSPROMPT_PROBE = f"""
CONTROL_ROOT="$(dirname "$ISTOTA_DEFERRED_DIR")/.control"
CONTROL="$CONTROL_ROOT/$(basename "$ISTOTA_DEFERRED_DIR")/task_${{ISTOTA_TASK_ID}}"
COMPOSED="$CONTROL/system_prompt.txt"
SIBLING="$ISTOTA_DEFERRED_DIR/sysprompt-sibling-probe.txt"
PLANTED="./task_${{ISTOTA_TASK_ID}}_prompt.txt"
cwd_control_files() {{
  ls -d ./task_*_prompt.txt ./prompt.txt ./system_prompt.txt 2>/dev/null \
    | tr '\\n' ' '
}}
echo SYSPROMPT_PROBE_BEGIN
echo "cwd=$(pwd)"
echo "control=$CONTROL"
echo "path=$COMPOSED"
if [ -f "$COMPOSED" ]; then echo "exists=yes"; else echo "exists=no"; fi
if [ -f "$CONTROL/prompt.txt" ]; then
  echo "user_half=present"
else
  echo "user_half=absent"
fi
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
if err=$( {{ echo tampered >> "$COMPOSED"; }} 2>&1 ); then
  echo "append=accepted"
else
  echo "append=refused [$err]"
fi
if echo probe > "$SIBLING" 2>/dev/null; then
  echo "sibling=writable"
  rm -f "$SIBLING"
else
  echo "sibling=refused"
fi
touch "$PLANTED" 2>/dev/null
HITS="$(cwd_control_files)"
if [ -n "$HITS" ]; then
  echo "cwd_glob_control=yes [$HITS]"
else
  echo "cwd_glob_control=no"
fi
rm -f "$PLANTED"
HITS="$(cwd_control_files)"
if [ -n "$HITS" ]; then
  echo "control_in_cwd=yes [$HITS]"
else
  echo "control_in_cwd=no"
fi
if err=$(touch "$CONTROL/planted-probe" 2>&1); then
  echo "control_dir=writable"
  rm -f "$CONTROL/planted-probe"
else
  echo "control_dir=unwritable [$err]"
fi
if [ -e '{NEIGHBOUR_TASK_DIR}' ]; then
  echo "neighbour=present"
else
  echo "neighbour=absent"
fi
if grep -qF '{NEIGHBOUR_SENTINEL}' '{NEIGHBOUR_TASK_DIR}/system_prompt.txt' \
    2>/dev/null; then
  echo "neighbour_readable=yes"
else
  echo "neighbour_readable=no"
fi
echo "control_user_dir=[$(ls -A "$(dirname "$CONTROL")" 2>&1 | tr '\\n' ' ')]"
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
    """The task control directory, observed from inside a live task.

    Istota's standing instructions travel to the brain as a file so they land
    with system authority rather than as the first user message, which native
    compaction summarizes away (ISSUE-375). That file, the user half beside it,
    the briefing metadata and the prepared image renditions are all written by
    the daemon into `{temp_dir}/.control/{user_id}/task_{id}` — a directory
    outside the model's own per-user temp directory. That is a property no unit
    test can observe: the default suite patches `_bwrap_available` and asserts
    argv.

    **What this witnesses is the read-only bind**, which is one of the change's
    two guards. The other is the pair of `native_fs_roots` entries, and nothing
    here can see it: the probe reaches the filesystem through the Bash tool,
    which is confined by the mount namespace, so a regression dropping the deny
    root would be masked by the bind on this shape — and on the shapes where
    the deny root is the *only* guard there is no sandbox to run this in.
    `tests/test_executor.py` holds that half.

    Read the probe answers together. Any one of them passes in states the
    others refuse — `append=refused` alone is equally true of a directory that
    was made read-only wholesale, `composed=present` alone is equally true of a
    file nothing bound at all, and `control_in_cwd=no` alone is equally true of
    a task whose prompt files were never written, of a glob that matches
    nothing by construction, and of a shell that landed in another directory.

    **The controls, and what each turned red. Three were needed, and finding
    out why is the useful part.** The obvious one — seed `_extra_ro_binds` with
    `[]` in `execute_task`, rebuild, run — turned
    `test_the_control_directory_is_readable_but_not_writable` red on
    `exists=yes`, with the probe reporting `exists=no` and `composed=absent`.
    It did *not* touch the two write answers, and could not have: nothing else
    binds the control directory, so with no `--ro-bind` the path is absent from
    the namespace altogether, `append` and `touch` fail on ENOENT, and both
    read `refused` / `unwritable` for a reason that has nothing to do with a
    boundary. A control that leaves an assertion passing has not exercised it.
    That is also why both write answers now carry the error text.

    So the second control keeps the bind and removes only the boundary:
    `--ro-bind` becomes `--bind` in `build_bwrap_cmd`'s `extra_ro_binds` loop.
    Against that image the probe answered `exists=yes composed=present
    append=accepted control_dir=writable`, and
    `test_the_control_directory_is_readable_but_not_writable` failed on
    `append=refused`; with that one assertion neutered so the run could reach
    past it, it failed again on `control_dir=unwritable`. Both write answers
    are therefore proven able to fail, separately.

    The third is for the isolation half, which neither of the first two moves:
    `_extra_ro_binds = [control_dir.parent]`, a bind one level wide. Every
    other answer was unchanged — `exists=yes`, `composed=present`,
    `append=refused [Read-only file system]`, `control_dir=unwritable`,
    `control_in_cwd=no` — while `control_user_dir` listed
    `[task_2 task_3 task_4 task_5 task_999999]`, and the scenario failed on
    `neighbour=absent` and then, with that assertion neutered, on
    `neighbour_readable=no`. That is the finding this control exists for: a
    bind widened from one task to one user leaks every other task's assembled
    prompt and leaves the whole rest of the probe green.

    Under all three controls `test_each_half_reached_the_model_on_its_own_
    channel` stayed green, correctly — it reads the endpoint transcript and
    knows nothing about binds — and so did every other scenario in this file.
    `control_in_cwd=no` went red under none of them, and that is what
    `cwd_glob_control=` is for: the move out of the working directory happens
    in `execute_task` and holds with no sandbox at all, so no bind-shaped
    control can reach that answer and it carries its own instead.
    """

    @pytest.mark.script(SYSPROMPT_SCRIPT)
    def test_the_control_directory_is_readable_but_not_writable(self, stack):
        # The neighbour goes in before the task runs, from the daemon's own
        # view, and this call is also the in-session control for it: a `cat`
        # that comes back with the sentinel is what makes the probe's
        # `neighbour=absent` the difference the bind makes rather than a
        # statement about a directory nobody created.
        seeded = stack.exec([
            "sh", "-c",
            f"mkdir -p {NEIGHBOUR_TASK_DIR} && "
            f"printf '%s\\n' {NEIGHBOUR_SENTINEL} "
            f"> {NEIGHBOUR_TASK_DIR}/system_prompt.txt && "
            f"cat {NEIGHBOUR_TASK_DIR}/system_prompt.txt",
        ])
        assert seeded.returncode == 0 and NEIGHBOUR_SENTINEL in seeded.stdout, (
            "could not plant a neighbouring task's control directory in the "
            "daemon's own view, so the isolation assertions below would pass "
            f"against nothing\n{seeded.stdout}\n{seeded.stderr}"
        )

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

        # Where the probe looked, before anything about what it found. Both of
        # these are aim rather than boundary: a probe pointed at a directory
        # the executor does not use would report every absence below as a
        # refusal, and `control_in_cwd` is a claim about the *model's working
        # directory* rather than about whatever directory the shell landed in.
        #
        # Matched with the trailing newline, so the answer is the whole line:
        # an unanchored `task_4` is a prefix of `task_41`, and an unanchored
        # `/data/tmp/testuser` is a prefix of another user's directory.
        assert f"control={CONTROL_DIR_PREFIX}{task_id}\n" in observed, (
            "the probe derived a control directory that is not the one "
            f"`get_task_control_dir` names for task {task_id}, so nothing "
            "below is about the executor's own layout\n"
            f"--- probe ---\n{observed}"
        )
        assert f"cwd={TASK_TEMP_DIR}\n" in observed, (
            "the task did not run with the per-user temp directory as its "
            "working directory, so `control_in_cwd` was asked of the wrong "
            f"directory\n--- probe ---\n{observed}"
        )
        assert "cwd_glob_control=yes" in observed, (
            "planting a `task_<id>_prompt.txt` in the working directory did "
            "not make the glob find one, so `control_in_cwd=no` below is a "
            f"broken probe rather than a property\n--- probe ---\n{observed}"
        )

        assert "exists=yes" in observed, (
            "the executor wrote no composed system prompt for this task, or it "
            "is not in the control directory the brain was given\n"
            f"--- probe ---\n{observed}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "composed=present" in observed, (
            "the file exists but carries none of Istota's standing "
            f"instructions\n--- probe ---\n{observed}"
        )
        assert "user_half=present" in observed, (
            "the user half is not in the control directory, so "
            "`control_in_cwd=no` below would only say it was written nowhere "
            f"rather than that it moved\n--- probe ---\n{observed}"
        )
        assert "request_in_system=no" in observed, (
            "the user's request is inside the system file, which would make "
            f"task material permanent rather than summarizable\n"
            f"--- probe ---\n{observed}"
        )
        assert "append=refused" in observed, (
            "the model can append to its own standing instructions — the "
            "`--ro-bind` of the control directory is missing, or bwrap was "
            "skipped entirely (check the daemon log for `Sandbox enabled but "
            f"bubblewrap unavailable`)\n--- probe ---\n{observed}"
        )
        assert "sibling=writable" in observed, (
            "a file in the per-user temp directory is not writable either, so "
            "the refusal above is a read-only task directory rather than the "
            f"control directory's own bind\n--- probe ---\n{observed}"
        )
        assert "control_in_cwd=no\n" in observed, (
            "a prompt half is still in the model's working directory, so the "
            "executor wrote it to the per-user temp directory rather than to "
            "the control directory — every task of this user can read it "
            f"there\n--- probe ---\n{observed}"
        )
        assert "control_dir=unwritable" in observed, (
            "the model can create a file in the control directory, so the "
            "guard is still per-file rather than per-directory and anything "
            "the framework writes there later starts unprotected\n"
            f"--- probe ---\n{observed}"
        )
        # The isolation half. Without these two, widening the bind from the
        # task's own directory to `{temp_dir}/.control/{user_id}` — or to
        # `.control` whole — leaves every answer above unchanged while every
        # other task's assembled prompt becomes readable in the namespace.
        assert "neighbour=absent" in observed, (
            "another task's control directory is in this task's namespace, so "
            "the read-only bind is wider than one task and the per-task "
            "isolation the layout exists for is not there\n"
            f"--- probe ---\n{observed}"
        )
        assert "neighbour_readable=no" in observed, (
            "another task's system prompt is readable from inside this task — "
            "that file carries USER.md and the channel context\n"
            f"--- probe ---\n{observed}"
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


# --------------------------------------------------------------------------
# One tool server, one namespace, for the whole attempt
# --------------------------------------------------------------------------

#: Written to the namespace's `/tmp` by the first Bash call and read back by
#: the second. `/tmp` is a `--tmpfs` inside the sandbox: with one namespace per
#: attempt it is the same tmpfs on the second call, and with the per-call
#: sandbox this replaced it was a fresh one every time.
TMPFS_SENTINEL = "TOOLSERVER_TMPFS_SURVIVED"

#: Written by Bash, read back by the `Read` tool. The two tools are different
#: families behind the same server — `Read` used to run on a daemon worker
#: thread — so this is the leg that says the file tools reach the task's
#: filesystem view through the tool server in the shipped image.
READ_TOOL_SENTINEL = "TOOLSERVER_READ_TOOL_SAW_THIS"

TOOLSERVER_SETUP = f"""
echo TOOLSERVER_SETUP_BEGIN
echo "deferred=$ISTOTA_DEFERRED_DIR"
echo '{TMPFS_SENTINEL}' > /tmp/tool-server-probe.txt
echo '{READ_TOOL_SENTINEL}' > "$ISTOTA_DEFERRED_DIR/tool-server-probe.txt"
echo TOOLSERVER_SETUP_END
"""

TOOLSERVER_READBACK = """
echo TOOLSERVER_READBACK_BEGIN
if [ -f /tmp/tool-server-probe.txt ]; then
  echo "tmp=$(cat /tmp/tool-server-probe.txt)"
else
  echo "tmp=MISSING"
fi
echo TOOLSERVER_READBACK_END
"""

TOOLSERVER_SCRIPT = [
    {
        "tool_calls": [
            {"id": "call-1", "name": "Bash", "arguments": {"command": TOOLSERVER_SETUP}}
        ]
    },
    # Both in one turn: three turns fit inside the endpoint's `MAX_TURNS` of
    # four with a turn to spare, where four calls in four turns would sit
    # exactly on the ceiling.
    {
        "tool_calls": [
            {
                "id": "call-2",
                "name": "Bash",
                "arguments": {"command": TOOLSERVER_READBACK},
            },
            {
                "id": "call-3",
                "name": "Read",
                "arguments": {"file_path": f"{TASK_TEMP_DIR}/tool-server-probe.txt"},
            },
        ]
    },
    {"text": "I looked at what survived between the two calls"},
]


class TestOneNamespaceForTheWholeAttempt:
    """The tool server's own shape, observed in the shipped image.

    `NativeBrain` spawns `istota.tool_server` once per task *attempt* through
    `build_bwrap_cmd(..., profile=NATIVE)` and every tool runs in that one
    namespace (ISSUE-389). Before it, `Bash` rebuilt a whole bwrap namespace
    per call and the five file tools ran on daemon worker threads behind a
    Python path allowlist — two execution paths, neither of them this one.

    `tests/linux/test_tool_server_lifecycle.py` asserts the same property
    against a real kernel by driving `start_tool_server` directly. What this
    adds is the deployment: the executor's own argv, the rendered config, the
    image's bubblewrap, and the brain's own spawn.

    **What this does not witness.** Neither scenario distinguishes a sandbox
    that ran from one that was skipped: unconfined, `/tmp` is the container's
    own and the file survives for a duller reason, and the `Read` succeeds
    against the same path. `TestTheDatabaseMasks` above is what makes that
    distinction, and it runs in the same session against the same stack. These
    two say the *server* is there and holds one view across calls and across
    tool families — which is exactly what a per-call sandbox could not do, and
    what a Bash-only scenario cannot see.

    **The control, and what it turned red.** Both assertions read strings out
    of the endpoint transcript, and a transcript assertion is the shape that
    passes on nothing at all. Replacing the two writes in `TOOLSERVER_SETUP`
    with a no-op — the tool calls unchanged, so the server still runs both —
    failed exactly
    `test_the_tmpfs_written_in_the_first_call_is_there_in_the_second` (on
    `tmp=MISSING`) and `test_the_read_tool_sees_what_bash_wrote` (on the absent
    sentinel). That is a non-vacuity control: it shows each assertion depends on
    the file the other tool wrote. It is *not* a control for the per-attempt
    namespace itself, which would mean reinstating the per-call sandbox — the
    linux tier holds that half, where `start_tool_server` can be driven
    directly.
    """

    @pytest.mark.script(TOOLSERVER_SCRIPT)
    def test_the_tmpfs_written_in_the_first_call_is_there_in_the_second(self, stack):
        task_id = stack.submit("write something and read it back")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        setup = marked_block(
            stack, "TOOLSERVER_SETUP_BEGIN", "TOOLSERVER_SETUP_END", "the setup probe"
        )
        assert f"deferred={TASK_TEMP_DIR}" in setup, (
            "the task temp dir is not where this file says it is, so the Read "
            f"call below is aimed at nothing\n--- probe ---\n{setup}"
        )

        readback = marked_block(
            stack,
            "TOOLSERVER_READBACK_BEGIN",
            "TOOLSERVER_READBACK_END",
            "the read-back probe",
        )
        assert f"tmp={TMPFS_SENTINEL}" in readback, (
            "the second Bash call did not see what the first wrote to /tmp, so "
            "the two ran in different namespaces — a tool server per call "
            "rather than per attempt\n"
            f"--- probe ---\n{readback}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )

    @pytest.mark.script(TOOLSERVER_SCRIPT)
    def test_the_read_tool_sees_what_bash_wrote(self, stack):
        """The other tool family, through the same server and the same view.

        The sentinel differs from the tmpfs one on purpose: both travel in the
        same transcript, and one string for both would pass on either leg
        alone.
        """
        task_id = stack.submit("write something and read it back")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        assert READ_TOOL_SENTINEL in stack.endpoint.transcript(), (
            "the Read tool's result never carried the file's contents back — "
            "it did not run, it was refused, or its result never reached the "
            "model\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
