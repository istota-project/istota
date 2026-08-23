"""The Linux tier's driver, checked from the side that does not need Docker.

`scripts/test-linux.sh` cannot be run to completion here — the container mode
needs a Docker daemon and the native mode needs a Linux kernel with a working
bubblewrap, and the whole point of the tier is that the ordinary suite needs
neither. What can be checked without either is the places the driver duplicates
something that lives elsewhere in the repo, because a copy that drifts is how a
runner starts quietly running the wrong set of tests — and the mode switch
itself, which the driver will resolve and print without running anything.
"""

import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "test-linux.sh"
DOCKERFILE = REPO_ROOT / "docker" / "test" / "Dockerfile"


def _function_body(name: str) -> str:
    """One shell function's body, sliced out of the driver by name.

    The driver has two run paths and several of the assertions below belong to
    exactly one of them — the `-e ISTOTA_LINUX_TIER=1` spelling is a `docker
    run` flag and means nothing natively, and sourcing the cgroup helper is a
    thing native mode must never do. A whole-file grep cannot tell the two
    apart, so it would keep passing after the branch it was written for had
    stopped carrying the line.
    """
    body = DRIVER.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}$", body, re.MULTILINE | re.DOTALL)
    assert match, f"could not find a `{name}()` function in the driver"
    return match.group(1)


def _code_of(name: str) -> str:
    """The same body with whole-line comments removed.

    For any assertion about what a branch *does* rather than what it explains.
    `run_native_tier` has to carry a paragraph naming
    `scripts/dev/linux-tier-cgroup.sh` and saying why it is not sourced there,
    and a grep over the raw text reads that paragraph as the very call it was
    written to forbid — so the honest version of "native mode never sources the
    cgroup helper" fails against the honest version of the driver.

    Whole-line only: a `#` inside a string or after a command is left alone,
    since dropping it would change the code the assertion then reads.
    """
    return "\n".join(
        line for line in _function_body(name).splitlines()
        if not line.lstrip().startswith("#")
    )


def _addopts_marker_expression() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    match = re.search(r"-m '([^']+)'", addopts)
    assert match, f"could not find a -m expression in addopts: {addopts!r}"
    return match.group(1)


def _driver_marker_expression() -> str:
    body = DRIVER.read_text()
    match = re.search(r"^default_markers='([^']+)'", body, re.MULTILINE)
    assert match, "could not find default_markers= in scripts/test-linux.sh"
    return match.group(1)


class TestDriverIsExecutableBash:
    def test_it_is_executable(self):
        import os

        assert DRIVER.exists()
        assert os.access(DRIVER, os.X_OK), "the driver is invoked as ./scripts/test-linux.sh"

    def test_it_parses_as_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(DRIVER)], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(
        not Path("/bin/bash").exists(), reason="no /bin/bash to check the old shell against",
    )
    def test_it_parses_under_the_system_bash(self):
        """macOS ships bash 3.2, and the driver's whole audience is on macOS.

        Not a formality: `"${empty_array[@]}"` under `set -u` is fatal there
        and fine on bash 5, and the driver has such an array. `-n` catches
        syntax, not that, so the real guard is the expansion form used at the
        call site — but a construct bash 3.2 cannot even parse would be worse.
        """
        result = subprocess.run(
            ["/bin/bash", "-n", str(DRIVER)], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
    @pytest.mark.parametrize("array", ["gitdir_bind", "progress_args"])
    def test_an_empty_optional_array_survives_set_u(self, array):
        """The exact expansions the driver uses, against the shell that breaks.

        `gitdir_bind` is empty in an ordinary clone and non-empty in a linked
        worktree, so the plain `"${a[@]}"` form worked in the checkout this was
        developed in and would have died in everyone else's. `progress_args`
        has the same shape and the same trap: it is empty on exactly the
        legacy-builder host the conditional flag exists to support, so the
        naive form would break the case it was written to fix.
        """
        form = re.search(rf'(\$\{{{array}\[@\][^\n]*?)\s*\\\n', DRIVER.read_text())
        assert form, f"could not find the {array} expansion in the driver"

        script = f'set -euo pipefail; a=(); printf "[%s]" {form.group(1).replace(array, "a")}; echo ok'
        result = subprocess.run(
            ["/bin/bash", "-c", script], capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"the driver's array expansion is fatal under set -u on "
            f"{subprocess.run(['/bin/bash', '--version'], capture_output=True, text=True).stdout.splitlines()[0]}: "
            f"{result.stderr}"
        )
        assert "ok" in result.stdout


class TestMarkerExpressionStaysInStepWithAddopts:
    """The driver restates pyproject's deselection set, and must not fall behind.

    There is no pytest syntax for "the default expression, plus linux" — `-m`
    replaces rather than composes — so the set is written out twice. The
    dangerous direction is a *new* default-deselected marker: addopts turns it
    off for everyone, the driver's stale copy does not mention it, and the
    Linux tier starts running tests nothing else runs.
    """

    def test_every_marker_addopts_deselects_is_deselected_by_the_driver(self):
        deselected = set(re.findall(r"not (\w+)", _addopts_marker_expression()))
        driver = _driver_marker_expression()

        missing = sorted(
            m for m in deselected
            if m != "linux" and not re.search(rf"\bnot {m}\b", driver)
        )
        assert not missing, (
            f"scripts/test-linux.sh does not deselect {missing}, which "
            f"pyproject's addopts does — the Linux tier would run them"
        )

    def test_the_driver_selects_linux(self):
        assert "linux" in _driver_marker_expression()

    def test_addopts_deselects_linux(self):
        """The user-facing half of the contract: Docker is never required.

        `uv run pytest` must not try to build a namespace, so the marker has to
        be off by default. This is the assertion that keeps the tier
        discretionary.
        """
        assert re.search(r"\bnot linux\b", _addopts_marker_expression())

    def test_linux_is_a_registered_marker(self):
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        markers = data["tool"]["pytest"]["ini_options"]["markers"]
        assert any(m.startswith("linux:") for m in markers)


class TestTheLinuxTierCannotSilentlySkip:
    """The tier exists to end silent non-execution, so it must not skip itself.

    The driver checks bwrap with its own probes (`--unshare-user`,
    `--unshare-net`) and the tests guard on `_bwrap_available()`, which probes
    neither — two predicates that can disagree. If they did, every linux test
    would skip, pytest would exit 0, and the driver would report a clean run
    having executed none of them. `ISTOTA_LINUX_TIER=1` collapses that: inside
    the driver, the skip path is a failure.
    """

    def test_the_container_branch_sets_the_sentinel(self):
        assert "-e ISTOTA_LINUX_TIER=1" in _function_body("run_in_container"), (
            "the sentinel must reach the container, or the guard below never fires"
        )

    def test_the_native_branch_sets_the_sentinel(self):
        """Two spellings of one requirement, which is why both are asserted.

        Container mode passes it as a `docker run -e`; native mode has to put
        it in the environment of the pytest it execs. A native run without it
        would let every `linux`-marked test skip itself and still exit 0, which
        is the exact failure the sentinel exists to make impossible — and the
        container assertion above cannot see that, because the flag spelling it
        looks for is meaningless outside a `docker run`.
        """
        body = _code_of("run_native_tier")
        assert re.search(r"^\s*export ISTOTA_LINUX_TIER=1\s*$", body, re.MULTILINE), (
            "native mode must export the sentinel too, or a native run can skip "
            "every linux test and report success"
        )

    def test_unavailable_skips_outside_the_runner(self, monkeypatch):
        from tests.linux.test_sandbox_real import _unavailable

        monkeypatch.delenv("ISTOTA_LINUX_TIER", raising=False)

        # BaseException, not Exception: pytest's Skipped and Failed both
        # derive from OutcomeException, which derives from BaseException so
        # a bare `except Exception` in test code cannot swallow an outcome.
        with pytest.raises(BaseException) as excinfo:
            _unavailable("no bwrap here")
        assert excinfo.typename == "Skipped"

    def test_unavailable_fails_inside_the_runner(self, monkeypatch):
        from tests.linux.test_sandbox_real import _unavailable

        monkeypatch.setenv("ISTOTA_LINUX_TIER", "1")

        with pytest.raises(BaseException) as excinfo:
            _unavailable("no bwrap here")
        assert excinfo.typename == "Failed"
        assert "no bwrap here" in str(excinfo.value)


class TestRunnerImagePinsItsToolchain:
    """An unpinned tool in the image decides, on its own schedule, that the
    tier is broken. Both of these gate the run: `uv` resolves the dependencies
    and `ruff check` runs before pytest is reached, so a new default rule in a
    ruff release fails a build that changed no source."""

    def test_uv_is_pinned(self):
        body = DOCKERFILE.read_text()
        assert "astral-sh/uv:latest" not in body, "pin uv, or a rebuild is not reproducible"
        assert re.search(r"astral-sh/uv:\d+\.\d+\.\d+", body)

    def test_ruff_is_pinned(self):
        body = DOCKERFILE.read_text()
        assert re.search(r"uv tool install ruff==\d+\.\d+\.\d+", body), (
            "pin ruff: it gates the whole run and is not a project dependency, "
            "so nothing else constrains its version"
        )


class TestTheBuildFlagsMatchTheBuilder:
    """`--progress` is a BuildKit flag, and the driver must not assume BuildKit.

    The legacy builder rejects it outright — `unknown flag: --progress`, before
    the Dockerfile is read. That matters because `DOCKER_BUILDKIT=0` is the
    escape from a `docker-container` default buildx builder that cannot reach
    the daemon, so the one host that needs the legacy path was the one the
    driver refused to build on (ISSUE-293).
    """

    def _run_selector(self, stub_help: str, env: dict[str, str] | None = None) -> list[str]:
        """The driver's own flag selector, against a stubbed `docker`.

        Returns the arguments it selected. `/bin/bash` rather than whatever
        `bash` resolves to, for the same reason as the rest of this class:
        3.2 is the shell this driver has to work on, and Homebrew's 5.x is
        what a developer's PATH would supply instead.

        The environment is built explicitly rather than inherited.
        `ISTOTA_TEST_BUILD_PROGRESS` is the value under test and the driver's
        own header advertises exporting it, so passing `os.environ` through
        would let a developer's debugging setting decide the assertion.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "docker"
            stub.write_text("#!/bin/sh\ncat <<'STUB_EOF'\n" + stub_help + "\nSTUB_EOF\n")
            stub.chmod(0o755)

            script = (
                'set -euo pipefail; '
                f'eval "$(sed -n \'/^build_progress_args()/,/^}}/p\' {DRIVER})"; '
                'build_progress_args'
            )
            result = subprocess.run(
                ["/bin/bash", "-c", script],
                capture_output=True,
                text=True,
                env={"PATH": f"{tmp}:/usr/bin:/bin", **(env or {})},
            )
            assert result.returncode == 0, result.stderr
            # NUL-delimited, so a value containing a space or a newline stays
            # one argument — which is the point of the delimiter.
            return [arg for arg in result.stdout.split("\0") if arg]

    def test_the_flag_is_dropped_when_the_builder_does_not_take_it(self):
        selected = self._run_selector("Usage: docker build [OPTIONS] PATH\n  -t, --tag list")
        assert selected == [], (
            "the legacy builder refuses --progress; the driver must not pass it"
        )

    def test_the_flag_is_passed_when_the_builder_takes_it(self):
        selected = self._run_selector("Usage: docker buildx build\n      --progress string")
        assert selected == ["--progress", "quiet"]

    def test_the_progress_override_still_reaches_a_buildkit_build(self):
        selected = self._run_selector(
            "      --progress string", env={"ISTOTA_TEST_BUILD_PROGRESS": "plain"},
        )
        assert selected == ["--progress", "plain"]

    def test_a_progress_value_containing_a_newline_stays_one_argument(self):
        """Otherwise it arrives as a second build context.

        `docker build --progress pl ain -f … "$REPO_ROOT"` names two contexts
        and fails on something unrelated to what was set.
        """
        selected = self._run_selector(
            "      --progress string",
            env={"ISTOTA_TEST_BUILD_PROGRESS": "pl\nain"},
        )
        assert selected == ["--progress", "pl\nain"]

    def test_the_driver_does_not_pass_progress_unconditionally(self):
        """The shape of the bug: a literal `--progress` on the build line."""
        body = DRIVER.read_text()
        # Leading whitespace allowed: the build moved inside `run_container_tier`
        # when native mode landed, and an anchored `^docker build` would then
        # match nothing and fail on "could not find" rather than on the flag.
        build_line = re.search(r"^\s*docker build .*$", body, re.MULTILINE)
        assert build_line, "could not find the docker build invocation"
        assert "--progress" not in build_line.group(0), (
            f"--progress is hardcoded on the build line: {build_line.group(0)!r}"
        )


# --- the mode switch -------------------------------------------------------

BWRAP_OK = "#!/bin/sh\nexit 0\n"

BWRAP_NO_USERNS = (
    "#!/bin/sh\n"
    'case "$*" in\n'
    '  *--unshare-user*) echo "bwrap: setting up uid map: Permission denied" >&2; exit 1 ;;\n'
    "esac\n"
    "exit 0\n"
)

BWRAP_NO_NETNS = (
    "#!/bin/sh\n"
    'case "$*" in\n'
    '  *--unshare-net*) echo "bwrap: loopback: Failed RTM_NEWADDR" >&2; exit 1 ;;\n'
    "esac\n"
    "exit 0\n"
)


@dataclass
class Resolved:
    returncode: int
    mode: str
    output: str
    systemctl_argv: list[str]


# What `systemctl list-units --type=service --state=active` prints for a
# deployment installed under the default namespace, and for one installed
# under a namespace the operator chose. The Ansible role names every unit
# `{{ istota_namespace }}-*.service` (deploy/ansible/tasks/main.yml) and
# `istota_namespace` is an inventory variable, so the second is not an exotic
# case — it is what the canonical install looks like wherever it was set.
UNITS_NONE = "systemd-journald.service loaded active running Journal Service"
UNITS_DEFAULT_NAMESPACE = "istota-scheduler.service loaded active running Istota scheduler"
UNITS_OTHER_NAMESPACE = "assistant-scheduler.service loaded active running Scheduler"


def _resolve(
    *,
    mode: str | None = None,
    uname: str = "Linux",
    bwrap: str | None = BWRAP_OK,
    units: str = UNITS_NONE,
    deployment_config: bool = False,
    print_mode: str | None = "1",
    home_config: bool = False,
) -> Resolved:
    """Ask the driver which mode this (stubbed) host resolves to.

    `ISTOTA_LINUX_TIER_PRINT_MODE=1` makes it resolve and print rather than
    run, which is the only way to check this from inside the ordinary suite:
    letting it resolve for real on a Linux machine with bwrap starts a
    recursive full pytest run under a test's own timeout.

    Every input the resolution reads is stubbed — `uname`, `bwrap`,
    `systemctl`, `docker`, `HOME` and the extra deployment config path — and
    `PATH` holds *only* the stub directory plus symlinks to the two binaries
    the driver needs to start at all. That last part is not fussiness: with
    `/usr/bin` on the path, "no bwrap here" would be false on any Linux box
    with the `bubblewrap` package installed, so the fallback cases would assert
    something different on a developer's Linux machine than on a macOS one.
    That is the same class of defect as the negative control in
    `tests/test_discretionary_tier_reach.py` that only worked on macOS.

    The `systemctl` stub records its argv, so a test can assert *what* was
    probed rather than only that the refusal plumbing works. A stub that
    answers "yes" to every question proves nothing about which question was
    asked, which is how the first version of this file missed a probe that
    could not see a deployment's own units.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        bindir = tmpdir / "bin"
        bindir.mkdir()
        argv_log = tmpdir / "systemctl-argv"

        def _stub(name: str, body: str) -> None:
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)

        # `#!/usr/bin/env bash` makes the kernel run `env`, which looks `bash`
        # up on PATH — and the driver's first line calls `dirname`. Those two
        # are the whole of what a trimmed PATH still has to supply.
        for real in ("bash", "dirname"):
            found = shutil.which(real)
            assert found, f"no {real} to link into the stub PATH"
            (bindir / real).symlink_to(found)

        _stub("uname", f'#!/bin/sh\necho "{uname}"\n')
        if bwrap is not None:
            _stub("bwrap", bwrap)
        # `echo`, not a `cat` heredoc: `cat` is an external binary and PATH
        # holds only this directory, so a heredoc stub prints nothing and the
        # driver reads an empty unit list — which looks exactly like "no
        # deployment here" and would have made every refusal test below pass
        # for the wrong reason. Found by the trimmed PATH; it is the second
        # thing that narrowing bought.
        unit_lines = "\n".join(
            "echo %s" % shlex.quote(line) for line in units.splitlines()
        )
        _stub(
            "systemctl",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{argv_log}"\n'
            'case "$*" in\n'
            f"  *list-units*)\n{unit_lines}\n"
            "    exit 0 ;;\n"
            "esac\n"
            "exit 3\n",
        )
        # A `docker` that fails everything: it stands in for a host with no
        # daemon, so no container arm of the deployment probe can fire, and a
        # resolution that wrongly reached the container path dies visibly here
        # instead of starting a real image build from inside the ordinary suite.
        _stub("docker", "#!/bin/sh\nexit 1\n")

        config = tmpdir / "config.toml"
        if deployment_config:
            config.write_text("# a deployment lives here\n")

        # The local single-user install writes here (`setup_wizard.py`'s
        # DEFAULT_CONFIG_PATH), and HOME is redirected into the temp dir, so
        # this is the real search-order path rather than an injected one.
        if home_config:
            home_cfg = tmpdir / ".config" / "istota"
            home_cfg.mkdir(parents=True)
            (home_cfg / "config.toml").write_text("# a local install lives here\n")

        env = {
            "PATH": str(bindir),
            "HOME": str(tmpdir),
            "ISTOTA_LINUX_TIER_DEPLOYMENT_CONFIG": str(config),
        }
        if print_mode is not None:
            env["ISTOTA_LINUX_TIER_PRINT_MODE"] = print_mode
        if mode is not None:
            env["ISTOTA_LINUX_TIER_MODE"] = mode

        result = subprocess.run(
            [str(DRIVER)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=120,
        )
        return Resolved(
            returncode=result.returncode,
            mode=result.stdout.strip(),
            output=result.stdout + result.stderr,
            systemctl_argv=(
                argv_log.read_text().splitlines() if argv_log.exists() else []
            ),
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
class TestTheModeSwitchResolves:
    """Which mode a host lands in, given the three things `auto` reads."""

    def test_auto_picks_native_on_a_linux_host_with_a_working_bwrap(self):
        result = _resolve()
        assert result.returncode == 0, result.output
        assert result.mode == "native", result.output

    def test_auto_falls_back_to_the_container_off_linux(self):
        """macOS is the audience the driver was written for and must not move."""
        result = _resolve(uname="Darwin")
        assert result.returncode == 0, result.output
        assert result.mode == "container", result.output

    def test_auto_falls_back_to_the_container_with_no_bwrap_on_path(self):
        result = _resolve(bwrap=None)
        assert result.returncode == 0, result.output
        assert result.mode == "container", result.output

    def test_auto_falls_back_to_the_container_when_the_user_namespace_probe_fails(self):
        result = _resolve(bwrap=BWRAP_NO_USERNS)
        assert result.returncode == 0, result.output
        assert result.mode == "container", result.output

    def test_auto_falls_back_to_the_container_when_the_network_probe_fails(self):
        """Two probes, not one, on the host as well as in the container.

        `--unshare-user` and `--unshare-net` fail for different reasons, so a
        single folded invocation would let a passing user-namespace probe vouch
        for a network namespace that cannot come up — and native mode would be
        selected for a host where every network-isolated sandbox test dies.
        """
        result = _resolve(bwrap=BWRAP_NO_NETNS)
        assert result.returncode == 0, result.output
        assert result.mode == "container", result.output

    def test_explicit_container_stays_on_the_container_path(self):
        result = _resolve(mode="container")
        assert result.returncode == 0, result.output
        assert result.mode == "container", result.output

    def test_explicit_native_is_honoured(self):
        result = _resolve(mode="native")
        assert result.returncode == 0, result.output
        assert result.mode == "native", result.output

    def test_an_unknown_mode_is_rejected_as_a_failure(self):
        """1, not 75: a typo is a broken invocation, not a tier out of reach."""
        result = _resolve(mode="nativ")
        assert result.returncode == 1, (result.returncode, result.output)
        assert "ISTOTA_LINUX_TIER_MODE" in result.output

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_a_negative_print_mode_does_not_suppress_the_run(self, value):
        """`=0` left exported must not turn the tier into a no-op that exits 0.

        This repo already states the rule twice — `ISTOTA_UPDATE_GOLDEN`'s
        `updating()` helper and `PRECOMMIT_SCANS_REQUIRED` both parse
        affirmative and negative sets rather than reading truthiness, "because
        a bare truthiness read would let `…=0` left exported in a shell turn
        every golden into a rubber stamp". Here the prize is larger: a tier
        that printed a word, ran nothing, and exited 0 is exactly the silent
        non-execution the sentinel and the bwrap probes exist to prevent.

        The docker stub fails, so a real run reaches the container branch's
        daemon precheck and exits 1 — non-zero and unmistakably not a print.
        """
        result = _resolve(mode="container", print_mode=value)
        assert result.mode != "container", (
            f"ISTOTA_LINUX_TIER_PRINT_MODE={value} was read as 'print': {result.output[:400]}"
        )
        assert result.returncode != 0

    def test_an_unparseable_print_mode_is_refused_rather_than_guessed(self):
        result = _resolve(mode="container", print_mode="maybe")
        assert result.returncode == 1, (result.returncode, result.output)
        assert "ISTOTA_LINUX_TIER_PRINT_MODE" in result.output


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
class TestNativeModeRefusesOnADeploymentHost:
    """Unsandboxed and correct is not the same as safe.

    The native tier spawns real bwrap namespaces and claims every core through
    `-n auto`. On the machine running the deployment that is the daemon's own
    machine, and the cgroup helper next door rearranges the very tree
    `task_cgroup` puts per-task groups in. So the driver probes for a
    deployment rather than trusting whoever typed the command to remember
    which box they are on.
    """

    def test_a_running_scheduler_unit_turns_auto_away(self):
        """75, not 1, and not a silent fallback.

        75 is the tier's own "did not run", shared with the sandbox refusal.
        Every real failure in this script exits 1, so an operator reading only
        the status can tell "I am on the wrong machine" from "the suite went
        red". And it is a refusal rather than a quiet drop to container mode
        because the point is that the operator finds out which box they are on.
        """
        result = _resolve(units=UNITS_DEFAULT_NAMESPACE)
        assert result.returncode == 75, (result.returncode, result.output)
        assert result.mode != "native"

    def test_a_unit_under_a_non_default_namespace_turns_auto_away(self):
        """The canonical deployment, which the first cut of this probe missed.

        The Ansible role names every unit `{{ istota_namespace }}-*.service`
        (`deploy/ansible/tasks/main.yml`, the `dest:` of each systemd
        template), and `istota_namespace` is an inventory variable. A probe
        asking `systemctl is-active istota-scheduler.service` by name is
        therefore blind on any install that set it, and `auto` would have
        resolved to native there, spawning real namespaces and claiming every
        core beside the live daemon. That is the one direction this guard
        exists to prevent.
        """
        result = _resolve(units=UNITS_OTHER_NAMESPACE)
        assert result.returncode == 75, (result.returncode, result.output)
        assert result.mode != "native"

    def test_the_probe_asks_systemd_what_is_running_rather_than_naming_units(self):
        """The mechanism, asserted, not just its outcome.

        Both cases above would also pass against a stub that answered "yes" to
        every question, which is how the namespace gap survived the first round
        of tests. Requiring `list-units` pins the probe to the shape that can
        see a unit name nobody wrote down in advance.
        """
        result = _resolve(units=UNITS_OTHER_NAMESPACE)
        assert any("list-units" in call for call in result.systemctl_argv), (
            f"the driver did not enumerate active units: {result.systemctl_argv}"
        )

    def test_an_unrelated_active_service_does_not_turn_auto_away(self):
        """The negative control for the widened probe.

        A substring match on "service" or on the namespace alone would refuse
        on every Linux box with systemd, which disables native mode for the
        developers it exists for while passing every assertion above.
        """
        result = _resolve(units=UNITS_NONE)
        assert result.returncode == 0, result.output
        assert result.mode == "native", result.output

    def test_a_deployment_config_file_turns_auto_away(self):
        """The second arm, for a host with no systemd or a stopped unit.

        A deployment that is merely installed still owns its config, and a
        stopped scheduler can be started by a timer or an operator while the
        suite is running.
        """
        result = _resolve(deployment_config=True)
        assert result.returncode == 75, (result.returncode, result.output)
        assert result.mode != "native"

    def test_a_local_single_user_install_turns_auto_away(self):
        """`~/.config/istota/config.toml` — the shape most likely on a workstation.

        `setup_wizard.py`'s `DEFAULT_CONFIG_PATH` writes there and `istota
        serve` runs it without a system unit, so neither the unit arm nor
        `/etc/istota/config.toml` sees it. This is not injected through the
        override variable: HOME is redirected and the file is at the real path
        from the config search order in `config.py`.
        """
        result = _resolve(home_config=True)
        assert result.returncode == 75, (result.returncode, result.output)
        assert result.mode != "native"

    def test_the_refusal_names_the_reason_and_both_ways_out(self):
        result = _resolve(units=UNITS_DEFAULT_NAMESPACE)
        assert "deployment" in result.output.lower(), result.output[:600]
        assert "ISTOTA_LINUX_TIER_MODE=container" in result.output, result.output[:600]
        assert "ISTOTA_LINUX_TIER_MODE=native" in result.output, result.output[:600]

    def test_explicit_native_is_the_override_for_someone_who_means_it(self):
        """Named deliberately, so it cannot be reached by forgetting.

        The refusal exists because `auto` would otherwise pick native silently.
        Someone who types the variable has said which machine they are on.
        """
        result = _resolve(mode="native", units=UNITS_DEFAULT_NAMESPACE)
        assert result.returncode == 0, result.output
        assert result.mode == "native", result.output

    def test_the_container_mode_is_unaffected_by_the_deployment_probe(self):
        """A container on a deployment host is what happens today; keep it."""
        result = _resolve(mode="container", units=UNITS_DEFAULT_NAMESPACE)
        assert result.returncode == 0, result.output
        assert result.mode == "container", result.output

    def test_the_extra_config_path_can_only_make_the_probe_stricter(self):
        """The test seam must not double as a way to switch the guard off.

        An override that *replaces* the paths probed is one exported variable
        between an operator and an unsandboxed core-claiming run beside a live
        daemon — and the driver's own message calls the two ways on "neither
        reachable by forgetting". So it adds a path and never removes one:
        pointing it at a file that does not exist leaves the real search-order
        paths being checked, which is what this asserts.
        """
        result = _resolve(deployment_config=False, home_config=True)
        assert result.returncode == 75, (result.returncode, result.output)


class TestNativeModeKeepsTheTierHonest:
    """What native mode must carry over from the container, and what it must not.

    Everything that makes the tier mean something is duplicated the moment
    there are two ways to run it: the sentinel, the marker expression and the
    lint gate. The cgroup helper is the one thing that must *not* cross over.
    """

    def test_the_marker_expression_is_written_once(self):
        """One assignment, used by both branches.

        The expression is already a restatement of pyproject's addopts, held in
        step by `TestMarkerExpressionStaysInStepWithAddopts`. A second copy
        inside a branch would put that check one branch behind: it reads the
        first `default_markers=` it finds, so a stale native copy could run a
        marker that is meant to be off by default and nothing would say so.
        """
        assignments = re.findall(r"^default_markers=", DRIVER.read_text(), re.MULTILINE)
        assert len(assignments) == 1, (
            f"{len(assignments)} default_markers= assignments; the marker "
            f"expression must be written once and shared by both modes"
        )

    @pytest.mark.parametrize("branch", ["run_native_tier", "run_container_tier"])
    def test_neither_branch_builds_its_own_marker_expression(self, branch):
        body = _function_body(branch)
        assert "default_markers=" not in body, (
            f"{branch} assigns its own marker expression"
        )
        assert "pytest_args" in body, (
            f"{branch} does not use the shared pytest_args"
        )

    def test_the_container_branch_sources_the_cgroup_helper(self):
        assert "linux-tier-cgroup.sh" in _code_of("run_container_tier")

    def test_the_native_branch_never_sources_the_cgroup_helper(self):
        """The single most destructive thing native mode could do.

        `scripts/dev/linux-tier-cgroup.sh` remounts `/sys/fs/cgroup`
        read-write, moves every pid in the root cgroup into a `supervisor`
        leaf and writes `cgroup.subtree_control`. In a throwaway container that
        is the point of the file. On a real host it rearranges the machine's
        cgroup tree, and on a deployment that tree is where the daemon's own
        per-task cgroups live.
        """
        assert "linux-tier-cgroup.sh" not in _code_of("run_native_tier"), (
            "native mode must never source the cgroup helper: it rearranges the "
            "host's own cgroup tree"
        )

    def test_the_native_branch_clears_an_inherited_cgroup_root(self):
        """Unset, not merely not-set-by-us.

        The cgroup tests treat `ISTOTA_TEST_CGROUP_ROOT` as a promise and fail
        rather than skip when it is present and unusable, so a value left
        exported in the caller's shell would turn the documented native-mode
        skip into a red suite.
        """
        body = _code_of("run_native_tier")
        assert re.search(r"\bunset ISTOTA_TEST_CGROUP_ROOT\b|-u ISTOTA_TEST_CGROUP_ROOT", body), (
            "native mode must clear ISTOTA_TEST_CGROUP_ROOT out of the environment"
        )

    def test_the_native_branch_runs_from_the_repo_root(self):
        """Container mode is cwd-immune by construction; native mode is not.

        The container pins everything — `-v "$REPO_ROOT:/src:ro"`, `-w /src`,
        an absolute `-f` Dockerfile path — so where the driver was invoked from
        cannot reach it. Native mode runs `uv run ruff check … src tests
        testbed` and `uv run pytest`, both of which resolve against the
        process's cwd: `uv` walks up from there to find a project, and the
        three ruff paths are relative. Invoked from inside some other checkout
        it would lint and collect that one and report the answer as this
        repository's.
        """
        body = _code_of("run_native_tier")
        assert re.search(r'^\s*cd "\$REPO_ROOT"\s*$', body, re.MULTILINE), (
            "native mode must cd to REPO_ROOT before running uv"
        )
        assert body.index('cd "$REPO_ROOT"') < body.index("uv run"), (
            "the cd must come before the first uv invocation"
        )

    @pytest.mark.parametrize(
        "var",
        ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"],
    )
    def test_both_branches_supply_a_git_identity(self, var):
        """A dozen tests build throwaway repositories and commit into them.

        `run_in_container` says why it passes these rather than setting a
        global config: they survive the tests that repoint HOME. Native mode
        inherits the host's identity where there is one — but a fresh VM or a
        CI runner has none, and git's "Please tell me who you are" then reads
        as a code regression rather than as a missing prerequisite. The
        variables cost nothing where an identity already exists.
        """
        assert var in _code_of("run_in_container")
        assert var in _code_of("run_native_tier")

    @pytest.mark.parametrize("branch", ["run_native_tier", "run_container_tier"])
    def test_ruff_gates_both_branches(self, branch):
        """A lint failure is cheaper to read before a suite's worth of output.

        Ordered against the *invocation* rather than the word "pytest", which
        also appears in the container branch's daemon precheck ("'uv run
        pytest' on the host does not need it") several lines above ruff.
        """
        body = _code_of(branch)
        ruff = re.search(r"\bruff check\b", body)
        run = re.search(r"\bexec (?:uv run (?:--frozen )?)?pytest\b", body)
        assert ruff, f"{branch} does not run ruff"
        assert run, f"{branch} does not exec pytest"
        assert ruff.start() < run.start(), f"{branch} runs pytest before ruff"


class TestTheDriverHeaderDescribesBothModes:
    """The header is the only documentation a reader of the script itself gets.

    It said, flatly, that the driver builds a Debian image — which after this
    change is true of one of two modes, and false on exactly the host where the
    other one is picked.
    """

    def _header(self) -> str:
        lines = []
        for line in DRIVER.read_text().splitlines():
            if line.startswith("#!"):
                continue
            if not line.startswith("#"):
                break
            lines.append(line)
        return "\n".join(lines)

    def test_the_header_names_the_mode_variable_and_all_three_values(self):
        """One literal, not three word searches.

        "auto", "native" and "container" are all ordinary English words that
        the old header already contained by accident, so asserting each on its
        own passes against a header that documents none of them.
        """
        assert "ISTOTA_LINUX_TIER_MODE=auto|native|container" in self._header()
