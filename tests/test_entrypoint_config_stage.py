"""The Docker entrypoint's config *stage* — whether it renders, not what it renders.

``tests/test_render_config.py`` covers ``render-config.sh``: given a set of
inputs, what lands in ``config.toml``. This file covers the decision one level
up, which nothing reached before: on a boot where ``config.toml`` already
exists, does the environment in ``docker/.env`` still reach the daemon.

Until ISSUE-368 it did not. The whole render sat behind ``if [ ! -f
"$CONFIG_FILE" ]``, ``config.toml`` lives on the ``istota_data`` named volume,
and ``rebuild.sh`` keeps volumes unless asked not to — so every boot after the
first printed "Config already exists, skipping generation" and 170 ``ISTOTA_*``
variables became unsettable. The failure was silent in both directions.

Nothing in the suite can execute ``entrypoint.sh``: it waits on a provisioning
flag and then polls ``http://nextcloud`` sixty times. The stage is extracted by
text instead — the same technique ``test_render_config.py`` uses for the
credential chain — which fails loudly (no match, no test) rather than drifting.
The extracted region really does run the real ``render-config.sh``, so these are
not assertions about a mock.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "docker" / "istota" / "entrypoint.sh"
ENTRYPOINT_DIR = REPO / "docker" / "istota"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="entrypoint.sh is #!/bin/bash and shells out to python3",
)

#: The region of entrypoint.sh that decides whether to render and then renders.
#: Bounded by two section banners the file already had.
_STAGE = re.compile(
    r"^# --- Generate config ---$.*?(?=^# --- Module workspace seeding ---$)",
    re.M | re.S,
)


def config_stage() -> str:
    match = _STAGE.search(ENTRYPOINT.read_text())
    assert match, (
        "the '# --- Generate config ---' region moved or its closing banner "
        "changed; this extraction needs updating"
    )
    return match.group(0)


#: What a real boot has resolved by the time it reaches the stage. These are
#: shell locals in entrypoint.sh, which is why the stage exports them itself.
BOOT = {
    "USER_NAME": "testuser",
    # The reported repro is a native-brain deployment whose model was changed
    # in docker/.env; `[brain.native]` is only rendered under this kind.
    "ISTOTA_BRAIN_KIND": "native",
    "NC_URL": "http://nextcloud:80",
    "APP_PASSWORD": "app-password-value",
    "BOT_USER": "istota",
}


def run_stage(
    data_dir: Path,
    *,
    entrypoint_dir: Path | None = None,
    drop: tuple[str, ...] = (),
    **env: str,
) -> subprocess.CompletedProcess:
    """Run one boot's worth of the config stage against ``data_dir``.

    The environment is built from scratch, not inherited: a developer host with
    ``ISTOTA_*`` exported is the normal state of anyone who runs the stack, and
    inheriting would make these assertions depend on the machine.

    ``entrypoint_dir`` swaps in another directory to resolve ``render-config.sh``
    from, which is how a failing render is exercised. ``drop`` removes a key from
    the boot environment, since the defaults are merged in below.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    script = "set -euo pipefail\n" + config_stage()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "ENTRYPOINT_DIR": str(entrypoint_dir or ENTRYPOINT_DIR),
        "CONFIG_FILE": str(data_dir / "config.toml"),
        "CONFIG_READY_FLAG": str(data_dir / ".config-current"),
        "WEB_SESSION_SECRET_FILE": str(data_dir / ".web_session_secret"),
        **BOOT,
        **env,
    }
    for key in drop:
        environment.pop(key, None)
    return subprocess.run(
        ["bash", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def boot(data_dir: Path, **env: str) -> dict:
    """Run the stage, require it to succeed, and return the parsed config."""
    proc = run_stage(data_dir, **env)
    assert proc.returncode == 0, (
        f"the config stage exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    config = data_dir / "config.toml"
    assert config.exists(), f"no config written\n{proc.stdout}\n{proc.stderr}"
    return tomllib.loads(config.read_text())


class TestTheSecondBootReadsTheEnvironmentAgain:
    """ISSUE-368, as reported: a changed `.env` value must reach the config."""

    def test_a_changed_value_reaches_the_config_on_the_second_boot(self, tmp_path):
        first = boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        assert first["brain"]["native"]["model"] == "z-ai/glm-5.2"

        second = boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash")
        assert second["brain"]["native"]["model"] == "z-ai/glm-5.3-flash", (
            "the second boot kept the first boot's model. This is ISSUE-368: "
            "docker/.env reaches the running system through the render and "
            "through nothing else."
        )

    def test_a_value_removed_from_the_environment_leaves_the_config(self, tmp_path):
        """The direction an add-if-missing backfill pass could never cover."""
        first = boot(tmp_path, ISTOTA_DEVELOPER_ENABLED="true")
        assert "developer" in first

        second = boot(tmp_path, ISTOTA_DEVELOPER_ENABLED="false")
        assert "developer" not in second

    def test_the_location_ingest_token_tracks_the_environment(self, tmp_path):
        """`.claude/rules/testbed.md`'s "second entrypoint wart" is this issue.

        With location enabled, regenerating `LOCATION_INGEST_TOKEN` wrote it to
        the flag file while the config kept the old one, so the flag recorded a
        value nothing read.
        """
        boot(
            tmp_path,
            ISTOTA_LOCATION_ENABLED="true",
            LOCATION_INGEST_TOKEN="first-token",
        )
        second = boot(
            tmp_path,
            ISTOTA_LOCATION_ENABLED="true",
            LOCATION_INGEST_TOKEN="second-token",
        )
        resources = second["users"]["testuser"]["resources"]
        overland = [r for r in resources if r["type"] == "overland"]
        assert len(overland) == 1, f"expected one overland resource, got {overland}"
        assert overland[0]["ingest_token"] == "second-token"


class TestWhatARerenderMustNotLose:
    """The render invents one value. Re-running it must not mint a new one."""

    def test_the_web_session_secret_survives_a_rerender(self, tmp_path):
        oauth = {"OAUTH_CLIENT_ID": "client-id", "OAUTH_CLIENT_SECRET": "client-secret"}
        first = boot(tmp_path, **oauth)
        second = boot(tmp_path, **oauth)

        assert first["web"]["session_secret_key"] == second["web"]["session_secret_key"], (
            "a fresh session signing key on every boot invalidates every logged-in "
            "web session (web_app._resolve_session_secret reads this value)."
        )

    def test_the_secret_is_persisted_beside_the_config_with_tight_permissions(
        self, tmp_path
    ):
        boot(tmp_path, OAUTH_CLIENT_ID="client-id", OAUTH_CLIENT_SECRET="client-secret")

        persisted = tmp_path / ".web_session_secret"
        assert persisted.exists(), "nothing persisted the session signing key"
        assert (persisted.stat().st_mode & 0o777) == 0o600

    def test_an_operator_supplied_key_wins_over_the_persisted_one(self, tmp_path):
        boot(tmp_path, OAUTH_CLIENT_ID="client-id", OAUTH_CLIENT_SECRET="client-secret")
        rendered = boot(
            tmp_path,
            OAUTH_CLIENT_ID="client-id",
            OAUTH_CLIENT_SECRET="client-secret",
            ISTOTA_WEB_SESSION_SECRET_KEY="operator-pinned-key",
        )

        assert rendered["web"]["session_secret_key"] == "operator-pinned-key"

    def test_repinning_and_then_unpinning_keeps_the_last_pinned_key(self, tmp_path):
        """Guarding the sidecar write on *absence* keeps the first value for good.

        So an operator who pins A, repins to B and then unpins gets A back and
        drops every logged-in session — the exact failure the sidecar exists to
        prevent, arriving by the route meant to avoid it.
        """
        oauth = {"OAUTH_CLIENT_ID": "client-id", "OAUTH_CLIENT_SECRET": "client-secret"}
        boot(tmp_path, **oauth, ISTOTA_WEB_SESSION_SECRET_KEY="key-a")
        boot(tmp_path, **oauth, ISTOTA_WEB_SESSION_SECRET_KEY="key-b")

        unpinned = boot(tmp_path, **oauth)

        assert unpinned["web"]["session_secret_key"] == "key-b"

    def test_an_existing_config_donates_its_secret_on_upgrade(self, tmp_path):
        """The migration path: a deployment that already has one keeps it.

        Nothing persisted this before the fix, so the only copy on an upgrading
        deployment is the one in `config.toml` — read it out before the first
        re-render overwrites it, or every session in flight is dropped once.
        """
        oauth = {"OAUTH_CLIENT_ID": "client-id", "OAUTH_CLIENT_SECRET": "client-secret"}
        boot(tmp_path, **oauth)
        existing = tomllib.loads((tmp_path / "config.toml").read_text())
        inherited = existing["web"]["session_secret_key"]

        # Exactly the pre-fix state: a config with a secret, no sidecar file.
        (tmp_path / ".web_session_secret").unlink()

        assert boot(tmp_path, **oauth)["web"]["session_secret_key"] == inherited


class TestThePreviousConfigIsKept:
    def test_the_prior_config_is_copied_aside_before_the_rerender(self, tmp_path):
        boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash")

        previous = tmp_path / "config.toml.prev"
        assert previous.exists(), (
            "a re-render discards whatever was in config.toml, including an "
            "operator's hand edit. One copy of the last one is the safety net."
        )
        assert 'model = "z-ai/glm-5.2"' in previous.read_text()

    def test_the_first_boot_leaves_no_backup(self, tmp_path):
        boot(tmp_path)
        assert not (tmp_path / "config.toml.prev").exists()


class TestTheDriftReport:
    """Option 2 of the entry: the class of failure is the silence."""

    def test_a_rerender_that_changed_something_names_the_key(self, tmp_path):
        run_stage(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        proc = run_stage(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash")

        output = proc.stdout + proc.stderr
        assert "brain.native.model" in output, output
        assert "z-ai/glm-5.3-flash" in output, output

    def test_an_unchanged_rerender_says_nothing_about_drift(self, tmp_path):
        run_stage(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        proc = run_stage(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")

        output = proc.stdout + proc.stderr
        assert "brain.native.model" not in output, (
            f"a boot that changed nothing reported drift:\n{output}"
        )

    def test_a_credential_that_changed_is_named_but_not_printed(self, tmp_path):
        run_stage(tmp_path, ISTOTA_EMAIL_ENABLED="true", ISTOTA_EMAIL_IMAP_PASSWORD="old-imap-password")
        proc = run_stage(
            tmp_path,
            ISTOTA_EMAIL_ENABLED="true",
            ISTOTA_EMAIL_IMAP_PASSWORD="new-imap-password",
        )

        output = proc.stdout + proc.stderr
        assert "imap_password" in output, output
        assert "new-imap-password" not in output, (
            f"a credential's value reached the container log:\n{output}"
        )
        assert "old-imap-password" not in output, output


class TestThePreserveEscapeHatch:
    def test_preserve_keeps_the_existing_file(self, tmp_path):
        boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        rendered = boot(
            tmp_path,
            ISTOTA_CONFIG_RENDER="preserve",
            ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash",
        )

        assert rendered["brain"]["native"]["model"] == "z-ai/glm-5.2"

    def test_preserve_still_reports_what_it_is_holding_back(self, tmp_path):
        run_stage(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        proc = run_stage(
            tmp_path,
            ISTOTA_CONFIG_RENDER="preserve",
            ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash",
        )

        output = proc.stdout + proc.stderr
        assert "brain.native.model" in output, output
        assert "z-ai/glm-5.3-flash" in output, output
        assert not (tmp_path / "config.toml.probe").exists(), (
            "the comparison render was left on disk"
        )

    def test_preserve_still_renders_when_there_is_nothing_to_preserve(self, tmp_path):
        rendered = boot(tmp_path, ISTOTA_CONFIG_RENDER="preserve")
        assert rendered["users"]["testuser"]["display_name"] == "testuser"

    def test_an_unrecognised_mode_renders_and_says_so(self, tmp_path):
        boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.2")
        proc = run_stage(
            tmp_path,
            ISTOTA_CONFIG_RENDER="perserve",
            ISTOTA_BRAIN_NATIVE_MODEL="z-ai/glm-5.3-flash",
        )

        assert proc.returncode == 0, proc.stderr
        output = proc.stdout + proc.stderr
        assert "perserve" in output, output
        rendered = tomllib.loads((tmp_path / "config.toml").read_text())
        assert rendered["brain"]["native"]["model"] == "z-ai/glm-5.3-flash", (
            "a typo in the mode must fail towards the default, not towards the "
            "behaviour the issue was filed about"
        )


class TestTheReadinessFlag:
    """The race a per-boot render opens, and the signal that closes it.

    `web` polls for a file and then reads the config. While the config never
    changed after first boot that poll could not lose; once it is rewritten on
    every boot, `web` can read the previous boot's file. It waits on a flag this
    stage writes instead.
    """

    def test_the_stage_publishes_the_flag_after_the_render(self, tmp_path):
        boot(tmp_path)
        assert (tmp_path / ".config-current").exists()

    def test_the_flag_is_cleared_before_the_provisioning_wait(self):
        source = ENTRYPOINT.read_text()
        cleared = source.index('rm -f "$CONFIG_READY_FLAG"')
        waiting = source.index("Waiting for Nextcloud provisioning")
        assert cleared < waiting, (
            "the readiness flag has to be cleared before the boot's first long "
            "wait, or a reader finds the previous boot's flag and reads the "
            "previous boot's config."
        )

    def test_the_web_service_waits_on_the_flag_rather_than_the_config(self):
        compose = (REPO / "docker" / "docker-compose.yml").read_text()
        assert "/data/config/.config-current" in compose, (
            "docker-compose.yml's web service still waits for config.toml to "
            "exist, which on every boot but the first is already true."
        )


class TestTheRenderIsTheOnlyWriter:
    """Replaces the old assertion that the backfill passes stayed put.

    The three add-if-missing passes — `log_channel` / `alerts_channel`, `[web]`
    / `[site]`, and the module resources — existed because the render ran once.
    A render on every boot produces all three from the same inputs, so keeping
    them would mean two code paths writing the same keys with different rules.
    """

    def test_no_backfill_pass_survives_in_the_entrypoint(self):
        source = ENTRYPOINT.read_text()
        body = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert "Backfilled" not in body, (
            "a backfill pass is still writing config.toml behind the render"
        )

    def test_the_entrypoint_writes_the_config_only_through_the_render(self):
        stage = config_stage()
        body = "\n".join(
            line for line in stage.splitlines() if not line.lstrip().startswith("#")
        )
        writes = re.findall(r'>>?\s*"\$CONFIG_FILE"', body)
        assert not writes, f"the stage writes config.toml directly: {writes}"


def _failing_render_dir(tmp_path: Path, *, message: str = "render exploded") -> Path:
    """A directory whose `render-config.sh` fails without writing anything."""
    directory = tmp_path / "broken-render"
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "render-config.sh"
    script.write_text(f'#!/bin/bash\necho "{message}" >&2\nexit 2\n')
    script.chmod(0o755)
    return directory


class TestARenderThatFails:
    """The regression a per-boot render can introduce, and must not.

    Before this change a boot with a config already in place skipped the render,
    so a broken render could not stop a working deployment. `render-config.sh`
    writes to a `.partial` sibling and `mv`s it, so a failure leaves the previous
    config intact and known good — and aborting on it would turn a transient
    fault into a crash loop under `restart: unless-stopped`, taking `web` and
    `webhooks` with it, since the readiness flag would never be published.
    """

    def test_a_later_boot_falls_back_to_the_existing_config(self, tmp_path):
        data = tmp_path / "data"
        boot(data, ISTOTA_BRAIN_NATIVE_MODEL="vendor/model-a")

        proc = run_stage(
            data,
            entrypoint_dir=_failing_render_dir(tmp_path),
            ISTOTA_BRAIN_NATIVE_MODEL="vendor/model-b",
        )

        assert proc.returncode == 0, (
            f"a failed render on a boot with a good config aborted the "
            f"entrypoint:\n{proc.stdout}\n{proc.stderr}"
        )
        rendered = tomllib.loads((data / "config.toml").read_text())
        assert rendered["brain"]["native"]["model"] == "vendor/model-a"

    def test_the_fallback_is_loud(self, tmp_path):
        data = tmp_path / "data"
        boot(data)

        proc = run_stage(data, entrypoint_dir=_failing_render_dir(tmp_path))

        assert "ERROR" in proc.stderr, proc.stderr

    def test_the_fallback_still_publishes_the_readiness_flag(self, tmp_path):
        """Otherwise a config fault also takes the web UI down."""
        data = tmp_path / "data"
        boot(data)
        (data / ".config-current").unlink()

        run_stage(data, entrypoint_dir=_failing_render_dir(tmp_path))

        assert (data / ".config-current").exists()

    def test_a_first_boot_with_no_config_still_fails_hard(self, tmp_path):
        """Nothing to fall back to, so the pre-existing behaviour stands."""
        data = tmp_path / "data"

        proc = run_stage(data, entrypoint_dir=_failing_render_dir(tmp_path))

        assert proc.returncode != 0
        assert not (data / "config.toml").exists()
        assert not (data / ".config-current").exists()


class TestTheReadinessFlagIdentifiesTheConfig:
    """Existence alone did not answer the question the flag was added for.

    A reader starting concurrently can test the flag before this script's `rm -f`
    has run, find the previous boot's, and go — so the flag carries the rendered
    config's hash and the reader compares it against the file it is about to
    read.
    """

    def test_the_flag_holds_the_hash_of_the_config_beside_it(self, tmp_path):
        boot(tmp_path)

        digest = hashlib.sha256((tmp_path / "config.toml").read_bytes()).hexdigest()
        assert (tmp_path / ".config-current").read_text().strip() == digest

    def test_a_flag_left_by_a_boot_that_rendered_something_else_does_not_match(
        self, tmp_path
    ):
        boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="vendor/model-a")
        stale = (tmp_path / ".config-current").read_text().strip()

        boot(tmp_path, ISTOTA_BRAIN_NATIVE_MODEL="vendor/model-b")
        fresh = (tmp_path / ".config-current").read_text().strip()

        assert stale != fresh, (
            "a reader comparing the flag against config.toml could not tell the "
            "previous boot's flag from this boot's"
        )

    def test_both_config_readers_compare_rather_than_test_for_existence(self):
        """`web` and `webhooks` share the volume and both read the config."""
        compose = (REPO / "docker" / "docker-compose.yml").read_text()

        assert compose.count("config_is_current()") == 2, (
            "one of the two services that reads config.toml still waits on the "
            "flag existing rather than on it describing the file it will read"
        )


class TestTheRoomTokensStayOutOfTheLog:
    """A Talk room token is a bearer capability, and the log is not private."""

    def test_a_changed_log_channel_is_named_without_its_token(self, tmp_path):
        run_stage(tmp_path, USER_LOG_CHANNEL="roomtokenaaa")
        proc = run_stage(tmp_path, USER_LOG_CHANNEL="roomtokenbbb")

        output = proc.stdout + proc.stderr
        assert "log_channel" in output, output
        assert "roomtokenbbb" not in output, (
            f"a Talk room token reached the container log:\n{output}"
        )
        assert "roomtokenaaa" not in output, output

    def test_a_changed_alerts_channel_is_named_without_its_token(self, tmp_path):
        run_stage(tmp_path, USER_ALERTS_CHANNEL="alertsroomaaa")
        proc = run_stage(tmp_path, USER_ALERTS_CHANNEL="alertsroombbb")

        output = proc.stdout + proc.stderr
        assert "alerts_channel" in output, output
        assert "alertsroombbb" not in output, output


class TestTheEntrypointsOwnVariablesReachIt:
    """The other half of the passthrough rule, which nothing covered.

    `tests/test_render_config.py::test_every_var_the_render_reads_is_passed_by_compose`
    scans `render-config.sh` and, for the `passed` set, the *whole* compose file.
    So a variable read by `entrypoint.sh` rather than by the render is outside
    its scan entirely, and a variable passed to some *other* service satisfies
    it. `ISTOTA_WEB_SESSION_SECRET_KEY` was both at once: read here, passed only
    to `web`, so the documented first arm of the session-key resolution could
    never fire on the shipped stack. That is precisely the ISSUE-368 class — a
    setting read on the boot path and not passed through — reappearing inside
    the fix for it.
    """

    #: Where the `istota` service's `environment:` block sits, found by scanning
    #: rather than by line number.
    def _istota_environment(self) -> set[str]:
        compose = (REPO / "docker" / "docker-compose.yml").read_text().splitlines()
        start = next(
            i for i, line in enumerate(compose) if line.rstrip() == "  istota:"
        )
        end = next(
            (
                i
                for i in range(start + 1, len(compose))
                if compose[i].startswith("  ") and compose[i][2:3] not in (" ", "")
            ),
            len(compose),
        )
        block = compose[start:end]
        return set(re.findall(r"^\s*(ISTOTA_[A-Z0-9_]*):", "\n".join(block), re.M))

    def test_the_istota_service_block_was_actually_located(self):
        """A scan that found nothing would make the test below vacuous."""
        found = self._istota_environment()
        assert len(found) > 50, f"only found {len(found)} variables; the scan has rotted"
        assert "ISTOTA_BOT_NAME" in found

    def test_every_istota_var_the_entrypoint_reads_is_passed_to_that_service(self):
        source = ENTRYPOINT.read_text()
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        read = set(re.findall(r"\$\{?(ISTOTA_[A-Z0-9_]*)", code))
        assert read, "the scan found no ISTOTA_ reads; the regex has rotted"

        # Set by this script for its children rather than read from compose.
        SELF_ASSIGNED = {"ISTOTA_ADMINS_FILE", "ISTOTA_SECRET_KEY"}

        missing = sorted(read - self._istota_environment() - SELF_ASSIGNED)
        assert not missing, (
            f"entrypoint.sh reads {missing}, which docker-compose.yml does not "
            f"pass to the `istota` service. Each expands to empty on a real "
            f"boot, unsettable by the operator, while the suite stays green."
        )
