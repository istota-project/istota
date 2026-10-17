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


class TestDiffDocuments:
    def test_a_masked_key_is_masked_and_a_change_still_shows(self):
        current = {"whatsapp": {"business_phone_number": "+15550001234"}}
        new = {"whatsapp": {"business_phone_number": "+15559991234"}}
        lines = edits.diff_documents(current, new)
        text = "\n".join(lines)
        assert "0001234" not in text and "9991234" not in text
        assert '-business_phone_number = "+*******1234"' in lines
        assert '+business_phone_number = "+*******1234 (changed)"' in lines

    def test_an_unchanged_masked_key_makes_no_line(self):
        document = {"whatsapp": {"business_phone_number": "+15550001234"}, "email": {"imap_host": "a"}}
        changed = {"whatsapp": {"business_phone_number": "+15550001234"}, "email": {"imap_host": "b"}}
        lines = edits.diff_documents(document, changed)
        assert not [line for line in lines if line[:1] in "+-" and "business_phone_number" in line]
        assert "0001234" not in "\n".join(lines)


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

    async def run_check(document: dict, **kwargs) -> CheckResult:
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
        # CURRENT's inline extra_headers has a secret file, so it is also a move.
        assert [(problem["kind"], problem.get("action")) for problem in found.warnings] == [
            ("retired", None), ("warning", "move"),
        ]

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

    @pytest.mark.parametrize("change_set", [
        {"raw": 'db_path = "/data/istota.db"\ntemp_dir = "/data/tmp"\n'},
        {"raw": '[email]\nimap_host = "imap.example.org"\n'},
        {"ops": [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}]},
    ])
    def test_a_current_file_that_does_not_parse_refuses_every_change(self, change_set):
        """Against nothing, the read-only check refuses a raw document that keeps
        the layout keys and passes one that drops them."""
        with pytest.raises(ChangeError, match=r"fix it on the host with `istota-stack apply`"):
            _preview('db_path = "/data/istota.db"\n[email\n', change_set)

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

    def test_the_check_reads_new_file_credentials_as_their_variables(self):
        environs: list[dict] = []

        async def run_check(document: dict, **kwargs) -> CheckResult:
            environs.append(kwargs.get("environ"))
            return CheckResult()

        asyncio.run(edits.preview(
            CURRENT,
            {"secrets": {"istota_servarr_radarr_api_key": "arr-key-1", "istota_email_imap_password": ""}},
            config=Config(admin_users={"alice"}), acting_admin="alice", run_check=run_check, environ={},
        ))
        assert environs == [{"ISTOTA_SERVARR_RADARR_API_KEY": "arr-key-1", "ISTOTA_EMAIL_IMAP_PASSWORD": ""}]

    def test_a_servarr_key_is_a_file_credential(self):
        found, seen = _preview(CURRENT, {
            "ops": [{"op": "set", "path": ["servarr", "apps", "radarr", "url"], "value": "http://192.0.2.10:7878"}],
            "secrets": {"istota_servarr_radarr_api_key": "arr-key-2"},
        })
        assert not found.blocked, found.errors
        assert found.file_secrets == {"istota_servarr_radarr_api_key": "arr-key-2"}
        assert seen[-1]["servarr"]["apps"]["radarr"] == {"url": "http://192.0.2.10:7878"}
        assert "arr-key-2" not in found.document_toml

    def test_a_servarr_key_by_its_label_is_refused(self):
        with pytest.raises(ChangeError):
            _preview(CURRENT, {"secrets": {"servarr.apps.radarr.api_key": "arr-key-3"}})

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

    def test_user_briefings_are_retired_and_not_written(self):
        added = CURRENT + '\n[[users.sam.briefings]]\nname = "morning"\ncron = "0 7 * * *"\n'
        found, _ = _preview(CURRENT, {"raw": added})
        assert found.errors and found.errors[0].startswith("users.sam.briefings is not edited here.")

    def test_a_retired_user_briefings_block_may_be_removed(self):
        held = CURRENT + '\n[users.sam]\ndisplay_name = "Sam"\n\n[[users.sam.briefings]]\nname = "morning"\n'
        found, _ = _preview(held, {"ops": [{"op": "unset", "path": ["users", "sam", "briefings"]}]})
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


class TestFileCredentialProblems:
    def test_an_inline_credential_with_an_empty_file_is_a_move(self):
        document = {"sms": {"twilio": {"auth_token": "INLINE-TOKEN-SENTINEL"}}}
        found = edits.file_credential_problems(document, {})
        assert found == [{
            "kind": "warning",
            "path": ["sms", "twilio", "auth_token"],
            "message": (
                "sms.twilio.auth_token is stored in config.toml in plain text; "
                "move it to the secret file istota_sms_twilio_auth_token"
            ),
            "blocking": False,
            "pre_existing": False,
            "action": "move",
        }]

    def test_an_inline_table_credential_is_a_move(self):
        document = {"brain": {"native": {"extra_headers": {"X-Key": "HEADER-SENTINEL"}}}}
        found = edits.file_credential_problems(document, {})
        assert [(p["path"], p["action"]) for p in found] == [
            (["brain", "native", "extra_headers"], "move"),
        ]
        assert "HEADER-SENTINEL" not in str(found)

    def test_a_credential_set_in_both_places_stays_an_unset(self):
        document = {"sms": {"twilio": {"auth_token": "INLINE-TOKEN-SENTINEL"}}}
        found = edits.file_credential_problems(
            document, {"ISTOTA_SMS_TWILIO_AUTH_TOKEN": "from-the-file"},
        )
        assert [(p["path"], p["action"]) for p in found] == [
            (["sms", "twilio", "auth_token"], "unset"),
        ]
        assert "where it is ignored" in found[0]["message"]

    @pytest.mark.parametrize("value", ["", "   ", {}])
    def test_an_empty_inline_value_is_nothing_to_move(self, value):
        document = {"sms": {"twilio": {"auth_token": value}}}
        assert edits.file_credential_problems(document, {}) == []

    def test_a_credential_with_no_file_is_not_reported(self):
        document = {"web": {"map": {"api_key": "browser-visible"}}}
        assert edits.file_credential_problems(document, {}) == []

    def test_preview_reads_a_file_set_in_the_same_change_set(self):
        text = CURRENT + '\n[caldav]\npassword = "old-inline"\n'
        found, _ = _preview(text, {"secrets": {"istota_caldav_password": "new-file-value"}})
        actions = {
            tuple(p["path"]): p.get("action") for p in found.warnings
            if p["path"] == ["caldav", "password"]
        }
        assert actions == {("caldav", "password"): "unset"}


FLAT_TOKEN = "FLAT-WHATSAPP-TOKEN-SENTINEL"
FLAT_LABEL = "whatsapp.cloud.access_token"
WITH_FLAT = CURRENT + f'\n[whatsapp]\nenabled = false\naccess_token = "{FLAT_TOKEN}"\n'


class TestLegacyFlatWhatsapp:
    """`load_config` reads a flat `[whatsapp] access_token` as
    `[whatsapp.cloud] access_token`, so it is held inline all the same."""

    def test_it_is_inline(self):
        document = {"whatsapp": {"access_token": FLAT_TOKEN}}
        assert edits.inline_file_credentials(document) == [
            (("whatsapp", "cloud", "access_token"), "istota_whatsapp_access_token"),
        ]
        assert document == {"whatsapp": {"access_token": FLAT_TOKEN}}

    def test_it_is_a_move_and_an_unset_beside_a_set_file(self):
        document = {"whatsapp": {"access_token": FLAT_TOKEN}}
        moves = edits.file_credential_problems(document, {})
        assert [(p["path"], p["action"]) for p in moves] == [(FLAT_LABEL.split("."), "move")]
        unsets = edits.file_credential_problems(document, {"ISTOTA_WHATSAPP_ACCESS_TOKEN": "f"})
        assert [(p["path"], p["action"]) for p in unsets] == [(FLAT_LABEL.split("."), "unset")]
        assert FLAT_TOKEN not in repr(moves) + repr(unsets)

    def test_a_move_removes_the_flat_key(self):
        found, seen = _preview(WITH_FLAT, {"move_to_file": [FLAT_LABEL]})
        assert not found.blocked, found.errors
        assert seen[0]["whatsapp"] == {"enabled": False}
        assert found.moves == [{"label": FLAT_LABEL, "file": "istota_whatsapp_access_token"}]
        assert FLAT_TOKEN not in repr(found.as_json()) + found.document_toml + repr(seen)
        assert [p["path"] for p in found.warnings] == [["brain", "native", "extra_headers"]]

    def test_an_unset_of_the_nested_label_removes_the_flat_key(self):
        found, seen = _preview(
            WITH_FLAT, {"ops": [{"op": "unset", "path": FLAT_LABEL.split(".")}]},
        )
        assert not found.blocked, found.errors
        assert seen[0]["whatsapp"] == {"enabled": False}
        assert FLAT_TOKEN not in repr(found.as_json()) + found.document_toml + repr(seen)

    def test_a_raw_edit_cannot_write_the_flat_key_inline(self):
        found, _ = _preview(CURRENT, {"raw": WITH_FLAT})
        assert any("istota_whatsapp_access_token" in error for error in found.errors), found.errors

    def test_a_stale_flat_key_left_alone_is_not_an_error(self):
        found, _ = _preview(WITH_FLAT, {"ops": [{"op": "set", "path": ["email", "imap_host"], "value": "x"}]})
        assert found.errors == []


MOVED_TOKEN = "move-me-token-6d2a"
WITH_TOKEN = CURRENT + f'\n[sms.twilio]\nauth_token = "{MOVED_TOKEN}"\n'


class TestMoveToFile:
    def test_a_move_drops_the_leaf_and_names_its_file(self):
        found, seen = _preview(WITH_TOKEN, {"move_to_file": ["sms.twilio.auth_token"]})
        assert not found.blocked, found.errors
        assert "sms" not in seen[0]
        assert "sms" not in found.document
        assert found.moves == [{"label": "sms.twilio.auth_token", "file": "istota_sms_twilio_auth_token"}]
        assert found.as_json()["moves"] == found.moves
        assert found.file_secrets == {} and found.inline_secrets == {} and found.secrets == {}
        assert found.changed and found.changed_sections == ["sms.twilio"]
        assert found.secret_names == ["istota_sms_twilio_auth_token"]
        assert 'auth_token = "<redacted>"' in "\n".join(found.diff)
        assert any(line.startswith("-auth_token") for line in found.diff)
        shown = repr(found.as_json()) + found.document_toml
        assert MOVED_TOKEN not in shown
        # The move answers its own warning; the headers' warning stays.
        assert [problem["path"] for problem in found.warnings] == [["brain", "native", "extra_headers"]]

    def test_a_table_credential_moves_whole(self):
        found, seen = _preview(CURRENT, {"move_to_file": ["brain.native.extra_headers"]})
        assert not found.blocked, found.errors
        assert "brain" not in seen[0]
        assert found.moves == [
            {"label": "brain.native.extra_headers", "file": "istota_brain_native_extra_headers"},
        ]
        assert "header-value-1" not in repr(found.as_json()) + found.document_toml

    def test_a_label_named_twice_is_one_move(self):
        found, _ = _preview(WITH_TOKEN, {"move_to_file": ["sms.twilio.auth_token"] * 2})
        assert len(found.moves) == 1

    @pytest.mark.parametrize("moves", [
        "sms.twilio.auth_token",
        [["sms", "twilio", "auth_token"]],
        ["email.imap_host"],
        ["web.map.api_key"],
        ['sms.twilio."auth_token'],
        [""],
    ])
    def test_anything_but_installation_credentials_is_refused(self, moves):
        with pytest.raises(ChangeError) as raised:
            _preview(WITH_TOKEN, {"move_to_file": moves})
        assert "imap_host" not in str(raised.value) and "map.api_key" not in str(raised.value)

    def test_a_move_and_a_replace_of_the_same_credential_is_refused(self):
        with pytest.raises(ChangeError, match="sms.twilio.auth_token"):
            _preview(WITH_TOKEN, {
                "move_to_file": ["sms.twilio.auth_token"],
                "secrets": {"istota_sms_twilio_auth_token": "typed-again"},
            })

    def test_a_move_with_raw_is_refused(self):
        with pytest.raises(ChangeError, match="raw"):
            _preview(WITH_TOKEN, {"raw": WITH_TOKEN, "move_to_file": ["sms.twilio.auth_token"]})

    def test_nothing_inline_is_nothing_to_move(self):
        found, _ = _preview(CURRENT, {"move_to_file": ["sms.twilio.auth_token"]})
        assert found.blocked
        assert found.errors == ["sms.twilio.auth_token holds no value in config.toml to move"]

    def test_a_credential_whose_file_is_set_is_not_moved_over_it(self):
        found, _ = _preview(
            WITH_TOKEN, {"move_to_file": ["sms.twilio.auth_token"]},
            environ={"ISTOTA_SMS_TWILIO_AUTH_TOKEN": "from-the-file"},
        )
        assert found.blocked
        assert any(
            error.startswith("sms.twilio.auth_token is already set in istota_sms_twilio_auth_token")
            for error in found.errors
        ), found.errors

    def test_moving_a_risky_credential_is_risky(self):
        text = CURRENT + '\n[web]\noauth2_client_secret = "inline-client-secret"\n'
        found, _ = _preview(text, {"move_to_file": ["web.oauth2_client_secret"]})
        assert found.risky_keys == ["web.oauth2_client_secret"]


class TestMovingAReadOnlyCredential:
    """A move applies the same value from the secret file, so a credential the
    editor never changes may still be moved; a new value is still refused."""

    NEXTCLOUD = CURRENT + '\n[nextcloud]\napp_password = "NC-APP-PASSWORD-SENTINEL"\n'
    SESSION = CURRENT + '\n[web]\nsession_secret_key = "SESSION-KEY-SENTINEL-0123456789abcdef"\n'

    @pytest.mark.parametrize("text,label", [
        (NEXTCLOUD, "nextcloud.app_password"),
        (SESSION, "web.session_secret_key"),
    ])
    def test_a_move_is_not_refused_as_read_only(self, text, label):
        found, seen = _preview(text, {"move_to_file": [label]})
        assert found.errors == []
        assert label.split(".")[0] not in seen[0]
        assert found.moves[0]["label"] == label
        assert "SENTINEL" not in repr(found.as_json()) + found.document_toml

    def test_a_new_value_by_op_is_still_refused(self):
        found, _ = _preview(self.NEXTCLOUD, {
            "ops": [{"op": "set", "path": ["nextcloud", "app_password"], "value": "typed"}],
        })
        assert any(e.startswith("nextcloud.app_password is not edited here.") for e in found.errors)

    def test_a_raw_edit_is_still_refused(self):
        raw = CURRENT + '\n[nextcloud]\napp_password = "typed-in-raw"\n'
        found, _ = _preview(self.NEXTCLOUD, {"raw": raw})
        assert any(e.startswith("nextcloud.app_password is not edited here.") for e in found.errors)

    def test_a_raw_removal_is_still_refused(self):
        found, _ = _preview(self.NEXTCLOUD, {"raw": CURRENT})
        assert any(e.startswith("nextcloud.app_password is not edited here.") for e in found.errors)

    def test_a_new_file_value_is_still_refused(self):
        found, _ = _preview(self.SESSION, {"secrets": {"istota_web_session_secret_key": "y" * 40}})
        assert any("istota_web_session_secret_key" in error for error in found.errors)

    def test_an_op_on_the_moved_path_is_refused_with_the_move(self):
        with pytest.raises(ChangeError, match="nextcloud.app_password is both moved and replaced"):
            _preview(self.NEXTCLOUD, {
                "move_to_file": ["nextcloud.app_password"],
                "ops": [{"op": "set", "path": ["nextcloud", "app_password"], "value": "typed"}],
            })


class TestLoggableNames:
    def test_a_file_credential_is_its_name(self):
        assert edits.loggable_secret_name("istota_email_imap_password") == "istota_email_imap_password"

    def test_an_inline_credential_is_its_section(self):
        assert edits.loggable_secret_name('brain.native.extra_headers."X-Secret.Name"') == "brain.native (inline)"


class TestDates:
    def test_a_date_anywhere_in_the_new_document_is_refused_by_its_label(self):
        found, _ = _preview(CURRENT, {"raw": CURRENT + "\n[custom]\nwhen = 1979-05-27\n"})
        assert found.blocked
        assert any(error.startswith("custom.when holds a date or time value") for error in found.errors)

    def test_a_time_inside_an_array_is_named_by_its_index(self):
        import datetime

        assert edits.check_dates({"a": {"times": ["x", datetime.time(7, 30)]}}) == [
            "a.times.1 holds a date or time value, which the host applier cannot carry; "
            "change it on the host with `istota-stack apply`"
        ]

    def test_no_date_no_error(self):
        assert edits.check_dates({"email": {"imap_host": "h", "imap_port": 993}}) == []


class TestRestoreChangeSet:
    def test_a_credential_cleared_since_stays_cleared_and_is_named(self):
        stored = (
            '[email]\nimap_host = "older.example.org"\n\n'
            f'[brain.native.extra_headers]\nX-Harmless = "{SENTINEL}"\nX-Gone = "{SENTINEL}"\n'
        )
        change_set, warnings = edits.restore_change_set(stored, CURRENT, "d1")
        assert change_set["base_digest"] == "d1" and change_set["ops"] == []
        assert "X-Gone" not in change_set["raw"]
        assert [warning["message"] for warning in warnings] == [
            "brain.native.extra_headers.X-Gone was cleared after this change; it stays cleared",
        ]
        assert warnings[0]["path"] == ["brain", "native", "extra_headers", "X-Gone"]
        assert warnings[0]["blocking"] is False
        found, seen = _preview(CURRENT, change_set)
        assert not found.blocked
        assert seen[0]["brain"]["native"]["extra_headers"] == {"X-Harmless": "header-value-1"}
        assert seen[0]["email"]["imap_host"] == "older.example.org"

    def test_an_unreadable_stored_document_is_a_change_error(self):
        with pytest.raises(ChangeError):
            edits.restore_change_set("not = [toml", CURRENT, "d1")
