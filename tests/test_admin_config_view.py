"""Tests for the read-only admin config view (`istota.webui.admin_config_view`)."""

from pathlib import Path

import pytest

from istota.webui import admin_config_view as view
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

    def test_retired_moneyman_section_is_absent(self):
        payload = view.build_config_view(Config())
        keys = {s["key"] for s in payload["sections"]}
        assert "moneyman" not in keys

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
        cfg.workspace_path = None
        field = _fields(view.build_config_view(cfg), "general")["workspace_path"]
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


@pytest.mark.parametrize("name", [
    "auth", "auth_enrol_ttl_hours", "auth_reset_ttl_hours",
    "auth_sign_in_code_ttl_minutes", "auth_min_password_length",
    "auth_throttle_window_seconds", "auth_throttle_max_email",
    "auth_throttle_max_ip", "auth_mail_link_max_email",
])
def test_web_auth_settings_are_visible(name):
    cfg = Config()
    field = _fields(view.build_config_view(cfg), "web")[name]
    assert field["secret"] is False
    assert field["value"] == getattr(cfg.web, name)


def test_unlisted_auth_and_password_fields_stay_secret():
    from dataclasses import dataclass

    @dataclass
    class ExtraConfig(Config):
        auth_value: str = "private-auth-value"
        password_value: str = "private-password-value"

    payload = view.build_config_view(ExtraConfig())
    fields = _all_fields(payload)
    for name in ("auth_value", "password_value"):
        assert fields[name]["secret"] is True
        assert fields[name]["value"] is None


# The editor's keys (web-admin-config-editor Stage 4). Everything above is the
# read-only payload as it was, and holds unchanged.

_EDITOR_DOCUMENT = """\
# a hand-written file
bot_name = "Filebot"

[web]
auth = ["email"]

[brain.native.extra_headers]
x-tenant = "HEADER-VALUE-SENTINEL"

[models.aliases]
fast = "haiku"

[email]
imap_password = "INLINE-PASSWORD-SENTINEL"
"""


def _field(payload: dict, dotted: str) -> dict:
    return _all_fields(payload)[dotted]


@pytest.fixture
def no_overrides(monkeypatch):
    from istota import config as config_module

    for var, _path in config_module.ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    for var, _section, _key in config_module.SECRET_ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ISTOTA_BRAIN_NATIVE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestPerFieldEditorKeys:
    def test_every_field_carries_its_path(self):
        payload = view.build_config_view(Config())
        assert _field(payload, "brain.native.max_tokens")["path"] == ["brain", "native", "max_tokens"]
        assert _field(payload, "bot_name")["path"] == ["bot_name"]

    def test_choices(self):
        payload = view.build_config_view(Config())
        assert "encrypted" in _field(payload, "web.token_storage")["choices"]
        assert _field(payload, "web.token_storage")["editor"] == "choice"
        assert set(_field(payload, "email.outbound_approval_floor")["choices"]) == {"off", "untrusted", "all"}
        assert _field(payload, "bot_name")["choices"] is None

    def test_multi_choices(self, no_overrides):
        field = _field(view.build_config_view(Config()), "web.auth")
        assert "email" in field["multi_choices"]
        assert field["editor"] == "multi_choice"

    def test_default_and_in_file(self, no_overrides):
        cfg = Config()
        cfg.bot_name = "Filebot"
        payload = view.build_config_view(cfg, document=_EDITOR_DOCUMENT)
        bot = _field(payload, "bot_name")
        assert bot["in_file"] is True
        assert bot["default"] == Config().bot_name
        assert _field(payload, "scheduler.poll_interval")["in_file"] is False

    def test_nothing_is_in_file_without_a_document(self):
        assert _field(view.build_config_view(Config()), "bot_name")["in_file"] is False

    def test_a_secret_has_no_default(self):
        field = _field(view.build_config_view(Config()), "email.imap_password")
        assert "default" not in field

    def test_credential_kind_is_file_for_the_app_password_with_no_variable_set(self, no_overrides):
        """The M7 case: routing is by membership, not by whether a variable is set."""
        field = _field(view.build_config_view(Config()), "nextcloud.app_password")
        assert field["credential"] == {"kind": "file", "name": "istota_nextcloud_app_password"}

    def test_an_inline_credential(self):
        field = _field(view.build_config_view(Config()), "brain.native.extra_headers")
        assert field["credential"] == {"kind": "inline", "name": "brain.native.extra_headers"}

    def test_a_plain_field_has_no_credential(self):
        assert _field(view.build_config_view(Config()), "bot_name")["credential"] is None

    def test_env_override_makes_a_key_read_only(self, monkeypatch, no_overrides):
        monkeypatch.setenv("ISTOTA_WEB_AUTH", "email")
        field = _field(view.build_config_view(Config()), "web.auth")
        assert field["env_override"] == "ISTOTA_WEB_AUTH"
        assert "ISTOTA_WEB_AUTH" in field["read_only_reason"]
        assert field["editor"] == "readonly"

    def test_no_env_override_when_unset(self, no_overrides):
        field = _field(view.build_config_view(Config()), "web.auth")
        assert field["env_override"] is None
        assert field["read_only_reason"] is None

    @pytest.mark.parametrize("dotted", ["site.hostname", "db_path", "browser.enabled"])
    def test_host_settings_are_read_only_with_a_reason(self, dotted):
        field = _field(view.build_config_view(Config()), dotted)
        assert field["editor"] == "readonly"
        assert field["read_only_reason"]

    def test_admin_users_routes_to_the_admins_editor(self):
        field = _field(view.build_config_view(Config()), "admin_users")
        assert field["editor"] == "readonly"
        assert "admins" in field["read_only_reason"]

    def test_risky(self):
        payload = view.build_config_view(Config())
        assert _field(payload, "security.sandbox_enabled")["risky"] is True
        assert _field(payload, "web.oauth2_client_secret")["risky"] is True
        assert _field(payload, "bot_name")["risky"] is False

    def test_editor_kinds(self, no_overrides):
        payload = view.build_config_view(Config())
        assert _field(payload, "security.sandbox_enabled")["editor"] == "bool"
        assert _field(payload, "scheduler.poll_interval")["editor"] == "int"
        assert _field(payload, "bot_name")["editor"] == "str"
        assert _field(payload, "email.imap_password")["editor"] == "secret"
        assert _field(payload, "models.aliases")["editor"] == "table"

    def test_docs_come_from_the_example_in_the_config_dir(self, tmp_path):
        (tmp_path / "config.example.toml").write_text(
            "# The name the bot answers to.\nbot_name = \"Istota\"\n", encoding="utf-8",
        )
        cfg = Config()
        cfg.skills_dir = tmp_path / "skills"
        payload = view.build_config_view(cfg)
        assert _field(payload, "bot_name")["doc"] == "The name the bot answers to."
        assert _field(payload, "scheduler.poll_interval")["doc"] is None


class TestTables:
    def test_a_table_carries_its_file_value_as_a_snippet(self):
        field = _field(view.build_config_view(Config(), document=_EDITOR_DOCUMENT), "models.aliases")
        assert field["toml"] == '[aliases]\nfast = "haiku"\n'

    def test_secrets_in_a_table_are_sentinels(self):
        from istota.config_editor.secrets import SENTINEL

        payload = view.build_config_view(Config(), document=_EDITOR_DOCUMENT)
        field = _field(payload, "brain.native.extra_headers")
        assert field["editor"] == "table"
        assert SENTINEL in field["toml"]
        assert "x-tenant" in field["toml"]
        assert "HEADER-VALUE-SENTINEL" not in field["toml"]

    def test_an_absent_table_is_an_empty_snippet(self):
        field = _field(view.build_config_view(Config(), document=_EDITOR_DOCUMENT), "default_briefings")
        assert field["toml"] == ""

    def test_an_empty_list_of_tables_stays_distinct_from_absent(self):
        payload = view.build_config_view(Config(), document="briefing_shared_blocks = []\n")
        assert _field(payload, "briefing_shared_blocks")["toml"] == "briefing_shared_blocks = []\n"

    def test_a_list_of_tables_renders_as_an_array_of_tables(self):
        document = '[[default_briefings]]\nname = "morning"\ncron = "0 7 * * *"\n'
        field = _field(view.build_config_view(Config(), document=document), "default_briefings")
        assert field["toml"].startswith("[[default_briefings]]\n")
        assert field["value"] == 0  # the loaded config's count, unchanged


class TestTopLevel:
    def test_defaults_without_the_new_inputs(self):
        payload = view.build_config_view(Config())
        assert payload["editable"] is False
        assert payload["applier"]["kind"] == "none"
        assert payload["applier"]["available"] is False
        assert payload["applier"]["reason"]
        assert payload["document_digest"] is None
        assert payload["started_at"] is None
        assert payload["problems"] == []
        assert payload["has_comments"] is False
        assert {r: e["managed"] for r, e in payload["managed"].items()} == {
            "config": False, "secrets": False, "admins": False,
        }

    def test_passthrough(self):
        payload = view.build_config_view(
            Config(), document="", document_digest="abc", started_at="2026-10-12T00:00:00Z",
        )
        assert payload["document_digest"] == "abc"
        assert payload["started_at"] == "2026-10-12T00:00:00Z"

    def test_editable_needs_the_host_applier_and_the_file(self):
        host = {"kind": "host", "available": True, "reason": ""}
        assert view.build_config_view(Config(), document="")["editable"] is False
        assert view.build_config_view(Config(), document="", applier=host)["editable"] is False
        assert view.build_config_view(
            Config(), document="", document_digest="abc", applier=host,
        )["editable"] is True

    def test_has_comments(self):
        assert view.build_config_view(Config(), document=_EDITOR_DOCUMENT)["has_comments"] is True
        no_comment = 'bot_name = "a # not a comment"\nx = \'#\'\ny = """\n# inside\n"""\n'
        assert view.build_config_view(Config(), document=no_comment)["has_comments"] is False
        assert view.build_config_view(Config(), document="a = 1 # trailing\n")["has_comments"] is True

    def test_problems_carry_whether_they_block(self):
        from istota.apply import CheckResult

        check = CheckResult(
            problems=[
                {"kind": "discarded", "path": ["scheduler", "talk_poll_timeout"], "message": "m1"},
                {"kind": "unknown", "path": ["nope"], "message": "m2"},
            ],
            error="boom",
        )
        problems = view.build_config_view(Config(), check=check)["problems"]
        assert [(p["kind"], p["blocking"]) for p in problems] == [
            ("discarded", True), ("unknown", False), ("error", True),
        ]
        assert problems[-1] == {"kind": "error", "path": [], "message": "boom", "blocking": True}

    def test_secrets_list_every_credential_file_without_a_value(self, monkeypatch, no_overrides):
        from istota.setup_wizard import SECRET_NAMES

        monkeypatch.setenv("ANTHROPIC_API_KEY", "ANTHROPIC-SENTINEL")
        cfg = Config()
        cfg.email.imap_password = "IMAP-SENTINEL"
        payload = view.build_config_view(cfg)
        entries = {entry["name"]: entry for entry in payload["secrets"]}
        assert list(entries) == list(SECRET_NAMES)
        assert entries["anthropic_api_key"]["set"] is True
        assert entries["anthropic_api_key"]["config_key"] is None
        assert entries["anthropic_api_key"]["label"] == "Anthropic API key"
        assert entries["claude_code_oauth_token"]["set"] is False
        assert entries["istota_email_imap_password"]["set"] is True
        assert entries["istota_email_imap_password"]["config_key"] == "email.imap_password"
        assert entries["istota_brain_native_api_key"]["config_key"] == "brain.native.api_key"
        assert entries["istota_web_session_secret_key"]["read_only_reason"]
        assert entries["istota_web_oauth2_client_secret"]["risky"] is True
        assert "ANTHROPIC-SENTINEL" not in str(payload)
        assert "IMAP-SENTINEL" not in str(payload)

    def test_admins(self):
        cfg = Config()
        cfg.admin_users = {"zoe", "alice"}
        admins = view.build_config_view(cfg)["admins"]
        assert admins == {"users": ["alice", "zoe"], "read_only_reason": None}

    def test_no_secret_value_from_the_document_reaches_the_payload(self):
        payload = view.build_config_view(Config(), document=_EDITOR_DOCUMENT)
        assert "HEADER-VALUE-SENTINEL" not in str(payload)
        assert "INLINE-PASSWORD-SENTINEL" not in str(payload)

    def test_an_unparsable_document_is_in_no_field(self):
        payload = view.build_config_view(Config(), document="x = = 1\n")
        assert _field(payload, "bot_name")["in_file"] is False
        assert _field(payload, "models.aliases")["toml"] == ""

    def test_the_payload_is_json_serializable(self):
        import json

        from istota.apply import CheckResult

        json.dumps(view.build_config_view(
            Config(), document=_EDITOR_DOCUMENT, document_digest="d",
            check=CheckResult(problems=[{"kind": "warning", "path": ["a"], "message": "m"}]),
            managed={"config": {"managed": True, "source": "ansible", "at": "t"}},
        ))


class TestRawDocument:
    def test_the_raw_document_has_a_sentinel_at_every_secret_leaf(self):
        import tomllib

        from istota.config_editor.secrets import SENTINEL

        raw = view.build_config_view(Config(), document=_EDITOR_DOCUMENT)["raw_toml"]
        parsed = tomllib.loads(raw)
        assert parsed["bot_name"] == "Filebot"
        assert parsed["email"]["imap_password"] == SENTINEL
        assert parsed["brain"]["native"]["extra_headers"] == {"x-tenant": SENTINEL}
        assert "HEADER-VALUE-SENTINEL" not in raw
        assert "INLINE-PASSWORD-SENTINEL" not in raw

    def test_no_raw_document_without_a_parsable_file(self):
        assert view.build_config_view(Config())["raw_toml"] is None
        assert view.build_config_view(Config(), document="x = = 1\n")["raw_toml"] is None

    def test_an_empty_file_is_an_empty_raw_document(self):
        assert view.build_config_view(Config(), document="")["raw_toml"] == ""


class TestRemovableProblems:
    @pytest.mark.parametrize("interval", [0, 60])
    async def test_retired_interval_warns_once_through_the_real_check(self, interval):
        from istota.config_editor.check import run_check
        from istota.lib import toml_write

        document = {
            "rclone_remote": "remote",
            "scheduler": {"vault_sync_interval": interval, "poll_interval": 7},
        }
        check = await run_check(document)
        assert check.error is None
        problems = view.build_config_view(
            Config(), document=toml_write.dumps(document), check=check,
        )["problems"]
        assert [(p["kind"], p["path"]) for p in problems] == [
            ("retired", ["scheduler", "vault_sync_interval"]),
            ("unknown", ["rclone_remote"]),
        ]
        assert problems[0]["message"] == "[scheduler] vault_sync_interval is retired and ignored"
        assert all(p["action"] == "unset" and not p["blocking"] for p in problems)

    def _check(self, *problems):
        from istota.apply import CheckResult

        return CheckResult(problems=list(problems))

    def test_unknown_and_retired_keys_in_the_file_can_be_removed(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_EMAIL_IMAP_PASSWORD", raising=False)
        document = '[web]\ntypo_key = 1\n\n[scheduler]\nvault_sync_interval = 5\n'
        check = self._check(
            {"kind": "unknown", "path": ["web", "typo_key"], "message": "m1"},
            {"kind": "retired", "path": ["scheduler", "vault_sync_interval"], "message": "m2"},
        )
        problems = view.build_config_view(Config(), document=document, check=check)["problems"]
        assert [(p["path"], p.get("action")) for p in problems] == [
            (["web", "typo_key"], "unset"),
            (["scheduler", "vault_sync_interval"], "unset"),
        ]

    def test_no_remove_action_for_a_key_the_file_does_not_hold_by_table(self):
        # Inside an array of tables: an unset op cannot reach it, so the raw
        # editor is where it is removed.
        document = '[[default_briefings]]\nname = "m"\nbogus = 1\n'
        check = self._check(
            {"kind": "unknown", "path": ["default_briefings", "0", "bogus"], "message": "m"},
            {"kind": "discarded", "path": ["scheduler", "poll_interval"], "message": "m"},
        )
        problems = view.build_config_view(Config(), document=document, check=check)["problems"]
        assert all("action" not in p for p in problems)

    def test_no_remove_action_when_a_plan_manages_the_config(self):
        document = '[web]\ntypo_key = 1\n'
        check = self._check({"kind": "unknown", "path": ["web", "typo_key"], "message": "m"})
        problems = view.build_config_view(
            Config(), document=document, check=check,
            managed={"config": {"managed": True, "source": "ansible", "at": "t"}},
        )["problems"]
        assert "action" not in problems[0]

    def test_a_file_credential_also_set_inline_is_a_removable_warning(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_EMAIL_IMAP_PASSWORD", "from-the-file")
        problems = view.build_config_view(Config(), document=_EDITOR_DOCUMENT)["problems"]
        inline = [p for p in problems if p["path"] == ["email", "imap_password"]]
        assert len(inline) == 1
        assert inline[0]["kind"] == "warning"
        assert inline[0]["blocking"] is False
        assert inline[0]["action"] == "unset"
        assert "also set in config.toml" in inline[0]["message"]
        assert "INLINE-PASSWORD-SENTINEL" not in str(problems)

    def test_no_inline_warning_when_the_secrets_file_is_empty(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_EMAIL_IMAP_PASSWORD", raising=False)
        problems = view.build_config_view(Config(), document=_EDITOR_DOCUMENT)["problems"]
        assert problems == []


class TestManaged:
    def test_a_managed_config_makes_config_fields_read_only(self):
        payload = view.build_config_view(
            Config(), managed={"config": {"managed": True, "source": "ansible", "at": "t"}},
        )
        assert payload["managed"]["config"] == {"managed": True, "source": "ansible", "at": "t"}
        field = _field(payload, "bot_name")
        assert field["editor"] == "readonly"
        assert "ansible" in field["read_only_reason"]
        # A file credential belongs to the secrets resource, which is not managed.
        assert _field(payload, "email.imap_password")["editor"] == "secret"
        assert payload["admins"]["read_only_reason"] is None

    def test_managed_secrets_lock_file_credentials_only(self):
        payload = view.build_config_view(
            Config(), managed={"secrets": {"managed": True, "source": "ansible", "at": "t"}},
        )
        assert _field(payload, "email.imap_password")["editor"] == "readonly"
        assert "ansible" in _field(payload, "email.imap_password")["read_only_reason"]
        assert _field(payload, "bot_name")["editor"] == "str"
        imap = next(e for e in payload["secrets"] if e["name"] == "istota_email_imap_password")
        assert "ansible" in imap["read_only_reason"]

    def test_managed_admins(self):
        payload = view.build_config_view(
            Config(), managed={"admins": {"managed": True, "source": "ansible", "at": "t"}},
        )
        assert "ansible" in payload["admins"]["read_only_reason"]

    def test_an_unmanaged_entry_locks_nothing(self):
        payload = view.build_config_view(
            Config(), managed={"config": {"managed": False, "source": "web", "at": "t"}},
        )
        assert _field(payload, "bot_name")["editor"] == "str"

    def test_a_malformed_entry_fails_closed(self):
        payload = view.build_config_view(Config(), managed={"config": "yes"})
        assert payload["managed"]["config"]["managed"] is True
        assert payload["managed"]["config"]["source"] == "unreadable"
        assert _field(payload, "bot_name")["editor"] == "readonly"

    def test_an_unreadable_marker_reads_every_resource_managed(self, tmp_path):
        from istota.apply import MANAGED_FILE, read_managed

        (tmp_path / MANAGED_FILE).write_text("{not json", encoding="utf-8")
        managed = read_managed(tmp_path, fail_closed=True)
        payload = view.build_config_view(Config(), managed=managed)
        for resource in ("config", "secrets", "admins"):
            assert payload["managed"][resource]["managed"] is True
            assert payload["managed"][resource]["source"] == "unreadable"
        assert "istota-stack apply" in _field(payload, "bot_name")["read_only_reason"]
