"""Row 12: credentials reach the daemon as files, never as compose environment.

The shipped compose file declares one `secrets:` file per credential, and the
drop (`istota-drop` → `istota-secrets`) reads them into the dropped process's
environment, so a value is in no `environment:` map and therefore not in
`docker inspect`. This boots the shipped file's `istota` service, from the
image this session built, through the documented setup path: the image's own
`istota setup --vm-dir`, run with `docker compose run` as uid 10001, writes the
config, the master key and the secret files; `docker compose up` then starts
the service from them.

What is witnessed: the container's inspect output carries no credential value,
each file under `/run/secrets` is mode 0400, the daemon's own environment does
carry the credential (so the files were read rather than ignored), and the
config holds none of them. The owner half (uid 10001) is the VM tier's: Docker
Desktop's file sharing shows every bind as uid 0 (`parity.OUTSTANDING_HALVES`).

The negative control is `scripts/test-image-negative-control.sh secrets`: one
credential passed through `environment:` (an overlay), which `docker inspect`
then shows.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from istota.setup_wizard import SECRET_NAMES
from tests.support import parity

from .conftest import REPO, require_docker

pytestmark = pytest.mark.image

COMPOSE = REPO / "docker" / "docker-compose.yml"
HEALTHY_TIMEOUT = 240

#: Overlays the negative control applies, `os.pathsep`-separated, the same
#: mechanism `ISTOTA_TESTBED_CONTROL_OVERLAYS` is for the smoke tier.
CONTROL_OVERLAYS_ENV = "ISTOTA_SECRETS_CONTROL_OVERLAYS"

#: The bundled Nextcloud's `${…:?}` variables, which compose interpolates on
#: every subcommand even for a service that does not depend on them. Values
#: nothing reads: `--no-deps` starts no Nextcloud.
BUNDLED_NEXTCLOUD_ENV = {
    "POSTGRES_PASSWORD": "unused-by-this-test",
    "ADMIN_PASSWORD": "unused-by-this-test",
    "BOT_PASSWORD": "unused-by-this-test",
    "USER_NAME": "alice",
    "USER_PASSWORD": "unused-by-this-test",
}


@dataclass(frozen=True)
class SecretsBoot:
    native_key: str
    session_key: str
    inspect: str
    modes: dict[str, str]
    daemon_env: str
    config: str
    logs: str


def _compose(args: list[str], *, env_file: Path, overlays: list[Path], project: str,
             timeout: int = 300, extra_env: dict[str, str] | None = None):
    argv = ["docker", "compose", "-f", str(COMPOSE)]
    for overlay in overlays:
        argv += ["-f", str(overlay)]
    argv += ["--project-name", project, "--env-file", str(env_file), *args]
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(extra_env or {})},
    )


@pytest.fixture(scope="module")
def secrets_boot(istota_image, tmp_path_factory) -> SecretsBoot:
    require_docker()
    work = tmp_path_factory.mktemp("row12")
    vm = work / "vm"
    secrets_dir = vm / "secrets"
    secrets_dir.mkdir(parents=True)
    # Compose refuses to run a service whose secret file is missing, before
    # anything in the container could write one, so the stack directory starts
    # with every file empty. `istota setup --vm-dir` then replaces them.
    for name in SECRET_NAMES:
        (secrets_dir / name).write_text("")

    native_key = f"row12-native-{secrets.token_hex(8)}"
    overlay = work / "image.yml"
    overlay.write_text(
        "services:\n  istota:\n    build: !reset null\n"
        f"    image: {istota_image.tag}\n"
    )
    overlays = [overlay] + [
        Path(part) for part in os.environ.get(CONTROL_OVERLAYS_ENV, "").split(os.pathsep) if part
    ]
    env_file = work / "compose.env"
    env_file.write_text("".join(
        f"{key}={value}\n" for key, value in {
            **BUNDLED_NEXTCLOUD_ENV,
            "ISTOTA_SECRETS_DIR": str(secrets_dir),
            # Read only by the negative control's overlay.
            "ISTOTA_ROW12_LEAKED_VALUE": native_key,
        }.items()
    ))
    project = f"istota-row12-{uuid.uuid4().hex[:8]}"
    call = dict(env_file=env_file, overlays=overlays, project=project)
    try:
        setup = _compose([
            "run", "--rm", "--no-deps", "-T",
            "-e", f"ISTOTA_BRAIN_NATIVE_API_KEY={native_key}",
            "-v", f"{vm}:/vm",
            "--entrypoint", "istota-drop", "istota",
            "istota", "setup", "--yes", "--vm-dir", "/vm", "--user", "alice",
            "--brain", "native", "--native-model", "scripted-test-model",
        ], **call)
        assert setup.returncode == 0, setup.stdout + setup.stderr
        session_key = (secrets_dir / "istota_web_session_secret_key").read_text()
        assert (secrets_dir / "istota_brain_native_api_key").read_text() == native_key

        up = _compose(["up", "-d", "--no-deps", "--no-build", "istota"], **call)
        assert up.returncode == 0, up.stdout + up.stderr
        container = _compose(["ps", "-q", "istota"], **call).stdout.strip()
        assert container, "no istota container came up"

        deadline = time.monotonic() + HEALTHY_TIMEOUT
        health = ""
        while time.monotonic() < deadline:
            health = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Health.Status}}", container],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if health == "healthy":
                break
            time.sleep(2)
        logs = _compose(["logs", "istota"], **call)
        assert health == "healthy", f"istota is {health!r}\n{logs.stdout[-4000:]}"

        def run(*argv: str) -> str:
            result = subprocess.run(
                ["docker", "exec", container, *argv],
                capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        inspect = subprocess.run(
            ["docker", "inspect", container], capture_output=True, text=True, timeout=30,
        ).stdout
        modes = {
            name: run("stat", "-c", "%a", f"/run/secrets/{name}").strip()
            for name in SECRET_NAMES
        }
        daemon_env = run("istota-drop", "sh", "-c", "tr '\\0' '\\n' </proc/1/environ")
        config = run("istota-drop", "cat", "/data/config/config.toml")
        return SecretsBoot(
            native_key=native_key, session_key=session_key, inspect=inspect,
            modes=modes, daemon_env=daemon_env, config=config, logs=logs.stdout,
        )
    finally:
        _compose(["down", "--volumes", "--remove-orphans", "--timeout", "5"], **call)


@parity.witness(12)
class TestTheCredentialsAreFilesNotEnvironment:
    def test_docker_inspect_carries_no_credential(self, secrets_boot):
        for label, value in (
            ("the native brain key", secrets_boot.native_key),
            ("the web session key", secrets_boot.session_key),
        ):
            assert value not in secrets_boot.inspect, (
                f"{label} is in `docker inspect istota`, so it reached the "
                "container through compose's environment rather than a secret file"
            )

    def test_every_secret_file_is_mode_0400(self, secrets_boot):
        assert secrets_boot.modes == {name: "400" for name in SECRET_NAMES}

    def test_the_daemon_read_them(self, secrets_boot):
        """Without this, a container that ignored its secret files entirely
        would pass the inspect assertion by carrying no credential at all."""
        lines = secrets_boot.daemon_env.splitlines()

        assert f"ISTOTA_BRAIN_NATIVE_API_KEY={secrets_boot.native_key}" in lines
        assert f"ISTOTA_WEB_SESSION_SECRET_KEY={secrets_boot.session_key}" in lines

    def test_the_config_holds_none_of_them(self, secrets_boot):
        assert secrets_boot.native_key not in secrets_boot.config
        assert secrets_boot.session_key not in secrets_boot.config
