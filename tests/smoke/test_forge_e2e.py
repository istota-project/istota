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
"""

from __future__ import annotations

import pytest

from testbed.services.gitlab import FORGE_PROJECT, FORGE_TOKEN

pytestmark = pytest.mark.smoke

# The profile is declared per class rather than per module, because
# `TestTheNegativeControl` genuinely runs a different one — a different image
# is a different profile, since the image is a compose-level property.
FORGE = pytest.mark.profile("forge")

BRANCH = "feature/from-the-smoke-tier"

# Written as one Bash call rather than several turns: what is under test is the
# chain, not the agent loop's ability to sequence, and every extra turn is
# another scripted response to keep in step.
_CLONE_AND_PUSH = f"""
set -eux
cd /data/repos
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
cd /data/repos/project
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
