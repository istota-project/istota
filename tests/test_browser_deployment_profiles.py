"""Browser resource settings reach the compose browser service."""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SETTINGS = {
    "BROWSER_MAX_INSTANCES": ("istota_browser_max_instances", 2),
    "BROWSER_INSTANCE_IDLE_S": ("istota_browser_instance_idle_s", 900),
    "BROWSER_MAX_TOTAL_SESSIONS": ("istota_browser_max_total_sessions", 4),
    "BROWSER_DISK_CACHE_BYTES": ("istota_browser_disk_cache_bytes", 104857600),
}


def test_compose_forwards_profile_limits():
    service = yaml.safe_load((REPO / "docker" / "docker-compose.yml").read_text())["services"]["browser"]
    env = service["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    for name, (_, default) in SETTINGS.items():
        assert env[name] == "${" + name + ":-" + str(default) + "}"
    assert env["MAX_BROWSER_SESSIONS"] == "${MAX_BROWSER_SESSIONS:-2}"

