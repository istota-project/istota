"""Row 5, the exec half: the VM's `istota` wrapper and the healthcheck run as 10001.

Stage 2 witnessed the daemon (`tests/image/test_istota_image.py`). A
`docker compose exec` does not inherit the daemon's drop: it starts as uid 0
with the container's cap_add set, in a container whose /proc/sys is writable.
So every exec path goes through `istota-drop`. This boots the shipped compose
file's `istota` service and drives the real `host/istota` wrapper and
`host/istota-stack exec` against it (ISTOTA_STACK_EXEC points the script's
`compose exec istota` prefix at this container; the drop is the script's own),
then runs `istota doctor` and `istota user ensure` through the wrapper and
requires that nothing under /data is owned by uid 0 afterwards. The
healthcheck's command is read from the compose file and its drop run the same
way.

The negative control is `scripts/test-stack-negative-control.sh no-drop`: a copy
of `istota-stack` whose exec path skips `istota-drop`, handed over in
ISTOTA_STACK_SCRIPT.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.support import parity

from .conftest import REPO
from .shipped_stack import COMPOSE, boot_shipped_stack

pytestmark = pytest.mark.image

SCRIPT = Path(os.environ.get("ISTOTA_STACK_SCRIPT") or REPO / "host" / "istota-stack")
WRAPPER = REPO / "host" / "istota"
STATUS = "grep -E '^(Uid|Gid|CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs):' /proc/self/status"


def _status(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.split()[0] if value.split() else ""
    return fields


@dataclass(frozen=True)
class WrapperRun:
    wrapper_status: dict[str, str]
    healthcheck_status: dict[str, str]
    healthcheck_prefix: str
    doctor_rc: int
    ensure: subprocess.CompletedProcess
    root_owned: list[str]


@pytest.fixture(scope="module")
def wrapper_run(istota_image, tmp_path_factory) -> WrapperRun:
    work = tmp_path_factory.mktemp("row5-exec")
    with boot_shipped_stack(
        istota_image.tag, work,
        setup_env={"ISTOTA_BRAIN_NATIVE_API_KEY": "row5-unused-key"},
        project_prefix="istota-row5",
    ) as stack:
        env = {
            **os.environ,
            "ISTOTA_STACK_BIN": str(SCRIPT),
            "ISTOTA_STACK_EXEC": f"docker exec -i {stack.container}",
        }

        def via(*argv: str, script: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run([*script, *argv], capture_output=True, text=True, env=env, timeout=180)

        status = via("sh", "-c", STATUS, script=["bash", str(SCRIPT), "exec"])
        assert status.returncode == 0, status.stderr

        doctor = via("doctor", "--json", script=["sh", str(WRAPPER)])
        ensure = via("user", "ensure", "--name", "bob", script=["sh", str(WRAPPER)])

        test = yaml.safe_load(COMPOSE.read_text())["services"]["istota"]["healthcheck"]["test"]
        command = test[1] if test[0] == "CMD-SHELL" else " ".join(test[1:])
        prefix = command.split()[0]
        health = stack.exec("sh", "-c", f"{prefix} sh -c \"{STATUS}\"")
        assert health.returncode == 0, health.stderr

        # The state volume. /data/config is a host bind, which Docker Desktop
        # shows as uid 0 whoever owns it (on the VM it is 10001), and nothing
        # the wrapper runs can write there: it is read-only.
        owned = stack.exec(
            "find", "/data", "-xdev", "-path", "/data/config", "-prune", "-o", "-uid", "0", "-print",
            user="0",
        )
        return WrapperRun(
            wrapper_status=_status(status.stdout),
            healthcheck_status=_status(health.stdout),
            healthcheck_prefix=prefix,
            doctor_rc=doctor.returncode,
            ensure=ensure,
            root_owned=[line for line in owned.stdout.splitlines() if line.strip()],
        )


EMPTY = "0000000000000000"


@parity.witness(5)
class TestTheVmWrapperExecsAsTheDaemon:
    def test_the_wrapper_runs_as_10001_with_no_capabilities(self, wrapper_run):
        status = wrapper_run.wrapper_status
        assert status["Uid"] == "10001" and status["Gid"] == "10001", status
        for cap in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            assert status[cap] == EMPTY, (cap, status)

    def test_the_healthcheck_drops_the_same_way(self, wrapper_run):
        assert wrapper_run.healthcheck_prefix == "istota-drop"
        status = wrapper_run.healthcheck_status
        assert status["Uid"] == "10001", status
        assert all(status[cap] == EMPTY for cap in ("CapPrm", "CapEff", "CapBnd")), status

    def test_the_wrapper_reached_the_cli(self, wrapper_run):
        """Without this, a wrapper that ran nothing would leave /data clean."""
        assert wrapper_run.ensure.returncode == 0, wrapper_run.ensure.stdout + wrapper_run.ensure.stderr
        assert wrapper_run.doctor_rc in (0, 1)

    def test_nothing_under_data_is_root_owned_after_wrapper_calls(self, wrapper_run):
        assert wrapper_run.root_owned == [], wrapper_run.root_owned
