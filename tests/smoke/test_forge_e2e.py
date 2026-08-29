"""The forge chain, end to end, through the artifact that ships.

Everything between the model and the server is asserted here in one piece:
executor, sandbox, skill proxy, the wrapper the developer skill writes, the
deny policy inside it, token injection, the real `glab`, and a server that
answers. Each of those has unit coverage; none of that coverage can tell you
they are wired to each other, which is precisely what ISSUE-263 was — every
component correct, the composition broken, and the failure arriving at the one
point in a task where it would publish.

The model is scripted (`testbed/services/model_endpoint.py`), so the "decisions"
here are fixed Bash calls rather than anything an LLM chose. That is the point:
this is a wiring test, and a real model would make it non-deterministic without
asserting anything extra.

**Read `tests/smoke/test_sandbox_in_stack.py` first if everything here fails.**
The wrapper is something the model execs, so every scenario depends on
bubblewrap working inside the container, and none of these failures would name
it.

**A scenario spanning two Bash calls must keep its checkout under
`$DEVELOPER_REPOS_DIR`**, which is the task's own subtree and the only part of
`developer.repos_dir` the sandbox binds. The root is present inside the
namespace as that bind's parent on bwrap's ephemeral root tmpfs, so writing
there succeeds and then disappears with the sandbox — one call's work, gone
before the next. ISSUE-338 was this file cloning into the root, and it presented
as a forge that took no REST calls rather than as a checkout that was thrown
away. `tests/test_smoke_tier.py::TestTheForgeScenarioClonesIntoTheBoundSubtree`
catches the same drift without Docker.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from istota.shell_exec import SIGPIPE_EXIT, SIGPIPE_NOTE
from testbed.services.gitlab import CONTAINER_REPOS_DIR, FORGE_PROJECT, FORGE_TOKEN

pytestmark = pytest.mark.smoke

# The profile is declared per class rather than per module, because
# `TestTheNegativeControl` genuinely runs a different one — a different image
# is a different profile, since the image is a compose-level property.
FORGE = pytest.mark.profile("forge")

BRANCH = "feature/from-the-smoke-tier"

# Written as one Bash call rather than several turns: what is under test is the
# chain, not the agent loop's ability to sequence, and every extra turn is
# another scripted response to keep in step.
#
# **The checkout goes under `$DEVELOPER_REPOS_DIR`, never under the configured
# `repos_dir` root, and that distinction is load-bearing across turns.**
# `build_bwrap_cmd` binds `{repos_dir}/{user_id}`; the root itself exists inside
# the namespace only as that bind's parent on bwrap's ephemeral root tmpfs. A
# clone into the root therefore succeeds, reaches the network, and is gone by
# the next Bash call, which gets a fresh sandbox. That was ISSUE-338: this file
# was written before the per-user split, `cd /data/repos/project` in the second
# turn failed with "No such file or directory", `glab` never ran, and the
# symptom was an absence of REST calls that read as a broken forge chain.
#
# The variable rather than the literal, because the developer skill's
# `setup_env` is the one place that knows the layout and it exports the answer —
# which is also the recipe `skill.md` gives the model. `set -u` (in `set -eux`)
# aborts if it was never exported, and the `-n` test catches the empty string
# that `-u` lets through, so the scenario cannot quietly clone somewhere else.
#
# **`_ENTER_REPOS_ROOT` must stay free of literal braces.** It is interpolated
# into `_CLONE_AND_PUSH`, which is the one template `.format(clone_url=...)` is
# applied to later — and f-string interpolation does not re-escape braces in the
# value it substitutes. So `echo "REPOS_ROOT=${DEVELOPER_REPOS_DIR}"`, a brace
# expansion, or a `--format={...}` added here becomes a `KeyError` from
# `.format()` in two of the three call sites. `_OPEN_MR` is not formatted and so
# does not share the hazard, which is what makes the asymmetry easy to miss.
_REPOS_ROOT_MARK = "REPOS_ROOT"

_ENTER_REPOS_ROOT = f"""
test -n "$DEVELOPER_REPOS_DIR"
echo "{_REPOS_ROOT_MARK}=$DEVELOPER_REPOS_DIR"
cd "$DEVELOPER_REPOS_DIR"
"""

_CLONE_AND_PUSH = f"""
set -eux
{_ENTER_REPOS_ROOT}
git clone {{clone_url}} project
cd project
git checkout -b {BRANCH}
echo "a change from the smoke tier" > smoke.txt
git -c user.email=smoke@example.com -c user.name=Smoke add smoke.txt
git -c user.email=smoke@example.com -c user.name=Smoke commit -m "Add smoke.txt"
git push -u origin {BRANCH}
"""

_OPEN_MR = f"""
set -eux
cd "$DEVELOPER_REPOS_DIR/project"
glab mr create --title "A merge request from the smoke tier" \
  --description "opened by tests/smoke/test_forge_e2e.py" \
  --source-branch {BRANCH} --target-branch main \
  --repo {FORGE_PROJECT} --yes
"""


def _script(*commands: str) -> list[dict]:
    """One scripted turn per command, then a closing answer.

    The closing turn is not optional: a turn ending in `tool_calls` asks for
    another round, and the scripted endpoint answers an unscripted round with
    an error frame rather than replaying — so a script that ended on a tool
    call would fail the task for a reason that has nothing to do with the
    forge.
    """
    turns = [
        {
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "name": "Bash",
                    "arguments": {"command": command},
                }
            ]
        }
        for index, command in enumerate(commands)
    ]
    return [*turns, {"text": "done"}]


#: What the Bash tool says when a command did not come back clean.
#:
#: Four, not one, and each is a different way for the tool to report. Three are
#: bracketed suffixes appended to the output (`session/tools/bash.py`); the
#: fourth is a whole result body returned *instead* of running anything, when
#: the spawn itself raised — which after `sandbox_wrap` means an unusable
#: bwrap, a refused namespace or a bad cwd. That one carries no bracketed
#: marker at all, and it is the failure this file's own header sends you to
#: `test_sandbox_in_stack.py` for, so a checker blind to it is blind to the
#: first thing to suspect.
_FAILURE_MARKERS = (
    "[exit code: ",
    "[command aborted]",
    "[command timed out",
    "Failed to start command:",
)


def _assert_every_command_succeeded(stack, task) -> None:
    """No Bash call in this task came back a failure.

    `assert task["status"] == "completed"` cannot say this and never could. The
    model is scripted: it ignores tool output and returns a fixed answer on its
    closing turn, so the task reaches `completed` with `success=True` whatever
    the commands did. Every scenario in this file therefore rested entirely on
    its service-side assertion, and when one of those failed it failed as a bare
    empty list — the sentence explaining why having gone to stderr inside the
    sandbox and no further. That is the second half of ISSUE-338.

    **Exit 141 is excluded, and it is the one exit code that has to be.**
    `pipefail` is on for every command the daemon runs, so a correct
    `… | head -1` makes bash exit 141 and the tool annotates it with
    `SIGPIPE_NOTE` rather than treating it as a fault. No script here
    short-circuits a pipe today, which is exactly why this needs writing down:
    this helper is the pattern the next scenario in the file will copy, and the
    first one that pipes into `head` would otherwise fail for being right.

    **The scan is endpoint-wide, not scoped to `task`.** `rescript` clears the
    recorded requests at every reset, so in practice the record holds this
    scenario's turns and nothing else; the residual is a poller task landing
    mid-script and taking a real tool turn. Left as is deliberately — a poller
    arriving after the script is consumed gets the exhausted-script frame and
    makes no tool calls, and one arriving before steals turn 0 and breaks the
    scenario loudly anyway. Recorded so the next reader does not reinstate the
    testbed's watermark rule here by rewriting it.

    Called *before* the service-side assertion, deliberately: a failed command
    explains an absent REST call, whereas an absent REST call explains nothing.

    Proven able to fail: reverting `_OPEN_MR` to `cd /data/repos/project` turned
    this red with `+ cd /data/repos/project` / `No such file or directory` /
    `[exit code: 1]` rendered in the report, where the pre-fix file failed as
    `assert 0 == 1`.
    """
    failures = [
        text
        for text in stack.endpoint.tool_results()
        if any(marker in text for marker in _FAILURE_MARKERS)
        and not _is_sigpipe_only(text)
    ]
    assert not failures, (
        "a command in the scenario failed, so whatever the assertions below "
        "would have measured never ran\n" + stack.diagnostics(task)
    )


def _is_sigpipe_only(text: str) -> bool:
    """A result whose sole complaint is the annotated SIGPIPE exit.

    `SIGPIPE_NOTE` is what distinguishes "bash exited 141 because a reader
    closed the pipe" from a command that genuinely returned 141, and the tool
    only ever appends it to the former.
    """
    if SIGPIPE_NOTE not in text:
        return False
    others = [m for m in _FAILURE_MARKERS if m != "[exit code: "]
    return f"[exit code: {SIGPIPE_EXIT}]" in text and not any(
        marker in text for marker in others
    )


def _assert_the_checkout_is_in_the_bound_subtree(stack, task) -> None:
    """`$DEVELOPER_REPOS_DIR` named a subtree of the root, not the root.

    The positive half of the ISSUE-338 guard, and it needs saying out loud
    rather than being inferred from the scenario passing. `_ENTER_REPOS_ROOT`
    echoes the value the daemon exported; this reads it back and requires it to
    be a strict child of the configured `repos_dir`. Equality with the root is
    the pre-split layout, under which the clone lands on bwrap's ephemeral root
    tmpfs and every later turn finds it gone.

    Stated as "one level below the root" rather than as `{root}/testuser` so it
    says what the sandbox actually binds without pinning the harness's default
    user id, and compared on path components rather than with `startswith` —
    which accepts `/data/repos/../elsewhere` as a child of `/data/repos`.

    The `set -x` trace is not a hazard here even though it carries the same
    text: bash writes it as `+ echo REPOS_ROOT=…`, so it fails `startswith`.
    Nor is the neighbouring hazard `test_sandbox_repos_isolation.py` records —
    a harness echoing the script back — since the command travels on the
    assistant message's `tool_calls` and `tool_results()` filters on role.

    Proven able to fail: reverting the scripts to the pre-fix literals turned
    this red on its "never echoed REPOS_ROOT" path.
    """
    marker = f"{_REPOS_ROOT_MARK}="
    root_parts = PurePosixPath(CONTAINER_REPOS_DIR).parts
    for text in stack.endpoint.tool_results():
        for line in text.splitlines():
            if not line.startswith(marker):
                continue
            root = line[len(marker):].strip()
            parts = PurePosixPath(root).parts
            contained = (
                len(parts) == len(root_parts) + 1
                and parts[: len(root_parts)] == root_parts
                and ".." not in parts
            )
            assert contained, (
                f"DEVELOPER_REPOS_DIR is {root!r}, which is not a per-user "
                f"subtree one level below {CONTAINER_REPOS_DIR}. The sandbox "
                "binds that subtree, so a checkout anywhere else does not "
                "survive the turn that made it (ISSUE-338)"
            )
            return
    raise AssertionError(
        f"the scenario never echoed {_REPOS_ROOT_MARK}, so its first command "
        "did not run and nothing below is about the forge\n"
        + stack.diagnostics(task)
    )


@FORGE
class TestTheHappyPath:
    """Clone, branch, commit, push, open a merge request.

    The script is installed with `Stack.script` rather than through the
    `script` marker, because the clone URL carries the stub's port and that is
    not known until something is listening. The marker is evaluated before the
    test body runs and has no way to reach the stack it is about to script.
    `script` waits for the daemon's own work to drain before rewinding — and
    holds the endpoint's barrier across the swap — since the endpoint routes by
    call order and cannot tell whose request it is answering.
    """

    def test_a_merge_request_is_opened_against_the_forge(self, stack):
        forge = stack.service("gitlab")
        stack.script(
            _script(
                _CLONE_AND_PUSH.format(clone_url=forge.clone_url(FORGE_PROJECT)),
                _OPEN_MR,
            )
        )
        task_id = stack.submit("branch, push and open a merge request")

        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        assert task["status"] == "completed", stack.diagnostics(task)
        _assert_the_checkout_is_in_the_bound_subtree(stack, task)
        _assert_every_command_succeeded(stack, task)
        opened = forge.rest_calls("POST", "/merge_requests")
        assert len(opened) == 1, stack.diagnostics(task)
        # `payload()` rather than `body`, which is the raw bytes the client
        # sent: a REST client may form-encode or JSON-encode the same verb, and
        # an assertion should not have to know which it picked.
        assert opened[0].payload().get("source_branch") == BRANCH, opened[0].payload()
        assert opened[0].payload().get("target_branch") == "main", opened[0].payload()

    def test_the_branch_really_landed_in_the_repository(self, stack):
        """The git half, asserted from the server's side.

        A recorded `POST /merge_requests` says glab was reached; it says
        nothing about whether the push before it worked, and glab will happily
        open a merge request for a branch that does not exist. This is the
        assertion that the credential helper, the proxy and `git http-backend`
        all did their part.
        """
        forge = stack.service("gitlab")
        stack.script(
            _script(_CLONE_AND_PUSH.format(clone_url=forge.clone_url(FORGE_PROJECT)))
        )
        task_id = stack.submit("branch and push")

        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        assert task["status"] == "completed", stack.diagnostics(task)
        _assert_the_checkout_is_in_the_bound_subtree(stack, task)
        _assert_every_command_succeeded(stack, task)
        assert BRANCH in forge.branches(FORGE_PROJECT), stack.diagnostics(task)


@FORGE
class TestTheDenyPolicy:
    def test_a_denied_verb_never_reaches_the_forge(self, stack):
        """`glab api --form` is a write in disguise, and the policy says so.

        Two assertions, and the second is the one that matters. Exit 3 says the
        wrapper refused; "nothing reached the stub" says it refused *before*
        talking to the forge rather than after, which is the difference between
        a guard and a log line.
        """
        stack.script(
            _script(
                "glab api --form title=x /projects/1/issues; "
                'echo "EXIT=$?"'
            )
        )
        task_id = stack.submit("try something the policy blocks")

        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        assert "EXIT=3" in stack.endpoint.transcript(), (
            "the wrapper did not exit EXIT_DENIED\n"
            + stack.diagnostics(task)
        )
        assert not stack.service("gitlab").rest_calls(contains="/issues"), (
            "a denied verb reached the forge\n" + stack.diagnostics(task)
        )

    def test_the_denial_is_visible_to_the_model_rather_than_silent(self, stack):
        """A refusal the model cannot see is a task that fails inexplicably.

        The wrapper writes its reason to stderr; what this asserts is that the
        reason survived the sandbox, the tool result and the transcript, so the
        model could act on it.
        """
        stack.script(
            _script("glab api --form title=x /projects/1/issues || true")
        )
        task_id = stack.submit("try something the policy blocks")
        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        assert "not permitted by this deployment" in stack.endpoint.transcript(), (
            stack.diagnostics(task)
        )


@FORGE
class TestTokenIsolation:
    def test_the_token_is_injected_without_the_model_ever_holding_it(
        self, stack
    ):
        """The whole point of the skill proxy, asserted from both ends.

        From the model's end: the token appears in no environment its own shell
        can read. From the forge's end: a token of exactly that length arrived
        anyway. Either assertion alone is satisfiable by a broken deployment —
        the first by one where the forge call simply fails, the second by one
        that hands the model the credential and lets it through.
        """
        stack.script(
            _script(
                # By variable *name*, never by value. Grepping for the literal
                # would put the token into the command — and the command is
                # something the model was sent, so the second assertion below
                # would then be false by construction rather than by defect.
                'echo "GITLAB_TOKEN_SET=$(env | grep -c \'^GITLAB_TOKEN=\' || true)"',
                'echo "GITHUB_TOKEN_SET=$(env | grep -c \'^GITHUB_TOKEN=\' || true)"',
                "glab repo view " + FORGE_PROJECT,
            )
        )
        task_id = stack.submit("look at the repository")

        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        # The positive counterpart to `TestTheNegativeControl`'s EXIT=6. That
        # control says a *missing* binary reports itself; without this, nothing
        # said the shipped image's `glab` exits 0 for the same verb, and the
        # pair only compared two ways of failing.
        _assert_every_command_succeeded(stack, task)
        transcript = stack.endpoint.transcript()
        assert "GITLAB_TOKEN_SET=0" in transcript, (
            "GITLAB_TOKEN was readable from the model's own environment\n"
            + stack.diagnostics(task)
        )
        assert "GITHUB_TOKEN_SET=0" in transcript, (
            "GITHUB_TOKEN was readable from the model's own environment\n"
            + stack.diagnostics(task)
        )
        # The literal must not be anywhere in what the model was sent, either.
        assert FORGE_TOKEN not in transcript, (
            "the forge token reached the model's transcript\n"
            + stack.diagnostics(task)
        )
        # `calls_matching()` rather than iterating `.calls`: the daemon's own
        # tasks keep running after `wait_for_task` returns, so handler threads
        # may still be appending, and only the accessor takes the lock.
        forge = stack.service("gitlab")
        reached = [call for call in forge.calls_matching() if call.auth]
        assert reached, (
            "no credential reached the forge; the wrapper never injected one\n"
            + stack.diagnostics(task)
        )
        assert any(
            call.auth.endswith(f":{len(FORGE_TOKEN)}") for call in reached
        ), [call.auth for call in reached]

    def test_git_was_credentialed_through_the_helper(self, stack):
        """The other credential route, which is not the wrapper's.

        `glab` gets its token from the skill proxy directly. `git push` gets
        one from the credential helper the developer skill writes, which shells
        out to `credential-fetch`, which asks the same proxy. Two paths, and
        only the stub's challenge makes the second observable at all.
        """
        forge = stack.service("gitlab")
        stack.script(
            _script(_CLONE_AND_PUSH.format(clone_url=forge.clone_url(FORGE_PROJECT)))
        )
        task_id = stack.submit("branch and push")
        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        _assert_every_command_succeeded(stack, task)
        assert forge.authenticated_git_calls(), (
            "git never answered the stub's challenge; the credential helper "
            "did not reach the skill proxy\n" + stack.diagnostics(task)
        )


@pytest.mark.profile("no-forge")
class TestTheNegativeControl:
    """The same scenarios against an image with the forge binaries removed.

    Without this class every assertion above is unfalsified: the file would
    pass identically if the daemon never ran a forge command at all, and a tier
    built to end silent non-execution would be silently not executing. The
    control reproduces ISSUE-263 exactly — a rendered config naming
    `/usr/local/lib/istota_forge/glab`, and nothing at that path.

    Slow, because it builds a second image. Worth it: this is the pair the
    stage's acceptance criterion is stated as, and it is the only thing here
    that proves the rest can see a break.
    """

    def test_doctor_names_the_missing_binaries_before_any_task_runs(
        self, stack
    ):
        """The report says so up front, which is what ISSUE-263 lacked.

        Two assertions, and the SKIP one is not padding. Every `developer.*`
        check skips when no forge token is configured, so "no FAIL was
        reported" is also what a correctly-configured *tokenless* deployment
        says — and a suite asserting only that is green on precisely the broken
        image. The check has to have run.
        """
        report = stack.doctor(scope="image")

        binaries = [
            check
            for check in report
            if check["name"].startswith("developer.forge_binaries")
        ]
        assert binaries, f"the check did not run at all: {[c['name'] for c in report]}"
        assert all(check["status"] != "skip" for check in binaries), (
            "developer.forge_binaries skipped, so a FAIL could never have been "
            f"reported: {binaries}"
        )
        assert any(check["status"] == "fail" for check in binaries), binaries
        # The path, so an operator reading the report knows what to put back.
        assert any("istota_forge" in check["detail"] for check in binaries), binaries

    @FORGE
    def test_doctor_is_clean_on_the_image_that_ships(self, stack):
        """The other half of the pair.

        The one test in this class that runs the *correct* image, so it
        overrides the class's profile rather than inheriting it —
        `get_closest_marker` takes the function's before the class's.

        A control that fails is only evidence if the same assertion passes on a
        correct artifact — otherwise it is measuring something incidental to
        the images being different.
        """
        report = stack.doctor(scope="image")

        binaries = [
            check
            for check in report
            if check["name"].startswith("developer.forge_binaries")
        ]
        assert binaries, f"the check did not run at all: {[c['name'] for c in report]}"
        assert all(check["status"] != "skip" for check in binaries), binaries
        assert all(check["status"] != "fail" for check in binaries), binaries

    def test_the_task_fails_diagnosably_rather_than_at_execve(
        self, stack
    ):
        """What the model is told when the binary is not there.

        ISSUE-263's signature was `os.execve` exiting 6 with ENOENT, at the one
        point in a task where it would publish — a bare exit code with nothing
        naming the cause. The wrapper reports it instead, and this asserts the
        report survives the sandbox and reaches the model, which is the
        difference between a diagnosable failure and a mystery.
        """
        stack.script(
            _script("glab repo view " + FORGE_PROJECT + "; echo \"EXIT=$?\"")
        )
        task_id = stack.submit("look at the repository")

        task = stack.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=300
        )

        transcript = stack.endpoint.transcript()
        assert "EXIT=6" in transcript, (
            "the wrapper did not exit EXIT_EXEC\n"
            + stack.diagnostics(task)
        )
        assert "cannot run" in transcript, (
            "the failure reached the model without saying what could not be run\n"
            + stack.diagnostics(task)
        )
        forge = stack.service("gitlab")
        assert not forge.rest_calls(contains="/projects/"), (
            "a forge call reached the stub from an image with no forge binary\n"
            + stack.diagnostics(task)
        )
