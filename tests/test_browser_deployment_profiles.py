"""Browser resource settings reach every deployed container shape."""
from pathlib import Path

import jinja2
import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy/ansible"
SETTINGS = {
    "BROWSER_MAX_INSTANCES": ("istota_browser_max_instances", 2),
    "BROWSER_INSTANCE_IDLE_S": ("istota_browser_instance_idle_s", 900),
    "BROWSER_MAX_TOTAL_SESSIONS": ("istota_browser_max_total_sessions", 4),
    "BROWSER_DISK_CACHE_BYTES": ("istota_browser_disk_cache_bytes", 104857600),
}


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.browser.yml"])
def test_compose_forwards_profile_limits(filename):
    service = yaml.safe_load((REPO / "docker" / filename).read_text())["services"]["browser"]
    env = service["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    for name, (_, default) in SETTINGS.items():
        assert env[name] == "${" + name + ":-" + str(default) + "}"
    assert env["MAX_BROWSER_SESSIONS"] == "${MAX_BROWSER_SESSIONS:-2}"


@pytest.mark.parametrize("custom", [False, True])
def test_ansible_renders_profile_limits_into_consumed_env_file(custom):
    values = yaml.safe_load((ANSIBLE / "defaults/main.yml").read_text())
    assert values["istota_browser_max_sessions"] == 3
    for variable, default in SETTINGS.values():
        assert values[variable] == default
    if custom:
        values.update({variable: default + 1 for variable, default in SETTINGS.values()})
        values["istota_browser_max_sessions"] = 5
    values.update(istota_home="/srv/app/istota", istota_repo_dir="/srv/app/istota/repo",
                  istota_namespace="istota", istota_browser_cpu_limit="2")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    tasks = yaml.safe_load((ANSIBLE / "tasks/main.yml").read_text())
    task = next(task for task in tasks if task.get("name") == "Deploy browser environment file")
    rendered = env.from_string(task["copy"]["content"]).render(values)
    entries = dict(line.split("=", 1) for line in rendered.splitlines())
    for name, (variable, _) in SETTINGS.items():
        assert entries[name] == str(values[variable])
    assert entries["MAX_BROWSER_SESSIONS"] == str(values["istota_browser_max_sessions"])
    compose = yaml.safe_load(env.from_string(
        (ANSIBLE / "templates/docker-compose.browser.yml.j2").read_text()
    ).render(values))
    destination = env.from_string(task["copy"]["dest"]).render(values)
    assert destination in compose["services"]["browser"]["env_file"]
