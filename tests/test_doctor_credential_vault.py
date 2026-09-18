"""`security.credential_vault`: what doctor says about a configured KDBX vault.

Its own file rather than a class in `tests/test_doctor.py`, because the fixtures
are the vault's — a real KDBX built by `pykeepass` in `tmp_path`, a real temp
database with a real `ISTOTA_SECRET_KEY` — and that file drives fabricated
binaries instead.

Two properties here are about a boundary rather than about a verdict, and both
would pass against an implementation that had neither.

**The library is asked for with `find_spec` and never imported.** The check runs
on the daemon's boot-path registry sweep, and an import there pulls `lxml`,
`argon2-cffi` and `pycryptodomex` into the daemon whether or not any user has a
vault. A check that imported directly would answer *correctly* on this machine,
where the library is installed, so the happy path cannot tell the two apart:
`test_the_library_is_not_imported_to_answer_for_it` patches `find_spec` to
answer `None` while the real library sits in the venv, which only a `find_spec`
implementation can honour.

**Nothing the probe arm prints is a credential key name.** A `CheckResult` is
rendered into the daemon's boot log *and* into the admin Health pane, so a
detail line enumerating one user's keys is read by every admin. The sweep is
over the whole result rather than over named fields, so a field added later is
covered without the sweep changing.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from istota import db, doctor, secrets_store
from istota.config import UserConfig
from istota.doctor import FAIL, OK, SKIP, WARN

PASSPHRASE = "doctor-vault-fixture-passphrase-not-a-real-one"

# Distinctive, so the no-key-names sweep below is unambiguous about what it
# found. Nothing in this file is a credential.
API_KEY_VALUE = "ak-doctor-vault-fixture"
TOPIC_VALUE = "ntfy-doctor-fixture-topic"

#: A group nobody owns, whose name is written *into the vault file*. It is the
#: only string in the fixture that a task inside the user's sandbox could choose
#: — §12 records that such a task can overwrite the file — so it is what makes
#: the sweep below about a reachable route rather than about three strings the
#: report structurally cannot carry.
UNOWNED_GROUP = "zz-unowned-group-marker"


def _by_name(results):
    return {r.name: r for r in results}


def _write_vault(path: Path, *, password: str = PASSPHRASE) -> Path:
    """`istota/karakeep/api_key`, `istota/ntfy/topic`, and one unowned group.

    The unowned group is not decoration: it is the file-controlled name the
    sweep below is really about.

    The library import is inside the helper for the rule the module under test
    follows: nothing in this repository pays `pykeepass`'s import graph at
    collection.
    """
    from pykeepass import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    kp.add_entry(kp.add_group(root, "karakeep"), "api_key", "", API_KEY_VALUE)
    kp.add_entry(kp.add_group(root, "ntfy"), "topic", "", TOPIC_VALUE)
    kp.add_entry(kp.add_group(root, UNOWNED_GROUP), "whatever", "", "unused")
    kp.save()
    return path


@pytest.fixture
def secret_key_env():
    """A real `ISTOTA_SECRET_KEY`, so the passphrase round-trips for real."""
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


@pytest.fixture
def vault_config(make_config, tmp_path):
    """A config with one user whose vault is configured, and the vault beside it.

    Relative form, which is the default and the one the descriptor covers: the
    file lands under `{workspace}/Users/alice/config/vault.kdbx` and
    `vault_path` names it the way `config.toml` would.
    """
    database = tmp_path / "vault-doctor.db"
    db.init_db(database)
    config = make_config(
        db_path=database,
        users={
            "alice": UserConfig(
                display_name="Alice",
                vault_path="config/vault.kdbx",
                vault_services=["karakeep", "ntfy"],
            )
        },
    )
    _write_vault(
        Path(config.workspace_path) / "Users" / "alice" / "config" / "vault.kdbx"
    )
    return config


def _provision(config, user_id="alice", value=PASSPHRASE):
    secrets_store.upsert_secret(config.db_path, user_id, "vault", "passphrase", value)


def _run(config, probe=True):
    return _by_name(doctor.check_credential_vault(config, probe))


class TestWhenNoVaultIsConfigured:
    """The default on every deployment, so it must cost nothing and say so."""

    def test_a_deployment_with_no_vault_gets_one_skip(self, make_config):
        results = doctor.check_credential_vault(make_config(), True)
        assert len(results) == 1
        assert results[0].name == "security.credential_vault"
        assert results[0].status == SKIP

    def test_empty_is_off_and_blank_but_present_is_configured(self, make_config):
        """The resolver's own line, and the split matters in both directions.

        An empty `vault_path` is the feature being off for that user, which is
        every user by default and must stay silent. `vault_path = "  "` is a
        configured value that resolves to nothing: the sync refuses it and logs
        a `VAULT_PATH_NOT_A_FILENAME` warning every cycle, so a doctor that
        skipped it would be the one surface an operator reads staying quiet
        about the line the daemon is complaining about hourly.
        """
        config = make_config(
            users={
                "alice": UserConfig(display_name="A", vault_path=""),
                "bob": UserConfig(display_name="B", vault_path="   "),
            }
        )
        assert doctor._vault_users(config) == ["bob"]

        result = _run(config, probe=False)["security.credential_vault.path"]
        assert result.status == FAIL
        assert "bob" in result.detail

    def test_it_opens_nothing_for_an_unconfigured_deployment(
        self, make_config, monkeypatch
    ):
        """The skip is ahead of every import and every filesystem touch, which
        is what makes it free on the boot path of the deployments that have no
        vault — which is all of them by default."""
        monkeypatch.setattr(
            doctor, "_vault_library_available", _must_not_be_called("find_spec")
        )
        assert doctor.check_credential_vault(make_config(), True)[0].status == SKIP


def _must_not_be_called(what):
    def _raise(*_a, **_k):
        raise AssertionError(f"{what} was reached for an unconfigured deployment")

    return _raise


class TestTheLibraryArm:
    def test_a_present_library_is_ok(self, vault_config):
        assert _run(vault_config)["security.credential_vault.library"].status == OK

    def test_an_absent_library_fails_with_an_operator_remedy(
        self, vault_config, monkeypatch
    ):
        monkeypatch.setattr(doctor, "_vault_library_available", lambda: False)
        result = _run(vault_config)["security.credential_vault.library"]
        assert result.status == FAIL
        assert "vault" in result.remedy

    def test_the_library_is_not_imported_to_answer_for_it(self, monkeypatch):
        """The discriminating assertion, and without it the property is
        invisible: an implementation that did `import pykeepass` answers
        correctly on this machine, where the library is installed, so every
        happy-path assertion passes either way.

        Patching `find_spec` to answer `None` while the real library sits in the
        venv is a state only a `find_spec` implementation can report — a direct
        import would still succeed and come back True.
        """
        real = importlib.util.find_spec

        def _hidden(name, *args, **kwargs):
            if name == "pykeepass":
                return None
            return real(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", _hidden)
        assert importlib.import_module("pykeepass") is not None
        assert doctor._vault_library_available() is False

    def test_a_finder_that_raises_reads_as_absent(self, monkeypatch):
        """A broken or shadowed distribution raises out of the path finders
        rather than answering None. Unimportable either way, and a check never
        raises."""

        def _boom(name, *args, **kwargs):
            raise ValueError("broken distribution")

        monkeypatch.setattr(importlib.util, "find_spec", _boom)
        assert doctor._vault_library_available() is False

    def test_the_contents_arm_skips_without_the_library(
        self, vault_config, secret_key_env, monkeypatch
    ):
        _provision(vault_config)
        monkeypatch.setattr(doctor, "_vault_library_available", lambda: False)
        result = _run(vault_config)["security.credential_vault.contents"]
        assert result.status == SKIP
        assert "library" in result.detail


class TestTheScheduleArm:
    def test_a_scheduled_deployment_names_the_interval_and_the_owned_set(
        self, vault_config
    ):
        result = _run(vault_config)["security.credential_vault.schedule"]
        assert result.status == OK
        assert "300" in result.detail
        assert "karakeep" in result.detail and "ntfy" in result.detail

    def test_a_zeroed_interval_warns_and_says_it_may_be_deliberate(
        self, vault_config
    ):
        """`vault_sync_interval = 0` is the documented off-switch, and the same
        predicate gates the startup pass — so a configured vault is applied by
        nothing. An operator who chose it must not be sent hunting a defect,
        which is why the remedy names the manual route first."""
        vault_config.scheduler.vault_sync_interval = 0
        result = _run(vault_config)["security.credential_vault.schedule"]
        assert result.status == WARN
        assert "vault-sync" in result.remedy

    def test_an_empty_owned_set_is_reported_rather_than_flagged(self, make_config):
        """Reading the file and applying nothing is the documented dry run."""
        config = make_config(
            users={"alice": UserConfig(display_name="A", vault_path="v.kdbx")}
        )
        result = _run(config)["security.credential_vault.schedule"]
        assert result.status == OK
        assert "owns nothing" in result.detail

    def test_an_ineligible_entry_warns_if_one_ever_reaches_here(self, make_config):
        """The backstop arm. `config._validate_vault_services` drops an
        ineligible name at load, so nothing reachable through `load_config`
        arrives with one — this is built by hand, which is the only way to
        exercise it and is why the arm is labelled defence in depth rather than
        presented as a case with a remedy behind it."""
        config = make_config(
            users={
                "alice": UserConfig(
                    display_name="A",
                    vault_path="v.kdbx",
                    vault_services=["karakeep", "monarch"],
                )
            }
        )
        result = _run(config)["security.credential_vault.schedule"]
        assert result.status == WARN
        assert "monarch" in result.detail


class TestThePathArm:
    def test_a_resolvable_vault_is_ok(self, vault_config):
        assert _run(vault_config)["security.credential_vault.path"].status == OK

    def test_a_refused_path_fails_and_carries_the_reason_id(self, vault_config):
        """The one FAIL in this arm, and the cross-user typo is why. An absolute
        `vault_path` under another user's workspace is refused by the resolver,
        produces no error on any other surface, and is the single case in this
        design about an attack rather than a mistake. The reason id is the same
        stable word the daemon log carries for every refused cycle."""
        from istota import storage

        other = Path(vault_config.workspace_path) / "Users" / "bob" / "v.kdbx"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"not a kdbx")
        vault_config.users["alice"].vault_path = str(other)

        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == FAIL
        assert storage.VAULT_PATH_INSIDE_WORKSPACE in result.detail

    def test_an_absent_file_warns_rather_than_failing(self, vault_config):
        """The credentials already in the table keep working and the user's own
        notification reaches them, so this is not the status that pages an
        operator at every boot."""
        vault_config.users["alice"].vault_path = "config/missing.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "nothing at the path" in result.detail

    def test_a_zero_byte_file_is_named_as_a_mid_write(self, vault_config):
        path = Path(vault_config.workspace_path) / "Users" / "alice" / "config"
        (path / "empty.kdbx").write_bytes(b"")
        vault_config.users["alice"].vault_path = "config/empty.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "zero bytes" in result.detail

    def test_a_fifo_is_reported_without_blocking(self, vault_config):
        """`stat` rather than the read `read_vault_bytes` performs, and this is
        the case that decides it: `open(2)` on a FIFO blocks until somebody
        writes, on an arm that runs unattended with no timeout behind it. A
        `stat` answers immediately. The test hangs rather than failing if that
        ever stops being true, which is the production failure exactly."""
        path = Path(vault_config.workspace_path) / "Users" / "alice" / "config"
        os.mkfifo(path / "pipe.kdbx")
        vault_config.users["alice"].vault_path = "config/pipe.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "not a regular file" in result.detail

    def test_an_oversize_file_is_reported_against_the_cap(self, vault_config):
        from istota.secrets_vault import VAULT_READ_CAP_BYTES

        path = (
            Path(vault_config.workspace_path)
            / "Users" / "alice" / "config" / "big.kdbx"
        )
        with path.open("wb") as fh:
            fh.truncate(VAULT_READ_CAP_BYTES + 1)
        vault_config.users["alice"].vault_path = "config/big.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "read cap" in result.detail

    def test_a_symlinked_leaf_is_reported_rather_than_followed(self, vault_config):
        """The reader refuses a symlinked leaf with `O_NOFOLLOW`, so following
        one here would report a file the sync will never open."""
        base = Path(vault_config.workspace_path) / "Users" / "alice" / "config"
        (base / "link.kdbx").symlink_to(base / "vault.kdbx")
        vault_config.users["alice"].vault_path = "config/link.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "symlink" in result.detail

    def test_the_descriptor_is_closed_on_every_path(self, vault_config, monkeypatch):
        """A gate running per user per sweep leaks one descriptor a cycle
        otherwise, which ends as a daemon that cannot open a socket days later
        with nothing pointing back here. Recorded rather than probed: an fd
        number is recyclable, so checking one is closed by trying to use it
        tests whichever fd the allocator handed out next."""
        closed = []
        real_close = os.close
        monkeypatch.setattr(
            os, "close", lambda fd: (closed.append(fd), real_close(fd))[1]
        )
        opened = []
        real_stat = os.stat

        def _record(path, *args, dir_fd=None, **kwargs):
            if dir_fd is not None:
                opened.append(dir_fd)
            return real_stat(path, *args, dir_fd=dir_fd, **kwargs)

        monkeypatch.setattr(os, "stat", _record)
        _run(vault_config, probe=False)

        assert opened, "the relative form handed over no descriptor to close"
        assert set(opened) <= set(closed)

    def test_an_absolute_path_under_the_database_directory_warns(
        self, vault_config, tmp_path
    ):
        """§1 refuses an absolute `vault_path` under every tree the sandbox
        binds read-write and deliberately leaves `db_path.parent` out of that
        list: the framework database is not a KDBX and comes back as a corrupt
        vault, so it is pointless rather than dangerous. An operator who has
        aimed the vault reader at their own database should be told, rather than
        left reading a notification about a corrupt file."""
        aimed = vault_config.db_path.parent / "istota.db"
        aimed.write_bytes(b"not a kdbx either")
        vault_config.users["alice"].vault_path = str(aimed)

        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "database" in result.detail
        assert "alice" in result.detail

    def test_a_relative_path_under_the_database_directory_does_not_warn(
        self, make_config, tmp_path
    ):
        """The control, and it is the case the naive test breaks. On the
        standalone shape the workspace, the temp dir and `db_path` all live
        under one directory, so an unconditional containment test WARNs about
        every ordinary relative vault there. The descriptor is the
        discriminator: the relative form has one and the absolute form does
        not."""
        shared = tmp_path / "istota-home"
        (shared / "Users" / "alice" / "config").mkdir(parents=True)
        database = shared / "istota.db"
        db.init_db(database)
        config = make_config(
            db_path=database,
            workspace_path=shared,
            nextcloud_mount_path=shared,
            users={
                "alice": UserConfig(
                    display_name="A", vault_path="config/vault.kdbx"
                )
            },
        )
        _write_vault(shared / "Users" / "alice" / "config" / "vault.kdbx")

        result = _run(config, probe=False)["security.credential_vault.path"]
        assert result.status == OK


class TestThePassphraseArm:
    def test_a_provisioned_passphrase_is_ok(self, vault_config, secret_key_env):
        _provision(vault_config)
        assert _run(vault_config)["security.credential_vault.passphrase"].status == OK

    def test_an_absent_passphrase_fails_and_names_the_generate_flag(
        self, vault_config
    ):
        result = _run(vault_config)["security.credential_vault.passphrase"]
        assert result.status == FAIL
        assert "--generate" in result.remedy

    def test_it_asks_presence_and_never_decrypts(
        self, vault_config, secret_key_env, monkeypatch
    ):
        """`secret_exists`, never `get_secret`. Presence is the whole question,
        and a decrypt would put the passphrase in this process for a diagnostic
        and stamp `last_accessed_at` on the row besides."""
        _provision(vault_config)
        monkeypatch.setattr(
            secrets_store,
            "get_secret",
            _must_not_be_called("secrets_store.get_secret"),
        )
        assert _run(vault_config, probe=False)[
            "security.credential_vault.passphrase"
        ].status == OK

    def test_an_unreadable_secrets_table_warns_rather_than_raising(
        self, vault_config, monkeypatch
    ):
        def _boom(*_a, **_k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(secrets_store, "secret_exists", _boom)
        result = _run(vault_config, probe=False)[
            "security.credential_vault.passphrase"
        ]
        assert result.status == WARN
        assert "alice" in result.detail


class TestTheContentsArm:
    def test_probe_false_opens_nothing(self, vault_config, secret_key_env):
        _provision(vault_config)
        result = _run(vault_config, probe=False)[
            "security.credential_vault.contents"
        ]
        assert result.status == SKIP
        assert "probing is disabled" in result.detail

    def test_probe_reports_counts_for_a_working_vault(
        self, vault_config, secret_key_env
    ):
        _provision(vault_config)
        result = _run(vault_config)["security.credential_vault.contents"]
        assert result.status == OK
        assert "2 of 2 owned group(s) present" in result.detail
        assert "2 key(s)" in result.detail

    def test_a_wrong_passphrase_reports_the_class_and_not_the_sentence(
        self, vault_config, secret_key_env
    ):
        """The class is a stable word the operator can grep; its human sentence
        is the notification's job and carries a remedy aimed at the user."""
        _provision(vault_config, value="the-wrong-passphrase-entirely-and-then-some")
        result = _run(vault_config)["security.credential_vault.contents"]
        assert result.status == WARN
        assert "VaultLocked" in result.detail
        assert "vault-status" in result.remedy

    def test_a_raise_out_of_the_read_is_contained(
        self, vault_config, secret_key_env, monkeypatch
    ):
        from istota import secrets_vault

        def _boom(*_a, **_k):
            raise RuntimeError("unexpected")

        _provision(vault_config)
        monkeypatch.setattr(secrets_vault, "vault_status", _boom)
        result = _run(vault_config)["security.credential_vault.contents"]
        assert result.status == WARN
        assert "RuntimeError" in result.detail

    def test_no_result_carries_a_name_or_a_value_out_of_the_file(
        self, vault_config, secret_key_env
    ):
        """§3 makes service and key names loggable in the *sync* log, and a
        `CheckResult` is a different surface: it is rendered into the daemon's
        boot log and into the admin Health pane, so a detail line enumerating
        one user's credential keys is read by every admin.

        Swept over the whole result rather than over named fields, so a field
        added later is covered without this changing.

        **What is in the sweep and why the obvious members are the weak half.**
        `VaultStatusReport` carries no key names at all — `key_counts` is a
        count per service — and carries no values, so the three fixture strings
        below are absent by the shape of the type rather than by this arm's
        discretion, and a mutation cannot reach them. `UNOWNED_GROUP` is the
        member that makes this test discriminating: it is a name written into
        the *file*, it reaches the report through `groups` and `key_counts`, and
        §12 records that a task in that user's own sandbox can write it. Printing
        what the file turned out to hold turns this red on that member alone.

        `karakeep` and `ntfy` are deliberately not swept for: those are operator
        config, and `…schedule` reports them on purpose.
        """
        _provision(vault_config)
        rendered = " ".join(
            f"{r.name} {r.status} {r.detail} {r.remedy}"
            for r in doctor.check_credential_vault(vault_config, True)
        )
        for leaked in (
            UNOWNED_GROUP,
            API_KEY_VALUE,
            TOPIC_VALUE,
            PASSPHRASE,
            "api_key",
            "topic",
        ):
            assert leaked not in rendered, f"{leaked!r} reached a CheckResult"


class TestTheRegistryContract:
    """What the layers above doctor read off this check."""

    def test_every_result_is_named_under_the_registry_entry(self, vault_config):
        for result in doctor.check_credential_vault(vault_config, True):
            assert result.name == "security.credential_vault" or result.name.startswith(
                "security.credential_vault."
            )

    def test_every_result_carries_the_registry_scope(self, vault_config):
        expected = doctor.CHECK_SCOPES["security.credential_vault"]
        for result in doctor.check_credential_vault(vault_config, True):
            assert result.scope == expected

    def test_every_warn_and_fail_carries_a_remedy(self, vault_config):
        """doctor's own rule: a finding an operator cannot act on is a log line,
        not a check. Driven over a config broken in every arm at once."""
        vault_config.users["alice"].vault_path = "config/missing.kdbx"
        vault_config.scheduler.vault_sync_interval = 0
        for result in doctor.check_credential_vault(vault_config, True):
            if result.status in (WARN, FAIL):
                assert result.remedy, f"{result.name} has no remedy"

    def test_the_check_runs_through_the_registry(self, vault_config, secret_key_env):
        _provision(vault_config)
        results = doctor.run_checks(
            vault_config, only=("security.credential_vault",), probe=False
        )
        assert {r.name for r in results} == {
            "security.credential_vault.library",
            "security.credential_vault.schedule",
            "security.credential_vault.path",
            "security.credential_vault.passphrase",
            "security.credential_vault.contents",
        }


class TestTheUserLabel:
    def test_a_user_id_cannot_forge_a_line_of_the_report(self):
        """The ids come from `config.toml` rather than from the vault file, so
        this bounds a log line rather than defending a boundary — but the same
        result is rendered into the admin Health pane, and a newline in a TOML
        key would forge a row of it."""
        label = doctor._vault_label("alice\nSTATUS ok everything is fine" + "x" * 200)
        assert "\n" not in label
        assert len(label) <= 65


def test_the_fifo_helper_is_available():
    """`os.mkfifo` is POSIX-only; this suite does not claim to run elsewhere."""
    assert hasattr(os, "mkfifo")
    assert stat.S_ISFIFO is not None
