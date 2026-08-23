"""No configured credential reaches the model's context.

This is the skill proxy's whole purpose stated as a test rather than as a
design note. `execute_task` splits every `sensitive` variable the authorized
skills declare out of the environment the model's process gets, and hands them
instead to a Unix-socket proxy that injects them into the skill CLI it spawns
*outside* the sandbox. Nothing has ever checked the result end to end: the unit
tests assert that `_split_credential_env` returns two dicts, which is a
statement about a function rather than about what a running deployment puts in
front of a model.

`ScriptedEndpoint.transcript()` is what makes the end-to-end version possible.
It returns every message the endpoint was ever sent, and — the part no other
witness has — that includes tool *results*, which is where the output of a
command the model ran comes back. So a scripted `env` dump lands in the
transcript, and the assertion is over the whole transcript rather than over the
dump alone: a credential in the system prompt, in a skill's menu text or in a
task's own prompt would be just as exposed and just as invisible to a scan of
the tool result.

**The profile is `forge` because it is the one with a real credential to
lose.** `ISTOTA_DEVELOPER_GITLAB_TOKEN` is rendered into `config.toml` by the
shipped generator, resolved into `GITLAB_TOKEN` by the developer skill's `env`
manifest, and marked `sensitive` there — so it is a value the deployment
genuinely has to keep away from the model while still letting a `glab` the
model runs authenticate with it. `test_forge_e2e.py` asserts the second half
(the token reaches the forge); this asserts the first.
"""

from __future__ import annotations

import pytest

from testbed.services.gitlab import FORGE_TOKEN

pytestmark = pytest.mark.smoke

#: Per class, matching `test_forge_e2e.py`; the module-level marker stays a
#: bare `pytest.mark.smoke` because `tests/test_smoke_tier.py` greps for it.
FORGE = pytest.mark.profile("forge")

CONTAINER_CONFIG = "/data/config/config.toml"

# Everywhere a credential could be picked up from inside a task, in one turn.
#
# `env` is the obvious one and the least interesting: it is what the split is
# *about*. The other two are what a model would try next, and neither has a
# witness anywhere else in the suite.
#
# `/proc/*/environ` reads the environment of every process the task can see.
# Under `--unshare-pid` that is the sandbox's own tree and not the daemon's, so
# a mask that failed to unshare the pid namespace would show up here as the
# scheduler's environment — which holds every credential in the deployment.
#
# `config.toml` is where `render-config.sh` writes the forge token in plaintext.
# `sandbox_ro_paths` defaults to `[]` and the config directory is never bound,
# so this must fail to open; before that default changed, a single broad
# read-only bind is exactly how the file used to be reachable.
SECRET_PROBE = f"""
echo SECRET_PROBE_BEGIN
echo '--- env ---'
env
echo '--- proc ---'
cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n'
echo '--- config ---'
if cat {CONTAINER_CONFIG} 2>/dev/null; then
  echo "config_read=readable"
else
  echo "config_read=unreadable"
fi
echo SECRET_PROBE_END
"""

SECRET_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": SECRET_PROBE},
            }
        ]
    },
    {"text": "I looked for credentials"},
]

#: A prompt that authorizes the developer skill.
#:
#: Not decoration. `build_skill_env` resolves a skill's config- and
#: secret-derived variables only for the skills a task *authorized*, so a task
#: that never selected `developer` never has `GITLAB_TOKEN` in its environment
#: to begin with — and this scenario would then be asserting the absence of
#: something nothing put there. The words are the skill's own triggers.
PROMPT = "branch, push and open a merge request"


def _probe_output(stack) -> str:
    """The marked block out of the transcript, or a readable failure."""
    transcript = stack.endpoint.transcript()
    begin = transcript.find("SECRET_PROBE_BEGIN")
    end = transcript.find("SECRET_PROBE_END", begin + 1)
    if begin < 0 or end < 0:
        raise AssertionError(
            "the probe's output never reached the model, so the Bash tool did "
            "not run — this says nothing about credential isolation either "
            f"way\n--- daemon logs ---\n{stack.logs(120)}"
        )
    return transcript[begin:end]


def _published_credentials(stack) -> dict[str, str]:
    """Every credential the profile's services published, by service name.

    Read off the services rather than from a list in this file, which is what
    makes the scan grow with the tier: `HttpStub.start` refuses a non-loopback
    bind without a credential, so any stub a future profile adds is already
    obliged to name the value it is exposing, and it lands here for free.
    """
    published = {}
    for name, service in stack.services.items():
        credential = getattr(service, "credential", None)
        if credential:
            published[name] = credential
    return published


@FORGE
class TestNoCredentialReachesTheModel:
    @pytest.mark.script(SECRET_SCRIPT)
    def test_the_probe_ran_and_the_model_saw_its_output(self, stack):
        """The control for everything below, and it runs first for a reason.

        Every other assertion in this file is an absence, and an absence is
        satisfied perfectly by a task whose Bash call never executed. This is
        what distinguishes "no credential was found" from "nothing looked".
        """
        task_id = stack.submit(PROMPT)
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        observed = _probe_output(stack)

        assert "PATH=" in observed, (
            "the environment dump is empty, so `env` did not run and the scan "
            f"below is vacuous\n--- probe ---\n{observed[:2000]}"
        )
        # The proxy socket is in the model's environment by design — it is the
        # *replacement* for the credentials, and its presence is what proves
        # the split ran rather than the skill having declared nothing.
        assert "ISTOTA_SKILL_PROXY_SOCK=" in observed, (
            "the task ran with no skill-proxy socket, so credentials were "
            "never split out of its environment and the assertions below "
            f"would pass on a deployment with no isolation at all\n{observed[:2000]}"
        )
        # And the developer skill has to have been *authorized*, or
        # `GITLAB_TOKEN` was never in this task's environment for the split to
        # remove — which would make the scenario below assert the absence of
        # something nothing put there. `DEVELOPER_REPOS_DIR` is the marker
        # because it comes from the same `env` manifest and is the one entry
        # there that is not `sensitive`, so it survives the split that the
        # token does not.
        assert "DEVELOPER_REPOS_DIR=" in observed, (
            "the developer skill was not authorized for this task, so no forge "
            "credential was ever resolved into its environment and the "
            "assertions below are vacuous. The prompt has to carry the skill's "
            f"own triggers — see PROMPT.\n{observed[:2000]}"
        )

    @pytest.mark.script(SECRET_SCRIPT)
    def test_no_configured_credential_appears_anywhere_in_the_transcript(
        self, stack
    ):
        """Over the whole transcript, not over the tool result.

        A credential that reached the model through the system prompt, through
        a skill's menu text or through the task's own prompt is exactly as
        exposed as one in the environment, and a scan of the `env` dump alone
        would miss all three.
        """
        task_id = stack.submit(PROMPT)
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        _probe_output(stack)  # the control above, restated as a precondition
        transcript = stack.endpoint.transcript()

        published = _published_credentials(stack)
        assert published, (
            "no service in this profile published a credential, so this scan "
            "has nothing to look for"
        )
        leaked = {
            name: value for name, value in published.items() if value in transcript
        }
        assert not leaked, (
            f"credential(s) reached the model's context: {sorted(leaked)}. The "
            "value is not printed here on purpose — this repository is public "
            "and a failing assertion's text ends up in terminal output that "
            "gets pasted. Grep the transcript for the service's own constant."
        )

    @pytest.mark.script(SECRET_SCRIPT)
    def test_the_forge_token_is_absent_from_the_environment_it_authenticates(
        self, stack
    ):
        """Named separately from the sweep above, because it is the real one.

        The forge token is the only credential in this profile that a
        deployment must both *hold* and *withhold*: `glab` run by the model
        authenticates with it — `test_forge_e2e.py` asserts that it reaches the
        forge — while the model itself must never see it. The sweep would catch
        this too; naming it means the failure says which property broke.
        """
        task_id = stack.submit(PROMPT)
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        observed = _probe_output(stack)

        # First, that there was something to withhold. A task that never
        # authorized the developer skill has no `GITLAB_TOKEN` in its
        # environment for the split to remove, and the two assertions below
        # would then be true of nothing. Stated here as well as in the control
        # above, because this is the assertion someone will read on its own.
        assert "DEVELOPER_REPOS_DIR=" in observed, (
            "the developer skill was not authorized for this task, so nothing "
            "resolved a forge credential and this scenario asserts nothing"
        )
        assert "GITLAB_TOKEN=" not in observed, (
            "GITLAB_TOKEN is in the environment the model can read. The skill "
            "proxy is what removes it (`_split_credential_env`), so either it "
            "is disabled or the variable stopped being marked `sensitive` in "
            "the developer skill's `env` manifest."
        )
        assert FORGE_TOKEN not in observed, (
            "the forge token's value is readable from inside the task"
        )

    @pytest.mark.script(SECRET_SCRIPT)
    def test_the_rendered_config_is_not_in_the_namespace(self, stack):
        """`config.toml` carries the token in plaintext, and is not bound in.

        Asserted positively — the read has to *fail* — rather than by scanning
        for the token, because a scan passes just as well against a config that
        was never rendered. What proves the boundary is that the path is not
        openable while the daemon is plainly reading it.
        """
        task_id = stack.submit(PROMPT)
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        observed = _probe_output(stack)
        after_marker = observed.split("--- config ---", 1)
        assert len(after_marker) == 2, observed[:2000]
        config_read = after_marker[1]

        assert "gitlab_token" not in config_read, (
            f"{CONTAINER_CONFIG} is readable from inside the task, and it "
            "holds the forge token in plaintext"
        )
        # A marker the probe writes from `cat`'s exit status, not `cat`'s error
        # message. `passthrough_env_vars` defaults to carrying `LANG` into the
        # sandbox, so coreutils localizes its strerror — and this is the
        # negative control for the whole file, which makes it the last
        # assertion that should be able to fail for a reason of its own.
        assert "config_read=unreadable" in config_read, (
            f"reading {CONTAINER_CONFIG} succeeded, so it is reachable in some "
            f"form from inside the sandbox\n{config_read[:1000]}"
        )
