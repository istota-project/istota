"""Who may write which profile field, and who already holds an identity.

The authority tuples in `user_profiles` are the one list the self PUT, the admin
PATCH and `istota user ensure` check against. `find_identity_conflicts` is the
uniqueness rule all three apply to an email address, an SMS number and a
WhatsApp number.
"""

from __future__ import annotations

import sqlite3

import pytest

from istota import db, user_profiles
from istota.user_profiles import (
    ADMIN_EDITABLE_FIELDS,
    SELF_EDITABLE_FIELDS,
    _PROFILE_COLUMNS,
    duplicate_email_addresses,
    find_identity_conflicts,
)
from istota.webui import auth as web_auth


class TestTheAuthorityTables:
    def test_admin_fields_are_profile_columns(self):
        assert set(ADMIN_EDITABLE_FIELDS) <= set(_PROFILE_COLUMNS)

    def test_self_fields_are_profile_columns(self):
        assert set(SELF_EDITABLE_FIELDS) <= set(_PROFILE_COLUMNS)

    @pytest.mark.parametrize(
        "field", ["max_foreground_workers", "max_background_workers"],
    )
    def test_a_user_cannot_raise_their_own_worker_cap(self, field):
        assert field not in SELF_EDITABLE_FIELDS
        assert field in ADMIN_EDITABLE_FIELDS

    @pytest.mark.parametrize("field", ["sms_phone_number", "default_briefings"])
    def test_operator_bound_fields_are_admin_only(self, field):
        assert field not in SELF_EDITABLE_FIELDS
        assert field in ADMIN_EDITABLE_FIELDS

    def test_the_self_put_accepts_exactly_the_self_set(self):
        pytest.importorskip("fastapi")
        from istota.webui import app as web_app

        assert set(web_app._PROFILE_EDITABLE_FIELDS) == set(SELF_EDITABLE_FIELDS)

    def test_every_self_field_has_a_coercer(self):
        pytest.importorskip("fastapi")
        from istota.webui import app as web_app

        for field in SELF_EDITABLE_FIELDS:
            assert field in web_app._PROFILE_FIELD_SPECS, field


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _profile(db_path, user_id, **fields):
    user_profiles.ensure_profile(db_path, user_id)
    if fields:
        user_profiles.update_profile(db_path, user_id, **fields)


def _conflicts(db_path, user_id, **kwargs):
    with db.get_db(db_path) as conn:
        return find_identity_conflicts(conn, user_id, **kwargs)


class TestEmailConflicts:
    def test_another_users_address_is_a_conflict_naming_them(self, db_path):
        _profile(db_path, "alice")
        _profile(db_path, "bob", email_addresses=["bob@example.com"])
        assert _conflicts(
            db_path, "alice", email_addresses=["bob@example.com"],
        ) == {"bob@example.com": "bob"}

    def test_the_comparison_is_case_folded(self, db_path):
        _profile(db_path, "alice")
        _profile(db_path, "bob", email_addresses=["Bob@Example.com"])
        assert _conflicts(
            db_path, "alice", email_addresses=[" bob@EXAMPLE.com"],
        ) == {" bob@EXAMPLE.com": "bob"}

    def test_another_users_login_email_is_a_conflict(self, db_path):
        _profile(db_path, "alice")
        _profile(db_path, "bob")
        web_auth.upsert_identity(db_path, "bob", "bob-login@example.com")
        assert _conflicts(
            db_path, "alice", email_addresses=["bob-login@example.com"],
        ) == {"bob-login@example.com": "bob"}

    def test_a_users_own_login_email_is_not_a_conflict(self, db_path):
        _profile(db_path, "alice")
        web_auth.upsert_identity(db_path, "alice", "alice@example.com")
        assert _conflicts(
            db_path, "alice", email_addresses=["alice@example.com"],
        ) == {}

    def test_an_already_stored_duplicate_resubmitted_passes(self, db_path):
        # A deployment may hold a duplicate from before the rule; refusing the
        # resubmit would make every unrelated save by either holder fail.
        _profile(db_path, "alice", email_addresses=["shared@example.com"])
        _profile(db_path, "bob", email_addresses=["shared@example.com"])
        assert _conflicts(
            db_path, "alice",
            email_addresses=["shared@example.com", "new@example.com"],
        ) == {}

    def test_an_unknown_address_is_free(self, db_path):
        _profile(db_path, "alice")
        _profile(db_path, "bob", email_addresses=["bob@example.com"])
        assert _conflicts(
            db_path, "alice", email_addresses=["alice@example.com", ""],
        ) == {}


class TestPhoneConflicts:
    def test_another_users_sms_number_names_them(self, db_path):
        _profile(db_path, "alice")
        _profile(db_path, "bob", sms_phone_number="+15550100001")
        assert _conflicts(db_path, "alice", sms="+15550100001") == {
            "+15550100001": "bob",
        }

    def test_a_users_own_sms_number_is_not_a_conflict(self, db_path):
        _profile(db_path, "alice", sms_phone_number="+15550100001")
        assert _conflicts(db_path, "alice", sms="+15550100001") == {}

    def test_another_users_whatsapp_number_names_them(self, db_path):
        _profile(db_path, "alice")
        _profile(db_path, "bob")
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(
                conn, "bob", bootstrap_phone_number="+15550100002",
            )
        assert _conflicts(db_path, "alice", whatsapp="+15550100002") == {
            "+15550100002": "bob",
        }
        assert _conflicts(db_path, "bob", whatsapp="+15550100002") == {}

    def test_empty_values_are_never_conflicts(self, db_path):
        _profile(db_path, "alice")
        assert _conflicts(db_path, "alice", sms="", whatsapp="") == {}

    def test_a_missing_whatsapp_table_reads_as_no_holder(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE user_profiles (user_id TEXT, email_addresses TEXT, "
            "sms_phone_number TEXT)"
        )
        assert find_identity_conflicts(
            conn, "alice", email_addresses=["a@example.com"],
            whatsapp="+15550100002",
        ) == {}


class TestDuplicateAddresses:
    def test_a_clean_database_has_none(self, db_path):
        _profile(db_path, "alice", email_addresses=["alice@example.com"])
        web_auth.upsert_identity(db_path, "alice", "alice@example.com")
        with db.get_db(db_path) as conn:
            assert duplicate_email_addresses(conn) == {}

    def test_lists_every_holder_of_a_shared_address(self, db_path):
        _profile(db_path, "alice", email_addresses=["Shared@example.com"])
        _profile(db_path, "bob", email_addresses=["shared@example.com"])
        _profile(db_path, "carol")
        web_auth.upsert_identity(db_path, "carol", "shared@example.com")
        with db.get_db(db_path) as conn:
            assert duplicate_email_addresses(conn) == {
                "shared@example.com": ["alice", "bob", "carol (login)"],
            }
