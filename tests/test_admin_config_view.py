"""Tests for the read-only admin config view (`istota.admin_config_view`)."""

from pathlib import Path

import pytest

from istota import admin_config_view as view
from istota.config import Config


def _fields(payload: dict, section_key: str) -> dict[str, dict]:
    for section in payload["sections"]:
        if section["key"] == section_key:
            return {f["name"]: f for f in section["fields"]}
    raise AssertionError(f"section {section_key!r} not in {[s['key'] for s in payload['sections']]}")


def _all_fields(payload: dict) -> dict[str, dict]:
    return {f["key"]: f for s in payload["sections"] for f in s["fields"]}


class TestSections:
    def test_general_section_carries_top_level_scalars(self):
        cfg = Config()
        cfg.bot_name = "Testbot"
        payload = view.build_config_view(cfg)
        general = _fields(payload, "general")
        assert general["bot_name"]["value"] == "Testbot"
        assert general["bot_name"]["type"] == "str"

    def test_each_nested_dataclass_becomes_a_section(self):
        payload = view.build_config_view(Config())
        keys = {s["key"] for s in payload["sections"]}
        assert {"general", "nextcloud", "scheduler", "logging", "web", "security"} <= keys

    def test_nested_dataclasses_get_their_own_dotted_section(self):
        payload = view.build_config_view(Config())
        keys = {s["key"] for s in payload["sections"]}
        assert "brain.native" in keys
        assert "security.network" in keys

    def test_sections_are_ordered_general_first(self):
        payload = view.build_config_view(Config())
        assert payload["sections"][0]["key"] == "general"

    def test_config_path_is_reported(self, tmp_path):
        cfg = Config()
        cfg.config_path = tmp_path / "config.toml"
        payload = view.build_config_view(cfg)
        assert payload["config_path"] == str(tmp_path / "config.toml")

    def test_config_path_is_null_when_unset(self):
        assert view.build_config_view(Config())["config_path"] is None

    def test_view_declares_itself_read_only(self):
        assert view.build_config_view(Config())["editable"] is False


class TestValueRendering:
    def test_paths_render_as_strings(self):
        cfg = Config()
        cfg.db_path = Path("/srv/data/istota.db")
        assert _fields(view.build_config_view(cfg), "general")["db_path"]["value"] == (
            "/srv/data/istota.db"
        )

    def test_none_path_renders_as_null(self):
        cfg = Config()
        cfg.nextcloud_mount_path = None
        field = _fields(view.build_config_view(cfg), "general")["nextcloud_mount_path"]
        assert field["value"] is None

    def test_bools_and_ints_keep_their_type(self):
        payload = view.build_config_view(Config())
        scheduler = _fields(payload, "scheduler")
        assert scheduler["poll_interval"]["type"] == "int"
        assert isinstance(scheduler["poll_interval"]["value"], int)
        security = _fields(payload, "security")
        assert security["sandbox_enabled"]["type"] == "bool"
        assert security["sandbox_enabled"]["value"] is True

    def test_lists_render_as_lists(self):
        cfg = Config()
        cfg.disabled_skills = ["browse", "devbox"]
        field = _fields(view.build_config_view(cfg), "general")["disabled_skills"]
        assert field["value"] == ["browse", "devbox"]
        assert field["type"] == "list"

    def test_admin_users_set_renders_sorted(self):
        cfg = Config()
        cfg.admin_users = {"zoe", "alice"}
        field = _fields(view.build_config_view(cfg), "general")["admin_users"]
        assert field["value"] == ["alice", "zoe"]

    def test_every_value_is_json_serializable(self):
        import json

        cfg = Config()
        cfg.admin_users = {"a"}
        cfg.models.aliases = {"smart": {"anthropic": "opus:high"}}
        json.dumps(view.build_config_view(cfg))  # must not raise


class TestRedaction:
    def test_a_set_credential_is_redacted_but_reported_as_set(self):
        cfg = Config()
        cfg.nextcloud.app_password = "hunter2"
        field = _fields(view.build_config_view(cfg), "nextcloud")["app_password"]
        assert field["secret"] is True
        assert field["set"] is True
        assert field["value"] is None
        assert "hunter2" not in str(field)

    def test_an_unset_credential_reports_not_set(self):
        cfg = Config()
        cfg.nextcloud.app_password = ""
        field = _fields(view.build_config_view(cfg), "nextcloud")["app_password"]
        assert field["secret"] is True
        assert field["set"] is False
        assert field["value"] is None

    @pytest.mark.parametrize(
        "dotted",
        [
            "nextcloud.app_password",
            "email.imap_password",
            "email.smtp_password",
            "developer.gitlab_token",
            "developer.github_token",
            "google_workspace.client_secret",
            "web.oauth2_client_secret",
            "web.session_secret_key",
            "caldav.password",
            "brain.native.api_key",
        ],
    )
    def test_every_known_credential_field_is_redacted(self, dotted):
        """A new credential field that does not match the patterns fails here.

        Name-pattern redaction is only as good as its coverage, so the coverage
        is asserted rather than assumed.
        """
        cfg = Config()
        target = cfg
        *path, leaf = dotted.split(".")
        for part in path:
            target = getattr(target, part)
        setattr(target, leaf, "SUPER-SECRET-VALUE")

        payload = view.build_config_view(cfg)
        assert "SUPER-SECRET-VALUE" not in str(payload)
        field = _all_fields(payload)[dotted]
        assert field["secret"] is True
        assert field["value"] is None

    @pytest.mark.parametrize(
        "header", ["authorization", "x-api-key", "api-key", "X-Auth-Token"]
    )
    def test_provider_auth_headers_never_reach_the_browser(self, header):
        """`extra_headers` is merged over the Authorization header by
        `llm/openai_compat.py`, so it is where a non-Anthropic deployment's key
        lives. Header names are hyphenated, which the field-name patterns miss.
        """
        cfg = Config()
        cfg.brain.native.extra_headers = {header: "Bearer SUPER-SECRET-VALUE"}
        payload = view.build_config_view(cfg)
        assert "SUPER-SECRET-VALUE" not in str(payload)
        assert _all_fields(payload)["brain.native.extra_headers"]["secret"] is True

    def test_no_secret_shaped_value_escapes_anywhere_in_the_tree(self):
        """Sweep by *value*, not by name.

        The name-shaped version of this test could not fail — it re-applied the
        implementation's own predicate to the implementation's own output, so a
        credential whose name the patterns missed was invisible to it. This
        stuffs a sentinel into every string and dict leaf of a real Config and
        asserts none of them surface, which is the property that matters.
        """
        import dataclasses

        SENTINEL = "SENTINEL-SECRET-a1b2c3"
        cfg = Config()

        def stuff(obj, prefix=""):
            for f in dataclasses.fields(obj):
                value = getattr(obj, f.name, None)
                key = f"{prefix}.{f.name}" if prefix else f.name
                if dataclasses.is_dataclass(value) and not isinstance(value, type):
                    stuff(value, key)
                    continue
                # An allowlisted key is a declared non-credential, so planting a
                # secret in one and demanding it stay hidden would contradict
                # the allowlist rather than test it.
                if key in view.NON_SECRET_KEYS:
                    continue
                if isinstance(value, str) and view.is_secret_name(f.name):
                    setattr(obj, f.name, SENTINEL)
                elif isinstance(value, dict):
                    setattr(obj, f.name, {"authorization": SENTINEL})

        stuff(cfg)
        assert SENTINEL not in str(view.build_config_view(cfg))

    def test_operational_settings_are_not_over_redacted(self):
        """The allowlist keeps the viewer useful: these match the credential
        patterns on a substring but carry no secret, and hiding them defeats the
        point of a configuration page."""
        payload = _all_fields(view.build_config_view(Config()))
        for key in view.NON_SECRET_KEYS:
            assert key in payload, f"{key} is allowlisted but no longer exists"
            assert payload[key]["secret"] is False, f"{key} should not be redacted"

    def test_a_non_string_secret_still_redacts(self):
        """`set` must not assume the value is a string."""
        cfg = Config()
        cfg.models.aliases = {}
        cfg.developer.gitlab_token = ""
        field = _all_fields(view.build_config_view(cfg))["developer.gitlab_token"]
        assert field["set"] is False


class TestExclusions:
    def test_per_user_config_is_not_dumped(self):
        from istota.config import UserConfig

        cfg = Config()
        cfg.users = {"alice": UserConfig(display_name="Alice")}
        payload = view.build_config_view(cfg)
        assert "alice" not in str(payload["sections"])

    def test_user_count_is_summarized_instead(self):
        from istota.config import UserConfig

        cfg = Config()
        cfg.users = {"alice": UserConfig(), "bob": UserConfig()}
        field = _fields(view.build_config_view(cfg), "general")["users"]
        assert field["value"] == 2
        assert field["type"] == "count"

    def test_internal_test_only_fields_are_hidden(self):
        payload = view.build_config_view(Config())
        assert "general.bundled_skills_dir" not in _all_fields(payload)

    def test_dataclass_lists_are_summarized_as_counts(self):
        from istota.config import BriefingConfig

        cfg = Config()
        cfg.default_briefings = [BriefingConfig(name="morning", cron="0 7 * * *")]
        field = _fields(view.build_config_view(cfg), "general")["default_briefings"]
        assert field["value"] == 1
        assert field["type"] == "count"


class TestLabels:
    def test_section_labels_are_the_toml_header(self):
        payload = view.build_config_view(Config())
        section = next(s for s in payload["sections"] if s["key"] == "brain.native")
        assert section["label"] == "[brain.native]"

    def test_general_section_is_labelled_as_top_level(self):
        payload = view.build_config_view(Config())
        assert payload["sections"][0]["label"] == "General"
