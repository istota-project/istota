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
import gc
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from istota import db, doctor, secrets_store
from istota.config import UserConfig
from istota.doctor import DEPLOYMENT, FAIL, OK, SKIP, WARN
from istota.skills._loader import (
    OVERLAY_IS_A_SYMLINK,
    OVERLAY_NOT_A_REGULAR_FILE,
    OVERLAY_UNREADABLY_LARGE,
)

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


def _write_unscoped_vault(config, user_id="alice") -> Path:
    """Rewrite that user's vault with no top-level `istota` group (§1)."""
    from pykeepass import create_database

    path = (
        Path(config.workspace_path) / "Users" / user_id / "config" / "vault.kdbx"
    )
    path.unlink()
    kp = create_database(str(path), password=PASSPHRASE)
    kp.add_entry(kp.add_group(kp.root_group, UNOWNED_GROUP), "whatever", "", "unused")
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


def _add_mixed_users(config):
    """Three more users, one per failure class, beside `vault_config`'s working one.

    Every fixture in this file was single-user until a review found that the
    path arm returned on whichever of its three finding lists filled first — so
    one refused path dropped every other user's finding, including the
    misaimed-at-the-database warning that arm exists for, and reported a user
    whose *file* was merely missing under a FAIL whose remedy told them to edit
    a config line that was correct. Neither half is visible with one user.
    """
    ws = Path(config.workspace_path)
    (ws / "Users" / "dave" / "config").mkdir(parents=True, exist_ok=True)
    _write_vault(ws / "Users" / "dave" / "config" / "vault.kdbx")
    # Carol's directory has to exist and be empty: the resolver refuses a path
    # whose *directory* is missing (`no_directory_at_the_configured_path`, its
    # own reason id), so without this she is a refusal rather than the
    # resolves-but-the-file-is-absent case these tests are about.
    (ws / "Users" / "carol" / "config").mkdir(parents=True, exist_ok=True)
    config.users["bob"] = UserConfig(
        display_name="B", vault_path=str(ws / "Users" / "carol" / "v.kdbx")
    )
    config.users["carol"] = UserConfig(
        display_name="C", vault_path="config/missing.kdbx"
    )
    config.users["dave"] = UserConfig(
        display_name="D", vault_path="config/vault.kdbx", vault_services=["ntfy"]
    )
    return config


def _run(config, probe=True):
    return _by_name(doctor.check_credential_vault(config, probe))


def _contents(config, probe=True):
    """The parse arm, which is a registry entry of its own.

    Split out because opening a vault costs a mount read, a Fernet decrypt that
    writes `last_accessed_at`, and an Argon2id derivation *per configured user*
    — and `run_checks`' `skip` matches by prefix, so a dotted child of
    `security.credential_vault` could not be excluded from the heartbeat and the
    hourly sweep without taking the four cheap arms with it.
    """
    return doctor.check_vault_contents(config, probe)


def _all(config, probe=True):
    """Every result about the vault, across both registry entries."""
    return [*doctor.check_credential_vault(config, probe), _contents(config, probe)]


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
        # A mapping now, not a list of ids: `vault_path_for` merges a
        # `user_vault_config` row over the TOML attribute, so the value costs a
        # database read and every arm takes it from here rather than asking
        # again. Asserting the value too is what says the merge ran.
        assert doctor._vault_users(config) == {"bob": "   "}

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

    def test_the_contents_check_skips_without_the_library(
        self, vault_config, secret_key_env, monkeypatch
    ):
        _provision(vault_config)
        monkeypatch.setattr(doctor, "_vault_library_available", lambda: False)
        result = _contents(vault_config)
        assert result.status == SKIP
        assert "library" in result.detail


class TestTheScheduleArm:
    def test_a_scheduled_deployment_names_the_interval_and_the_count(
        self, vault_config
    ):
        """The owned-services line went with the service mapping: a vault owns
        no typed service now, so the arm reports the interval and how many
        vaults it applies to."""
        result = _run(vault_config)["security.credential_vault.schedule"]
        assert result.status == OK
        assert "300" in result.detail
        assert "vault(s) configured" in result.detail
        assert "karakeep" not in result.detail

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

    def test_a_vault_that_declares_nothing_is_still_reported(self, make_config):
        """There is nothing left to declare: the arm counts vaults rather than
        asking what each one owns."""
        config = make_config(
            users={"alice": UserConfig(display_name="A", vault_path="v.kdbx")}
        )
        result = _run(config)["security.credential_vault.schedule"]
        assert result.status == OK
        assert "1 vault(s) configured" in result.detail

    def test_a_stale_vault_services_line_reaches_no_finding(self, make_config):
        """The eligibility backstop this replaced warned about a name a vault
        may not own. There is no such name and no such question — a line left
        in a rendered `config.toml` is inert, and the arm says nothing about
        it."""
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
        assert result.status == OK
        assert "monarch" not in result.detail


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

    def test_an_empty_file_is_named_as_a_mid_write(self, vault_config):
        path = Path(vault_config.workspace_path) / "Users" / "alice" / "config"
        (path / "empty.kdbx").write_bytes(b"")
        vault_config.users["alice"].vault_path = "config/empty.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert "empty" in result.detail
        assert "mid-write" in result.remedy

    def test_a_fifo_is_reported_without_blocking(self, vault_config):
        """The reader refuses a FIFO immediately rather than blocking on it.

        This is the case worth pinning and the reason is **not** the one an
        earlier version of this file gave. `open(2)` on a FIFO would block until
        somebody writes, which on an unattended arm with no timeout behind it is
        a hung boot path — and `read_overlay_bytes` opens
        `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` and checks `S_ISREG` on the fd
        precisely so it does not. The property belongs to that primitive, which
        is why this arm reuses it rather than keeping a fourth copy of the same
        three answers. The test hangs rather than failing if it is ever lost.
        """
        path = Path(vault_config.workspace_path) / "Users" / "alice" / "config"
        os.mkfifo(path / "pipe.kdbx")
        vault_config.users["alice"].vault_path = "config/pipe.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert OVERLAY_NOT_A_REGULAR_FILE in result.detail

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
        assert OVERLAY_UNREADABLY_LARGE in result.detail

    def test_a_symlinked_leaf_is_reported_rather_than_followed(self, vault_config):
        """The reader refuses a symlinked leaf with `O_NOFOLLOW`, so following
        one here would report a file the sync will never open."""
        base = Path(vault_config.workspace_path) / "Users" / "alice" / "config"
        (base / "link.kdbx").symlink_to(base / "vault.kdbx")
        vault_config.users["alice"].vault_path = "config/link.kdbx"
        result = _run(vault_config)["security.credential_vault.path"]
        assert result.status == WARN
        assert OVERLAY_IS_A_SYMLINK in result.detail

    def test_the_refusal_ids_are_the_readers_own_words(self, vault_config):
        """Not decoration: the id in this report is the id in the daemon's log.

        `read_overlay_bytes` is the one reader for all of these, so reusing it
        here means an operator greps one word rather than translating between a
        doctor sentence and a sync warning. The three above are asserted against
        the imported constants for the same reason the resolver's `VAULT_PATH_*`
        words are.
        """
        assert OVERLAY_IS_A_SYMLINK == "overlay_is_a_symlink"
        assert OVERLAY_NOT_A_REGULAR_FILE == "overlay_not_a_regular_file"
        assert OVERLAY_UNREADABLY_LARGE == "overlay_unreadably_large"

    @pytest.mark.parametrize("probe", [False, True])
    def test_no_descriptor_is_left_open(self, vault_config, secret_key_env, probe):
        """Counted rather than recorded, because recording could not settle it.

        A gate running per user per sweep leaks one descriptor a cycle
        otherwise, which ends as a daemon that cannot open a socket days later
        with nothing pointing back here. The first version of this test recorded
        `os.close` calls and asserted the opened set was a subset of the closed
        one — which an fd number satisfies whenever *anything* closed that
        number at any point in the run, and `open_overlay_dir` closes its
        intermediate component descriptors during the walk, so the closed set
        arrives already populated with recyclable numbers. Counting the
        process's open descriptors before and after is the measurement that
        discriminates.

        Driven over a four-user mix, so the refusal and missing-file paths are
        exercised rather than the happy one alone, and over both probe modes,
        since the parse takes a second resolve of its own.
        """
        _provision(vault_config)
        _add_mixed_users(vault_config)

        def _open_fds():
            # `gc.collect()` first, and it does not weaken the measurement: a
            # descriptor this check leaked is referenced by nothing and is held
            # open regardless, while a sqlite or lxml handle that is merely
            # awaiting finalization is not a leak and is exactly what an
            # uncollected sample counts. Without it the reading depends on where
            # the interpreter's generational threshold happens to fall inside a
            # parse that allocates tens of thousands of objects — so an
            # unrelated change to how many objects the call path builds moves
            # `before` by four and reddens this test, which is how it behaved
            # twice while this stage was written.
            gc.collect()
            return len(os.listdir("/dev/fd"))

        _all(vault_config, probe)  # warm any lazy import before measuring
        before = _open_fds()
        for _ in range(5):
            _all(vault_config, probe)
        assert _open_fds() == before

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

    def test_one_refusal_does_not_swallow_every_other_users_finding(
        self, vault_config
    ):
        """The defect a single-user fixture cannot see.

        The arm accumulated three independent lists and returned on the first
        non-empty one, so `notes` — the misaimed-at-the-database warning, which
        is the whole reason that branch exists — was reachable only on a
        deployment where nobody had a refusal and nobody's file was missing.
        One refused path dropped it, and dropped every other user's finding with
        it.
        """
        _add_mixed_users(vault_config)
        aimed = vault_config.db_path.parent / "istota.db"
        aimed.write_bytes(b"not a kdbx")
        vault_config.users["dave"].vault_path = str(aimed)

        result = _run(vault_config, probe=False)["security.credential_vault.path"]
        assert result.status == FAIL  # bob is refused
        for user_id in ("bob", "carol", "dave"):
            assert user_id in result.detail, f"{user_id} was dropped from the report"
        assert "alice" not in result.detail  # hers is fine, and counted instead
        assert "1 other(s) are fine" in result.detail

    def test_a_missing_file_is_not_reported_under_the_refusal_remedy(
        self, vault_config
    ):
        """The second half of the same defect. `refused + problems` were joined
        into one string under the FAIL wording and the FAIL remedy, so a user
        whose path resolves and whose *file* is missing was told the daemon may
        not open their path and sent to correct a config line that was right.

        The status is still FAIL, because somebody genuinely is refused — what
        must hold is that the remedy carries both sentences, so each user's
        condition has an action behind it.
        """
        _add_mixed_users(vault_config)
        result = _run(vault_config, probe=False)["security.credential_vault.path"]
        assert result.status == FAIL
        assert "carol: nothing at the path" in result.detail
        assert "vault_path in config.toml" in result.remedy  # for bob
        assert "the user's own device" in result.remedy  # for carol

    def test_a_raise_inside_the_per_user_body_is_contained(
        self, vault_config, monkeypatch
    ):
        """The one block in this arm holding a descriptor has a `finally` and no
        `except` of its own, so an escape would be caught by `run_checks` and
        collapsed into a single synthetic FAIL that replaced all four of this
        check's findings."""

        def _boom(*_a, **_k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(doctor, "_vault_file_note", _boom)
        result = _run(vault_config, probe=False)["security.credential_vault.path"]
        assert result.status == WARN
        assert "RuntimeError" in result.detail

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


class TestAFolderConventionUser:
    """§7's enable is a file in the folder plus a passphrase, and §10 says this
    check follows it. Every fixture above configures a `vault_path`, so the
    arms that serve the ordinary shape — a user who chose their file from the
    settings card and has no TOML line at all — were reached by nothing.
    """

    def _passphrase_only(self, make_config, tmp_path, *files):
        """A user with a `vault/passphrase` row, no `vault_path`, and `files`
        in their vault folder."""
        database = tmp_path / "vault-folder.db"
        db.init_db(database)
        config = make_config(
            db_path=database,
            users={"alice": UserConfig(display_name="Alice", vault_path="")},
        )
        folder = (
            Path(config.workspace_path)
            / "Users" / "alice" / config.bot_dir_name / "vault"
        )
        folder.mkdir(parents=True, exist_ok=True)
        for name in files:
            _write_vault(folder / name)
        _provision(config)
        return config, folder

    def test_a_passphrase_with_no_path_is_a_configured_vault(
        self, make_config, tmp_path, secret_key_env
    ):
        """The half `_vault_users` gained. Reading `vault_path` alone reported
        every folder-convention user as having no vault at all, which after §7
        is the ordinary shape rather than an edge."""
        config, _folder = self._passphrase_only(make_config, tmp_path, "personal.kdbx")

        assert doctor._vault_users(config) == {"alice": ""}
        assert _run(config, probe=False)["security.credential_vault.path"].status == OK

    def test_a_user_with_neither_is_still_absent(
        self, make_config, tmp_path, secret_key_env
    ):
        """The control: without it the enumerator could be returning every
        configured user, and the SKIP every deployment gets by default would
        have stopped working."""
        database = tmp_path / "none.db"
        db.init_db(database)
        config = make_config(
            db_path=database,
            users={"alice": UserConfig(display_name="Alice", vault_path="")},
        )

        assert doctor._vault_users(config) == {}
        assert doctor.check_credential_vault(config, True)[0].status == SKIP

    def test_an_empty_folder_warns_and_names_it(
        self, make_config, tmp_path, secret_key_env
    ):
        """§10: `VAULT_DIR_EMPTY` reads as "configured and no file yet". A WARN
        rather than a FAIL, because it is the user's to fix and their own card
        already asks for exactly this."""
        config, folder = self._passphrase_only(make_config, tmp_path)

        result = _run(config, probe=False)["security.credential_vault.path"]

        assert result.status == WARN
        assert "alice" in result.detail
        assert folder.name in result.detail
        assert "vault folder" in result.remedy

    def test_several_files_and_no_choice_warns(
        self, make_config, tmp_path, secret_key_env
    ):
        """§10: `VAULT_DIR_UNCHOSEN` reads as "several files, none chosen".
        Also not a fault — the dropdown is one click away."""
        config, _folder = self._passphrase_only(
            make_config, tmp_path, "personal.kdbx", "work.kdbx"
        )

        result = _run(config, probe=False)["security.credential_vault.path"]

        assert result.status == WARN
        assert "alice" in result.detail
        assert "Settings" in result.remedy

    def test_a_refused_toml_path_still_fails_beside_a_folder_user(
        self, make_config, tmp_path, secret_key_env
    ):
        """The discriminating pair. A folder refusal WARNs and a refused
        configured path FAILs, and the arm reports the worst class present — so
        collapsing the two would either downgrade the operator's FAIL or
        upgrade a user's dropdown into one."""
        config, _folder = self._passphrase_only(make_config, tmp_path)
        config.users["bob"] = UserConfig(
            display_name="Bob", vault_path="no-such-dir/vault.kdbx"
        )

        result = _run(config, probe=False)["security.credential_vault.path"]

        assert result.status == FAIL
        assert "alice" in result.detail and "bob" in result.detail


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


class TestTheContentsCheck:
    def test_probe_false_opens_nothing(self, vault_config, secret_key_env):
        _provision(vault_config)
        result = _contents(vault_config, probe=False)
        assert result.status == SKIP
        assert "probing is disabled" in result.detail

    def test_probe_reports_counts_for_a_working_vault(
        self, vault_config, secret_key_env
    ):
        """Counts and never names, which is this arm's whole rule.

        The fixture's `istota` group holds three entries, each with a password
        and nothing else, so the file produces exactly three names.
        """
        _provision(vault_config)
        result = _contents(vault_config)
        assert result.status == OK
        assert "3 credential(s)" in result.detail
        assert "karakeep" not in result.detail and "api_key" not in result.detail

    def test_an_unscoped_read_is_reported_as_a_posture(
        self, vault_config, secret_key_env
    ):
        """§1: a vault reading its whole file shares everything in it, which is
        an operator-visible security posture rather than a user preference. The
        *names* still never appear."""
        _provision(vault_config)
        _write_unscoped_vault(vault_config)

        result = _contents(vault_config)

        assert result.status == OK
        assert "whole file shared" in result.detail
        assert UNOWNED_GROUP not in result.detail

    def test_a_wrong_passphrase_reports_the_class_and_not_the_sentence(
        self, vault_config, secret_key_env
    ):
        """The class is a stable word the operator can grep; its human sentence
        is the notification's job and carries a remedy aimed at the user."""
        _provision(vault_config, value="the-wrong-passphrase-entirely-and-then-some")
        result = _contents(vault_config)
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
        result = _contents(vault_config)
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

        **What is in the sweep, and the discriminating member moved.** It used
        to be that `VaultStatusReport` carried no credential names at all —
        `key_counts` was a count per service — so the values below were absent
        by the shape of the type and only `groups` could leak file text. Both
        fields are gone: the report now carries `names`, the whole derived
        namespace, written into the *file* by somebody a task in that user's
        own sandbox can be. So `names` is what makes this discriminating, and
        `UNOWNED_GROUP` below is a group title that appears in one of them.
        Printing what the file turned out to hold — rather than how many —
        turns this red on that member.

        `karakeep` and `ntfy` used to be excluded from the sweep as operator
        config the schedule arm reported on purpose. That arm reports no
        service names any more, so the exclusion is about the fixture's own
        group titles rather than about config.
        """
        _provision(vault_config)
        rendered = " ".join(
            f"{r.name} {r.status} {r.detail} {r.remedy}"
            for r in _all(vault_config, True)
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
        }

    def test_the_contents_check_is_a_sibling_rather_than_a_child(
        self, vault_config, secret_key_env
    ):
        """The naming is load-bearing and reads like a style choice.

        `only` and `skip` both match by **prefix**, so a
        `security.credential_vault.contents` would be pulled in by every caller
        naming the parent and — worse — re-included by any skip list that named
        the parent to exclude the expensive arm. A sibling name is what lets the
        two be selected apart, which is the entire mechanism the split exists to
        use.
        """
        _provision(vault_config)
        assert doctor.CHECK_SCOPES["security.vault_contents"] == DEPLOYMENT
        cheap = doctor.run_checks(
            vault_config, only=("security.credential_vault",), probe=True
        )
        assert not any(r.name == "security.vault_contents" for r in cheap)

        both = doctor.run_checks(
            vault_config,
            only=("security.credential_vault", "security.vault_contents"),
            probe=True,
        )
        assert sum(r.name == "security.vault_contents" for r in both) == 1

    def test_the_expensive_check_is_skipped_by_the_sweep_and_the_heartbeat(self):
        """Both lists exclude by registry name, and both are the tree's own
        mechanism for a cost paid per user. The sweep and the heartbeat are the
        two unattended callers whose cadence makes the Argon2id derivation per
        configured vault multiply; the boot run and `istota doctor` still answer
        it."""
        from istota.heartbeat import _SELF_CHECK_SKIPPED
        from istota.scheduler import SWEEP_SKIPPED_CHECKS

        assert "security.vault_contents" in SWEEP_SKIPPED_CHECKS
        assert "security.vault_contents" in _SELF_CHECK_SKIPPED
        # And neither names the parent, which would take the four cheap arms
        # with it by prefix.
        assert "security.credential_vault" not in SWEEP_SKIPPED_CHECKS
        assert "security.credential_vault" not in _SELF_CHECK_SKIPPED

    def test_the_sweep_still_answers_the_cheap_arms(self, vault_config):
        """The point of the split: a vault that stops resolving is still
        reported hourly, and only the parse waits for a boot or an operator."""
        from istota.scheduler import SWEEP_SKIPPED_CHECKS

        names = {
            r.name
            for r in doctor.run_checks(
                vault_config,
                only=("security.credential_vault", "security.vault_contents"),
                skip=SWEEP_SKIPPED_CHECKS,
                probe=True,
            )
        }
        assert "security.credential_vault.path" in names
        assert "security.vault_contents" not in names


class TestTheDatabaseDirectoryComparison:
    """`_vault_db_dir` answers None rather than guessing, and both other answers
    used to be wrong rather than merely unhelpful."""

    def test_an_absolute_db_path_gives_its_resolved_parent(self, tmp_path, make_config):
        config = make_config(db_path=tmp_path / "data" / "istota.db")
        assert doctor._vault_db_dir(config) == Path(
            os.path.realpath(tmp_path / "data")
        )

    @pytest.mark.parametrize("raw", ["", None, "data/istota.db", "istota.db"])
    def test_anything_but_an_absolute_path_answers_none(
        self, raw, make_config, monkeypatch
    ):
        """`Config.db_path` *defaults* to the relative `data/istota.db` and can
        be unset, and `Path("").parent` is `Path(".")` — which `realpath`
        resolves to the process's current directory. So the earlier version was
        wrong in both directions: unset compared every absolute `vault_path`
        under the daemon's cwd against a directory holding no database (noisy,
        and plausible on the standalone shape, where the daemon may be started
        from the operator's home), and relative compared against a `data/` with
        nothing to do with the database (permissive — a genuinely misaimed path
        went unreported).

        Refusing to answer is the right failure for a WARN about a mistake, and
        every shipped deployment renders an absolute `db_path`, so nothing real
        is lost. The cwd is moved under the test's own tmp dir so a stray answer
        would be visible rather than accidentally matching.
        """
        monkeypatch.chdir(make_config().temp_dir.parent)
        config = make_config()
        config.db_path = raw
        assert doctor._vault_db_dir(config) is None

    def test_an_unset_db_path_does_not_warn_about_a_vault_under_the_cwd(
        self, vault_config, tmp_path, monkeypatch
    ):
        """The noisy direction, driven rather than asserted on the helper.

        With `db_path` unset, `realpath(Path("").parent)` is the cwd, so an
        absolute `vault_path` sitting under it was reported as "inside the
        framework database's own directory" with a remedy telling the operator
        to move a file that was where it belonged.
        """
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "vault.kdbx").write_bytes(b"not a kdbx, but it is a file")
        monkeypatch.chdir(outside)
        vault_config.db_path = None
        vault_config.users["alice"].vault_path = str(outside / "vault.kdbx")

        result = _run(vault_config, probe=False)["security.credential_vault.path"]
        assert "database" not in result.detail


class TestAMalformedFieldDoesNotCostEveryFinding:
    """No arm reads `vault_services` any more, so a malformed one is inert.

    The reader this class was written against (`_vault_declared_services`) went
    with the schedule arm's owned-services line. These stay as the standing
    guard that a field nothing consumes cannot cost the operator every finding
    the check had already computed — `run_checks` contains a raise into one
    synthetic FAIL replacing all four.
    """

    @pytest.mark.parametrize("value", [5, ["karakeep", 7, None], "karakeep"])
    def test_a_malformed_field_is_not_read_at_all(self, vault_config, value):
        vault_config.users["alice"].vault_services = value
        results = _run(vault_config, probe=False)
        assert len(results) == 4
        detail = results["security.credential_vault.schedule"].detail
        assert "1 vault(s) configured" in detail
        assert "karakeep" not in detail and "7" not in detail

    def test_through_the_registry_a_malformed_field_still_yields_four_findings(
        self, vault_config
    ):
        """The blast radius, asserted where it actually bites: `run_checks` is
        what turns a raise into the synthetic FAIL."""
        vault_config.users["alice"].vault_services = 5
        results = doctor.run_checks(
            vault_config, only=("security.credential_vault",), probe=False
        )
        assert len(results) == 4
        assert not any("the check itself raised" in r.detail for r in results)


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
