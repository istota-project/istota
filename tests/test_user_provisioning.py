"""Shared user provisioning through real read-only and caller-owned connections."""

from contextlib import closing, contextmanager
import sqlite3

import pytest

from istota import db, user_profiles
from istota.lib.sqlite_util import connect_read_only


@contextmanager
def _read_only(path):
    with closing(connect_read_only(path)) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "users.db"
    db.init_db(path)
    return path


def test_plan_reads_without_creating_profile_binding_or_locks(db_path):
    with _read_only(db_path) as conn:
        plan = user_profiles.plan_user_provisioning(
            conn, "alice", {"display_name": "Alice"}, mode="managed",
            whatsapp_op="set", whatsapp_number="+15551234567",
        )
        assert plan.profile.display_name == "Alice"
        assert plan.profile.timezone == "UTC"
        assert plan.asserted == {"display_name", "whatsapp_number"}
        assert user_profiles.get_profile(db_path, "alice", conn=conn) is None
        assert db.get_whatsapp_binding(conn, "alice") is None
        assert user_profiles.managed_fields(conn, "alice") == set()


def test_caller_owns_profile_binding_and_managed_transaction(db_path):
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = user_profiles.provision_user(
            db_path, "alice", {"display_name": "Alice"}, mode="managed",
            whatsapp_op="set", whatsapp_number="+15551234567", conn=conn,
        )
        assert result.state == "created"
        assert result.binding.bootstrap_phone_number == "+15551234567"
        assert user_profiles.managed_fields(conn, "alice") == {
            "display_name", "whatsapp_number",
        }
        conn.rollback()
    assert user_profiles.get_profile(db_path, "alice") is None
    with db.get_db(db_path) as conn:
        assert db.get_whatsapp_binding(conn, "alice") is None
        assert user_profiles.managed_fields(conn, "alice") == set()


def test_read_only_plan_checks_login_email_and_case_collisions(db_path):
    from istota.webui.auth import upsert_identity

    upsert_identity(db_path, "bob", email="bob@example.com", create_profile=True)
    with _read_only(db_path) as conn:
        with pytest.raises(user_profiles.ProvisioningRefused, match="already held"):
            user_profiles.plan_user_provisioning(
                conn, "alice", {"email_addresses": ["BOB@example.com"]},
            )
        with pytest.raises(user_profiles.ProvisioningRefused, match="case"):
            user_profiles.plan_user_provisioning(conn, "BOB", {})


def test_seed_plan_keeps_stored_values_and_execution_releases_locks(db_path):
    user_profiles.provision_user(
        db_path, "alice", {"display_name": "Stored"}, mode="managed",
    )
    updates = {"display_name": "Inventory", "sms_phone_number": "+15551234567"}
    with _read_only(db_path) as conn:
        plan = user_profiles.plan_user_provisioning(conn, "alice", updates, mode="seed")
        assert plan.profile.display_name == "Stored"
        assert plan.updates == {"sms_phone_number": "+15551234567"}
        assert plan.seed_kept == ["display_name"]
        assert user_profiles.managed_fields(conn, "alice") == {"display_name"}
    result = user_profiles.provision_user(db_path, "alice", updates, mode="seed")
    assert result.state == "updated"
    assert result.profile == plan.profile
    with db.get_db(db_path) as conn:
        assert user_profiles.managed_fields(conn, "alice") == set()


@pytest.mark.parametrize("enrollment", [
    {"whatsapp_op": "set", "whatsapp_number": "invalid"},
    {"whatsapp_op": "set", "whatsapp_number": ""},
    {"whatsapp_op": "reset"},
])
def test_read_only_plan_refuses_invalid_enrollment(db_path, enrollment):
    with _read_only(db_path) as conn:
        with pytest.raises(user_profiles.ProvisioningRefused):
            user_profiles.plan_user_provisioning(conn, "alice", {}, **enrollment)
        assert user_profiles.get_profile(db_path, "alice", conn=conn) is None


def test_execution_revalidates_identity_after_read_only_planning(db_path):
    with _read_only(db_path) as conn:
        user_profiles.plan_user_provisioning(
            conn, "alice", {"email_addresses": ["shared@example.com"]},
        )
    user_profiles.provision_user(db_path, "bob", {"email_addresses": ["shared@example.com"]})
    with pytest.raises(user_profiles.ProvisioningRefused, match="already held"):
        user_profiles.provision_user(db_path, "alice", {"email_addresses": ["shared@example.com"]})
    assert user_profiles.get_profile(db_path, "alice") is None


def test_owned_transaction_rolls_back_after_late_write_failure(db_path):
    with db.get_db(db_path) as conn:
        conn.execute("DROP TABLE user_profile_managed_fields")
    with pytest.raises(user_profiles.ProvisioningRefused, match="Nothing was written"):
        user_profiles.provision_user(
            db_path, "alice", {"display_name": "Alice"}, mode="managed",
            whatsapp_op="set", whatsapp_number="+15551234567",
        )
    assert user_profiles.get_profile(db_path, "alice") is None
    with db.get_db(db_path) as conn:
        assert db.get_whatsapp_binding(conn, "alice") is None


def test_plan_refuses_default_room_without_membership(db_path):
    with db.get_db(db_path) as conn:
        db.register_room(conn, "private-room", "bob", origin="web", name="Private")
    with _read_only(db_path) as conn:
        with pytest.raises(user_profiles.ProvisioningRefused, match="not a member"):
            user_profiles.plan_user_provisioning(conn, "alice", {"default_room": "private-room"})


def test_caller_connection_requires_transaction(db_path):
    with db.get_db(db_path) as conn:
        with pytest.raises(ValueError, match="caller-owned write transaction"):
            user_profiles.provision_user(db_path, "alice", {}, conn=conn)
