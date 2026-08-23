"""Who can actually run the four discretionary tiers, and who is told so.

All four need a Docker daemon that will create and run containers. A sandboxed
task's Docker access is the devbox allowlist proxy, which permits ping,
version, container list, inspect-own, archive-own, restart-own and exec-on-own
— and nothing that creates or starts a container. So a task cannot run any of
these tiers, and granting it the capability would hand every task the escape
the proxy exists to deny.

That was not the problem. The problem (ISSUE-293) was that nothing said so:
`AGENTS.md` instructs the agent to run the Linux tier on exactly the changes
that most need it, and the two shell runners prechecked with `docker version`
— which the proxy *allows* — so the precheck passed and the tier died several
minutes later inside `docker build`, with an error about a buildx driver.

The pytest tiers never had that failure mode: they precheck with `docker info`,
which the proxy denies, so they skip. The asymmetry between the two prechecks
is the whole of it, and these tests pin the refusal at the front of both
scripts where it fires before any Docker call at all.
"""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LINUX_DRIVER = REPO_ROOT / "scripts" / "test-linux.sh"
UPGRADE_DRIVER = REPO_ROOT / "scripts" / "test-upgrade.sh"
TESTING_DOC = REPO_ROOT / "docs" / "development" / "testing.md"
AGENTS_DOC = REPO_ROOT / "AGENTS.md"

RUNNERS = pytest.mark.parametrize(
    "driver", [LINUX_DRIVER, UPGRADE_DRIVER], ids=lambda p: p.name,
)


@dataclass
class Run:
    returncode: int
    output: str
    docker_argv: list[str]


def _run(driver: Path, env: dict[str, str], *, docker: str) -> Run:
    """Run a tier driver against a stubbed `docker`, and record what it called.

    `docker` is "ok" for a stub that succeeds at everything, or "fail" for one
    that refuses everything the way an absent daemon would.

    Both are stubs on purpose. An earlier version of this file left `docker`
    off the PATH entirely and relied on there being none at `/usr/bin/docker`
    — true on macOS, false on any Linux host with the distro package, where
    the "no daemon" control would instead sail past the precheck and start a
    real image build from inside the ordinary suite, then trip this timeout
    with the build still running. `uv run pytest` must never reach for a
    daemon; that is the contract in AGENTS.md and the reason the tier is
    discretionary at all.

    `uv` is stubbed alongside it because `test-upgrade.sh` calls `uv run` and
    would otherwise exit 127 on the trimmed PATH — which would make a
    non-zero exit prove nothing about the refusal.

    The stub appends its argv to a file, so a test can assert that no Docker
    call happened at all. That is the positive form of "the refusal comes
    first": asserting an error string is *absent* passes just as well when the
    ordering is wrong and the daemon simply answered.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        argv_log = tmpdir / "docker-argv"
        exit_code = 0 if docker == "ok" else 1
        (tmpdir / "docker").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{argv_log}"\nexit {exit_code}\n'
        )
        (tmpdir / "docker").chmod(0o755)
        (tmpdir / "uv").write_text("#!/bin/sh\nexit 0\n")
        (tmpdir / "uv").chmod(0o755)

        # An explicit environment, not `os.environ` plus overrides: the driver
        # reads ISTOTA_TEST_BUILD_PROGRESS and ISTOTA_TEST_IMAGE, and a
        # developer who has exported either — the first is documented in the
        # driver's own header as the way to debug a build — would otherwise
        # change what these tests assert.
        #
        # ISTOTA_LINUX_TIER_MODE=container is not incidental. `test-linux.sh`
        # defaults to `auto`, which on a Linux host with a working bubblewrap
        # resolves to *native* — no Docker call at all, and a full recursive
        # pytest run started from inside this test, which blows the timeout
        # below with the child still going. Every assertion in this file is
        # about the container path's Docker precheck, so the mode is pinned
        # rather than inferred. The refusal tests do not depend on it (they
        # exit before the mode is read), but pinning it in one place keeps a
        # later test from inheriting the trap. `**env` comes after, so a caller
        # can still override it.
        result = subprocess.run(
            [str(driver)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                "PATH": f"{tmp}:/usr/bin:/bin",
                "HOME": tmp,
                "ISTOTA_LINUX_TIER_MODE": "container",
                **env,
            },
            timeout=120,
        )
        called = argv_log.read_text().splitlines() if argv_log.exists() else []
        return Run(result.returncode, (result.stdout + result.stderr).lower(), called)


class TestASandboxedTaskIsTurnedAwayUpFront:
    @RUNNERS
    def test_it_refuses_and_says_why(self, driver):
        """The sentinel alone must stop the run, with a message an agent can act on.

        Run against a `docker` that returns 0 for anything, so the script has
        no Docker-shaped reason to fail: without the refusal `test-linux.sh`
        runs to completion and exits 0.
        """
        run = _run(driver, {"ISTOTA_SANDBOXED": "1"}, docker="ok")
        assert run.returncode != 0, (
            f"{driver.name} ran under ISTOTA_SANDBOXED=1; it cannot reach a "
            f"daemon that will start a container, so it must refuse"
        )
        assert "sandbox" in run.output, run.output[:400]
        assert "not a test failure" in run.output, (
            "an agent must not read the refusal as a failing suite"
        )

    @RUNNERS
    def test_the_refusal_is_not_a_failure_exit(self, driver):
        """75, not 1: the tier did not run, rather than ran and went red.

        Every real failure in these scripts exits 1, so an agent reading only
        the status has to be able to tell the two apart — which is the same
        confusion, one layer down, that this change exists to remove.
        """
        run = _run(driver, {"ISTOTA_SANDBOXED": "1"}, docker="ok")
        assert run.returncode == 75, (
            f"{driver.name} exited {run.returncode}; 75 is 'did not run'"
        )

    @RUNNERS
    def test_it_refuses_before_calling_docker_at_all(self, driver):
        """The ordering, asserted positively.

        `docker version` is on the proxy's allowlist, so a sandboxed task
        passes the daemon precheck — putting the sandbox check first is what
        stops the tier getting as far as a build it cannot run. Asserted as
        "the stub was never invoked" rather than "the daemon error is absent",
        because the absence holds just as well when the ordering is wrong and
        the daemon simply answered.
        """
        run = _run(driver, {"ISTOTA_SANDBOXED": "1"}, docker="ok")
        assert run.docker_argv == [], (
            f"{driver.name} called docker before refusing: {run.docker_argv}"
        )

    @RUNNERS
    def test_an_unsandboxed_run_still_reaches_the_daemon_precheck(self, driver):
        """The negative control: without the sentinel the refusal must not fire.

        A check that refused unconditionally would pass every assertion above
        while disabling the tier for the humans it exists for. The `fail` stub
        is what makes this deterministic — the script gets a `docker` that
        answers the way an unreachable daemon does, on every platform.
        """
        run = _run(driver, {"ISTOTA_SANDBOXED": ""}, docker="fail")
        assert "needs a running docker daemon" in run.output, (
            f"{driver.name} did not reach its daemon precheck: {run.output[:400]}"
        )
        assert run.docker_argv, "the daemon precheck did not call docker"


def _section(doc: Path, heading: str) -> str:
    """The body of one markdown section, up to the next heading of its level.

    Scoped rather than whole-file on purpose: both documents already mention
    `ISTOTA_SANDBOXED` elsewhere — AGENTS.md in its skill-proxy bullet — so a
    whole-file search passes without the statement this is about ever being
    written. The section that sends the agent to the tiers is the one that has
    to carry the caveat.
    """
    body = doc.read_text()
    # Anchored to a whole line, so `## Verification` cannot be found inside a
    # longer `## Verification budget` heading and silently rescope the
    # assertion to a section that was never checked. Terminated on any heading
    # of this level *or shallower* — `^## ` alone does not match a following
    # `# `, so a section ending at an h1 would otherwise swallow the file.
    depth = len(heading.split(" ", 1)[0])
    start = re.search(rf"^{re.escape(heading)}\s*$", body, re.MULTILINE)
    assert start, f"no heading {heading!r} in {doc.name}"
    rest = body[start.end():]
    end = re.search(rf"^#{{1,{depth}}} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


DOC_SECTIONS = pytest.mark.parametrize(
    ("doc", "heading"),
    [(TESTING_DOC, "## Deployment tiers"), (AGENTS_DOC, "## Verification")],
    ids=["testing.md", "AGENTS.md"],
)


class TestTheDocsNameTheBoundary:
    """The filed symptom was documentary: an instruction impossible to follow.

    Both documents tell an agent to run these tiers. Both must also say who
    can — the instruction outlived the proxy that made it impossible.
    """

    @DOC_SECTIONS
    def test_the_tier_section_names_the_sentinel(self, doc, heading):
        assert "ISTOTA_SANDBOXED" in _section(doc, heading), (
            f"{doc.name} '{heading}' must say that a sandboxed task cannot run "
            f"the discretionary tiers, and name the signal that decides it"
        )

    @DOC_SECTIONS
    def test_the_fallback_is_stated(self, doc, heading):
        """Telling an agent it cannot run something is half an instruction."""
        section = _section(doc, heading).lower()
        assert "ask for the run" in section, (
            f"{doc.name} '{heading}' must say what to do instead of running the tier"
        )
