"""The suite's result must not depend on what the ambient shell exports.

ISSUE-301: `tests/conftest.py` was careful about process globals — autouse
fixtures reset the async runtime singleton, the TalkClient, the warned-hosts
caches, the yfinance lookup — and careless about the environment. It only ever
called `os.environ.setdefault` and never removed anything, so every variable the
shell happened to carry was visible to every test, and a test asserting on a
default got the operator's real value instead. Thirty of the thirty-two failures
on the deployment host were this — eleven reading a real value, nineteen sending
a loopback stub server through a configured proxy — and none of them said
anything about the code.

That is not a cosmetic problem, because the environments carrying the real
config are exactly the ones istota runs in: a task sandbox, a cron `command`
job, an operator shell on the server. The suite was unrunnable-as-written
wherever the code actually lives.

Two halves are checked here. `scrubbed_env_names` is the policy — a pure
function over a mapping, so the whole table of what goes and what stays is
assertable without a subprocess. `TestTheScrubHoldsUnderARealRun` is the seam:
a real pytest process, started with the polluting variables exported, running
the tests that ISSUE-301 reported as failing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .support.env_isolation import (
    CREDENTIAL_PATTERNS,
    NO_PROXY_NAMES,
    NO_PROXY_VALUE,
    NO_SCRUB_FLAG,
    SUITE_ENV_DEFAULTS,
    manifest_env_names,
    scrubbed_env_names,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestTheManifestNamesAreDerivedNotTyped:
    """The skill manifests are the source of the non-prefixed names.

    A hand-maintained list is how ISSUE-301 happened: it grows a hole every
    time a skill declares a variable, and the hole is silent until a test that
    asserts on a default starts reading the operator's value. Scraping
    `skill.md` means a new skill's variables are scrubbed the day they are
    declared.
    """

    def test_the_scrape_found_the_manifests(self):
        # A regex that quietly stopped matching would empty the set and make
        # every assertion below vacuous while staying green.
        assert len(manifest_env_names()) > 30, (
            f"only scraped {len(manifest_env_names())} env names from the skill "
            "manifests — the frontmatter shape probably changed"
        )

    @pytest.mark.parametrize(
        "name",
        [
            "NTFY_SERVER_URL",  # Group A: the ntfy topic-override failures
            "NTFY_TOPIC",
            "CALDAV_URL",  # Group A: the scheduler command-task failures
            "CALDAV_USERNAME",
            "NC_URL",
            "NC_USER",
            "GITLAB_TOKEN",
            "IMAP_PASSWORD",
        ],
    )
    def test_a_declared_variable_is_scraped(self, name):
        assert name in manifest_env_names()

    def test_every_scraped_name_is_scrubbed(self):
        polluted = {name: "ambient" for name in manifest_env_names()}
        assert scrubbed_env_names(polluted) == set(polluted)

    def test_it_reads_the_frontmatter_shape_a_manifest_actually_uses(self, tmp_path):
        # The regex against a manifest written here rather than against the real
        # tree, so the *shape* it depends on is stated. `test_the_scrape_found_
        # the_manifests` says the count is plausible; this says why.
        manifest = tmp_path / "example" / "skill.md"
        manifest.parent.mkdir()
        manifest.write_text(
            "---\n"
            'name: example\n'
            'env: [{"var":"EXAMPLE_TOKEN","from":"secret","service":"x",'
            '"key":"token","sensitive":true},'
            '{"var":"EXAMPLE_BASE_URL","from":"secret","service":"x","key":"url"}]\n'
            "---\n\n"
            "# Example\n\n"
            'Prose mentioning "var":"NOT_A_DECLARATION" is matched too, which is\n'
            "fine: over-scrubbing a name nothing reads costs nothing.\n"
        )
        assert manifest_env_names(tmp_path) == {
            "EXAMPLE_TOKEN", "EXAMPLE_BASE_URL", "NOT_A_DECLARATION",
        }

    def test_a_missing_skills_tree_scrapes_nothing(self, tmp_path):
        # The silent-narrowing failure mode, stated. `_scrape` swallows an
        # unreadable manifest and `glob` on a missing root yields nothing, so a
        # wrong SKILLS_ROOT produces an empty set rather than an error — which
        # is what `test_the_scrape_found_the_manifests` is there to catch.
        assert manifest_env_names(tmp_path / "no-such-dir") == frozenset()


class TestTheCredentialPatternsHaveNotDrifted:
    """`env_isolation.CREDENTIAL_PATTERNS` is a copy, so it needs a guard.

    The import is here rather than at module scope in `env_isolation` because
    `istota.executor` costs 666ms on top of what `conftest.py` already pulls
    in, and a conftest import is paid at collection by every xdist worker on
    every run. Here it is paid once.
    """

    def test_the_copy_matches_the_original(self):
        from istota.executor import _CREDENTIAL_ENV_PATTERNS

        assert CREDENTIAL_PATTERNS == _CREDENTIAL_ENV_PATTERNS, (
            "executor's credential patterns changed; copy the new set into "
            "tests/support/env_isolation.py"
        )


class TestThePolicy:
    @pytest.mark.parametrize(
        "name",
        [
            # The framework namespace, closed by default.
            "ISTOTA_SANDBOXED",
            "ISTOTA_SKILL_PROXY_SOCK",
            "ISTOTA_DB_PATH",
            "ISTOTA_CONFIG_PATH",
            "ISTOTA_DEFERRED_DIR",
            "ISTOTA_USER_ID",
            "ISTOTA_SECRET_KEY",
            "ISTOTA_A_VARIABLE_INVENTED_TOMORROW",
            # Credential shape, whatever the prefix.
            "SOME_VENDOR_API_KEY",
            "SOME_VENDOR_TOKEN",
            "SOME_VENDOR_PASSWORD",
            "SOME_VENDOR_SECRET",
            # Named leftovers that no manifest declares.
            "NEXTCLOUD_MOUNT_PATH",
            "BROWSER_API_URL",
            "WHISPER_MAX_MODEL",
            "PRECOMMIT_SCANS_REQUIRED",
            # git reads these at "command line" scope, above every config file,
            # so the `GIT_CONFIG_NOSYSTEM` + `GIT_CONFIG_GLOBAL=/dev/null` pair
            # that six fixture helpers set does not neutralise them. istota's
            # own developer skill exports the trio.
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
        ],
    )
    def test_it_goes(self, name):
        assert scrubbed_env_names({name: "ambient"}) == {name}

    @pytest.mark.parametrize(
        "name",
        [
            # The harness's own inputs. These are read from the ambient shell
            # on purpose — they are how a person selects a discretionary tier —
            # and they live in `tests/`, not in a skill manifest.
            "ISTOTA_TEST_CGROUP_ROOT",
            "ISTOTA_TEST_PLATFORM",
            "ISTOTA_TEST_KEEP",
            "ISTOTA_TESTBED_KEEP",
            "ISTOTA_IMAGE_TAG",
            "ISTOTA_DEVBOX_IMAGE_TAG",
            "ISTOTA_LINUX_TIER",
            "ISTOTA_UPGRADE_FROM",
            "ISTOTA_UPGRADE_SHAPES",
            # Ordinary shell furniture. Deleting any of these breaks every
            # test that spawns a subprocess.
            "PATH",
            "HOME",
            "TMPDIR",
            "LANG",
            "TERM",
            "VIRTUAL_ENV",
            "XDG_CACHE_HOME",
            # Docker's own client config. The image/smoke/full tiers need it,
            # and a host on colima or orbstack sets DOCKER_HOST.
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            # The proxy variables stay: four of the seven discretionary tiers
            # reach the network from a test body, and on the proxied host that
            # reported this issue deleting these would trade nineteen failures
            # for a tier that cannot reach anything. `NO_PROXY` is what gets
            # forced instead — see TestTheProxyIsBypassedForLoopback.
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            # Contains TOKEN and is not a credential.
            "TOKENIZERS_PARALLELISM",
        ],
    )
    def test_it_stays(self, name):
        assert scrubbed_env_names({name: "ambient"}) == set()

    def test_the_keep_list_beats_the_prefix(self):
        # `ISTOTA_TEST_CGROUP_ROOT` matches the `ISTOTA_` prefix and must
        # survive it anyway; without this the linux tier loses its root.
        both = {"ISTOTA_TEST_CGROUP_ROOT": "/sys/fs/cgroup", "ISTOTA_USER_ID": "alice"}
        assert scrubbed_env_names(both) == {"ISTOTA_USER_ID"}

    def test_the_suite_defaults_are_scrubbed_before_being_re_set(self):
        # They match the `ISTOTA_` prefix and are deliberately not kept: the
        # fixture sets them to the suite's value afterwards, so an ambient
        # `ISTOTA_FEEDS_SKIP_DEFAULT_SEED=0` cannot survive into a test.
        assert set(SUITE_ENV_DEFAULTS) <= scrubbed_env_names(
            dict.fromkeys(SUITE_ENV_DEFAULTS, "0")
        )

    def test_it_reports_only_names_that_are_present(self):
        assert scrubbed_env_names({}) == set()


class TestTheProxyIsBypassedForLoopback:
    """Group B, at the level the failure actually happened.

    Nineteen tests drive stub HTTP servers on `127.0.0.1:<ephemeral>` and were
    sent to a configured proxy, which answered 405 or refused. The fix is
    `NO_PROXY`, not deleting the proxy — so what has to hold is that the two
    clients the suite uses agree loopback is direct while an external host is
    still proxied, which is what keeps the Docker tiers working on a proxied
    host.
    """

    @pytest.fixture(autouse=True)
    def _behind_a_proxy(self, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    def test_the_fixture_forced_no_proxy(self):
        for name in NO_PROXY_NAMES:
            assert os.environ[name] == NO_PROXY_VALUE

    @pytest.mark.parametrize(
        "host", ["127.0.0.1:45743", "127.0.0.1", "localhost:8080", "localhost"]
    )
    def test_urllib_goes_direct_to_loopback(self, host):
        import urllib.request

        assert urllib.request.proxy_bypass(host) is True

    def test_urllib_still_proxies_an_external_host(self):
        import urllib.request

        # The half that makes this a bypass rather than a deletion. If this
        # ever returns True the Docker tiers have lost their proxy and the
        # reasoning in env_isolation.NO_PROXY_VALUE no longer holds.
        assert not urllib.request.proxy_bypass("gitlab.example.com")
        assert urllib.request.getproxies().get("http") == "http://127.0.0.1:9"

    @pytest.mark.parametrize("url", ["http://127.0.0.1:45743/x", "http://localhost/x"])
    def test_httpx_mounts_no_transport_for_loopback(self, url):
        import httpx

        client = httpx.Client(trust_env=True)
        try:
            assert client._transport_for_url(httpx.URL(url)) is client._transport
        finally:
            client.close()


_AMBIENT = {
    "NTFY_SERVER_URL": "https://ntfy.example.com",
    "CALDAV_URL": "https://dav.example.com/remote.php/dav",
    "NC_URL": "https://nc.example.com",
    "ISTOTA_SANDBOXED": "1",
    "GIT_DIR": "/some/other/repo/.git",
    "ISTOTA_FEEDS_SKIP_DEFAULT_SEED": "0",
}


@pytest.fixture(scope="module")
def _polluted_shell():
    """The operator's shell, as far as a test in this module can tell.

    Module-scoped and writing `os.environ` directly, because a function-scoped
    `monkeypatch` would be undone by the same teardown as the scrub and prove
    nothing. Set before the autouse scrub runs for each test in the class, so
    what the test body sees is the scrub's work rather than an empty shell —
    which is the only way these assertions can fail on a developer laptop.

    The cost of that scope is a window: between setup and teardown these values
    are really in `os.environ`, and a *session*-scoped fixture created lazily
    during one of this module's tests would see them un-scrubbed —
    `testbed/stack.py::conflicting_process_env` refuses a compose boot over
    exactly that. Nothing here requests one (every test in the class is pure),
    and the marker-selected tiers that do never collect this module. Keep it
    that way: this fixture must not grow a consumer that boots anything.
    """
    saved = {name: os.environ.get(name) for name in _AMBIENT}
    os.environ.update(_AMBIENT)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.mark.usefixtures("_polluted_shell")
class TestTheFixtureRan:
    """What the autouse fixture leaves behind, observed from a test body."""

    @pytest.mark.parametrize(
        "name",
        ["NTFY_SERVER_URL", "CALDAV_URL", "NC_URL", "ISTOTA_SANDBOXED", "GIT_DIR"],
    )
    def test_a_scrubbed_name_is_absent(self, name):
        assert os.environ.get(name) != _AMBIENT[name]
        assert name not in os.environ

    def test_a_suite_default_beats_the_ambient_value(self):
        # `_polluted_shell` sets it to "0". The scrub deletes it with everything
        # else and the fixture puts the suite's own value back, so the shell
        # cannot turn the feeds seeder on under thousands of tests that assume
        # an empty DB.
        assert os.environ["ISTOTA_FEEDS_SKIP_DEFAULT_SEED"] == "1"

    @pytest.mark.parametrize(("name", "value"), sorted(SUITE_ENV_DEFAULTS.items()))
    def test_a_suite_default_is_present(self, name, value):
        assert os.environ.get(name) == value


@pytest.fixture
def _clears_a_suite_default(monkeypatch):
    """Stands in for `test_feeds_migrate.ctx` and `money/test_migrate.seed_ctx`.

    Both clear a suite default in a plain function-scoped fixture and expect it
    to stay cleared for the test body. That only holds because pytest sets up
    autouse fixtures before explicitly-requested ones at the same scope, which
    is the ordering the scrub depends on and nothing else pins.
    """
    monkeypatch.delenv("ISTOTA_FEEDS_SKIP_DEFAULT_SEED", raising=False)


class TestFixtureOrdering:
    def test_a_requested_fixture_can_still_clear_a_suite_default(
        self, _clears_a_suite_default
    ):
        assert "ISTOTA_FEEDS_SKIP_DEFAULT_SEED" not in os.environ

    def test_a_test_body_can_still_clear_a_suite_default(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_FEEDS_SKIP_DEFAULT_SEED", raising=False)
        assert "ISTOTA_FEEDS_SKIP_DEFAULT_SEED" not in os.environ


class TestTheScrubHoldsUnderARealRun:
    """The seam: a pytest process started with the polluting shell.

    The policy tests above are a table; this is the thing ISSUE-301 actually
    reported. The variables are exported into a child `pytest`, which runs the
    node ids that failed on the deployment host. Without the fixture every one
    of them goes red, which is the whole content of the issue.

    Reading this test tells you almost nothing about whether it can fail, which
    is the standing rule for anything asserting against a separately-configured
    process. So `test_the_control_goes_red_without_the_scrub` runs the identical
    child with the fixture switched off and requires each reported node id in
    pytest's own `FAILED` summary — a control can otherwise pass on an
    unrelated failure.

    `-p no:cacheprovider` so the child does not write into the parent run's
    `.pytest_cache`, and `-n0` because the selection is seven tests and an
    xdist pool would cost more than the run.
    """

    POLLUTION = {
        "NTFY_SERVER_URL": "https://ntfy.example.com",
        "NTFY_TOPIC": "ambient-topic",
        "CALDAV_URL": "https://dav.example.com/remote.php/dav",
        "CALDAV_USERNAME": "ambient-user",
        "NC_URL": "https://nc.example.com",
        "NC_USER": "ambient-user",
        "ISTOTA_SANDBOXED": "1",
        "ISTOTA_CONFIG_PATH": "/etc/istota/config.toml",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
    }

    # The node ids ISSUE-301 named, one per group-A symptom plus a group-B
    # loopback file. Named explicitly rather than run whole-file so a rename
    # fails loudly instead of quietly covering less.
    SELECTION = (
        "tests/test_skills_ntfy.py::TestTopicOverride",
        "tests/test_skill_proxy.py::TestSkillClientDirect::test_direct_fallback_without_env",
        "tests/test_gitlab_service.py::TestRestSurface",
    )

    SELECTION_SIZE = 7

    # What has to go red with the scrub off. Six of the seven, named
    # individually: a control that only checked the exit code would pass on any
    # unrelated failure, and one that counted would pass on the wrong six.
    # `test_malformed_override_rejected` is the seventh and stays green either
    # way — it rejects its topic before any server URL is read.
    CONTROL_FAILURES = (
        "tests/test_skills_ntfy.py::TestTopicOverride::test_flag_overrides_env_default",
        "tests/test_skills_ntfy.py::TestTopicOverride::test_flag_works_without_env_default",
        "tests/test_skills_ntfy.py::TestTopicOverride::test_env_default_used_when_flag_absent",
        "tests/test_skill_proxy.py::TestSkillClientDirect::test_direct_fallback_without_env",
        "tests/test_gitlab_service.py::TestRestSurface::test_an_unimplemented_endpoint_says_so_with_the_path",
        "tests/test_gitlab_service.py::TestRestSurface::test_calls_are_recorded_with_their_query_and_body",
    )

    def _run(self, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
        # Built from `os.environ` *minus what this run's own fixture put there*.
        # The parent has already been scrubbed by the time a test body runs, so
        # inheriting its `NO_PROXY` would hand the child the fix it is supposed
        # to be tested without, and the group-B half of the control would pass
        # for the wrong reason.
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
        for name in NO_PROXY_NAMES:
            env.pop(name, None)
        env.update(env_extra)
        return subprocess.run(
            [
                sys.executable, "-m", "pytest",
                *self.SELECTION,
                "-q", "-n0", "-p", "no:cacheprovider",
                "--no-header", "-p", "no:randomly", "-rf",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_the_reported_tests_pass_under_a_polluted_shell(self):
        result = self._run(self.POLLUTION)
        assert result.returncode == 0, (
            "the tests ISSUE-301 reported still fail under a polluted shell:\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )
        assert f"{self.SELECTION_SIZE} passed" in result.stdout, (
            "the selection shrank; the control below is checking less than it "
            f"claims:\n{result.stdout[-2000:]}"
        )

    def test_the_control_goes_red_without_the_scrub(self):
        result = self._run({**self.POLLUTION, NO_SCRUB_FLAG: "1"})

        assert result.returncode != 0, (
            "with the scrub switched off the reported tests still pass, so the "
            f"test above proves nothing:\n{result.stdout[-4000:]}"
        )
        missing = [
            node for node in self.CONTROL_FAILURES
            if f"FAILED {node}" not in result.stdout
        ]
        assert not missing, (
            "these node ids did not fail with the scrub off, so the scrub is "
            f"not what makes them pass: {missing}\n{result.stdout[-4000:]}"
        )
