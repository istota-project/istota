"""`config_editor.schema` and `config_editor.check`: what the editor may do with
each key, and the subprocess that validates a document."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from istota import apply as apply_module
from istota.config import Config
from istota.config_editor import check, schema


def _resolve(path: tuple[str, ...]) -> None:
    node = Config()
    for segment in path:
        assert hasattr(node, segment), f"{'.'.join(path)} is not on the Config tree"
        node = getattr(node, segment)


class TestReadOnly:
    def test_every_read_only_path_is_a_field(self):
        for path in schema.READ_ONLY:
            _resolve(path)

    @pytest.mark.parametrize("dotted", [
        "site.hostname", "nextcloud.url", "nextcloud.username", "nextcloud.app_password",
        "devbox.network_subnet", "web.enabled", "web.port", "browser.enabled",
        "browser.api_url", "talk.signaling.enabled", "talk.signaling.url",
        "whatsapp.enabled", "whatsapp.provider", "web.session_secret_key",
        "db_path", "workspace_path", "config_path", "users", "admin_users",
    ])
    def test_the_spec_list_is_read_only_with_a_reason(self, dotted):
        reason = schema.READ_ONLY[tuple(dotted.split("."))]
        assert reason.strip()

    def test_each_host_reason_names_where_it_is_changed(self):
        for path in [("site", "hostname"), ("db_path",), ("browser", "enabled"), ("web", "port")]:
            assert "istota-stack" in schema.READ_ONLY[path]


class TestRiskyAndTables:
    def test_every_risky_key_is_a_field(self):
        for path in schema.RISKY_KEYS:
            _resolve(path)

    def test_the_oauth_client_secret_is_risky_by_its_path(self):
        assert ("web", "oauth2_client_secret") in schema.RISKY_KEYS

    @pytest.mark.parametrize("dotted", [
        "models.aliases", "default_briefings", "briefing_shared_blocks",
        "brain.source_type_overrides", "brain.native.model_overrides",
        "brain.native.extra_headers",
    ])
    def test_structured_fields_are_tables(self, dotted):
        assert tuple(dotted.split(".")) in schema.TABLE_PATHS

    def test_a_scalar_list_is_not_a_table(self):
        assert ("disabled_skills",) not in schema.TABLE_PATHS


class TestEditorKind:
    @pytest.mark.parametrize("dotted,declared,value,kind", [
        ("security.sandbox_enabled", bool, True, "bool"),
        ("scheduler.poll_interval", int, 2, "int"),
        ("bot_name", str, "Istota", "str"),
        ("skills_dir", Path, Path("x"), "path"),
        ("disabled_skills", list[str], [], "list"),
        ("web.token_storage", str, "ephemeral", "choice"),
        ("web.auth", list[str], ["email"], "multi_choice"),
        ("email.imap_password", str, "", "secret"),
        ("models.aliases", dict, {}, "table"),
        ("db_path", Path, Path("/x"), "readonly"),
    ])
    def test_kinds(self, dotted, declared, value, kind):
        assert schema.editor_kind(tuple(dotted.split(".")), declared, value) == kind

    def test_an_optional_path_is_a_path(self):
        assert schema.editor_kind(("module_data_dir",), Path | None, None) == "path"

    def test_a_file_credential_that_is_not_secret_is_still_edited_as_one(self):
        """Routing follows file membership, not secrecy."""
        path = ("sms", "telnyx", "public_key")
        assert schema.editor_kind(path, str, "pk") == "secret"
        assert schema.credential_for(path) == {
            "kind": "file", "name": "istota_sms_telnyx_public_key",
        }

    def test_an_identifier_with_no_file_is_a_plain_field(self):
        path = ("sms", "twilio", "account_sid")
        assert schema.credential_for(path) is None
        assert schema.editor_kind(path, str, "AC1") == "str"

    def test_an_inline_credential_is_named_by_its_label(self):
        assert schema.credential_for(("brain", "native", "extra_headers")) == {
            "kind": "inline", "name": "brain.native.extra_headers",
        }

    def test_choices(self):
        assert "encrypted" in schema.choices_for(("web", "token_storage"))
        assert schema.choices_for(("bot_name",)) is None
        assert "email" in schema.multi_choices_for(("web", "auth"))
        assert schema.multi_choices_for(("bot_name",)) is None


class TestEnvOverride:
    def test_a_set_non_secret_override_is_named(self):
        assert schema.env_override_for(("web", "auth"), {"ISTOTA_WEB_AUTH": "email"}) == "ISTOTA_WEB_AUTH"

    def test_an_empty_variable_overrides_nothing(self):
        assert schema.env_override_for(("web", "auth"), {"ISTOTA_WEB_AUTH": "  "}) is None

    def test_a_file_credentials_variable_is_never_an_override(self):
        """On the stack every secrets file is exported, so the variable is always set."""
        environ = {"ISTOTA_EMAIL_IMAP_PASSWORD": "x"}
        assert schema.env_override_for(("email", "imap_password"), environ) is None

    def test_an_inline_credentials_variable_is(self):
        environ = {"ISTOTA_SMS_TWILIO_AUTH_TOKEN": "x"}
        assert schema.env_override_for(("sms", "twilio", "auth_token"), environ) == (
            "ISTOTA_SMS_TWILIO_AUTH_TOKEN"
        )


def _fake(script: str) -> list[str]:
    return [sys.executable, "-c", script]


class TestRunCheck:
    async def test_the_reply_is_read(self):
        script = (
            "import sys, json; sys.stdin.read();"
            "print(json.dumps({'problems': [{'kind': 'discarded', 'path': ['a'], 'message': 'm'}],"
            " 'error': None}))"
        )
        result = await check.run_check({"bot_name": "x"}, command=_fake(script))
        assert result.problems == [{"kind": "discarded", "path": ["a"], "message": "m"}]
        assert result.error is None

    async def test_the_plan_is_fed_on_stdin_as_a_config_section(self):
        script = (
            "import sys, json, tomllib; plan = tomllib.loads(sys.stdin.read());"
            "print(json.dumps({'problems': [], 'error': plan['config']['bot_name']}))"
        )
        result = await check.run_check({"bot_name": "fed"}, command=_fake(script))
        assert result.error == "fed"

    async def test_a_reply_that_is_not_json_is_a_blocking_error(self, caplog):
        result = await check.run_check(
            {"bot_name": "SECRET-STDIN"}, command=_fake("import sys; sys.stdin.read(); print('nope')"),
        )
        assert result.error == check.INCOMPLETE
        assert result.refused
        assert "SECRET-STDIN" not in caplog.text

    async def test_a_reply_of_the_wrong_shape_is_a_blocking_error(self):
        script = "import sys; sys.stdin.read(); print('{\"problems\": 3, \"error\": null}')"
        result = await check.run_check({}, command=_fake(script))
        assert result.error == check.INCOMPLETE

    async def test_a_timeout_is_a_blocking_error(self):
        result = await check.run_check(
            {}, command=_fake("import time; time.sleep(30)"), timeout=0.5,
        )
        assert result.error == check.INCOMPLETE

    async def test_a_document_toml_cannot_write_is_an_error_without_a_process(self):
        result = await check.run_check({"bot_name": object()}, command=["/nonexistent"])
        assert result.error is not None
        assert result.error != check.INCOMPLETE

    async def test_the_real_command_reports_a_discarded_value(self):
        """The seam: the default command is `istota apply --check -f - --json`."""
        result = await check.run_check({"scheduler": {"talk_poll_timeout": 0}})
        assert result.error is None
        assert any(
            problem["kind"] == "discarded" and problem["path"] == ["scheduler", "talk_poll_timeout"]
            for problem in result.problems
        ), result.problems


class TestCheckCurrent:
    async def test_an_unparsable_file_is_an_error_without_a_process(self, monkeypatch):
        async def refuse(*args, **kwargs):
            raise AssertionError("no check should run")

        monkeypatch.setattr(check, "run_check", refuse)
        result = await check.check_current("x = = 1\n", "d-unparsable")
        assert result.error and result.error.startswith("config.toml does not parse")

    async def test_one_check_per_digest(self, monkeypatch):
        calls = []

        async def fake(document, **kwargs):
            calls.append(document)
            return apply_module.CheckResult(problems=[])

        monkeypatch.setattr(check, "run_check", fake)
        check.forget_cached()
        await check.check_current('bot_name = "a"\n', "d1")
        await check.check_current('bot_name = "a"\n', "d1")
        await check.check_current('bot_name = "b"\n', "d2")
        assert calls == [{"bot_name": "a"}, {"bot_name": "b"}]

    async def test_an_incomplete_check_is_not_cached(self, monkeypatch):
        calls = []

        async def fake(document, **kwargs):
            calls.append(document)
            return apply_module.CheckResult(error=check.INCOMPLETE)

        monkeypatch.setattr(check, "run_check", fake)
        check.forget_cached()
        await check.check_current('bot_name = "a"\n', "d3")
        await check.check_current('bot_name = "a"\n', "d3")
        assert len(calls) == 2
