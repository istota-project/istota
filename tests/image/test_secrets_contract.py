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
from dataclasses import dataclass
from pathlib import Path

import pytest

from istota.setup_wizard import SECRET_NAMES
from tests.support import parity

from .shipped_stack import boot_shipped_stack

pytestmark = pytest.mark.image

#: Overlays the negative control applies, `os.pathsep`-separated, the same
#: mechanism `ISTOTA_TESTBED_CONTROL_OVERLAYS` is for the smoke tier.
CONTROL_OVERLAYS_ENV = "ISTOTA_SECRETS_CONTROL_OVERLAYS"


@dataclass(frozen=True)
class SecretsBoot:
    native_key: str
    session_key: str
    inspect: str
    modes: dict[str, str]
    daemon_env: str
    config: str
    logs: str


@pytest.fixture(scope="module")
def secrets_boot(istota_image, tmp_path_factory) -> SecretsBoot:
    work = tmp_path_factory.mktemp("row12")
    native_key = f"row12-native-{secrets.token_hex(8)}"
    overlays = [
        Path(part) for part in os.environ.get(CONTROL_OVERLAYS_ENV, "").split(os.pathsep) if part
    ]
    with boot_shipped_stack(
        istota_image.tag, work,
        setup_env={"ISTOTA_BRAIN_NATIVE_API_KEY": native_key},
        # Read only by the negative control's overlay.
        env_values={"ISTOTA_ROW12_LEAKED_VALUE": native_key},
        overlays=overlays, project_prefix="istota-row12",
    ) as stack:
        session_key = (stack.secrets_dir / "istota_web_session_secret_key").read_text()
        assert (stack.secrets_dir / "istota_brain_native_api_key").read_text() == native_key

        def run(*argv: str) -> str:
            result = stack.exec(*argv, timeout=60)
            assert result.returncode == 0, result.stderr
            return result.stdout

        inspect = subprocess.run(
            ["docker", "inspect", stack.container], capture_output=True, text=True, timeout=30,
        ).stdout
        modes = {
            name: run("stat", "-c", "%a", f"/run/secrets/{name}").strip()
            for name in SECRET_NAMES
        }
        daemon_env = run("istota-drop", "sh", "-c", "tr '\\0' '\\n' </proc/1/environ")
        config = run("istota-drop", "cat", "/data/config/config.toml")
        return SecretsBoot(
            native_key=native_key, session_key=session_key, inspect=inspect,
            modes=modes, daemon_env=daemon_env, config=config, logs=stack.logs,
        )


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
