"""The Docker entrypoint's config generation, driven as a script.

``docker/istota/render-config.sh`` is ``entrypoint.sh``'s ``if [ ! -f
"$CONFIG_FILE" ]`` block — roughly 460 lines, sixteen ``cat >>`` heredocs and a
dozen conditional appends — lifted out so it can be run without a Nextcloud to
provision against. Nothing in the suite could reach it before: getting there
meant seeding ``/mnt/shared/.istota-provisioned`` and then sitting in the
entrypoint's 60x2s polling loop against ``http://nextcloud``.

Its inputs are **not** the environment in the ordinary sense. They are shell
locals the provisioning phase produces earlier in the same script, and the
extraction turns each into an explicit exported variable. The script is
*executed*, never sourced: ``entrypoint.sh`` runs ``set -euo pipefail``, so a
sourced script inherits ``-u`` and any unset variable it reads would abort the
whole entrypoint rather than the render.

What these tests assert is the property the extraction has to preserve — that
the rendered file is a config ``load_config`` accepts, carrying the values the
inputs asked for. The byte-identity of the move itself was checked once, by
hand, against the pre-extraction block; see the spec. There is no golden file
here on purpose: a fixture of the whole rendered config would turn every
deliberate config change into a fixture edit, and the reviewer could not tell an
intended diff from an accident.

Scope, stated because it is easy to assume otherwise: the three *backfill*
passes that run in the entrypoint's ``else`` branch — ``log_channel`` /
``alerts_channel``, ``[web]`` / ``[site]``, and ``user_resources`` — are the
upgrade-repair path and stayed in ``entrypoint.sh``. Nothing here covers them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
import re
from pathlib import Path

import pytest

from istota.config import load_config

REPO = Path(__file__).resolve().parent.parent
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="render-config.sh is #!/bin/bash and shells out to python3",
)


def render(tmp_path: Path, **env: str) -> Path:
    """Run render-config.sh with a fabricated environment; return the file.

    The environment is built from scratch rather than inherited. A developer
    host with ``ISTOTA_*`` variables exported — which is the normal state of
    anyone who runs the stack locally — would otherwise leak them into the
    render and make these assertions depend on the machine.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.toml"
    proc = subprocess.run(
        ["bash", str(RENDER_CONFIG)],
        env={
            "PATH": os.environ.get("PATH", ""),
            "CONFIG_FILE": str(config_file),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"render-config.sh exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert config_file.exists(), f"no config written\n{proc.stdout}\n{proc.stderr}"
    return config_file


# What a caller normally hands over. BOT_USER is in here because the tests below
# assert on it, not because the script needs it — it carries a `:-istota`
# default, like every other optional input.
REQUIRED = {
    "USER_NAME": "testuser",
    "NC_URL": "http://nextcloud:80",
    "APP_PASSWORD": "app-password-value",
    "BOT_USER": "istota",
}

# The three the script refuses to run without, alongside CONFIG_FILE. Everything
# else is either guarded by a `-n` test or carries a `:-` default.
REQUIRED_INPUTS = ("USER_NAME", "NC_URL", "APP_PASSWORD")


def _resources(rendered: dict, type_: str) -> list[dict]:
    """The `[[users.testuser.resources]]` entries of one type.

    Connected services are rendered as an array of tables under the user, not as
    top-level sections — `type = "money"` under the user, not `[money]`.
    """
    entries = rendered["users"]["testuser"].get("resources", [])
    return [r for r in entries if r.get("type") == type_]


def _resource(rendered: dict, type_: str) -> dict:
    matches = _resources(rendered, type_)
    assert len(matches) == 1, f"expected exactly one {type_} resource, got {matches}"
    return matches[0]


class TestTheRenderedConfigLoads:
    """The whole point of the extraction: run it, then load the result."""

    def test_the_minimal_environment_produces_a_loadable_config(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.nextcloud.url == "http://nextcloud:80"
        assert config.nextcloud.username == "istota"
        assert "testuser" in config.users

    def test_the_output_is_valid_toml_before_anything_interprets_it(self, tmp_path):
        # load_config tolerates a certain amount; tomllib does not. A heredoc
        # that lost its terminator, or an unescaped quote in a password, shows
        # up here rather than as a mysteriously absent config section.
        path = render(tmp_path, **REQUIRED)
        tomllib.loads(path.read_text())

    def test_the_two_nextcloud_path_keys_default_to_bare_metal(self, tmp_path):
        """`dav_prefix` and `auto_share_bot_dir` exist for the Docker shape,
        where the daemon's storage root is a `files_external` mount rather than
        the bot's own file tree. An operator who sets neither must get exactly
        what every deployment got before they existed."""
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.nextcloud.dav_prefix == ""
        assert config.nextcloud.auto_share_bot_dir is True

    def test_the_two_nextcloud_path_keys_are_honoured_when_given(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_NEXTCLOUD_DAV_PREFIX="Shared Files",
                ISTOTA_NEXTCLOUD_AUTO_SHARE_BOT_DIR="false",
            )
        )

        assert config.nextcloud.dav_prefix == "Shared Files"
        assert config.nextcloud.auto_share_bot_dir is False

    def test_user_display_name_and_timezone_default_from_the_user_name(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))
        profile = config.users["testuser"]

        assert profile.display_name == "testuser"
        assert profile.timezone == "UTC"

    def test_user_display_name_and_timezone_are_honoured_when_given(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                USER_DISPLAY_NAME="Test Person",
                USER_TIMEZONE="Europe/Warsaw",
            )
        )
        profile = config.users["testuser"]

        assert profile.display_name == "Test Person"
        assert profile.timezone == "Europe/Warsaw"


class TestTheStorageBackend:
    """`NC_URL` decides which of the two shipped storage backends is rendered.

    `Config.storage_is_nextcloud` is `bool(self.nextcloud.url)`, and it routes
    `storage_backend`, the prompt's file-tool vocabulary, the `nextcloud` entry
    in `available_capabilities()` and `doctor`'s `runtime.mount_liveness`. Both
    values are shipped install shapes — the Nextcloud-free one is what
    `istota setup` produces and what every lean testbed profile runs — so the
    render has to reach both.
    """

    def test_a_url_renders_the_nextcloud_backend(self, tmp_path):
        config = load_config(render(tmp_path, **REQUIRED))

        assert config.storage_is_nextcloud is True
        assert config.storage_backend == "nextcloud"

    def test_an_empty_url_renders_the_local_backend(self, tmp_path):
        """Set-but-empty, not unset, and the difference is the whole test.

        The preflight is `[ -n "${NC_URL+x}" ]` (`render-config.sh:68`), which
        tests whether the variable is *set*. An unset `NC_URL` therefore fails
        the render outright with exit 2 — asserted one class down in
        `TestTheInputContract` — while the empty string passes it and reaches
        the `url = ""` line the local install needs.
        """
        env = {**REQUIRED, "NC_URL": "", "APP_PASSWORD": ""}
        config = load_config(render(tmp_path, **env))

        assert config.nextcloud.url == ""
        assert config.storage_is_nextcloud is False
        assert config.storage_backend == "local"
        # The mount path is a hardcoded literal in the generator, so it is
        # rendered under both backends and `use_mount` stays true — the local
        # install is a plain directory at the same place, with nothing mounted
        # on it. This is why `doctor.check_mount_liveness` gates on the backend
        # rather than on the path being configured.
        assert config.nextcloud_mount_path == Path("/mnt/shared")
        assert config.use_mount is True

    def test_the_local_backend_drops_the_nextcloud_capability(self, tmp_path):
        """The prompt-visible half, at the point the render produces it.

        A skill declaring `requires_capability: [nextcloud]` is folded into the
        effective disabled set when the capability is absent, so it leaves both
        eager selection and the on-demand menu.
        """
        env = {**REQUIRED, "NC_URL": "", "APP_PASSWORD": ""}

        assert "nextcloud" in load_config(render(tmp_path / "nc", **REQUIRED)).available_capabilities()
        assert "nextcloud" not in load_config(render(tmp_path / "local", **env)).available_capabilities()


class TestQuotingSurvivesTheRender:
    """A generated TOML file is a quoting problem wearing a config's clothes.

    Every value here is interpolated into a `"`-delimited TOML basic string by a
    shell heredoc, so the two characters that matter are `"` and `\\`. A single
    quote is inert and proves nothing — the first draft of this class tested one
    and passed while the real case was broken.

    The failure mode is the bad one: `render-config.sh` exits 0 and prints
    "Config written to", so the entrypoint's file-exists guard treats the
    corrupt file as complete on every subsequent boot and never regenerates it.
    """

    # The credentials an operator types into docker/.env by hand, and therefore
    # the ones that can carry a shell metacharacter. The rest of the values in
    # the rendered config are machine-generated hex or come from a URL.
    @pytest.mark.parametrize(
        "variable,section,key,extra",
        [
            ("APP_PASSWORD", "nextcloud", "app_password", {}),
            (
                "ISTOTA_EMAIL_IMAP_PASSWORD",
                "email",
                "imap_password",
                {
                    "ISTOTA_EMAIL_ENABLED": "true",
                    "ISTOTA_EMAIL_BOT_ADDRESS": "bot@example.com",
                    "ISTOTA_EMAIL_IMAP_HOST": "imap.example.com",
                    "ISTOTA_EMAIL_IMAP_USER": "bot",
                },
            ),
            (
                "ISTOTA_DEVELOPER_GITLAB_TOKEN",
                "developer",
                "gitlab_token",
                {
                    "ISTOTA_DEVELOPER_ENABLED": "true",
                    "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
                },
            ),
            (
                "ISTOTA_DEVELOPER_GITHUB_TOKEN",
                "developer",
                "github_token",
                {
                    "ISTOTA_DEVELOPER_ENABLED": "true",
                    "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
                },
            ),
        ],
    )
    @pytest.mark.parametrize(
        "value",
        ['pa"ss', "back\\slash", 'both"and\\', "pa'ss word"],
        ids=["double-quote", "backslash", "both", "single-quote-and-space"],
    )
    def test_a_credential_survives_the_round_trip(
        self, tmp_path, variable, section, key, extra, value
    ):
        path = render(tmp_path, **{**REQUIRED, **extra, variable: value})

        rendered = tomllib.loads(path.read_text())
        assert rendered[section][key] == value

    def test_the_monarch_python_helper_escapes_a_backslash_and_a_quote(self, tmp_path):
        # render-config.sh renders these two through a python3 heredoc rather
        # than a shell heredoc, precisely so a quote or a backslash in a
        # password cannot break the TOML. It was the only value that got that
        # treatment; the parametrized cases above are the rest catching up.
        path = render(
            tmp_path,
            **REQUIRED,
            ISTOTA_MONEY_ENABLED="true",
            MONARCH_EMAIL='we"ird@example.com',
            MONARCH_PASSWORD="back\\slash",
        )
        money = _resource(tomllib.loads(path.read_text()), "money")

        assert money["monarch_email"] == 'we"ird@example.com'
        assert money["monarch_password"] == "back\\slash"


class TestTheDeveloperBlock:
    """The three shapes the image tier's Group C fabricates, at unit level.

    Group A's forge assertions need the third one to exist and to carry a token,
    because a doctor check that SKIPs is not an assertion. If this class stops
    producing a `[developer]` block with a token in it, that tier goes quietly
    green on a broken image.
    """

    def test_developer_off_emits_no_developer_section(self, tmp_path):
        rendered = tomllib.loads(render(tmp_path, **REQUIRED).read_text())

        assert rendered.get("developer", {}).get("enabled", False) is False

    def test_developer_on_without_a_token_still_sets_the_binary_paths(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
            )
        )

        assert config.developer.enabled is True
        assert config.developer.repos_dir == "/data/repos"
        # The paths must be present even with no token: ISSUE-263 was a config
        # that named binaries which did not exist, not a missing key.
        assert config.developer.gh_bin_path
        assert config.developer.glab_bin_path

    def test_developer_on_with_a_token_renders_it(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
                ISTOTA_DEVELOPER_GITLAB_TOKEN="fabricated-gitlab-token",
                ISTOTA_DEVELOPER_GITLAB_URL="http://gitlab.test",
            )
        )

        assert config.developer.gitlab_token == "fabricated-gitlab-token"
        assert config.developer.gitlab_url == "http://gitlab.test"

    def test_the_reviewer_username_reaches_the_rendered_config(self, tmp_path):
        """ISSUE-289. The compose stack renders its own `[developer]` block, so
        a setting the Ansible role gained is unreachable here until this file
        gains it too — and the symptom is an MR with no reviewer, not an
        error."""
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
                ISTOTA_DEVELOPER_GITLAB_REVIEWER="reviewer-user",
                ISTOTA_DEVELOPER_GITLAB_REVIEWER_ID="1234567",
            )
        )

        assert config.developer.gitlab_reviewer == "reviewer-user"
        assert config.developer.gitlab_reviewer_id == "1234567"

    def test_the_forge_binary_paths_can_be_overridden(self, tmp_path):
        # 30bb7c83's bug: the Ansible role installs to /usr/bin and renders that
        # path, while the dataclass default is /usr/local/bin. Both deployments
        # have to be expressible from here.
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                ISTOTA_DEVELOPER_ENABLED="true",
                ISTOTA_DEVELOPER_REPOS_DIR="/data/repos",
                ISTOTA_DEVELOPER_GH_BIN_PATH="/usr/bin/gh",
                ISTOTA_DEVELOPER_GLAB_BIN_PATH="/usr/bin/glab",
            )
        )

        assert config.developer.gh_bin_path == "/usr/bin/gh"
        assert config.developer.glab_bin_path == "/usr/bin/glab"


class TestChannelsAndResources:
    def test_log_and_alerts_channels_come_from_the_provisioned_tokens(self, tmp_path):
        config = load_config(
            render(tmp_path, **REQUIRED, LOG_TOKEN="logtok", ALERTS_TOKEN="alerttok")
        )
        profile = config.users["testuser"]

        assert profile.log_channel == "logtok"
        assert profile.alerts_channel == "alerttok"

    def test_an_explicit_channel_overrides_the_provisioned_token(self, tmp_path):
        config = load_config(
            render(
                tmp_path,
                **REQUIRED,
                LOG_TOKEN="logtok",
                USER_LOG_CHANNEL="explicit",
            )
        )

        assert config.users["testuser"].log_channel == "explicit"

    def test_the_location_ingest_resource_needs_both_the_module_and_the_token(
        self, tmp_path
    ):
        without = tomllib.loads(
            render(
                tmp_path / "a",
                **REQUIRED,
                ISTOTA_LOCATION_ENABLED="true",
            ).read_text()
        )
        with_token = tomllib.loads(
            render(
                tmp_path / "b",
                **REQUIRED,
                ISTOTA_LOCATION_ENABLED="true",
                LOCATION_INGEST_TOKEN="ingest-token",
            ).read_text()
        )

        assert _resources(without, "overland") == []
        assert _resource(with_token, "overland")["ingest_token"] == "ingest-token"


class TestTheWebBlock:
    def test_oauth_needs_both_halves_of_the_client_credential(self, tmp_path):
        # A client id with no secret is a half-provisioned Nextcloud, and the
        # rendered [web] block would name an oauth2 flow that cannot complete.
        rendered = tomllib.loads(
            render(tmp_path, **REQUIRED, OAUTH_CLIENT_ID="only-the-id").read_text()
        )

        assert "oauth2_client_id" not in rendered.get("web", {})

    def test_a_complete_oauth_credential_renders_the_endpoints_off_nc_url(
        self, tmp_path
    ):
        rendered = tomllib.loads(
            render(
                tmp_path,
                **REQUIRED,
                OAUTH_CLIENT_ID="client-id",
                OAUTH_CLIENT_SECRET="client-secret",
            ).read_text()
        )
        web = rendered["web"]

        assert web["oauth2_client_id"] == "client-id"
        assert web["oauth2_token_endpoint"].startswith("http://nextcloud:80/")

    def test_each_run_mints_a_fresh_session_secret(self, tmp_path):
        def secret(where: Path) -> str:
            rendered = tomllib.loads(
                render(
                    where,
                    **REQUIRED,
                    OAUTH_CLIENT_ID="client-id",
                    OAUTH_CLIENT_SECRET="client-secret",
                ).read_text()
            )
            return rendered["web"]["session_secret_key"]

        assert secret(tmp_path / "a") != secret(tmp_path / "b")


class TestTheInputContract:
    """What the script does when its caller gets the hand-off wrong.

    The entrypoint calls this as a subprocess precisely so a missing input
    aborts the render and not the boot. That only holds if the render actually
    aborts, rather than writing a config with an empty Nextcloud URL in it.
    """

    @pytest.mark.parametrize("missing", REQUIRED_INPUTS)
    def test_a_missing_required_input_fails_the_render(self, tmp_path, missing):
        env = {k: v for k, v in REQUIRED.items() if k != missing}
        config_file = tmp_path / "config.toml"
        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={
                "PATH": os.environ.get("PATH", ""),
                "CONFIG_FILE": str(config_file),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0, f"rendered anyway without {missing}"
        assert missing in proc.stderr, (
            f"the failure does not name {missing}; an operator reading this "
            f"boot log has to guess.\n{proc.stderr}"
        )
        # The assertion the preflight actually exists for, and the one the first
        # draft of this test left out. Bare `set -u` also exits non-zero and
        # also names the variable — but only *after* the first `cat >` has
        # truncated the destination, leaving 374 bytes of config that the
        # entrypoint's file-exists guard accepts as complete forever. Without
        # this line the test passes with the preflight deleted.
        assert not config_file.exists(), (
            f"a partial config was written despite the missing {missing}"
        )

    def test_config_file_itself_is_required(self, tmp_path):
        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={"PATH": os.environ.get("PATH", ""), **REQUIRED},
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0
        assert "CONFIG_FILE" in proc.stderr

    def test_a_failure_part_way_through_leaves_no_config_behind(self, tmp_path):
        """The failure mode that turns into a silent production incident.

        Everything the preflight does not cover fails *after* the first
        ``cat >`` has truncated the destination: python3 absent for the session
        secret, ENOSPC, a future unguarded ``${VAR}``. entrypoint.sh then finds a
        file on the next boot, skips the render for good, runs the backfill
        passes over the fragment and execs the daemon on it.

        Reproduced by breaking ``python3``, which the render shells out to for
        the session secret — a real dependency of the script, failing at a point
        the preflight cannot reach, rather than a fault injected into the render
        itself. A stub in front of the real PATH rather than an empty PATH,
        which would take ``cat``, ``mv`` and ``sed`` with it and fail the render
        for a reason that has nothing to do with the property under test.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_file = tmp_path / "config.toml"
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        broken = stub_bin / "python3"
        broken.write_text("#!/bin/sh\necho 'python3 is broken' >&2\nexit 1\n")
        broken.chmod(0o755)

        proc = subprocess.run(
            ["bash", str(RENDER_CONFIG)],
            env={
                "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
                "CONFIG_FILE": str(config_file),
                **REQUIRED,
                # So the session secret — the first python3 call — is reached.
                "OAUTH_CLIENT_ID": "client-id",
                "OAUTH_CLIENT_SECRET": "client-secret",
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0, "the render reported success without python3"
        assert not config_file.exists(), (
            "a truncated config.toml was left on disk; the entrypoint's "
            "file-exists guard will treat it as complete on every later boot"
        )
        assert not list(tmp_path.glob("*.partial")), "the partial file was not cleaned up"

    def test_the_script_is_executable_and_parses_under_bash(self):
        # `sh -n` would check the wrong grammar: the file is #!/bin/bash and
        # /bin/sh is dash in the image.
        assert os.access(RENDER_CONFIG, os.X_OK), "render-config.sh is not executable"
        subprocess.run(["bash", "-n", str(RENDER_CONFIG)], check=True, timeout=30)


class TestTheEntrypointStillOwnsWhatItKept:
    """Guards on the seam, not on the script.

    A future edit that re-inlines the render, or that drags a backfill pass into
    the extracted file, breaks the property Stage 4 was for. Both are cheap to
    catch by reading the entrypoint.
    """

    def test_the_entrypoint_calls_the_script_rather_than_inlining_the_render(self):
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()

        assert "render-config.sh" in entrypoint
        # The unmistakable first line of the old inline block.
        assert "# Istota configuration — generated by Docker entrypoint" not in entrypoint

    def test_every_provisioning_local_the_render_reads_is_exported_to_it(self):
        """The one failure mode the extraction itself creates.

        Before the split, a variable the render read was in scope by
        construction. Now it has to be named in the entrypoint's ``export``
        list, and a name that is missing renders as its ``:-`` default or empty
        — silently, in production, while every test here stays green, because
        these tests fabricate the environment directly and never exercise the
        hand-off.

        ``LOCATION_INGEST_TOKEN`` is the shape to worry about: assigned in the
        entrypoint's provisioning phase, never present in docker-compose.yml, so
        nothing else would put it in the child's environment.

        ISTOTA_* is excluded because those reach the container from compose
        rather than from the entrypoint. Names assigned inside the render are
        read from the file rather than listed here, so adding a local does not
        mean editing this test.
        """
        rendered = RENDER_CONFIG.read_text()
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()

        # Comments are stripped first: the header documents the contract in
        # prose and names variables inside it, including placeholders like
        # `${VAR}`, none of which the script actually reads.
        code = "\n".join(
            line for line in rendered.splitlines() if not line.lstrip().startswith("#")
        )
        referenced = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", code))
        assigned = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)=", code, re.M))

        needed = {n for n in referenced - assigned if not n.startswith("ISTOTA_")}
        assert needed, "the scan found nothing to check; the regex has rotted"

        # The export that hands off to the render, not the unrelated one-liner
        # for ISTOTA_ADMINS_FILE near the top of the entrypoint. Identified by
        # the input every render must receive.
        blocks = [
            match.group(1)
            for match in re.finditer(
                r"^\s*export\s+((?:[^\n]*\\\n)*[^\n]*)", entrypoint, re.M
            )
            if "CONFIG_FILE" in match.group(1)
        ]
        assert len(blocks) == 1, (
            f"expected exactly one export block naming CONFIG_FILE, found {len(blocks)}"
        )
        exported = set(re.findall(r"[A-Z][A-Z0-9_]*", blocks[0]))

        missing = sorted(needed - exported)
        assert not missing, (
            f"render-config.sh reads {missing}, which entrypoint.sh does not "
            "export to it. Each would render as its default or empty on a real "
            "boot while every test in this file still passes."
        )

    @pytest.mark.parametrize(
        "prefix", ["ISTOTA_DEVELOPER_", "ISTOTA_EMAIL_", "ISTOTA_NEXTCLOUD_"]
    )
    def test_every_var_the_render_reads_is_passed_by_compose(self, prefix):
        """The other half of the hand-off, which nothing checked.

        The test above excludes ``ISTOTA_*`` on the grounds that compose puts
        those in the container. Nothing held compose to it, and the failure is
        the same silent one: the render substitutes its ``:-`` default and the
        setting is simply absent from the config, in production, with the suite
        green.

        Scoped by prefix rather than run over every ``ISTOTA_*`` name, because
        both of these families are wholly operator-set — no other layer assigns
        one, so any name the render reads has to arrive through compose. Names
        the entrypoint itself computes (``LOCATION_INGEST_TOKEN`` and friends)
        would fail a blanket scan for the wrong reason.

        Two of the three prefixes are here because both have been out of step,
        months apart.
        ISSUE-289 was the reviewer setting, present in the Ansible role and the
        render and absent from compose, which cost nothing until an MR opened
        with nobody on it. The email pair was ``ISTOTA_EMAIL_AUTHSERV_ID`` and
        ``ISTOTA_EMAIL_CONFIRM_SENDER_MATCH``, both documented in
        ``docker/.env.example`` and read by the render — so an operator asking
        for ``confirm_sender_match = "gate"`` on a Docker deploy silently got
        ``off``, which is the gate switched off rather than a setting ignored.

        ``ISTOTA_NEXTCLOUD_`` joined them with ``dav_prefix`` and
        ``auto_share_bot_dir``. Both are wholly operator-set in the same sense —
        the render is the only thing that reads them — and both fail the same
        silent way: the daemon addresses a Nextcloud path that does not exist on
        the Docker shape, and every share and every ``files`` verb 404s while
        the suite stays green.
        """
        code = "\n".join(
            line
            for line in RENDER_CONFIG.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        read = {
            name
            for name in re.findall(r"\$\{?(" + prefix + r"[A-Z0-9_]*)", code)
        }
        assert read, f"the scan found no {prefix}* reads; the regex has rotted"

        compose = (REPO / "docker" / "docker-compose.yml").read_text()
        passed = set(re.findall(r"^\s*(" + prefix + r"[A-Z0-9_]*):", compose, re.M))

        missing = sorted(read - passed)
        assert not missing, (
            f"render-config.sh reads {missing}, which docker-compose.yml does "
            "not pass into the container. Each renders as its default or empty "
            "on a real boot, unsettable by the operator."
        )

    def test_the_backfill_passes_stayed_in_the_entrypoint(self):
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()
        rendered = RENDER_CONFIG.read_text()

        for marker in ("Upgrade path:", "backfill"):
            assert marker in entrypoint, f"{marker} left the entrypoint"
        assert "Upgrade path:" not in rendered, "a backfill pass moved into the render"
