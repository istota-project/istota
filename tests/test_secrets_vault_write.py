"""Create-only vault writes against real KDBX files."""

import dataclasses
import fcntl
import os
import stat
import sys
import threading
from types import SimpleNamespace

import pytest
from pykeepass import create_database

from istota import db, secrets_store, secrets_vault
from istota.config import load_config
from istota.secrets_vault import (
    PasswordPolicy,
    VaultChanged,
    VaultWriteRefused,
    create_entry,
    generate_password,
    parse_vault,
    read_vault_bytes,
)
from istota.storage import VaultLocation


def _create(path, slug="example", *, username="bot@example.com", url="https://example.com", digest=None, lock_root=None):
    if digest is None:
        _, digest = read_vault_bytes(path)
    lock_root = lock_root or path.parent / "daemon-state"
    lock_root.mkdir(exist_ok=True)
    return create_entry(
        VaultLocation(path=path, dir_fd=None), "test-passphrase", slug=slug,
        username=username, password="new-value", url=url, expected_digest=digest,
        lock_root=lock_root,
    )


def test_create_preserves_unscoped_names(tmp_path):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    kp.add_entry(kp.root_group, "old", "alice", "old-value")
    kp.save()
    before, digest = read_vault_bytes(path)
    old = parse_vault(before, "test-passphrase")

    _create(path, digest=digest)

    after, _ = read_vault_bytes(path)
    read = parse_vault(after, "test-passphrase")
    assert read.scoped is False
    assert read.services["old"] == old.services["old"]
    assert read.services["generated_example"] == "new-value"
    assert read.services["generated_example_username"] == "bot@example.com"
    assert read.services["generated_example_url"] == "https://example.com"


def test_scoped_write_uses_existing_casefolded_generated_group(tmp_path):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    root = kp.add_group(kp.root_group, " Istota ")
    kp.add_group(root, " Generated ")
    kp.add_entry(root, "old", "", "old-value")
    kp.save()

    _create(path)

    read = parse_vault(read_vault_bytes(path)[0], "test-passphrase")
    assert read.scoped is True
    assert read.services["old"] == "old-value"
    from pykeepass import PyKeePass
    reopened = PyKeePass(str(path), password="test-passphrase")
    assert len(reopened.find_groups(name=" Generated ", first=False)) == 1


def test_collision_with_held_name_refuses_without_changing_bytes(tmp_path):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    generated = kp.add_group(kp.root_group, "generated")
    kp.add_entry(generated, "example", "", "")
    kp.save()
    before = path.read_bytes()

    with pytest.raises(VaultWriteRefused, match="already exists"):
        _create(path)
    assert path.read_bytes() == before


def test_collision_with_skipped_name_refuses(tmp_path, monkeypatch):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    original = secrets_vault.parse_vault

    def skipped(data, password):
        read = original(data, password)
        return dataclasses.replace(read, skipped=(("generated_example", "duplicate_name"),))

    monkeypatch.setattr(secrets_vault, "parse_vault", skipped)
    with pytest.raises(VaultWriteRefused, match="already exists"):
        _create(path)


def test_collision_with_duplicate_name_refuses(tmp_path):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    generated = kp.add_group(kp.root_group, "generated")
    kp.add_entry(generated, "example", "", "one")
    kp.add_entry(generated, "example", "", "two", force_creation=True)
    kp.save()
    before = path.read_bytes()
    with pytest.raises(VaultWriteRefused, match="already exists"):
        _create(path)
    assert path.read_bytes() == before


def test_truncated_read_and_uncanonical_slug_refuse(tmp_path, monkeypatch):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    original = secrets_vault.parse_vault

    def truncated(data, password):
        return dataclasses.replace(original(data, password), truncated="entry")

    monkeypatch.setattr(secrets_vault, "parse_vault", truncated)
    with pytest.raises(VaultWriteRefused, match="cap"):
        _create(path)
    with pytest.raises(VaultWriteRefused, match="slug"):
        _create(path, "example__other")


def test_entry_cap_and_unencodable_field_refuse(tmp_path, monkeypatch):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    kp.add_entry(kp.root_group, "old", "", "old-value")
    kp.save()
    before = path.read_bytes()
    monkeypatch.setattr(secrets_vault, "VAULT_MAX_ENTRIES", 1)
    with pytest.raises(VaultWriteRefused, match="entry cap"):
        _create(path)
    with pytest.raises(VaultWriteRefused, match="UTF-8"):
        _create(path, username="\ud800")
    assert path.read_bytes() == before


def test_surrounding_whitespace_refuses_before_writing(tmp_path):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    before = path.read_bytes()
    with pytest.raises(VaultWriteRefused, match="whitespace"):
        _create(path, username=" alice ")
    assert path.read_bytes() == before


def test_file_mode_and_owner_survive_replace(tmp_path):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    os.chmod(path, 0o640)
    before = path.stat()
    _create(path)
    after = path.stat()
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


def test_unpreservable_owner_refuses(tmp_path, monkeypatch):
    path = tmp_path / "file"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)

    def denied(*_):
        raise PermissionError()

    monkeypatch.setattr(secrets_vault.os, "fchown", denied)
    try:
        original = SimpleNamespace(st_uid=os.getuid() + 1, st_gid=os.getgid(), st_mode=0o600)
        with pytest.raises(VaultWriteRefused, match="owner"):
            secrets_vault._preserve_vault_metadata(fd, original)
    finally:
        os.close(fd)


def test_changed_digest_and_failed_temp_parse_leave_original_bytes(tmp_path, monkeypatch):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    _, old_digest = read_vault_bytes(path)
    kp.add_entry(kp.root_group, "another", "", "value")
    kp.save()
    current = path.read_bytes()
    with pytest.raises(VaultChanged):
        _create(path, digest=old_digest)
    assert path.read_bytes() == current

    original = secrets_vault.parse_vault
    calls = 0

    def fail_on_temp(data, password):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise secrets_vault.VaultCorrupt("test parse failure")
        return original(data, password)

    monkeypatch.setattr(secrets_vault, "parse_vault", fail_on_temp)
    with pytest.raises(secrets_vault.VaultCorrupt):
        _create(path)
    assert path.read_bytes() == current


def test_contended_lock_refuses(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    path = vault_dir / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    lock_root = tmp_path / "state"
    lock_root.mkdir()
    lock = secrets_vault._vault_lock_path(VaultLocation(path, None), lock_root)
    assert lock.parent == lock_root
    assert lock.parent != path.parent
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (vault_dir / ".istota-vault-lock").touch()
        with pytest.raises(VaultWriteRefused, match="holds the lock"):
            _create(path, lock_root=lock_root)
    finally:
        os.close(fd)


def test_password_policy_and_redacted_result(tmp_path):
    no_symbols = PasswordPolicy(length=8, require_symbols=False, allow_symbols=False)
    password = generate_password(no_symbols)
    assert len(password) == 8
    assert any(c.islower() for c in password)
    assert any(c.isupper() for c in password)
    assert any(c.isdigit() for c in password)
    assert password.isalnum()
    with pytest.raises(VaultWriteRefused):
        generate_password(PasswordPolicy(length=2))
    with pytest.raises(VaultWriteRefused):
        generate_password(PasswordPolicy(require_symbols=True, allow_symbols=False))

    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    result = _create(path)
    assert "new-value" not in repr(result)
    assert result.digest == read_vault_bytes(path)[1]


def test_password_is_never_logged(tmp_path, caplog):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    with caplog.at_level("DEBUG"):
        _create(path)
        with pytest.raises(VaultWriteRefused):
            _create(path)
    assert all("new-value" not in record.getMessage() for record in caplog.records)


def test_empty_optional_fields_are_held_in_the_new_entry(tmp_path):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    result = _create(path, username="", url="")
    read = parse_vault(path.read_bytes(), "test-passphrase")
    assert read.services[result.name] == "new-value"
    assert result.username_name in read.held
    assert result.url_name in read.held


def test_two_creates_can_retry_after_contention(tmp_path):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    errors = []

    def write(slug):
        for _ in range(100):
            try:
                _create(path, slug)
                return
            except (VaultWriteRefused, VaultChanged):
                threading.Event().wait(0.1)
        errors.append(slug)

    threads = [threading.Thread(target=write, args=(slug,)) for slug in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    read = parse_vault(read_vault_bytes(path)[0], "test-passphrase")
    assert "generated_one" in read.services
    assert "generated_two" in read.services


def test_duplicate_scoped_roots_refuse(tmp_path):
    path = tmp_path / "vault.kdbx"
    kp = create_database(str(path), password="test-passphrase")
    kp.add_group(kp.root_group, "istota")
    kp.add_group(kp.root_group, " Istota ")
    kp.save()
    before = path.read_bytes()
    with pytest.raises(VaultWriteRefused, match="ambiguous"):
        _create(path)
    assert path.read_bytes() == before


def test_dir_fd_selects_the_vault_parent(tmp_path):
    real = tmp_path / "real"
    wrong = tmp_path / "wrong"
    real.mkdir()
    wrong.mkdir()
    real_path = real / "vault.kdbx"
    wrong_path = wrong / "vault.kdbx"
    create_database(str(real_path), password="test-passphrase")
    create_database(str(wrong_path), password="test-passphrase")
    wrong_before = wrong_path.read_bytes()
    fd = os.open(real, os.O_RDONLY)
    try:
        _, digest = read_vault_bytes(real_path)
        create_entry(
            VaultLocation(path=wrong_path, dir_fd=fd), "test-passphrase",
            slug="example", username="bot@example.com", password="new-value",
            url="https://example.com", expected_digest=digest,
            lock_root=tmp_path,
        )
    finally:
        os.close(fd)
    assert wrong_path.read_bytes() == wrong_before
    assert "generated_example" in parse_vault(real_path.read_bytes(), "test-passphrase").services


def test_operator_cli_creates_and_applies_without_printing_password(tmp_path, monkeypatch, capsys):
    from istota.cli import main

    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir()
    db.init_db(db_path)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    secrets_store.set_secret(db_path, "alice", "vault", "passphrase", "test-passphrase")
    config = tmp_path / "config.toml"
    config.write_text(
        f'db_path = "{db_path}"\n'
        f'[users.alice]\nvault_path = "{path}"\n'
        'email_addresses = ["alice@example.com"]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "istota", "-c", str(config), "secret", "vault-new", "--user", "alice",
        "--slug", "example", "--url", "https://example.com", "--no-symbols",
    ])
    main()
    printed = capsys.readouterr()
    output = printed.out
    assert "generated_example" in output
    assert "may overwrite the new entry" in printed.err
    read = parse_vault(path.read_bytes(), "test-passphrase")
    assert read.services["generated_example_username"] == "alice@example.com"
    assert read.services["generated_example"] not in output
    assert secrets_store.get_secret(db_path, "alice", "vault_entries", "generated_example") == read.services["generated_example"]


def test_sync_holds_same_lock_as_create(tmp_path, monkeypatch):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir()
    db.init_db(db_path)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    secrets_store.set_secret(db_path, "alice", "vault", "passphrase", "test-passphrase")
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'db_path = "{db_path}"\n[users.alice]\nvault_path = "{path}"\n')
    config = load_config(config_file)
    entered = threading.Event()
    release = threading.Event()
    original = secrets_vault._sync_resolved

    def waiting_sync(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(secrets_vault, "_sync_resolved", waiting_sync)
    thread = threading.Thread(target=secrets_vault.sync_user, args=(config, "alice"), kwargs={"deliver": False})
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(VaultWriteRefused, match="holds the lock"):
            _create(path, lock_root=db_path.parent)
    finally:
        release.set()
        thread.join()
    _, digest = read_vault_bytes(path)
    result = create_entry(
        VaultLocation(path=path, dir_fd=None), "test-passphrase", slug="example",
        username="bot@example.com", password="new-value", url="https://example.com",
        expected_digest=digest, lock_root=db_path.parent, db_path=db_path, user_id="alice",
    )
    assert secrets_store.get_secret(db_path, "alice", "vault_entries", result.name) == "new-value"


def test_sync_returns_busy_without_applying_when_lock_is_held(tmp_path, monkeypatch):
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir()
    db.init_db(db_path)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    secrets_store.set_secret(db_path, "alice", "vault", "passphrase", "test-passphrase")
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'db_path = "{db_path}"\n[users.alice]\nvault_path = "{path}"\n')
    config = load_config(config_file)
    location = VaultLocation(path, None)
    lock_path = secrets_vault._vault_lock_path(location, db_path.parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(secrets_vault, "_VAULT_LOCK_WAIT_SECONDS", 0.1)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = secrets_vault.sync_user(config, "alice", deliver=False)
    finally:
        os.close(fd)
    assert result.outcome == secrets_vault.OUTCOME_BUSY
    assert secrets_store.get_secret(db_path, "alice", "vault_entries", "generated_example") is None
