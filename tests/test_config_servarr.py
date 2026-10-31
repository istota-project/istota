"""``[servarr]``: the install-wide arr connection, as the loader reads it.

The keys are full admin on each app, so two properties matter beyond parsing:
``servarr.apps`` reaches the dataclass tree as ``ServarrApp`` instances (whose
``repr`` hides the key), not as the raw dicts the generic walk would pass
through, and every redaction path recognises ``api_key`` as a credential.
Everything else here is a warning, never a refusal to start.
"""

from __future__ import annotations

import logging
import textwrap

import pytest

from istota.config import (
    PRIVATE_HOST_PATTERN,
    Config,
    ServarrApp,
    ServarrConfig,
    load_config,
)
from istota.config_mapper import collect_problems
from istota.webui.admin_config_view import is_secret_path

KEY = "never-shown-arr-sentinel"


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    with collect_problems() as problems:
        config = load_config(path)
    return config, problems


def _messages(problems, *path):
    return [p["message"] for p in problems if p["path"][: len(path)] == path]


class TestParsing:
    def test_apps_become_servarr_app_instances(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            [private_hosts]
            "10.0.0.5" = ["servarr"]

            [servarr]
            requesters = ["bob"]

            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"

            [servarr.apps.sonarr]
            url = "http://10.0.0.5:8989"
            api_key = "{KEY}"

            [servarr.defaults.radarr]
            quality_profile = "HD - 720p/1080p"
            root_folder = "/data/library/content/Films"
            minimum_availability = "released"
        """)
        apps = config.servarr.apps
        assert set(apps) == {"radarr", "sonarr"}
        assert all(isinstance(app, ServarrApp) for app in apps.values())
        assert apps["radarr"].url == "http://10.0.0.5:7878"
        assert apps["radarr"].api_key == KEY
        assert config.servarr.requesters == ["bob"]
        assert config.servarr.private_hosts == ["10.0.0.5"]
        assert config.servarr.defaults["radarr"]["quality_profile"] == "HD - 720p/1080p"
        assert problems == []

    def test_the_defaults_are_empty(self):
        servarr = Config().servarr
        assert servarr.apps == {} and servarr.requesters == [] and servarr.defaults == {}
        assert servarr.lookup_timeout_seconds == 30
        assert servarr.release_search_timeout_seconds == 120

    def test_an_empty_defaults_table_loads_without_a_warning(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
            [servarr.defaults]
        """)
        assert config.servarr.defaults == {}
        assert _messages(problems, "servarr") == []

    def test_the_key_is_not_in_the_repr(self, tmp_path):
        config, _ = _load(tmp_path, f"""
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
        """)
        assert KEY not in repr(config.servarr)
        assert KEY not in repr(config.servarr.apps["radarr"])

    def test_an_app_without_a_url_is_discarded(self, tmp_path):
        config, problems = _load(tmp_path, f'[servarr.apps.radarr]\napi_key = "{KEY}"\n')
        assert "radarr" not in config.servarr.apps
        assert config.servarr.incomplete_apps == ["radarr"]
        discarded = [p for p in problems if p["kind"] == "discarded"
                     and p["path"][:3] == ("servarr", "apps", "radarr")]
        assert len(discarded) == 1
        assert KEY not in discarded[0]["message"]

    @pytest.mark.parametrize("table", [
        'url = "http://10.0.0.5:7878"',
        'url = "http://10.0.0.5:7878"\napi_key = ""',
    ])
    def test_an_app_without_a_key_is_left_out_with_a_warning(self, tmp_path, table):
        """Not discarded: the key belongs in a stack secret file, which the
        process checking a plan may not have yet, so a missing one is reported
        and the app waits for it, as every other file credential does."""
        config, problems = _load(tmp_path, f"[servarr.apps.radarr]\n{table}\n")
        assert "radarr" not in config.servarr.apps
        assert config.servarr.incomplete_apps == ["radarr"]
        found = [p for p in problems if p["path"][:3] == ("servarr", "apps", "radarr")]
        assert [p["kind"] for p in found] == ["warning"]
        assert "istota_servarr_radarr_api_key" in found[0]["message"]
        assert config.servarr.keyless_apps == ["radarr"]

    SONARR_URL_ONLY = '[servarr.apps.sonarr]\nurl = "http://10.0.0.5:8989"\n'

    def test_a_key_the_daemon_holds_is_not_reported_missing(self, tmp_path, monkeypatch):
        """#771: the keys reach the servarr CLI alone, so every other skill CLI
        loads the config without them; the daemon names the apps it holds keys
        for, and the warning would be true of the process and false of the
        deployment."""
        monkeypatch.setenv("ISTOTA_SERVARR_DAEMON_KEYED", "sonarr")
        config, problems = _load(tmp_path, self.SONARR_URL_ONLY)
        assert _messages(problems, "servarr") == []
        assert "sonarr" not in config.servarr.apps
        assert config.servarr.keyless_apps == ["sonarr"]
        assert config.servarr.daemon_keyed_apps == ["sonarr"]

    def test_a_key_the_daemon_lacks_is_still_reported(self, tmp_path, monkeypatch):
        """A task on a deployment that really has no key: the daemon names no
        app, and the warning is true everywhere."""
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")
        monkeypatch.setenv("ISTOTA_SERVARR_DAEMON_KEYED", "radarr")
        config, problems = _load(tmp_path, self.SONARR_URL_ONLY)
        assert len(_messages(problems, "servarr", "apps", "sonarr")) == 1
        assert config.servarr.daemon_keyed_apps == []
        assert "servarr" not in config.available_capabilities()

    def test_the_capability_holds_when_the_daemon_has_the_key(self, tmp_path, monkeypatch):
        """#772: the daemon offered the skill, so a skill CLI that cannot see
        the key must not call it disabled."""
        monkeypatch.setenv("ISTOTA_SERVARR_DAEMON_KEYED", "sonarr")
        config, _ = _load(tmp_path, self.SONARR_URL_ONLY)
        assert "servarr" in config.available_capabilities()

    def test_the_daemon_name_does_not_rescue_a_missing_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_DAEMON_KEYED", "sonarr")
        config, problems = _load(tmp_path, f'[servarr.apps.sonarr]\napi_key = "{KEY}"\n')
        assert len(_messages(problems, "servarr", "apps", "sonarr")) == 1
        assert "servarr" not in config.available_capabilities()

    def test_the_daemon_keyed_list_cannot_be_set_from_the_file(self, tmp_path):
        config, problems = _load(tmp_path, '[servarr]\ndaemon_keyed_apps = ["sonarr"]\n')
        assert config.servarr.daemon_keyed_apps == []
        assert "servarr" not in config.available_capabilities()

    def test_an_app_without_a_url_is_not_keyless(self, tmp_path):
        config, _ = _load(tmp_path, f'[servarr.apps.radarr]\napi_key = "{KEY}"\n')
        assert config.servarr.keyless_apps == []

    @pytest.mark.parametrize("name", ["bazarr", "readarr"])
    def test_a_name_the_skill_does_not_know_is_left_to_validation(self, tmp_path, name):
        """No secret file exists for it, so no warning names one; the
        reserved or unknown warning is the one it gets."""
        _, problems = _load(tmp_path, f'[servarr.apps.{name}]\nurl = "http://10.0.0.5:6767"\n')
        [message] = _messages(problems, "servarr", "apps", name)
        assert "api_key" not in message
        assert ("reserved" if name == "bazarr" else "not an app the skill knows") in message

    def test_a_complete_app_is_not_recorded_as_incomplete(self, tmp_path):
        config, _ = _load(tmp_path, f"""
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"

            [servarr.apps.sonarr]
            url = "http://10.0.0.5:8989"
        """)
        assert set(config.servarr.apps) == {"radarr"}
        assert config.servarr.incomplete_apps == ["sonarr"]

    def test_an_app_that_is_not_a_table_is_recorded_as_incomplete(self, tmp_path):
        config, _ = _load(tmp_path, '[servarr.apps]\nradarr = "http://10.0.0.5:7878"\n')
        assert config.servarr.apps == {}
        assert config.servarr.incomplete_apps == ["radarr"]

    def test_incomplete_apps_cannot_be_set_from_the_file(self, tmp_path):
        config, problems = _load(tmp_path, '[servarr]\nincomplete_apps = ["radarr"]\n')
        assert config.servarr.incomplete_apps == []
        assert any(p["kind"] == "unknown" and p["path"] == ("servarr", "incomplete_apps")
                   for p in problems)

    def test_apps_that_is_not_a_table_is_discarded(self, tmp_path):
        config, problems = _load(tmp_path, '[servarr]\napps = "radarr"\n')
        assert config.servarr.apps == {}
        assert any(p["kind"] == "discarded" and p["path"][:2] == ("servarr", "apps")
                   for p in problems)

    def test_an_array_of_apps_is_discarded_without_logging_its_keys(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        config, problems = _load(tmp_path, f"""
            [[servarr.apps]]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
        """)
        assert config.servarr.apps == {}
        assert any(p["kind"] == "discarded" and p["path"] == ("servarr", "apps")
                   for p in problems)
        assert KEY not in caplog.text
        assert KEY not in repr(problems)

    def test_an_unknown_key_inside_an_app_is_reported(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
            apikey = "typo"
        """)
        assert "radarr" in config.servarr.apps
        assert any(p["path"] == ("servarr", "apps", "radarr", "apikey") for p in problems)



class TestTheKeyFromItsSecretFile:
    """The keys are deployment credentials: a stack secret file each, exported
    by `istota-secrets` as `ISTOTA_SERVARR_<APP>_API_KEY`, which wins over an
    inline `api_key` the way every other file credential does."""

    URL_ONLY = """
        [servarr.apps.radarr]
        url = "http://10.0.0.5:7878"
    """

    def test_a_url_and_the_variable_make_a_complete_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", KEY)
        config, problems = _load(tmp_path, self.URL_ONLY)
        assert config.servarr.apps["radarr"] == ServarrApp(url="http://10.0.0.5:7878", api_key=KEY)
        assert config.servarr.incomplete_apps == []
        assert _messages(problems, "servarr") == []
        assert "servarr" in config.available_capabilities()

    def test_the_variable_wins_over_an_inline_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", "from-the-secret-file")
        config, _ = _load(tmp_path, f"""
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
        """)
        assert config.servarr.apps["radarr"].api_key == "from-the-secret-file"

    def test_an_empty_variable_leaves_the_inline_key(self, tmp_path, monkeypatch):
        """The wizard writes every secret file, empty when unused."""
        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", "")
        config, _ = _load(tmp_path, f"""
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
        """)
        assert config.servarr.apps["radarr"].api_key == KEY

    @pytest.mark.parametrize("app", ["sonarr", "radarr", "lidarr", "prowlarr"])
    def test_every_app_has_a_secret_file(self, tmp_path, monkeypatch, app):
        from istota.setup_wizard import SECRET_NAMES

        variable = f"ISTOTA_SERVARR_{app.upper()}_API_KEY"
        assert variable.lower() in SECRET_NAMES
        monkeypatch.setenv(variable, KEY)
        config, _ = _load(tmp_path, f'[servarr.apps.{app}]\nurl = "https://{app}.example.org"\n')
        assert config.servarr.apps[app].api_key == KEY

    def test_each_app_reads_its_own_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_SONARR_API_KEY", "sonarr-key")
        config, _ = _load(tmp_path, """
            [servarr.apps.sonarr]
            url = "http://10.0.0.5:8989"
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
        """)
        assert config.servarr.apps["sonarr"].api_key == "sonarr-key"
        assert "radarr" not in config.servarr.apps
        assert config.servarr.incomplete_apps == ["radarr"]

    def test_a_variable_with_no_table_configures_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", KEY)
        config, problems = _load(tmp_path, "")
        assert config.servarr.apps == {} and config.servarr.incomplete_apps == []
        assert "servarr" not in config.available_capabilities()

    def test_a_key_without_a_url_is_still_incomplete(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", KEY)
        config, problems = _load(tmp_path, "[servarr.apps.radarr]\n")
        assert config.servarr.apps == {}
        assert config.servarr.incomplete_apps == ["radarr"]
        assert KEY not in repr(problems)

    def test_the_key_from_the_variable_is_not_in_the_repr(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", KEY)
        config, _ = _load(tmp_path, self.URL_ONLY)
        assert KEY not in repr(config.servarr)

    def test_a_document_alone_does_not_read_the_environment(self, monkeypatch):
        """`config_from_document` maps what a document says, no more."""
        from istota.config import config_from_document

        monkeypatch.setenv("ISTOTA_SERVARR_RADARR_API_KEY", KEY)
        config = config_from_document({"servarr": {"apps": {"radarr": {"url": "http://10.0.0.5:7878"}}}})
        assert config.servarr.apps == {}
        assert config.servarr.incomplete_apps == ["radarr"]


class TestValidation:
    def test_an_unknown_app_is_a_warning_and_is_kept_out_of_the_capability(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        config, problems = _load(tmp_path, f"""
            [servarr.apps.readarr]
            url = "http://10.0.0.5:8787"
            api_key = "{KEY}"
        """)
        assert _messages(problems, "servarr", "apps", "readarr")
        assert "servarr" not in config.available_capabilities()

    def test_bazarr_is_reserved(self, tmp_path):
        _, problems = _load(tmp_path, f"""
            [servarr.apps.bazarr]
            url = "http://10.0.0.5:6767"
            api_key = "{KEY}"
        """)
        [message] = _messages(problems, "servarr", "apps", "bazarr")
        assert "reserved" in message

    def test_an_unknown_defaults_app_and_a_wrong_field_warn(self, tmp_path):
        _, problems = _load(tmp_path, """
            [servarr.defaults.readarr]
            quality_profile = "x"
            [servarr.defaults.sonarr]
            metadata_profile = "Standard"
            minimum_availability = "released"
            quality_profile = "HD"
        """)
        assert _messages(problems, "servarr", "defaults", "readarr")
        assert _messages(problems, "servarr", "defaults", "sonarr", "metadata_profile")
        assert _messages(problems, "servarr", "defaults", "sonarr", "minimum_availability")
        assert not _messages(problems, "servarr", "defaults", "sonarr", "quality_profile")

    def test_a_radarr_monitor_default_warns(self, tmp_path):
        _, problems = _load(tmp_path, """
            [servarr.defaults.radarr]
            monitor = "all"
        """)
        assert _messages(problems, "servarr", "defaults", "radarr", "monitor")

    def test_a_requester_who_is_not_a_configured_user_warns(self, tmp_path):
        _, problems = _load(tmp_path, """
            [servarr]
            requesters = ["Alice", "nobody"]
            [users.alice]
            display_name = "Alice"
            [users.bob]
            display_name = "Bob"
        """)
        [message] = _messages(problems, "servarr", "requesters")
        assert "nobody" in message
        assert "Alice" not in message

    @pytest.mark.parametrize("entry", ["http://10.0.0.5", "10.0.0.5:7878", "*.lan"])
    def test_a_malformed_private_host_warns(self, tmp_path, entry):
        config, problems = _load(
            tmp_path, f'[private_hosts]\n"10.0.0.5" = ["servarr"]\n"{entry}" = ["servarr"]\n',
        )
        [message] = _messages(problems, "private_hosts")
        assert repr(entry) in message
        assert entry in config.servarr.private_hosts

    def test_the_release_timeout_must_leave_a_minute_under_the_skill_timeout(self, tmp_path):
        _, problems = _load(tmp_path, f"""
            [security]
            skill_proxy_timeout = 150
            [servarr]
            release_search_timeout_seconds = 120
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
        """)
        [message] = _messages(problems, "servarr", "release_search_timeout_seconds")
        assert "150" in message

    def test_a_release_timeout_with_room_is_quiet(self, tmp_path):
        _, problems = _load(tmp_path, f"""
            [security]
            skill_proxy_timeout = 300
            [servarr.apps.radarr]
            url = "http://10.0.0.5:7878"
            api_key = "{KEY}"
        """)
        assert not _messages(problems, "servarr")

    @pytest.mark.parametrize("name", ["lookup_timeout_seconds", "release_search_timeout_seconds"])
    def test_a_non_positive_timeout_is_discarded(self, tmp_path, name):
        config, problems = _load(tmp_path, f"[servarr]\n{name} = 0\n")
        assert getattr(config.servarr, name) == getattr(ServarrConfig(), name)
        assert any(p["kind"] == "discarded" and p["path"] == ("servarr", name) for p in problems)


class TestSecretsAndCapability:
    def test_the_key_path_is_a_secret_path(self):
        assert is_secret_path(("servarr", "apps", "radarr", "api_key"))
        assert not is_secret_path(("servarr", "apps", "radarr", "url"))

    def test_the_capability_follows_the_apps(self):
        config = Config()
        assert "servarr" not in config.available_capabilities()
        config.servarr.apps = {"radarr": ServarrApp(url="http://10.0.0.5:7878", api_key=KEY)}
        assert "servarr" in config.available_capabilities()


class TestTheSharedPattern:
    @pytest.mark.parametrize("entry", ["10.0.0.5", "arr.lan", "fd00::5"])
    def test_the_pattern_accepts(self, entry):
        import re
        assert re.match(PRIVATE_HOST_PATTERN, entry)
