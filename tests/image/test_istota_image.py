"""What the shipped image actually contains, asserted against the built image.

Three groups, and the split is the design rather than an arrangement:

**Group A — the doctor umbrella.** `istota doctor` is the oracle: the one place
every environmental fact istota depends on is written down, so an image test can
run it instead of hand-writing thirty assertions that drift from the code.

**Group B — independent witnesses.** A self-check cannot be the only witness to
its own assumptions. The same blind spot that made the code look for
`/usr/local/bin/gh` would have made a doctor check look there too. So the facts
that matter most are also asserted directly, with the expected value written as
a **literal here** — not imported from `istota`, not parsed out of the
Dockerfile. A witness whose oracle vanishes along with its subject is not a
witness.

**Group C — the generated config.** `render-config.sh` under three fabricated
environments, one of which exists specifically to make Group A's forge checks
run rather than skip.

Two traps this file is built around, both found by reviewing an earlier draft of
the spec against the bug it was written to catch:

*The skip trap.* The compose defaults leave `repos_dir` and both tokens empty,
so the forge checks `skip` — and "no fail under the compose defaults" is green
on the exact pre-fix image that shipped ISSUE-263. Group A therefore names the
environment that makes the checks it cares about actually run, and asserts they
came back `ok` rather than merely not-failing. An assertion that tolerates
`skip` is not an assertion.

*The vacuity trap, found the hard way.* The statuses in `--json` are lowercase;
the text renderer uppercases them for display. The first draft of this file
filtered on `"FAIL"`, matched nothing, and passed every Group A assertion on a
correct image and equally on one with no forge binaries in it. Nothing in the
file could have caught that — only pointing the tier at
`docker/test/Dockerfile.no-forge` did. Hence
`test_the_status_vocabulary_is_what_this_file_expects`, which fails loudly if
these literals ever stop matching the product's vocabulary.

*The scope trap.* `run_in` is a volume-less `docker run`, where
`runtime.writable_dirs`, `runtime.framework_db` and `runtime.mount_liveness` are
`FAIL` against a perfectly good image — `/mnt/shared` does not exist and `/data`
holds no DB. Asserting over the whole registry would fail on a correct image,
and the tempting repair is to soften those checks, which weakens the runtime
product to make a test green. `--scope image` is what makes the right fix the
easy one.
"""

from __future__ import annotations

import json
import re

import pytest

from .conftest import REPO, assert_ok, sh

pytestmark = pytest.mark.image


# --- Literals. Deliberately not imported from the code under test. ------------

FORGE_LIB = "/usr/local/lib/istota_forge"

# The wire vocabulary of `doctor --json`. Lowercase — the *text* renderer
# uppercases for display and the JSON does not, and the first draft of this file
# compared against "FAIL". Nothing matched, so every Group A assertion was
# vacuously true, on a correct image and equally on one with no forge binaries
# at all. The negative control is what found it, which is the whole reason
# Stage 5's acceptance criterion demands one.
#
# `test_the_status_vocabulary_is_what_this_file_expects` below is the structural
# repair: if doctor's vocabulary ever moves again, these literals fail loudly
# instead of quietly matching nothing.
STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP = "ok", "warn", "fail", "skip"
KNOWN_STATUSES = {STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP}

# The declared `[project.scripts]`, written out rather than read from
# pyproject.toml: the question is whether the image puts these on PATH, and a
# list derived from the same file the build reads cannot answer it.
#
# The spec's own Group B list said `money --help` here. There is no `money`
# console script and there never was — `[project.scripts]` declares three, and
# the money CLI is reached as `istota-skill money`, which is what
# `src/istota/skills/money/skill.md` documents. Asserted below as itself.
CONSOLE_SCRIPTS = ("istota", "istota-skill", "istota-scheduler")
VENV_BIN = "/app/.venv/bin"
WEB_INDEX = "/app/web/build/index.html"
RENDER_CONFIG = "/render-config.sh"
ENTRYPOINT = "/entrypoint.sh"


def _dockerfile_arg(name: str) -> str:
    """The pinned value of a Dockerfile ARG.

    An absent or unparseable ARG is a **failure**, never a skip: this is the
    version the image claims to ship, and a test that shrugs at not finding it
    asserts nothing.
    """
    body = (REPO / "docker" / "istota" / "Dockerfile").read_text()
    match = re.search(rf"^ARG\s+{name}=(\S+)", body, re.M)
    assert match, f"{name} is not pinned in docker/istota/Dockerfile"
    return match.group(1)


# --- Group C's three environments, which Group A also consumes. ---------------


def _base_env(**extra: str) -> dict[str, str]:
    """The inputs render-config.sh needs, plus whatever the shape adds."""
    return {
        "CONFIG_FILE": "/tmp/rendered.toml",
        "USER_NAME": "testuser",
        "NC_URL": "http://nextcloud:80",
        "APP_PASSWORD": "app-password-value",
        "BOT_USER": "istota",
        **extra,
    }


# The third shape is the one with teeth. Without a token the forge checks skip,
# and a skipping check cannot fail on a broken image.
ENVIRONMENTS = {
    "developer-off": _base_env(),
    "developer-no-token": _base_env(
        ISTOTA_DEVELOPER_ENABLED="true",
        ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
    ),
    # The token values carry no real forge prefix on purpose: the checks that
    # consume them only ask whether a token is configured, and a fixture shaped
    # like a real credential trips the repo's own secret scanner.
    "developer-with-token": _base_env(
        ISTOTA_DEVELOPER_ENABLED="true",
        ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
        ISTOTA_DEVELOPER_GITLAB_TOKEN="fabricated-gitlab-token-for-tests",
        ISTOTA_DEVELOPER_GITLAB_URL="http://gitlab.test",
        ISTOTA_DEVELOPER_GITHUB_TOKEN="fabricated-github-token-for-tests",
    ),
}

# The environments whose forge checks must actually run. Named rather than
# derived: deriving it from "has a token" would make the assertion agree with
# whatever the dict happens to say, including after someone removes the token.
FORGE_ENVIRONMENTS = ("developer-with-token",)


def _render_and_doctor(image, env: dict[str, str]) -> list[dict]:
    """Render a config in the container, then run doctor against it.

    One `docker run`, not two: the rendered file lives in the container's
    filesystem and a second `--rm` run would not see it.
    """
    config = env["CONFIG_FILE"]
    result = sh(
        image,
        # `-c` is a global option and goes before the subcommand.
        f"{RENDER_CONFIG} >/dev/null && istota -c {config} doctor --json --scope image",
        env=env,
    )
    # doctor exits 1 on any FAIL, which is a result to inspect rather than a
    # crash — so the exit code is asserted per-test below, not here. What must
    # hold is that we got JSON at all.
    assert result.stdout.strip(), (
        f"no doctor output\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        pytest.fail(
            f"doctor --json emitted invalid JSON ({exc})\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
            pytrace=False,
        )


class TestGroupATheDoctorUmbrella:
    """`istota doctor --scope image` is clean, in environments that make it fire."""

    @pytest.mark.parametrize("shape", sorted(ENVIRONMENTS))
    def test_the_status_vocabulary_is_what_this_file_expects(self, istota_image, shape):
        """Every status literal below has to match something, or it asserts nothing.

        This is the guard that would have caught the lowercase/uppercase slip
        immediately instead of leaving four vacuous tests behind a green run.
        It compares the whole observed vocabulary against the expected one, so a
        status this file does not know about is a failure rather than a filter
        that silently selects nothing.
        """
        results = _render_and_doctor(istota_image, ENVIRONMENTS[shape])

        observed = {r["status"] for r in results}
        assert observed, "doctor reported no checks at all"
        assert observed <= KNOWN_STATUSES, (
            f"doctor --json used a status this file does not know about: "
            f"{sorted(observed - KNOWN_STATUSES)}. Every filter below is now "
            "matching nothing and passing for it."
        )

    @pytest.mark.parametrize("shape", sorted(ENVIRONMENTS))
    def test_no_check_fails(self, istota_image, shape):
        results = _render_and_doctor(istota_image, ENVIRONMENTS[shape])

        failed = [r for r in results if r["status"] == STATUS_FAIL]
        assert not failed, "\n".join(
            f"{r['name']}: {r['detail']} — {r['remedy']}" for r in failed
        )

    @pytest.mark.parametrize("shape", sorted(ENVIRONMENTS))
    def test_the_run_produced_checks_at_all(self, istota_image, shape):
        # Guard on the umbrella. A `--scope image` that filtered everything out
        # would make the assertion above vacuously true, and the failure mode
        # looks identical to a healthy image.
        results = _render_and_doctor(istota_image, ENVIRONMENTS[shape])

        assert len(results) >= 5, f"only {len(results)} checks ran: {results}"

    @pytest.mark.parametrize("shape", FORGE_ENVIRONMENTS)
    def test_the_forge_checks_ran_and_passed(self, istota_image, shape):
        # The whole reason this environment exists. Under the compose defaults
        # these checks skip, and "no FAIL" is then green on the pre-fix image
        # that shipped ISSUE-263.
        #
        # Asserted positively — every forge check reported `ok` — rather than as
        # "did not skip". Verified against the negative control: on an image
        # with /usr/local/lib/istota_forge removed, this reports `fail` on both.
        results = _render_and_doctor(istota_image, ENVIRONMENTS[shape])
        forge = [r for r in results if r["name"].startswith("developer.forge_binaries")]

        assert forge, "no developer.forge_binaries check ran at all"
        not_ok = [r for r in forge if r["status"] != STATUS_OK]
        assert not not_ok, (
            "the forge checks did not come back clean in the environment built "
            "to make them run: "
            + "\n".join(f"{r['name']}: {r['status']} — {r['detail']}" for r in not_ok)
        )

    @pytest.mark.parametrize("shape", sorted(ENVIRONMENTS))
    def test_every_warning_carries_a_remedy(self, istota_image, shape):
        # A WARN an operator cannot act on is a line of noise that trains them
        # to ignore the next one.
        results = _render_and_doctor(istota_image, ENVIRONMENTS[shape])
        mute = [r for r in results if r["status"] == STATUS_WARN and not r["remedy"]]

        assert not mute, [r["name"] for r in mute]


class TestGroupBTheForgeBinaries:
    """The assertion that fails on the pre-fix image.

    `/usr/local/lib/istota_forge/gh` is a literal here. Importing the path from
    `istota` would mean the test and the bug share an oracle: the code looked in
    the wrong place, so a check derived from the code would look there too.
    """

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_binary_exists_and_is_executable(self, istota_image, binary):
        path = f"{FORGE_LIB}/{binary}"
        result = sh(istota_image, f"test -x {path} && echo PRESENT")

        assert_ok(result, f"{path} is not an executable file")
        assert "PRESENT" in result.stdout

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_binary_actually_runs(self, istota_image, binary):
        # `dpkg-deb | tar` has no pipefail under dash, so a truncated extract
        # leaves a file that exists, is executable, and is not a program.
        assert_ok(
            sh(istota_image, f"{FORGE_LIB}/{binary} --version"),
            f"{FORGE_LIB}/{binary} --version",
        )

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_version_matches_the_pin(self, istota_image, binary, arg):
        # Marginal on its own — the Dockerfile already runs both --version calls
        # inside the build behind sha256-pinned downloads, so an image that
        # builds satisfies most of this. Kept because a version bump that edits
        # the ARG without the checksum is cheap to catch here.
        pinned = _dockerfile_arg(arg)
        out = assert_ok(
            sh(istota_image, f"{FORGE_LIB}/{binary} --version"), f"{binary} --version"
        )

        assert pinned in out, f"expected {pinned} in {out!r}"

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_nothing_resolves_the_binary_by_name(self, istota_image, binary):
        # Honest note: this passes trivially on an image that installs neither
        # binary, so it guards against a *future* regression — someone
        # `apt install`s gh and puts a real one on PATH, where the model's shell
        # would reach it before the per-task policy wrapper.
        result = sh(istota_image, f"command -v {binary} || true")

        assert not result.stdout.strip(), (
            f"{binary} resolves on the default PATH to {result.stdout.strip()!r}; "
            "the policy wrapper is supposed to be the only one a task can reach"
        )

    def test_neither_is_installed_as_a_package(self, istota_image):
        # Same character and same limitation as the previous test: `dpkg -i`
        # would drop a real binary at /usr/bin, resolvable by name.
        result = sh(istota_image, "dpkg-query -W gh glab 2>&1 || true")

        assert "no packages found" in result.stdout.lower(), result.stdout


class TestGroupBTheRuntime:
    @pytest.mark.parametrize(
        "command",
        [
            "bwrap --version",
            "claude --version",
            "tmux -V",
            "sqlite3 --version",
            "uv --version",
            "git --version",
            "tesseract --version",
        ],
    )
    def test_the_tool_is_present_and_runs(self, istota_image, command):
        assert_ok(sh(istota_image, command), command)

    @pytest.mark.parametrize("script", CONSOLE_SCRIPTS)
    def test_the_console_script_resolves_on_the_default_path(self, istota_image, script):
        # This pins the purpose of the Dockerfile's `ENV PATH` line. Without it
        # the tmux brain shells out and sees "istota-skill: command not found",
        # because a per-process PATH injection does not survive the interactive
        # TUI re-initializing its profile. No `uv run`, no profile, on purpose.
        #
        # `command -v` rather than `--help`: `istota-skill` is a dispatcher that
        # takes a skill name, so it exits 1 on `--help` while resolving
        # perfectly well. The question here is resolution, and conflating it
        # with an exit code makes the test fail for the wrong reason.
        result = sh(istota_image, f"command -v {script}")
        resolved = assert_ok(result, f"command -v {script}").strip()

        assert resolved == f"{VENV_BIN}/{script}", (
            f"{script} resolves to {resolved!r}, not the venv the image installs"
        )

    @pytest.mark.parametrize("script", CONSOLE_SCRIPTS)
    def test_the_console_script_actually_starts(self, istota_image, script):
        # Resolution alone would pass on a dangling symlink or a script whose
        # shebang points at a python that is not there. 127 is "not found";
        # anything else means the interpreter ran it.
        result = sh(istota_image, f"{script} --help >/dev/null 2>&1; echo $?")

        assert result.stdout.strip() != "127", f"{script} did not start"

    def test_the_money_cli_is_reachable_through_the_skill_dispatcher(self, istota_image):
        # There is no `money` binary; this is the entry point skill.md documents
        # and the one a task actually uses. It is here because the money extra
        # is a heavy install the image claims to make — an `istota[all]` sync
        # that quietly dropped it would surface first as a failing task.
        assert_ok(
            sh(istota_image, "istota-skill money --help"), "istota-skill money --help"
        )

    def test_the_web_build_is_present_and_not_empty(self, istota_image):
        # The web-builder stage can succeed and copy nothing; adapter-static
        # writing no index.html looks identical to a healthy build from here.
        result = sh(istota_image, f"test -s {WEB_INDEX} && wc -c < {WEB_INDEX}")

        assert_ok(result, f"{WEB_INDEX} is missing or empty")
        assert int(result.stdout.strip()) > 0

    @pytest.mark.parametrize("module", ["istota", "weasyprint"])
    def test_the_module_imports(self, istota_image, module):
        assert_ok(sh(istota_image, f"python -c 'import {module}'"), f"import {module}")

    @pytest.mark.parametrize("script", [ENTRYPOINT, RENDER_CONFIG])
    def test_the_shell_script_parses(self, istota_image, script):
        # `bash -n`, not `sh -n`: both files are #!/bin/bash, /bin/sh in this
        # image is dash, and dash would check the wrong grammar — giving a
        # different verdict here than the same check run on a macOS host.
        assert_ok(sh(istota_image, f"bash -n {script}"), f"bash -n {script}")

    @pytest.mark.parametrize("script", [ENTRYPOINT, RENDER_CONFIG])
    def test_the_shell_script_is_executable(self, istota_image, script):
        assert_ok(sh(istota_image, f"test -x {script}"), f"{script} is not executable")


class TestGroupCTheGeneratedConfig:
    """render-config.sh runs in the container and produces a loadable config.

    The path assertions are an explicit list of four, not a sweep. An earlier
    draft said "every filesystem path the resulting Config names either exists
    or is created on demand", which has no oracle: Config names a couple of
    dozen paths, almost none exist in a volume-less container, and "created on
    demand" becomes a hand-maintained allowlist unlinked from the code that
    creates them. A path field added later would then land in neither list and
    be silently unchecked, while the test kept passing over a shrinking
    fraction. Adding a fifth here is a deliberate edit.
    """

    @pytest.mark.parametrize("shape", sorted(ENVIRONMENTS))
    def test_the_render_succeeds(self, istota_image, shape):
        env = ENVIRONMENTS[shape]
        result = sh(istota_image, f"{RENDER_CONFIG} && test -s {env['CONFIG_FILE']}", env=env)

        assert_ok(result, f"render-config.sh under {shape}")

    @pytest.mark.parametrize("shape", sorted(ENVIRONMENTS))
    def test_load_config_accepts_the_result(self, istota_image, shape):
        env = ENVIRONMENTS[shape]
        # load_config takes a Path, not a str — it calls `.exists()` on it.
        script = (
            f"{RENDER_CONFIG} >/dev/null && python -c "
            f"'from pathlib import Path; from istota.config import load_config; "
            f'c = load_config(Path("{env["CONFIG_FILE"]}")); print(sorted(c.users))\''
        )
        result = sh(istota_image, script, env=env)

        assert_ok(result, f"load_config on the config rendered under {shape}")
        assert "testuser" in result.stdout

    def test_the_forge_paths_the_config_names_exist(self, istota_image):
        # Where ISSUE-263 lived, asserted end to end: the config names a path
        # and the path is a program. `_resolve_real_bin` is in the loop on
        # purpose — a config naming the old /usr/local/bin default should still
        # resolve to the shipped binary, and this is where that is observable.
        env = ENVIRONMENTS["developer-with-token"]
        script = (
            f"{RENDER_CONFIG} >/dev/null && python -c "
            f"'from pathlib import Path; from istota.config import load_config; "
            f"from istota.forge_bin import resolve_real_bin; "
            f'c = load_config(Path("{env["CONFIG_FILE"]}")).developer; '
            f'print(resolve_real_bin(c.gh_bin_path, "gh")); '
            f'print(resolve_real_bin(c.glab_bin_path, "glab"))\''
        )
        result = sh(istota_image, script, env=env)
        assert_ok(result, "resolving the configured forge paths")

        resolved = result.stdout.split()
        assert len(resolved) == 2, result.stdout
        for path in resolved:
            assert_ok(sh(istota_image, f"test -x {path}"), f"configured path {path}")

    def test_the_claude_binary_the_brain_shells_out_to_exists(self, istota_image):
        # The CLI brains exec `claude` by name; an image without it fails at the
        # first task rather than at boot.
        assert_ok(sh(istota_image, "command -v claude"), "claude on PATH")

    def test_the_web_root_the_config_serves_exists(self, istota_image):
        # `static_dir` is what the web service serves; a config naming a
        # directory the build never produced 404s the whole UI.
        assert_ok(sh(istota_image, f"test -s {WEB_INDEX}"), WEB_INDEX)
