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


class TestQuotingSurvivesTheRender:
    """A generated TOML file is a quoting problem wearing a config's clothes."""

    def test_a_password_with_a_quote_does_not_break_the_toml(self, tmp_path):
        # entrypoint.sh:780 has a python3 heredoc for exactly this reason on the
        # Monarch credentials. The app password goes through a plain shell
        # heredoc, so this records what that path actually tolerates.
        path = render(tmp_path, **{**REQUIRED, "APP_PASSWORD": "pa'ss word"})

        assert tomllib.loads(path.read_text())["nextcloud"]["app_password"] == "pa'ss word"

    def test_the_monarch_python_helper_escapes_a_backslash_and_a_quote(self, tmp_path):
        # entrypoint.sh:780 renders these two through a python3 heredoc rather
        # than a shell heredoc, precisely so a quote or a backslash in a
        # password cannot break the TOML. This is that escaping, exercised.
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
                ISTOTA_DEVELOPER_GITLAB_TOKEN="glpat-fabricated",
                ISTOTA_DEVELOPER_GITLAB_URL="http://gitlab.test",
            )
        )

        assert config.developer.gitlab_token == "glpat-fabricated"
        assert config.developer.gitlab_url == "http://gitlab.test"

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

    def test_the_backfill_passes_stayed_in_the_entrypoint(self):
        entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()
        rendered = RENDER_CONFIG.read_text()

        for marker in ("Upgrade path:", "backfill"):
            assert marker in entrypoint, f"{marker} left the entrypoint"
        assert "Upgrade path:" not in rendered, "a backfill pass moved into the render"
