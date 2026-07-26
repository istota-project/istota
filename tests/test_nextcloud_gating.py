"""The nextcloud skill's capability gate and network-allowlist entry."""

from istota.config import Config, NextcloudConfig
from istota.executor import _build_network_allowlist
from istota.skills._loader import effective_disabled_skills, load_skill_index


def _nc_config(url="https://cloud.example.com"):
    return Config(nextcloud=NextcloudConfig(url=url, username="istota", app_password="pw"))


class TestCapabilityGate:
    def test_capability_present_when_url_set(self):
        assert "nextcloud" in _nc_config().available_capabilities()

    def test_capability_absent_when_no_url(self):
        assert "nextcloud" not in _nc_config(url="").available_capabilities()

    def test_skill_declares_the_capability(self):
        index = load_skill_index(Config().skills_dir)
        assert index["nextcloud"].requires_capability == ["nextcloud"]

    def test_skill_disabled_on_a_deployment_without_nextcloud(self):
        config = _nc_config(url="")
        index = load_skill_index(config.skills_dir)
        assert "nextcloud" in effective_disabled_skills(config, "alice", index)

    def test_skill_available_when_nextcloud_configured(self):
        config = _nc_config()
        index = load_skill_index(config.skills_dir)
        assert "nextcloud" not in effective_disabled_skills(config, "alice", index)

    def test_files_skill_keeps_no_capability_gate(self):
        """files is storage-neutral prose over the mount — available everywhere."""
        index = load_skill_index(Config().skills_dir)
        assert index["files"].requires_capability == []


class TestNetworkAllowlist:
    def test_host_added_when_skill_authorized(self):
        hosts = _build_network_allowlist(_nc_config(), ["nextcloud"])
        assert "cloud.example.com:443" in hosts

    def test_host_absent_when_skill_not_authorized(self):
        hosts = _build_network_allowlist(_nc_config(), ["files"])
        assert "cloud.example.com:443" not in hosts

    def test_explicit_port_preserved(self):
        config = _nc_config(url="https://cloud.example.com:8443")
        hosts = _build_network_allowlist(config, ["nextcloud"])
        assert "cloud.example.com:8443" in hosts

    def test_no_url_adds_nothing(self):
        hosts = _build_network_allowlist(_nc_config(url=""), ["nextcloud"])
        assert not any(h.startswith("cloud.example.com") for h in hosts)


class TestCompanionSkills:
    def test_untrusted_input_and_sensitive_actions_ride_along(self):
        index = load_skill_index(Config().skills_dir)
        companions = index["nextcloud"].companion_skills
        assert "untrusted_input" in companions
        assert "sensitive_actions" in companions
