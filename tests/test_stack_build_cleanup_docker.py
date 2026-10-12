"""Real cleanup in a disposable Docker daemon, never the developer's daemon.

Run explicitly: uv run pytest tests/test_stack_build_cleanup_docker.py -m integration -n0
Needs local Docker and docker:29-dind; uses about 3 GB temporarily. Point
ISTOTA_STACK_SCRIPT at a copy with prune_build_artifacts disabled for the
negative control: the dangling-image and cache-size assertions must fail.
"""

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
SCRIPT = Path(os.environ.get("ISTOTA_STACK_SCRIPT") or Path(__file__).resolve().parents[1] / "host/istota-stack")


def _run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=300, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture
def daemon(tmp_path):
    name = "istota-cleanup-test-" + uuid.uuid4().hex[:12]
    # No host socket or published port. Even a broken prune cannot reach the
    # developer's images; the daemon and its anonymous volume are disposable.
    _run("docker", "run", "-d", "--privileged", "--name", name,
         "-e", "DOCKER_TLS_CERTDIR=", "docker:29-dind", "--host=unix:///var/run/docker.sock",
         "--storage-driver=overlay2", "--feature=containerd-snapshotter=false")
    try:
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", name, "docker", "info"], capture_output=True, timeout=10)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("disposable Docker daemon did not become ready")
        yield name
    finally:
        _run("docker", "rm", "-fv", name)


def test_cleanup_reclaims_builds_and_preserves_images_containers_and_data(daemon, tmp_path):
    def docker(*args):
        return _run("docker", "exec", daemon, "docker", *args)

    work = tmp_path / "build"
    work.mkdir()
    (work / "Dockerfile").write_text(
        "FROM busybox:1.37 AS waste\nARG NONCE\n"
        "RUN echo $NONCE > /nonce && dd if=/dev/urandom of=/cache bs=1M count=768\n"
        "FROM busybox:1.37\nCOPY --from=waste /nonce /nonce\nCMD [\"sleep\", \"600\"]\n"
    )
    (work / "compose.yml").write_text(
        "services:\n  daemon:\n    image: istota:${TAG}\n"
        "    build:\n      context: .\n      args:\n        NONCE: ${NONCE}\n"
    )
    _run("docker", "cp", str(work), f"{daemon}:/build")
    for tag, nonce in [("v0", "0"), ("v1", "1"), ("v1", "2"), ("v2", "3")]:
        _run("docker", "exec", "-e", f"TAG={tag}", "-e", f"NONCE={nonce}", daemon,
             "docker", "compose", "-p", "istota", "-f", "/build/compose.yml", "build")
    dangling = docker("image", "ls", "-q", "--filter", "dangling=true",
                      "--filter", "label=com.docker.compose.project=istota").splitlines()
    assert dangling, docker("image", "ls") + docker("image", "inspect", "-f", "{{json .Config.Labels}}", "istota:v1")
    docker("pull", "busybox:1.37")
    docker("tag", "busybox:1.37", "unrelated:keep")
    docker("run", "-d", "--name", "unrelated", "-v", "unrelated-data:/data", "unrelated:keep",
           "sh", "-c", "echo preserved > /data/value; sleep 600")
    docker("run", "-d", "--name", "current", "istota:v2")
    retained = {tag: docker("image", "inspect", "-f", "{{.Id}}", tag)
                for tag in ["istota:v1", "istota:v2", "unrelated:keep"]}

    def cache_bytes():
        records = docker("buildx", "du", "--format", "json")
        total = 0
        for line in records.splitlines():
            size = json.loads(line)["Size"]
            amount, unit = re.fullmatch(r"([0-9.]+)([kMGT]?B)", size).groups()
            total += float(amount) * {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[unit]
        return total

    before = cache_bytes()
    assert before > 2_500_000_000, before
    envfile = tmp_path / ".env"
    envfile.write_text("ISTOTA_TAG=v2\nISTOTA_PREVIOUS_TAG=v1\n")
    script = (
        f"source {shlex.quote(str(SCRIPT))}; "
        f"docker() {{ command docker exec {shlex.quote(daemon)} docker \"$@\"; }}; "
        f"ENV_FILE={shlex.quote(str(envfile))}; cleanup_update"
    )
    output = _run("bash", "-c", script)
    after = cache_bytes()
    assert after < before and after <= 2_147_483_648, (before, after, output)
    assert not docker("image", "ls", "-q", "--filter", "dangling=true",
                      "--filter", "label=com.docker.compose.project=istota")
    assert not docker("image", "ls", "-q", "istota:v0")
    for tag, image_id in retained.items():
        assert docker("image", "inspect", "-f", "{{.Id}}", tag) == image_id
    assert docker("inspect", "-f", "{{.State.Running}}", "current") == "true"
    assert docker("exec", "unrelated", "cat", "/data/value") == "preserved"
    # Starting the previous image proves rollback needs no cached build layers.
    assert docker("run", "--rm", "--pull=never", "istota:v1", "cat", "/nonce") == "2"
