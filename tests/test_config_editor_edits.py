"""`config_editor.edits`: a change set applied, checked and described."""

from __future__ import annotations

import asyncio

import pytest

from istota import user_profiles
from istota.apply import CheckResult
from istota.config import Config
from istota.config_editor import edits
from istota.config_editor.edits import ChangeError
from istota.config_editor.secrets import SENTINEL
from istota.webui import auth as web_auth


class TestApplyOps:
    def test_set_creates_the_tables_on_its_way(self):
        new = edits.apply_ops({}, [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}])
        assert new == {"email": {"imap_host": "imap.example.org"}}

    def test_the_input_is_not_changed(self):
        current = {"email": {"imap_host": "old"}}
        edits.apply_ops(current, [{"op": "set", "path": ["email", "imap_host"], "value": "new"}])
        assert current == {"email": {"imap_host": "old"}}

    def test_unset_removes_the_key_and_the_table_it_emptied(self):
        current = {"scheduler": {"talk_poll_timeout": 20}, "email": {"imap_host": "h", "imap_port": 993}}
        new = edits.apply_ops(current, [
            {"op": "unset", "path": ["scheduler", "talk_poll_timeout"]},
            {"op": "unset", "path": ["email", "imap_port"]},
        ])
        assert new == {"email": {"imap_host": "h"}}

    def test_unset_of_a_missing_key_is_nothing(self):
        assert edits.apply_ops({"a": 1}, [{"op": "unset", "path": ["b", "c"]}]) == {"a": 1}

    def test_set_toml_replaces_the_value_with_the_snippets_key(self):
        current = {"models": {"aliases": {"old": "x"}}}
        new = edits.apply_ops(current, [
            {"op": "set_toml", "path": ["models", "aliases"], "toml": '[aliases]\nfast = "haiku"\n'},
        ])
        assert new == {"models": {"aliases": {"fast": "haiku"}}}

    def test_set_toml_with_another_top_level_key_is_refused(self):
        with pytest.raises(ChangeError, match="must define aliases and nothing else"):
            edits.apply_ops({}, [
                {"op": "set_toml", "path": ["models", "aliases"], "toml": '[other]\nfast = "haiku"\n'},
            ])

    def test_an_empty_list_and_unset_stay_two_edits(self):
        current = {"briefing_shared_blocks": [{"name": "a"}]}
        emptied = edits.apply_ops(current, [
            {"op": "set_toml", "path": ["briefing_shared_blocks"], "toml": "briefing_shared_blocks = []\n"},
        ])
        removed = edits.apply_ops(current, [{"op": "unset", "path": ["briefing_shared_blocks"]}])
        assert emptied == {"briefing_shared_blocks": []}
        assert removed == {}

    def test_a_segment_holding_a_dot_is_one_key(self):
        new = edits.apply_ops({}, [{
            "op": "set_toml", "path": ["brain", "native", "model_overrides"],
            "toml": '[model_overrides."vendor.model-1"]\ncontext_window = 1000\n',
        }])
        assert new == {"brain": {"native": {"model_overrides": {"vendor.model-1": {"context_window": 1000}}}}}
        new = edits.apply_ops(new, [{
            "op": "unset", "path": ["brain", "native", "model_overrides", "vendor.model-1", "context_window"],
        }])
        assert new == {}

    @pytest.mark.parametrize("op", [
        {"op": "set", "path": ["email", "imap_host"], "value": None},
        {"op": "set", "path": ["email"], "value": {"imap_host": "h"}},
        {"op": "set", "path": "email.imap_host", "value": "h"},
        {"op": "set", "path": [], "value": "h"},
        {"op": "set", "path": ["email", ""], "value": "h"},
        {"op": "set", "path": ["email", "imap_host"]},
        {"op": "rename", "path": ["email"]},
        {"op": "set_toml", "path": ["models", "aliases"], "toml": "[aliases"},
    ])
    def test_a_malformed_op_is_refused(self, op):
        with pytest.raises(ChangeError):
            edits.apply_ops({}, [op])

    def test_a_set_through_a_scalar_is_refused(self):
        with pytest.raises(ChangeError, match="email is not a table"):
            edits.apply_ops({"email": "x"}, [{"op": "set", "path": ["email", "imap_host"], "value": "h"}])

    def test_raw_text_that_does_not_parse_names_the_line(self):
        with pytest.raises(ChangeError, match="line 2"):
            edits.apply_raw('a = 1\nb = = 2\n')


class TestCheckAdmins:
    def _identity(self, db_path, user_id):
        user_profiles.ensure_profile(db_path, user_id)
        web_auth.upsert_identity(db_path, user_id, f"{user_id}@example.org")

    def test_a_list_of_live_identities_holding_the_actor_passes(self, db_path):
        self._identity(db_path, "alice")
        self._identity(db_path, "bob")
        assert edits.check_admins(["alice", "bob"], acting_admin="alice", db_path=db_path) == []

    def test_an_empty_list_is_refused(self, db_path):
        assert edits.check_admins([], acting_admin="alice", db_path=db_path)

    def test_the_actor_must_stay(self, db_path):
        self._identity(db_path, "bob")
        errors = edits.check_admins(["bob"], acting_admin="alice", db_path=db_path)
        assert any("keep you" in error for error in errors)

    def test_an_id_with_no_identity_is_refused(self, db_path):
        self._identity(db_path, "alice")
        user_profiles.ensure_profile(db_path, "carol")
        errors = edits.check_admins(["alice", "carol"], acting_admin="alice", db_path=db_path)
        assert errors == ["carol has no live sign-in identity; give them one on the Users page first"]

    def test_a_disabled_identity_is_refused(self, db_path):
        self._identity(db_path, "alice")
        self._identity(db_path, "bob")
        web_auth.set_disabled(db_path, "bob", True)
        errors = edits.check_admins(["alice", "bob"], acting_admin="alice", db_path=db_path)
        assert errors and "bob" in errors[0]


class TestCheckSignIn:
    def test_unchanged_is_not_checked(self):
        assert edits.check_sign_in({"web": {"auth": ["nextcloud"]}}, {"web": {"auth": ["nextcloud"]}}) == []

    def test_email_and_nextcloud_pass(self):
        assert edits.check_sign_in({"web": {"auth": ["email", "nextcloud"]}}, {}) == []

    def test_a_set_without_email_is_refused(self):
        errors = edits.check_sign_in({"web": {"auth": ["nextcloud"]}}, {"web": {"auth": ["email"]}})
        assert len(errors) == 1 and "must keep email" in errors[0]

    def test_none_is_refused(self):
        errors = edits.check_sign_in({"web": {"auth": ["none"]}}, {"web": {"auth": ["email"]}})
        assert any("cannot be none" in error for error in errors)

    def test_unsetting_it_falls_back_to_the_default_without_email(self):
        assert edits.check_sign_in({}, {"web": {"auth": ["email"]}})

    def test_the_comma_string_form_is_read(self):
        assert edits.check_sign_in({"web": {"auth": "email, nextcloud"}}, {}) == []


class TestCheckChoices:
    def test_a_word_from_the_vocabulary_passes(self):
        assert edits.check_choices({"web": {"token_storage": "encrypted"}}, {}) == []

    def test_a_word_outside_it_is_refused(self):
        errors = edits.check_choices({"web": {"token_storage": "plain"}}, {})
        assert errors and errors[0].startswith("web.token_storage must be one of")

    def test_an_unchanged_bad_value_is_left_to_the_check(self):
        current = {"web": {"token_storage": "plain"}}
        assert edits.check_choices(current, current) == []

    def test_a_multi_choice_word_outside_it_is_refused(self):
        errors = edits.check_choices({"web": {"auth": ["email", "saml"]}}, {})
        assert errors and errors[0].startswith("web.auth takes a list of")


def _fake_check(result: CheckResult):
    seen: list[dict] = []

    async def run_check(document: dict) -> CheckResult:
        seen.append(document)
        return result

    return run_check, seen


def _preview(current_text, change_set, *, config=None, result=None, environ=None):
    run_check, seen = _fake_check(result or CheckResult())
    found = asyncio.run(edits.preview(
        current_text, change_set, config=config or Config(admin_users={"alice"}),
        acting_admin="alice", run_check=run_check, environ=environ or {},
    ))
    return found, seen


CURRENT = (
    '[email]\nimap_host = "old.example.org"\n\n'
    '[scheduler]\ntalk_poll_timeout = 0\n\n'
    '[brain.native.extra_headers]\nX-Harmless = "header-value-1"\n'
)


class TestPreview:
    def test_a_pre_existing_discard_blocks_and_says_so(self):
        result = CheckResult(problems=[
            {"kind": "discarded", "path": ["scheduler", "talk_poll_timeout"], "message": "bad"},
            {"kind": "retired", "path": ["old_key"], "message": "retired"},
        ])
        found, _ = _preview(CURRENT, {
            "ops": [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}],
        }, result=result)
        assert found.blocked
        assert [problem["path"] for problem in found.blocking] == [["scheduler", "talk_poll_timeout"]]
        assert found.blocking[0]["pre_existing"] is True
        assert [problem["kind"] for problem in found.warnings] == ["retired"]

    def test_a_discard_the_change_caused_is_not_pre_existing(self):
        result = CheckResult(problems=[
            {"kind": "discarded", "path": ["email", "imap_host"], "message": "bad"},
        ])
        found, _ = _preview(CURRENT, {
            "ops": [{"op": "set", "path": ["email", "imap_host"], "value": "x"}],
        }, result=result)
        assert found.blocking[0]["pre_existing"] is False

    def test_the_check_is_given_the_whole_new_document_and_the_preview_none_of_it(self):
        found, seen = _preview(CURRENT, {
            "ops": [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}],
            "secrets": {"brain.native.extra_headers.X-New": "header-value-2"},
        })
        assert seen[0]["brain"]["native"]["extra_headers"] == {
            "X-Harmless": "header-value-1", "X-New": "header-value-2",
        }
        assert found.document["brain"]["native"]["extra_headers"] == {
            "X-Harmless": SENTINEL, "X-New": SENTINEL,
        }
        assert found.inline_secrets == {"brain.native.extra_headers.X-New": "header-value-2"}
        shown = repr(found.as_json()) + found.document_toml
        assert "header-value-1" not in shown and "header-value-2" not in shown
        assert found.changed and found.changed_sections == ["brain.native", "email"]
        assert found.secret_names == ["brain.native.extra_headers.X-New"]

    def test_raw_with_sentinels_keeps_the_stored_values(self):
        raw = (
            '[email]\nimap_host = "imap.example.org"\n\n'
            '[scheduler]\ntalk_poll_timeout = 30\n\n'
            f'[brain.native.extra_headers]\nX-Harmless = "{SENTINEL}"\n'
        )
        found, seen = _preview(CURRENT, {"raw": raw})
        assert seen[0]["brain"]["native"]["extra_headers"]["X-Harmless"] == "header-value-1"
        assert found.inline_secrets == {}
        assert found.secret_names == []

    def test_a_sentinel_with_no_stored_value_is_refused(self):
        with pytest.raises(ChangeError, match="no stored value to keep at brain.native.api_key"):
            _preview(CURRENT, {"raw": f'[brain.native]\napi_key = "{SENTINEL}"\n'})

    def test_raw_and_ops_together_are_refused(self):
        with pytest.raises(ChangeError, match="ops must be empty"):
            _preview(CURRENT, {"raw": "", "ops": [{"op": "unset", "path": ["email"]}]})

    def test_no_change_is_unchanged(self):
        found, _ = _preview(CURRENT, {"ops": []})
        assert not found.changed and found.diff == []

    def test_file_credentials_are_named_not_shown(self):
        found, _ = _preview(CURRENT, {"secrets": {"istota_email_imap_password": "imap-secret-9"}})
        assert found.file_secrets == {"istota_email_imap_password": "imap-secret-9"}
        assert found.secret_names == ["istota_email_imap_password"]
        assert found.changed
        assert "imap-secret-9" not in repr(found.as_json()) + found.document_toml

    @pytest.mark.parametrize("secrets", [
        {"istota_email_imap_password": None},
        {"plain_setting": "x"},
        {"email.imap_host": "x"},
        {"email.imap_password": "x"},
    ])
    def test_a_credential_that_is_not_one_is_refused(self, secrets):
        with pytest.raises(ChangeError):
            _preview(CURRENT, {"secrets": secrets})

    def test_a_file_credential_given_inline_is_refused(self):
        found, _ = _preview(CURRENT, {
            "ops": [{"op": "set", "path": ["email", "imap_password"], "value": "inline-value"}],
        })
        assert any("istota_email_imap_password" in error for error in found.errors)

    def test_a_read_only_key_is_refused(self):
        found, _ = _preview(CURRENT, {"ops": [{"op": "set", "path": ["db_path"], "value": "/tmp/x.db"}]})
        assert found.errors and found.errors[0].startswith("db_path is not edited here.")

    def test_a_read_only_credential_is_refused(self):
        found, _ = _preview(CURRENT, {"secrets": {"istota_web_session_secret_key": "x" * 40}})
        assert any("istota_web_session_secret_key" in error for error in found.errors)

    def test_a_key_the_environment_sets_is_refused(self):
        found, _ = _preview(
            CURRENT, {"ops": [{"op": "set", "path": ["web", "auth"], "value": ["email", "nextcloud"]}]},
            environ={"ISTOTA_WEB_AUTH": "email"},
        )
        assert any("ISTOTA_WEB_AUTH" in error for error in found.errors)

    def test_users_may_change_in_the_raw_editor(self):
        found, _ = _preview(CURRENT, {"raw": CURRENT + '\n[users.sam]\ndisplay_name = "Sam"\n'})
        assert found.errors == []

    def test_risky_keys_and_admins_are_listed(self, db_path):
        user_profiles.ensure_profile(db_path, "alice")
        web_auth.upsert_identity(db_path, "alice", "alice@example.org")
        config = Config(admin_users={"alice", "bob"}, db_path=db_path)
        found, _ = _preview(CURRENT, {
            "ops": [{"op": "set", "path": ["security", "sandbox_enabled"], "value": False}],
            "secrets": {"istota_web_oauth2_client_secret": "s"},
            "admins": ["alice"],
        }, config=config)
        assert found.errors == []
        assert found.risky_keys == ["admins", "security.sandbox_enabled", "web.oauth2_client_secret"]
        assert found.admins == ["alice"] and found.admins_changed

    def test_the_same_admins_in_another_order_is_no_change(self):
        found, _ = _preview(CURRENT, {"admins": ["bob", "alice"]}, config=Config(admin_users={"alice", "bob"}))
        assert not found.admins_changed and found.admins is None

    def test_an_inline_credential_cleared_is_removed(self):
        found, seen = _preview(CURRENT, {"secrets": {"brain.native.extra_headers.X-Harmless": ""}})
        assert "brain" not in seen[0]
        assert found.secret_names == ["brain.native.extra_headers.X-Harmless"]

    def test_a_file_credential_also_set_inline_is_a_warning(self):
        text = CURRENT + '\n[caldav]\npassword = "old-inline"\n'
        found, _ = _preview(text, {"ops": []}, environ={"ISTOTA_CALDAV_PASSWORD": "from-the-file"})
        warning = [problem for problem in found.warnings if problem["path"] == ["caldav", "password"]]
        assert warning and "where it is ignored" in warning[0]["message"]
        assert not found.blocked

    def test_running_tasks_are_counted(self, db_path):
        from istota import db

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, "p", "alice")
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        found, _ = _preview(CURRENT, {"ops": []}, config=Config(admin_users={"alice"}, db_path=db_path))
        assert found.running_tasks == 1


class TestLoggableNames:
    def test_a_file_credential_is_its_name(self):
        assert edits.loggable_secret_name("istota_email_imap_password") == "istota_email_imap_password"

    def test_an_inline_credential_is_its_section(self):
        assert edits.loggable_secret_name('brain.native.extra_headers."X-Secret.Name"') == "brain.native (inline)"
