"""Every setting the browser image reads from its environment reaches it on the stack.

A scan rather than a list: the CDP and wedge-recovery tunables were read by the
image and set only by a deleted deployment role, so on the stack nobody could
change them (ISSUE-709). A list kept by hand drifts the same way.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BROWSER_DIR = REPO / "docker" / "browser"

ENV_READ = re.compile(r"""os\.environ\.get\(\s*["'](\w+)["']\s*,\s*["']([^"']*)["']\s*,?\s*\)""")
# Any read by a literal name, in any shape. Each must also match ENV_READ, so a
# read the scan cannot parse fails rather than going unchecked.
ANY_ENV_READ = re.compile(r"""(?:os\.environ\.get|os\.getenv|os\.environ\[)\(?\s*["'](\w+)["']""")

# Read by the image but not forwarded from compose, each for a reason.
NOT_FORWARDED = {
    "BROWSER_VNC_URL": "compose sets it from ISTOTA_BROWSER_VNC_URL, the daemon's name for the same value",
    "BROWSER_LIVENESS_PORT": "the image HEALTHCHECK and compose healthcheck hard-code 9224",
    "BROWSER_PROFILE_DIR": "entrypoint.sh owns it, and compose mounts the profile volume at its default",
    "SCREEN_WIDTH": "pool.py sizes each instance's Xvfb from it; a debugging override, not a deployment setting",
    "SCREEN_HEIGHT": "as SCREEN_WIDTH",
    "CHROME_EXECUTABLE": "the path the image installs Chrome at",
    "CHROME_LOG_STDERR": "a debugging switch, set with `docker compose run -e` when wanted",
}


def _image_env_reads() -> dict[str, str]:
    reads: dict[str, str] = {}
    for path in sorted(BROWSER_DIR.rglob("*.py")):
        for name, default in ENV_READ.findall(path.read_text()):
            assert reads.get(name, default) == default, f"{name} read with two defaults"
            reads[name] = default
    return reads


def _compose_browser_env() -> dict[str, str]:
    service = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())["services"]["browser"]
    env = service["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {name: str(value) for name, value in env.items()}


def test_scan_finds_the_browser_settings():
    reads = _image_env_reads()
    for name in (
        "BROWSER_MAX_INSTANCES",
        "BROWSER_DISK_CACHE_BYTES",
        "BROWSER_CDP_FAILURE_THRESHOLD",
        "BROWSER_WEDGE_RECOVERY_THRESHOLD",
        "BROWSE_WATCHDOG_DEADLINE_S",
    ):
        assert name in reads


def test_compose_forwards_every_image_setting_with_its_default():
    reads = _image_env_reads()
    env = _compose_browser_env()
    missing = []
    for name, default in sorted(reads.items()):
        if name in NOT_FORWARDED:
            continue
        expected = "${" + name + ":-" + default + "}"
        if env.get(name) != expected:
            missing.append(f"{name}: compose has {env.get(name)!r}, want {expected!r}")
    assert not missing, "\n".join(missing)


def test_not_forwarded_entries_are_still_read():
    reads = _image_env_reads()
    assert not set(NOT_FORWARDED) - set(reads)


def test_every_env_read_has_the_shape_the_scan_parses():
    loose: set[str] = set()
    for path in sorted(BROWSER_DIR.rglob("*.py")):
        loose.update(ANY_ENV_READ.findall(path.read_text()))
    assert loose == set(_image_env_reads())
