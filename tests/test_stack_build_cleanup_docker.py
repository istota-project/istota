"""Real cleanup in a disposable Docker daemon, never the developer's daemon.

Run explicitly: uv run pytest tests/test_stack_build_cleanup_docker.py -m integration -n0
Needs local Docker and docker:29-dind; uses about 5 GB temporarily. Point
ISTOTA_STACK_SCRIPT at a copy with prune_build_artifacts disabled for the
negative control: the dangling-image and cache-size assertions must fail. The
script as it stood before #908 is a control too: its `--keep-storage` stops near
2 GB of records no image holds, so the disk assertion fails.
"""

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
CACHE_BOUND_MB = 300

# What a build cache occupies on disk: overlay directories no image and no
# container reference. `docker buildx du` cannot say this, since it counts layers
# the retained images hold too.
UNREFERENCED_OVERLAY_MB = r"""
refs=$(for id in $(docker image ls -aq) $(docker ps -aq); do
    docker inspect -f '{{.GraphDriver.Data.UpperDir}}:{{.GraphDriver.Data.LowerDir}}' "$id"
done | tr ':' '\n' | sed -n 's#.*/overlay2/\([^/]*\)/diff#\1#p' | sort -u)
total=0
for dir in /var/lib/docker/overlay2/*; do
    name=${dir##*/}
    [ "$name" = l ] && continue
    echo "$refs" | grep -qx "$name" && continue
    size=$(du -sm "$dir" | cut -f1)
    total=$((total + size))
done
echo "$total"
"""


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

    def build(tag, base, nonce):
        _run("docker", "exec", "-e", f"TAG={tag}", "-e", f"BASE={base}", "-e", f"NONCE={nonce}", daemon,
             "docker", "compose", "-p", "istota", "-f", "/build/compose.yml", "build")

    def unreferenced_mb():
        return int(_run("docker", "exec", daemon, "sh", "-c", UNREFERENCED_OVERLAY_MB))

    work = tmp_path / "build"
    work.mkdir()
    # Two services over one base, as the real images are: the base's layers are
    # records two chains share, which is what `--keep-storage` does not count.
    (work / "Dockerfile").write_text(
        "FROM busybox:1.37 AS base\nARG BASE\n"
        "RUN echo $BASE > /base && dd if=/dev/urandom of=/stable bs=1M count=600\n"
        "RUN echo $BASE > /base2 && dd if=/dev/urandom of=/stable2 bs=1M count=100\n"
        "FROM base AS daemon\nARG NONCE\n"
        "RUN echo $NONCE > /nonce && dd if=/dev/urandom of=/source bs=1M count=300\n"
        "CMD [\"sleep\", \"600\"]\n"
        "FROM base AS web\nARG NONCE\n"
        "RUN echo $NONCE > /nonce && dd if=/dev/urandom of=/bundle bs=1M count=200\n"
        "CMD [\"sleep\", \"600\"]\n"
    )
    (work / "compose.yml").write_text(
        "x-args: &args\n  BASE: ${BASE}\n  NONCE: ${NONCE}\n"
        "services:\n"
        "  daemon:\n    image: istota:${TAG}\n"
        "    build:\n      context: .\n      target: daemon\n      args: *args\n"
        "  web:\n    image: istota-web:${TAG}\n"
        "    build:\n      context: .\n      target: web\n      args: *args\n"
    )
    _run("docker", "cp", str(work), f"{daemon}:/build")
    # Before the builds: a base image arriving in the local store between two
    # builds changes how FROM resolves, and the second misses the cache.
    docker("pull", "busybox:1.37")
    docker("tag", "busybox:1.37", "unrelated:keep")
    for tag, base, nonce in [("v0", "a", "0"), ("v1", "b", "1"), ("v1", "c", "2")]:
        build(tag, base, nonce)
    # Past the margin the age filter allows, so these are an earlier build's.
    time.sleep(30)
    first_began = int(time.time())
    build("v2", "c", "3")
    dangling = docker("image", "ls", "-q", "--filter", "dangling=true",
                      "--filter", "label=com.docker.compose.project=istota").splitlines()
    assert dangling, docker("image", "ls") + docker("image", "inspect", "-f", "{{json .Config.Labels}}", "istota:v1")
    docker("run", "-d", "--name", "unrelated", "-v", "unrelated-data:/data", "unrelated:keep",
           "sh", "-c", "echo preserved > /data/value; sleep 600")
    docker("run", "-d", "--name", "current", "istota:v2")
    retained = {tag: docker("image", "inspect", "-f", "{{.Id}}", tag)
                for tag in ["istota:v1", "istota:v2", "istota-web:v2", "unrelated:keep"]}

    envfile = tmp_path / ".env"
    envfile.write_text("ISTOTA_TAG=v2\nISTOTA_PREVIOUS_TAG=v1\n")
    script = (
        f"source {shlex.quote(str(SCRIPT))}; "
        f"docker() {{ command docker exec {shlex.quote(daemon)} docker \"$@\"; }}; "
        f"ENV_FILE={shlex.quote(str(envfile))}; RECLAIM_LOG={shlex.quote(str(tmp_path / 'reclaimed.log'))}; "
        f"cleanup_update {first_began}"
    )
    output = _run("bash", "-c", script)

    # The latest build's layers are all the retained images', so nothing stale
    # means almost nothing here. `--keep-storage 2GB` stops near 2 GB of stale
    # records instead, which is the control this assertion goes red on.
    after = unreferenced_mb()
    assert after <= CACHE_BOUND_MB, (after, output)
    assert not docker("image", "ls", "-q", "--filter", "dangling=true",
                      "--filter", "label=com.docker.compose.project=istota")
    assert not docker("image", "ls", "-q", "istota:v0")
    for tag, image_id in retained.items():
        assert docker("image", "inspect", "-f", "{{.Id}}", tag) == image_id
    assert docker("inspect", "-f", "{{.State.Running}}", "current") == "true"
    assert docker("exec", "unrelated", "cat", "/data/value") == "preserved"
    # Starting the previous image proves rollback needs no cached build layers.
    assert docker("run", "--rm", "--pull=never", "istota:v1", "cat", "/nonce") == "2"
    # The cost of a prune that clears everything is a cold build on every update.
    # Two rounds, with a stage of two steps: the age filter drops the first step
    # once a build has not needed it, and the stage's last step still has to hit.
    for tag, nonce in [("v3", "4"), ("v4", "5")]:
        began = int(time.time())
        rebuilt = subprocess.run(
            ["docker", "exec", "-e", f"TAG={tag}", "-e", "BASE=c", "-e", f"NONCE={nonce}", daemon,
             "docker", "compose", "--progress=plain", "-p", "istota", "-f", "/build/compose.yml", "build"],
            capture_output=True, text=True, timeout=300,
        )
        log = rebuilt.stdout + rebuilt.stderr
        assert rebuilt.returncode == 0, log
        step = re.search(r"#(\d+) \[[^\]]*base 3/3\]", log)
        assert step and f"#{step.group(1)} CACHED" in log, log
        previous = {"v3": "v2", "v4": "v3"}[tag]
        envfile.write_text(f"ISTOTA_TAG={tag}\nISTOTA_PREVIOUS_TAG={previous}\n")
        time.sleep(15)
        _run("bash", "-c", script.replace(f"cleanup_update {first_began}", f"cleanup_update {began}"))
