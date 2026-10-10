"""`istota devbox compose-file`: the stack's devbox services, from the config.

The render replaces the Ansible role's Jinja template for the one deployment
shape. What matters most is the mount table, because it is the boundary the
credential proxy now rests on: user U's credential socket volume is mounted
into U's devbox and the istota container, and into nothing else.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from istota.config import Config, ContainerConfig, DeveloperConfig, DevboxConfig
from istota.devbox import compose_file
from istota.executor import SANDBOX_CACHE_NPM, SANDBOX_CACHE_UV

REPO = Path(__file__).resolve().parent.parent
SHIPPED_COMPOSE = REPO / "docker" / "docker-compose.yml"


def _config(users=("alice", "bob"), **devbox) -> Config:
    config = Config()
    config.developer = DeveloperConfig(
        enabled=True,
        repos_dir="/data/repos",
        devbox_proxy_socket_dir="/data/devbox/cred",
        container=ContainerConfig(exec_socket_dir="/data/devbox/exec"),
    )
    config.devbox = DevboxConfig(enabled=True, users=list(users), **devbox)
    return config


def _doc(config: Config) -> dict:
    text = compose_file.render_devbox_compose(config)
    assert text.startswith("#"), "the generated file says what wrote it"
    return yaml.safe_load(text)


def _targets(service: dict) -> dict[str, dict]:
    return {m["target"]: m for m in service["volumes"]}


class TestOneServicePerUser:
    def test_each_user_gets_a_service_named_for_them(self):
        services = _doc(_config())["services"]
        assert set(services) == {"devbox-alice", "devbox-bob", "istota"}

    def test_the_build_is_the_devbox_image_at_the_daemons_uid(self):
        service = _doc(_config())["services"]["devbox-alice"]
        assert service["build"]["context"] == "devbox"
        assert (SHIPPED_COMPOSE.parent / service["build"]["context"] / "Dockerfile").is_file()
        assert service["build"]["args"] == {"DEV_UID": "10001", "DEV_GID": "10001"}

    def test_the_label_names_the_owner(self):
        service = _doc(_config())["services"]["devbox-bob"]
        assert service["labels"] == {"com.istota.user_id": "bob"}

    def test_the_service_keeps_its_containment(self):
        service = _doc(_config())["services"]["devbox-alice"]
        assert service["restart"] == "unless-stopped"
        assert service["init"] is True
        assert service["command"] == ["/usr/local/bin/istota-exec-run"]
        assert service["networks"] == ["devbox"]
        assert service["mem_limit"] == "4g"
        assert service["cpus"] == 2.0
        assert service["pids_limit"] == 512
        assert service["logging"]["driver"] == "json-file"
        assert service["depends_on"] == {"istota": {"condition": "service_healthy"}}
        for key in ("cap_add", "privileged", "pid", "network_mode", "security_opt"):
            assert key not in service, key

    def test_the_limits_come_from_the_config(self):
        service = _doc(_config(mem_limit="8g", cpus=4.0, pids_limit=1024))["services"]["devbox-alice"]
        assert (service["mem_limit"], service["cpus"], service["pids_limit"]) == ("8g", 4.0, 1024)

    def test_the_network_has_the_configured_subnet(self):
        doc = _doc(_config(network_subnet="172.31.0.0/24"))
        assert doc["networks"]["devbox"]["ipam"]["config"] == [{"subnet": "172.31.0.0/24"}]


class TestTheMountsAreTheBoundary:
    def test_each_devbox_mounts_only_its_own_volumes(self):
        services = _doc(_config())["services"]
        for user, other in (("alice", "bob"), ("bob", "alice")):
            sources = {m["source"] for m in services[f"devbox-{user}"]["volumes"]}
            assert sources == {
                f"devbox-home-{user}", f"devbox-exec-{user}", f"devbox-cred-{user}",
                "istota_data",
            }
            assert not [s for s in sources if s.endswith(f"-{other}")], (
                f"devbox-{user} mounts a volume of {other}'s"
            )

    def test_the_devbox_sees_its_sockets_where_the_image_expects_them(self):
        mounts = _targets(_doc(_config())["services"]["devbox-alice"])
        assert mounts["/run/istota-cred"]["source"] == "devbox-cred-alice"
        assert mounts["/run/istota-exec"]["source"] == "devbox-exec-alice"
        assert mounts["/home/dev"]["source"] == "devbox-home-alice"
        dockerfile = (REPO / "docker" / "devbox" / "Dockerfile").read_text()
        assert "ISTOTA_CRED_SOCK=/run/istota-cred/sock" in dockerfile

    def test_the_repos_mount_is_the_users_own_subtree_at_the_same_path(self):
        repos = _targets(_doc(_config())["services"]["devbox-alice"])["/data/repos/alice"]
        assert repos["source"] == "istota_data"
        assert repos["volume"] == {"subpath": "repos/alice"}

    def test_the_istota_service_mounts_every_users_sockets_under_the_config_paths(self):
        mounts = _targets(_doc(_config())["services"]["istota"])
        assert {t: m["source"] for t, m in mounts.items()} == {
            "/data/devbox/exec/alice": "devbox-exec-alice",
            "/data/devbox/cred/alice": "devbox-cred-alice",
            "/data/devbox/exec/bob": "devbox-exec-bob",
            "/data/devbox/cred/bob": "devbox-cred-bob",
        }

    def test_the_daemon_and_the_devbox_resolve_the_same_socket(self):
        """The istota side reaches `{exec_socket_dir}/{user}/exec.sock`, the
        devbox server listens on ISTOTA_EXEC_SOCKET; one volume joins them."""
        from istota.config import exec_socket_path

        config = _config()
        services = _doc(config)["services"]
        daemon = Path(exec_socket_path(config, "alice"))
        istota = _targets(services["istota"])[str(daemon.parent)]
        devbox = services["devbox-alice"]
        listen = Path(devbox["environment"]["ISTOTA_EXEC_SOCKET"])
        assert _targets(devbox)[str(listen.parent)]["source"] == istota["source"]
        assert listen.name == daemon.name

    def test_nothing_mounts_the_docker_socket_or_the_host(self):
        for service in _doc(_config())["services"].values():
            for mount in service["volumes"]:
                assert mount["type"] == "volume", mount
                assert "docker.sock" not in str(mount)

    def test_without_the_proxy_there_is_no_credential_volume(self):
        config = _config()
        config.developer.devbox_proxy_enabled = False
        doc = _doc(config)
        assert not [v for v in doc["volumes"] if v.startswith("devbox-cred-")]
        assert "/run/istota-cred" not in _targets(doc["services"]["devbox-alice"])


class TestTheTransportGate:
    def test_without_the_developer_skill_there_is_no_repos_mount_or_supervisor_env(self):
        config = _config()
        config.developer.enabled = False
        service = _doc(config)["services"]["devbox-alice"]
        assert "/data/repos/alice" not in _targets(service)
        assert "environment" not in service

    def test_with_it_the_supervisor_is_told_where_socket_and_repos_are(self):
        env = _doc(_config())["services"]["devbox-alice"]["environment"]
        assert env["ISTOTA_EXEC_SOCKET"] == "/run/istota-exec/exec.sock"
        assert env["ISTOTA_EXEC_REPOS_ROOT"] == "/data/repos/alice"
        assert env["ISTOTA_EXEC_IDLE_TIMEOUT_SECONDS"] == "3600"

    def test_the_caches_are_the_hosts_own_subdirectories(self):
        env = _doc(_config())["services"]["devbox-alice"]["environment"]
        assert env["UV_CACHE_DIR"] == f"/data/repos/alice/.package-caches/{SANDBOX_CACHE_UV}"
        assert env["npm_config_cache"] == f"/data/repos/alice/.package-caches/{SANDBOX_CACHE_NPM}"


class TestNothingToRender:
    def test_no_users_renders_no_services(self):
        assert _doc(_config(users=()))["services"] == {}

    def test_the_devbox_off_renders_no_services(self):
        config = _config()
        config.devbox.enabled = False
        assert _doc(config)["services"] == {}


class TestRefusals:
    @pytest.mark.parametrize("user", ["", "../x", "a/b", "-x", "al ice", 7])
    def test_an_unusable_user_id(self, user):
        with pytest.raises(compose_file.ComposeFileError):
            compose_file.render_devbox_compose(_config(users=("alice", user)))

    def test_a_user_named_twice(self):
        with pytest.raises(compose_file.ComposeFileError, match="twice"):
            compose_file.render_devbox_compose(_config(users=("alice", "alice")))

    @pytest.mark.parametrize("field", ["exec", "cred", "repos"])
    def test_a_directory_outside_the_state_volume(self, field):
        config = _config()
        if field == "exec":
            config.developer.container.exec_socket_dir = "/run/istota-exec"
        elif field == "cred":
            config.developer.devbox_proxy_socket_dir = "/var/run/istota"
        else:
            config.developer.repos_dir = "/srv/repos"
        with pytest.raises(compose_file.ComposeFileError, match="not under /data"):
            compose_file.render_devbox_compose(config)


class TestTheCliVerb:
    def _run(self, tmp_path: Path, body: str) -> subprocess.CompletedProcess:
        config = tmp_path / "config.toml"
        config.write_text(body)
        env = dict(os.environ, ISTOTA_CONFIG_PATH=str(config))
        return subprocess.run(
            [sys.executable, "-m", "istota.cli", "-c", str(config), "devbox", "compose-file"],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_it_prints_the_rendered_file(self, tmp_path):
        result = self._run(tmp_path, (
            '[developer]\ndevbox_proxy_socket_dir = "/data/devbox/cred"\n'
            '[developer.container]\nexec_socket_dir = "/data/devbox/exec"\n'
            '[devbox]\nenabled = true\nusers = ["alice"]\n'
        ))
        assert result.returncode == 0, result.stderr
        doc = yaml.safe_load(result.stdout)
        assert "devbox-alice" in doc["services"]

    def test_a_refusal_exits_one_and_names_the_key(self, tmp_path):
        result = self._run(tmp_path, (
            '[developer]\ndevbox_proxy_socket_dir = "/var/run/istota"\n'
            '[developer.container]\nexec_socket_dir = "/data/devbox/exec"\n'
            '[devbox]\nenabled = true\nusers = ["alice"]\n'
        ))
        assert result.returncode == 1
        assert "devbox_proxy_socket_dir" in result.stderr
        assert result.stdout == ""


class TestTheWizardPlacesTheSocketsWhereTheRenderAcceptsThem:
    def test_a_developer_setup_renders(self):
        from istota.setup_wizard import ContainerAnswers, render_container_config

        written = tomllib.loads(render_container_config(
            ContainerAnswers(user_id="alice", developer_enabled=True, session_secret="s" * 64),
            inline_credentials=False,
        ))
        dev = written["developer"]
        config = _config()
        config.developer.devbox_proxy_socket_dir = dev["devbox_proxy_socket_dir"]
        config.developer.container.exec_socket_dir = dev["container"]["exec_socket_dir"]
        config.developer.repos_dir = dev["repos_dir"]
        assert compose_file.render_devbox_compose(config)
