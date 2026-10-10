"""The shipped compose file's `istota` service, booted the way a VM boots it.

Shared by the row 12 witness (credentials are files) and the row 5 wrapper
witness (every exec path drops to uid 10001). The stack directory is laid out
as `/srv/istota` is: `config/` bound read-only at `/data/config`, `secrets/`
holding one file per credential. `istota setup --vm-dir` runs as uid 10001 in a
one-shot container with the config directory opened (ISTOTA_CONFIG_READ_ONLY),
as `istota-stack setup` runs it; then `docker compose up` starts the service
from what it wrote.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from istota.setup_wizard import SECRET_NAMES

from .conftest import REPO, require_docker

COMPOSE = REPO / "docker" / "docker-compose.yml"
HEALTHY_TIMEOUT = 240


@dataclass
class ShippedStack:
    container: str
    vm: Path
    secrets_dir: Path
    config_dir: Path
    logs: str
    compose: object

    def exec(self, *argv: str, user: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec"]
        if user:
            cmd += ["--user", user]
        return subprocess.run([*cmd, self.container, *argv], capture_output=True, text=True, timeout=timeout)


@contextmanager
def boot_shipped_stack(
    image_tag: str,
    work: Path,
    *,
    setup_env: dict[str, str],
    env_values: dict[str, str] | None = None,
    overlays: list[Path] | None = None,
    project_prefix: str = "istota-shipped",
) -> Iterator[ShippedStack]:
    require_docker()
    vm = work / "vm"
    secrets_dir = vm / "secrets"
    config_dir = vm / "config"
    secrets_dir.mkdir(parents=True)
    config_dir.mkdir()
    # Compose refuses to run a service whose secret file is missing, before
    # anything in the container could write one.
    for name in SECRET_NAMES:
        (secrets_dir / name).write_text("")

    overlay = work / "image.yml"
    # The full-integration workspace bind is `rslave`, which Docker Desktop
    # refuses for a macOS path; this stack stores locally and never reads it.
    overlay.write_text(
        "services:\n  istota:\n    build: !reset null\n"
        f"    image: {image_tag}\n"
        "    volumes:\n      - shipped_mount:/mnt/shared\n"
        "volumes:\n  shipped_mount:\n"
    )
    env_file = work / "compose.env"
    env_file.write_text("".join(
        f"{key}={value}\n" for key, value in {
            "ISTOTA_SECRETS_DIR": str(secrets_dir),
            "ISTOTA_CONFIG_DIR": str(config_dir),
            **(env_values or {}),
        }.items()
    ))
    project = f"{project_prefix}-{uuid.uuid4().hex[:8]}"

    def compose(args: list[str], *, timeout: int = 300, extra_env: dict[str, str] | None = None):
        argv = ["docker", "compose", "-f", str(COMPOSE)]
        for path in [overlay, *(overlays or [])]:
            argv += ["-f", str(path)]
        argv += ["--project-name", project, "--env-file", str(env_file), *args]
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(extra_env or {})},
        )

    try:
        setup_args = ["run", "--rm", "--no-deps", "-T"]
        for key, value in setup_env.items():
            setup_args += ["-e", f"{key}={value}"]
        setup = compose([
            *setup_args, "-v", f"{vm}:/vm", "--entrypoint", "istota-drop", "istota",
            "istota", "setup", "--yes", "--vm-dir", "/vm", "--user", "alice",
            "--brain", "native", "--native-model", "scripted-test-model",
        ], extra_env={"ISTOTA_CONFIG_READ_ONLY": "false"})
        assert setup.returncode == 0, setup.stdout + setup.stderr
        assert (config_dir / "config.toml").is_file(), "setup wrote no config into the config directory"

        up = compose(["up", "-d", "--no-deps", "--no-build", "istota"])
        assert up.returncode == 0, up.stdout + up.stderr
        container = compose(["ps", "-q", "istota"]).stdout.strip()
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
        logs = compose(["logs", "istota"]).stdout
        assert health == "healthy", f"istota is {health!r}\n{logs[-4000:]}"
        yield ShippedStack(container, vm, secrets_dir, config_dir, logs, compose)
    finally:
        compose(["down", "--volumes", "--remove-orphans", "--timeout", "5"])
