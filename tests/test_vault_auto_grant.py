"""ISSUE-590: a KeePass entry the sync sees for the first time is granted.

The grant is made once per entry. A grant the user narrows or deletes stays
as they left it, an entry tagged ``istota:nogrant`` or created by a task under
``generated/`` is never granted, and an entry stored before auto-grant existed
is left with whatever grant it had. Nothing is granted from a file with no
``istota`` group, and an entry deleted and restored keeps the decision made
the first time.
"""

import pytest

from istota import db, doctor
from istota.credentials import store as secrets_store
from istota.credentials import vault as secrets_vault
from istota.config import Config
from istota.credentials.broker import grants
from istota.credentials.broker.bindings import parse_binding
from tests.test_secrets_vault import _new_db, _read


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = tmp_path / "data.db"
    db.init_db(path)
    return path


def _vault(tmp_path):
    kp, path = _new_db(tmp_path)
    return kp, path, kp.add_group(kp.root_group, "istota")


def _sync(database, path):
    read, _ = _read(path)
    return secrets_vault.apply_vault(database, "alice", read)


def _grant(database, name):
    with db.get_db(database) as conn:
        return grants.get_grant(conn, "alice", name)


def test_a_new_entry_with_a_url_is_granted(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    grant = _grant(database, "nebula")
    assert grant is not None
    assert grant["scope_mode"] == "all"
    assert grant["allow_scheduled"] is True
    # The field rows share the entry's grant rather than getting their own.
    assert _grant(database, "nebula_username") == grant


def test_an_entry_without_a_url_is_granted_once_it_gets_one(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    entry = kp.add_entry(group, "nebula", "alice", "fixture-password")
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None
    entry.url = "https://nebula.example"
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is not None


def test_a_deleted_or_narrowed_grant_does_not_come_back(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    entry = kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    other = kp.add_entry(group, "pulsar", "alice", "fixture-password", url="https://pulsar.example")
    kp.save()
    _sync(database, path)
    with db.get_db(database) as conn:
        grants.delete_grant(conn, "alice", "nebula")
        narrowed = grants.put_grant(conn, "alice", "pulsar", scope_mode="rooms", rooms=["room-a"])
    entry.password = "changed-password"
    other.password = "changed-password"
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None
    assert _grant(database, "pulsar") == narrowed


def test_a_nogrant_tag_keeps_the_entry_ungranted_until_it_is_removed(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    entry = kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    entry.tags = ["istota:nogrant"]
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None
    entry.tags = []
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is not None


def test_an_entry_a_task_created_under_generated_is_not_granted(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    generated = kp.add_group(group, "Generated")
    kp.add_entry(generated, "signup", "alice", "fixture-password", url="https://signup.example")
    kp.save()
    _sync(database, path)
    assert _grant(database, "generated_signup") is None


def test_an_entry_stored_before_auto_grant_is_left_alone(tmp_path, database):
    binding = parse_binding("https://nebula.example", {}, [])
    secrets_store.upsert_secret(database, "alice", "vault_entries", "nebula", "fixture-password",
                                binding=dict(binding, credential="nebula"))
    kp, path, group = _vault(tmp_path)
    kp.add_entry(group, "nebula", "", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None


def test_an_entry_removed_and_added_again_keeps_its_first_decision(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    entry = kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    with db.get_db(database) as conn:
        grants.delete_grant(conn, "alice", "nebula")
    kp.delete_entry(entry)
    kp.save()
    _sync(database, path)
    kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None


def test_restoring_an_old_copy_of_the_file_does_not_widen_a_narrowed_grant(tmp_path, database):
    # A task can delete and rewrite the file, though it cannot open it.
    kp, path, group = _vault(tmp_path)
    entry = kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    with db.get_db(database) as conn:
        grants.put_grant(conn, "alice", "nebula", scope_mode="rooms", rooms=["room-a"])
    kept = path.read_bytes()
    kp.delete_entry(entry)
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None
    path.write_bytes(kept)
    _sync(database, path)
    assert _grant(database, "nebula") is None


def test_a_file_with_no_istota_group_grants_nothing_until_it_is_scoped(tmp_path, database):
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None
    group = kp.add_group(kp.root_group, "istota")
    kp.move_entry(entry, group)
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is not None


def test_a_pass_that_fails_before_granting_is_retried(tmp_path, database, monkeypatch):
    kp, path, group = _vault(tmp_path)
    kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    original = grants.auto_grant_vault_entries

    def fail(*args, **kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(grants, "auto_grant_vault_entries", fail)
    with pytest.raises(RuntimeError):
        _sync(database, path)
    monkeypatch.setattr(grants, "auto_grant_vault_entries", original)
    _sync(database, path)
    assert _grant(database, "nebula") is not None


def test_grant_existing_skips_an_opted_out_entry(tmp_path, database):
    kp, path, group = _vault(tmp_path)
    entry = kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    entry.tags = ["istota:nogrant"]
    kp.save()
    _sync(database, path)
    with db.get_db(database) as conn:
        assert grants.grant_what_exists(conn, "alice") == 0
    assert _grant(database, "nebula") is None


def test_a_name_held_by_a_local_credential_is_not_granted(tmp_path, database):
    binding = parse_binding("https://local.example", {}, [], source="local")
    secrets_store.upsert_secret(database, "alice", "vault_entries", "nebula", "typed-password",
                                binding=dict(binding, credential="nebula"))
    kp, path, group = _vault(tmp_path)
    kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    kp.save()
    _sync(database, path)
    assert _grant(database, "nebula") is None


def test_doctor_counts_neither_auto_granted_nor_opted_out_entries(tmp_path, database, monkeypatch):
    config = Config(db_path=database, temp_dir=tmp_path / "temp")
    config.security.credential_broker.enabled = True
    config.security.network.enabled = True
    monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda *args: (True, ""))
    kp, path, group = _vault(tmp_path)
    kp.add_entry(group, "nebula", "alice", "fixture-password", url="https://nebula.example")
    opted_out = kp.add_entry(group, "pulsar", "alice", "fixture-password", url="https://pulsar.example")
    opted_out.tags = ["istota:nogrant"]
    kp.save()
    _sync(database, path)
    results = {r.name: r for r in doctor.check_credential_broker(config, False)}
    bindings = results["security.credential_broker.bindings"]
    assert bindings.status == doctor.OK, bindings.detail
