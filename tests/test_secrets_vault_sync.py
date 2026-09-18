"""The vault sync pass: when it does work, when it declines to, and what it says.

`tests/test_secrets_vault.py` is the reader and the applier. This file is the
half that decides whether either of them runs at all — the digest cache, the
passphrase lookup, the outcome vocabulary the scheduler gate and the CLI report,
and the two `istota secret` verbs.

**The cache is the subject, and its failure mode is a test that cannot fail.**
A pass that skipped when it should have worked and a pass that did the work both
leave the secrets table in the same state, so almost every assertion here is
about `parse_vault` being called or not called rather than about rows. That is
`.claude/rules/testbed.md`'s "success indistinguishable from a no-op" shape, and
the answer is the same one: count the calls, and pair every skip assertion with
a control that does the work on the next cycle.

**Which failure classes cache their digest is the whole of §7 and it decides
whether §8's remedies work at all.** A `VaultLocked` is fixed by
`istota secret ensure`, which touches no byte of the file — so a cycle that
cached that digest would make the remedy inert, silently, and the only symptom
would be an operator following a correct instruction and nothing happening.
`TestWhichFailuresCacheTheirDigest` drives the remedy itself rather than
asserting on the state dict: provision the passphrase between two cycles with no
file change and require the second to succeed.

Every KDBX here is a real file built by `pykeepass`, for the reason
`test_secrets_vault.py`'s header gives. The values are invented and
`test_no_sync_log_record_carries_a_value` sweeps for them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from istota import secrets_store, storage
from istota.config import Config, UserConfig
from istota.secrets_vault import VAULT_ENTRY_SERVICE

PASSPHRASE = "sync-fixture-passphrase-not-a-real-one"
ROTATED_PASSPHRASE = "sync-fixture-passphrase-after-rotation"
API_KEY_VALUE = "ak-sync-fixture-alpha"
BASE_URL_VALUE = "https://karakeep.example.com"
TOPIC_VALUE = "ntfy-sync-fixture-topic"

SECRET_KEY = "deadbeef" * 8


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_sync_state():
    """The per-user digest/outcome state is module-level, so it must be reset.

    Both directions matter under xdist: a leftover digest makes a later test's
    first cycle a skip, and a leftover outcome suppresses a transition the next
    test is asserting on.
    """
    from istota import secrets_vault

    secrets_vault.reset_sync_state()
    yield
    secrets_vault.reset_sync_state()


@pytest.fixture
def secret_key(monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", SECRET_KEY)
    return SECRET_KEY


def _vault_config(tmp_path, *, vault_path: str, services: list[str]) -> Config:
    """A config with a workspace, one user `alice`, and an initialised DB."""
    from istota import db

    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        temp_dir=tmp_path / "tmp",
        workspace_path=mount,
        users={
            "alice": UserConfig(vault_path=vault_path, vault_services=services),
        },
    )


def _user_root(config: Config) -> Path:
    return Path(config.workspace_path) / "Users" / "alice"


def _write_vault(path: Path, *, password=PASSPHRASE, karakeep=True, ntfy=False):
    """A real KDBX at `path` holding `istota/karakeep` and optionally `ntfy`.

    Imported inside the helper for the reason `parse_vault` function-scopes its
    own import: `pykeepass` pulls `lxml`, `argon2-cffi` and `pycryptodomex`, and
    nothing here should pay that at collection.
    """
    from pykeepass import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    if karakeep:
        group = kp.add_group(root, "karakeep")
        kp.add_entry(group, "base_url", "", BASE_URL_VALUE)
        kp.add_entry(group, "api_key", "", API_KEY_VALUE)
    if ntfy:
        group = kp.add_group(root, "ntfy")
        kp.add_entry(group, "topic", "", TOPIC_VALUE)
    kp.save()
    return kp


def _provision_passphrase(config: Config, value: str = PASSPHRASE) -> None:
    secrets_store.set_secret(config.db_path, "alice", "vault", "passphrase", value)


@pytest.fixture
def ready(tmp_path, secret_key):
    """A configured user, a real vault at the configured path, a passphrase."""
    config = _vault_config(
        tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
    )
    path = _user_root(config) / "config" / "vault.kdbx"
    _write_vault(path)
    _provision_passphrase(config)
    return config, path


class _ParseCounter:
    """Counts `parse_vault` calls without replacing what it does.

    A stub returning a canned `VaultRead` would make every "the work happened"
    assertion below a statement about the stub. This wraps the real function, so
    a cycle that is *not* skipped still parses a real KDBX and still applies it.
    """

    def __init__(self, monkeypatch):
        from istota import secrets_vault

        self.calls = 0
        real = secrets_vault.parse_vault

        def counted(data, passphrase):
            self.calls += 1
            return real(data, passphrase)

        monkeypatch.setattr(secrets_vault, "parse_vault", counted)


@pytest.fixture
def parse_calls(monkeypatch):
    return _ParseCounter(monkeypatch)


# ---------------------------------------------------------------------------
# The digest cache
# ---------------------------------------------------------------------------


class TestTheDigestCache:
    """An unchanged file costs a read and nothing else."""

    def test_a_first_sync_parses_and_applies(self, ready, parse_calls):
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config, _path = ready
        result = sync_user(config, "alice")

        assert result.outcome == OUTCOME_OK
        assert parse_calls.calls == 1
        assert result.apply is not None
        assert result.apply.created == 2
        assert (
            secrets_store.get_secret(
                config.db_path, "alice", VAULT_ENTRY_SERVICE, "karakeep_api_key"
            )
            == API_KEY_VALUE
        )


    def test_an_unchanged_digest_does_no_work(self, ready, parse_calls):
        """The property §7 rests on: no Argon2id, no DB writes, no log lines."""
        from istota.secrets_vault import OUTCOME_UNCHANGED, sync_user

        config, _path = ready
        first = sync_user(config, "alice")
        assert parse_calls.calls == 1

        second = sync_user(config, "alice")
        assert second.outcome == OUTCOME_UNCHANGED
        assert second.apply is None
        # The discriminating assertion. Everything else about the two cycles is
        # identical, including the resulting table.
        assert parse_calls.calls == 1
        assert first.digest == second.digest

    def test_a_changed_file_parses_again(self, ready, parse_calls):
        """The control for the skip above: a moved digest is not a skip."""
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config, path = ready
        sync_user(config, "alice")
        assert parse_calls.calls == 1

        _write_vault(path, ntfy=True)
        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_OK
        assert parse_calls.calls == 2

    def test_an_unchanged_file_does_not_re_log(self, ready, caplog):
        """A vault that is fine says so once, not once per interval."""
        from istota.secrets_vault import sync_user

        config, _path = ready
        sync_user(config, "alice")
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="istota.secrets_vault"):
            sync_user(config, "alice")
        records = [
            r for r in caplog.records if r.name == "istota.secrets_vault"
        ]
        assert records == [], [r.getMessage() for r in records]


class TestWhichFailuresCacheTheirDigest:
    """§7's split, driven through the remedy rather than through the state dict.

    Caching every failure is the obvious rule and it breaks two of them: the fix
    for a wrong passphrase and the fix for an absent one are both
    `istota secret ensure`, which touches no byte of the vault. Under a blanket
    cache the operator follows the instruction, the digest is unchanged, the next
    cycle skips, and nothing happens until a restart.
    """

    def test_a_corrupt_digest_is_cached_so_the_next_cycle_neither_parses_nor_relogs(
        self, tmp_path, secret_key, parse_calls, caplog
    ):
        from istota.secrets_vault import OUTCOME_UNCHANGED, VaultCorrupt, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Non-empty, so `read_vault_bytes` returns a digest and the failure lands
        # in `parse_vault` — which is the ordinary corrupt case. A *zero-byte*
        # file is refused before a digest exists and so cannot be cached at all.
        path.write_bytes(b"this is not a KeePass database" * 8)
        _provision_passphrase(config)

        first = sync_user(config, "alice")
        assert first.outcome == VaultCorrupt.__name__
        assert parse_calls.calls == 1

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="istota.secrets_vault"):
            second = sync_user(config, "alice")
        assert second.outcome == OUTCOME_UNCHANGED
        assert parse_calls.calls == 1
        assert [r for r in caplog.records if r.name == "istota.secrets_vault"] == []

    def test_a_locked_digest_is_not_cached_and_provisioning_ends_it(
        self, tmp_path, secret_key, parse_calls
    ):
        """The test that would have caught the §8 remedy being inert.

        Two cycles over **identical bytes**, with the remedy performed between
        them and nothing at all done to the file.
        """
        from istota.secrets_vault import OUTCOME_OK, VaultLocked, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        _write_vault(path)
        _provision_passphrase(config, "the-wrong-passphrase-entirely")

        first = sync_user(config, "alice")
        assert first.outcome == VaultLocked.__name__
        assert parse_calls.calls == 1
        digest_before = path.read_bytes()

        _provision_passphrase(config, PASSPHRASE)
        second = sync_user(config, "alice")

        assert path.read_bytes() == digest_before, "the file must not have moved"
        assert second.outcome == OUTCOME_OK
        assert parse_calls.calls == 2
        # Provisioning the right passphrase ends the locked state *and* applies,
        # which is what makes the remedy the notification names a real one.
        assert (
            secrets_store.get_secret(
                config.db_path, "alice", VAULT_ENTRY_SERVICE, "karakeep_api_key"
            )
            == API_KEY_VALUE
        )


    def test_an_absent_passphrase_is_not_cached_and_provisioning_ends_it(
        self, tmp_path, secret_key, parse_calls
    ):
        """The same for the class that never reaches the unlock at all."""
        from istota.secrets_vault import (
            OUTCOME_OK,
            VaultPassphraseMissing,
            sync_user,
        )

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        _write_vault(path)

        first = sync_user(config, "alice")
        assert first.outcome == VaultPassphraseMissing.__name__
        assert parse_calls.calls == 0, "no passphrase means no Argon2id"
        bytes_before = path.read_bytes()

        _provision_passphrase(config)
        second = sync_user(config, "alice")

        assert path.read_bytes() == bytes_before
        assert second.outcome == OUTCOME_OK
        assert parse_calls.calls == 1

    def test_a_new_digest_after_a_cached_failure_retries(
        self, tmp_path, secret_key, parse_calls
    ):
        from istota.secrets_vault import OUTCOME_OK, VaultCorrupt, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a KeePass database at all" * 8)
        _provision_passphrase(config)

        assert sync_user(config, "alice").outcome == VaultCorrupt.__name__
        assert parse_calls.calls == 1

        _write_vault(path)
        assert sync_user(config, "alice").outcome == OUTCOME_OK
        assert parse_calls.calls == 2

    def test_a_rotated_master_passphrase_reads_as_locked_not_corrupt(
        self, tmp_path, secret_key
    ):
        """Two classes, two remedies, and both arrive as a changed digest.

        Rewriting the same owned set under a new password is what KeePassXC's
        "change master key" does. The file still parses as a KDBX, so calling it
        corrupt would send the operator to check their mount when what they need
        is `istota secret ensure`.
        """
        from istota.secrets_vault import OUTCOME_OK, VaultLocked, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        _write_vault(path)
        _provision_passphrase(config)
        assert sync_user(config, "alice").outcome == OUTCOME_OK

        _write_vault(path, password=ROTATED_PASSPHRASE)
        assert sync_user(config, "alice").outcome == VaultLocked.__name__

        _provision_passphrase(config, ROTATED_PASSPHRASE)
        assert sync_user(config, "alice").outcome == OUTCOME_OK

    def test_a_missing_library_is_not_cached(self, ready, monkeypatch, parse_calls):
        from istota import secrets_vault
        from istota.secrets_vault import (
            OUTCOME_OK,
            VaultLibraryMissing,
            sync_user,
        )

        config, _path = ready

        def _absent(data, passphrase):
            parse_calls.calls += 1
            raise VaultLibraryMissing("the 'vault' extra is not installed")

        real = secrets_vault.parse_vault
        monkeypatch.setattr(secrets_vault, "parse_vault", _absent)
        assert sync_user(config, "alice").outcome == VaultLibraryMissing.__name__
        assert parse_calls.calls == 1

        # Restored by hand rather than with `monkeypatch.undo()`, which takes the
        # whole fixture's record with it — including the `secret_key` fixture's
        # `setenv`, which shares this test's single `monkeypatch` instance.
        monkeypatch.setattr(secrets_vault, "parse_vault", real)
        # A second cycle on the same bytes has to reach the parse again: the
        # remedy is `uv sync --extra vault`, which moves nothing in the file.
        assert sync_user(config, "alice").outcome == OUTCOME_OK


class TestTheMasterKeyIsUnusable:
    """`VaultKeyUnusable`, which exists because `get_secret` answers three
    conditions with one `None`.

    Its remedy is the deployment's `ISTOTA_SECRET_KEY` rather than anything the
    user can do, which is why it is neither `VaultPassphraseMissing` (whose
    remedy is `istota secret ensure`) nor cached (an unchanged file is no
    evidence the key has been fixed). The third branch — a key that is present,
    long enough, and simply wrong — is the one with novel logic behind it: it is
    told from an absent row only by asking `secret_exists` after `get_secret`
    has already answered `None`.
    """

    def _configured(self, tmp_path):
        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        _write_vault(_user_root(config) / "config" / "vault.kdbx")
        return config

    def test_no_master_key_at_all(self, tmp_path, secret_key, monkeypatch, parse_calls):
        from istota.secrets_vault import VaultKeyUnusable, sync_user

        config = self._configured(tmp_path)
        _provision_passphrase(config)
        monkeypatch.delenv("ISTOTA_SECRET_KEY", raising=False)

        result = sync_user(config, "alice")
        assert result.outcome == VaultKeyUnusable.__name__
        # Checked before the parse, which is the point of checking it here at
        # all rather than leaving it to `apply_vault`'s own refusal.
        assert parse_calls.calls == 0

    def test_a_master_key_below_the_stores_floor(
        self, tmp_path, secret_key, monkeypatch, parse_calls
    ):
        from istota.secrets_vault import VaultKeyUnusable, sync_user

        config = self._configured(tmp_path)
        _provision_passphrase(config)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "too-short")

        assert sync_user(config, "alice").outcome == VaultKeyUnusable.__name__
        assert parse_calls.calls == 0

    def test_a_wrong_master_key_is_not_reported_as_an_absent_passphrase(
        self, tmp_path, secret_key, monkeypatch, parse_calls
    ):
        """The branch `secret_exists` exists for.

        The row is there and the key cannot read it, which is a deployment fault
        — reporting it as a missing passphrase would send the operator to
        `istota secret ensure`, a command that cannot help and that would
        overwrite a perfectly good row.
        """
        from istota.secrets_vault import VaultKeyUnusable, sync_user

        config = self._configured(tmp_path)
        _provision_passphrase(config)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "f00dcafe" * 8)

        result = sync_user(config, "alice")
        assert result.outcome == VaultKeyUnusable.__name__
        assert parse_calls.calls == 0

    def test_it_is_not_cached_so_fixing_the_key_ends_it(
        self, tmp_path, secret_key, monkeypatch, parse_calls
    ):
        from istota.secrets_vault import OUTCOME_OK, VaultKeyUnusable, sync_user

        config = self._configured(tmp_path)
        _provision_passphrase(config)
        path = _user_root(config) / "config" / "vault.kdbx"
        monkeypatch.delenv("ISTOTA_SECRET_KEY", raising=False)
        assert sync_user(config, "alice").outcome == VaultKeyUnusable.__name__
        bytes_before = path.read_bytes()

        monkeypatch.setenv("ISTOTA_SECRET_KEY", SECRET_KEY)
        assert path.read_bytes() == bytes_before
        assert sync_user(config, "alice").outcome == OUTCOME_OK
        assert parse_calls.calls == 1

    def test_the_reason_does_not_carry_the_keys_length(
        self, tmp_path, secret_key, monkeypatch
    ):
        """`reason` is rendered per user and, from Stage 5, into a notification.

        The store's own message names the key's length, which is a
        deployment-level fact about the master key and does not belong on a
        per-user surface. It goes to the daemon log instead.
        """
        from istota.secrets_vault import sync_user

        config = self._configured(tmp_path)
        _provision_passphrase(config)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "abcdefghij")

        reason = sync_user(config, "alice").reason
        assert "10" not in reason
        assert "ISTOTA_SECRET_KEY" in reason


class TestTheSkipCarriesTheSettledClass:
    """`OUTCOME_UNCHANGED` is not evidence of health, and the result says so.

    `VaultCorrupt` is cached, so a cycle over a file that is still unreadable
    answers `unchanged` exactly as a cycle over a working one does. A consumer
    reading that as success would close a notification about a vault that is
    still broken, which is the one thing the reporting design exists to prevent.
    """

    def test_a_skip_after_a_corrupt_cycle_says_what_it_settled_on(
        self, tmp_path, secret_key
    ):
        from istota.secrets_vault import OUTCOME_UNCHANGED, VaultCorrupt, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a KeePass database" * 8)
        _provision_passphrase(config)

        assert sync_user(config, "alice").outcome == VaultCorrupt.__name__
        second = sync_user(config, "alice")
        assert second.outcome == OUTCOME_UNCHANGED
        assert second.last_outcome == VaultCorrupt.__name__

    def test_a_skip_after_a_healthy_cycle_says_so_too(self, ready):
        from istota.secrets_vault import OUTCOME_OK, OUTCOME_UNCHANGED, sync_user

        config, _path = ready
        assert sync_user(config, "alice").outcome == OUTCOME_OK
        second = sync_user(config, "alice")
        assert second.outcome == OUTCOME_UNCHANGED
        # The discriminating half: the two skips are told apart by this field
        # and by nothing else on the result.
        assert second.last_outcome == OUTCOME_OK


class TestTheTransitionRule:
    """Retrying is not re-reporting, which needs the outcome class as state."""

    def test_a_repeated_uncached_failure_reports_once(
        self, tmp_path, secret_key, caplog
    ):
        from istota.secrets_vault import sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        _write_vault(_user_root(config) / "config" / "vault.kdbx")

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            first = sync_user(config, "alice")
            second = sync_user(config, "alice")
            third = sync_user(config, "alice")

        assert first.transition is True
        assert (second.transition, third.transition) == (False, False)
        warnings = [
            r
            for r in caplog.records
            if r.name == "istota.secrets_vault" and r.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]

    def test_a_change_of_failure_class_is_a_transition(
        self, tmp_path, secret_key
    ):
        from istota.secrets_vault import (
            VaultLocked,
            VaultPassphraseMissing,
            sync_user,
        )

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = _user_root(config) / "config" / "vault.kdbx"
        _write_vault(path)

        assert sync_user(config, "alice").outcome == VaultPassphraseMissing.__name__
        _provision_passphrase(config, "still-the-wrong-one-entirely")
        second = sync_user(config, "alice")
        assert second.outcome == VaultLocked.__name__
        assert second.transition is True

    def test_recovery_is_a_transition_too(self, tmp_path, secret_key):
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        _write_vault(_user_root(config) / "config" / "vault.kdbx")
        sync_user(config, "alice")

        _provision_passphrase(config)
        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_OK
        assert result.transition is True

        assert sync_user(config, "alice").transition is False


# ---------------------------------------------------------------------------
# §6: edge-triggered, not a standing invariant
# ---------------------------------------------------------------------------


class TestTheEdgeTriggeredProperty:
    def test_a_table_side_change_with_the_file_untouched_is_not_corrected(
        self, ready, parse_calls
    ):
        """§6's edge-triggered rule, asserted rather than left implicit.

        "Vault-wins" is what this sounds like and is not what it is: the file
        asserts its version of the world at the moment it is written and says
        nothing in between. What makes that safe is that the vault has a
        namespace of its own and no other writer in it — `vault_entries` is not
        in `secret_schema`, so neither the settings page nor `istota secret
        ensure` can reach it — rather than a loop correcting them. The row this
        test changes out of band is a *typed* service, which the vault stopped
        owning entirely; the edge-triggered property is what it still pins.

        **A future reconciliation loop has to delete this test on purpose.** Do
        not "fix" it by parsing on every cycle; that is a design change with §7's
        Argon2id cost behind it.
        """
        from istota.secrets_vault import OUTCOME_UNCHANGED, sync_user

        config, _path = ready
        sync_user(config, "alice")

        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "api_key", "changed-out-of-band"
        )
        result = sync_user(config, "alice")

        assert result.outcome == OUTCOME_UNCHANGED
        assert parse_calls.calls == 1
        assert (
            secrets_store.get_secret(config.db_path, "alice", "karakeep", "api_key")
            == "changed-out-of-band"
        )

    def test_the_next_save_parses_again(self, ready, parse_calls):
        """Half of the mitigation §7 names: any save at all moves the digest,
        so the cycle parses rather than skipping.

        The other half — that the parse then re-asserts the whole set over a
        row changed out of band — is the applying half's, and the change
        landing beside this one restores it. The name and this docstring say
        what is asserted here rather than what the pair asserts together."""
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config, path = ready
        sync_user(config, "alice")
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "api_key", "changed-out-of-band"
        )

        _write_vault(path)
        assert sync_user(config, "alice").outcome == OUTCOME_OK
        assert parse_calls.calls == 2
        # The other half of §7's mitigation, restored with the apply: the parse
        # re-asserts the whole namespace over a row changed out of band.
        assert (
            secrets_store.get_secret(
                config.db_path, "alice", VAULT_ENTRY_SERVICE, "karakeep_api_key"
            )
            == API_KEY_VALUE
        )



# ---------------------------------------------------------------------------
# force: the operator's escape hatch from the cache
# ---------------------------------------------------------------------------


class TestForce:
    def test_force_parses_a_digest_a_previous_cycle_cached(self, ready, parse_calls):
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config, _path = ready
        sync_user(config, "alice")
        assert sync_user(config, "alice").outcome != OUTCOME_OK
        assert parse_calls.calls == 1

        result = sync_user(config, "alice", force=True)
        assert result.outcome == OUTCOME_OK
        assert parse_calls.calls == 2

    def test_force_re_reports_rather_than_suppressing(self, ready, parse_calls):
        """`vault-sync` is what an operator runs to find out; it must answer."""
        from istota.secrets_vault import sync_user

        config, _path = ready
        sync_user(config, "alice")
        assert sync_user(config, "alice", force=True).transition is True


# ---------------------------------------------------------------------------
# The refused path, and the descriptor
# ---------------------------------------------------------------------------


class TestARefusedPath:
    def test_a_cross_user_absolute_path_reaches_an_outcome_class(
        self, tmp_path, secret_key
    ):
        """The one case in this design about an attack rather than a mistake.

        Before this, `resolve_user_vault_path` returned a bare None and the
        reason reached the daemon log and nothing else — which is the
        invisible-failure class §8 exists to close, in the one place it matters
        most.
        """
        from istota.secrets_vault import VaultPathRefused, sync_user

        config = _vault_config(tmp_path, vault_path="", services=["karakeep"])
        bob = Path(config.workspace_path) / "Users" / "bob" / "config"
        bob.mkdir(parents=True, exist_ok=True)
        config.users["alice"].vault_path = str(bob / "vault.kdbx")

        result = sync_user(config, "alice")
        assert result.outcome == VaultPathRefused.__name__
        # The id itself, not merely "some reason". Carrying it to a surface is
        # the entire justification for the resolver's return type changing, so a
        # refactor that flattened it into a generic string has to go red here.
        assert result.reason == storage.VAULT_PATH_INSIDE_WORKSPACE
        assert result.digest is None

    def test_a_refused_path_is_never_cached(self, tmp_path, secret_key):
        """The remedy is a config edit, so an unchanged anything proves nothing.

        There are no bytes to hash either, which is why this is in §7's uncached
        set by construction as well as by decision.
        """
        from istota.secrets_vault import VaultPathRefused, sync_user

        config = _vault_config(tmp_path, vault_path="", services=[])
        config.users["alice"].vault_path = "../bob/vault.kdbx"

        first = sync_user(config, "alice")
        second = sync_user(config, "alice")
        assert first.outcome == VaultPathRefused.__name__
        assert second.outcome == VaultPathRefused.__name__

    def test_an_unconfigured_user_is_not_a_failure(self, tmp_path, secret_key):
        from istota.secrets_vault import OUTCOME_NOT_CONFIGURED, sync_user

        config = _vault_config(tmp_path, vault_path="", services=[])
        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_NOT_CONFIGURED
        assert result.transition is False


class TestTheDescriptorLifetime:
    """`VaultLocation.dir_fd` is an open fd and this is the caller that closes it.

    A sync running every 300 seconds per user leaks one descriptor per cycle if a
    path forgets, which is a slow failure that eventually costs the daemon every
    socket it opens. Both arms are here because the failure path is the one a
    `try/finally` is easy to write around and easy to leave out.

    **It records the close rather than probing the number**, and the difference
    is not academic. The original form asked `os.fstat(seen[0])` after the cycle
    and read `OSError` as proof of closure — which holds only while nothing
    reopens in between, because the kernel hands out the lowest free descriptor
    and a number is reused the moment it is free. Stage 5 put a database write
    and a notification fan-out after the close, one of which takes the number
    straight back, and the test failed against a cycle that had closed the
    descriptor correctly. An assertion that a *number* is unused answers a
    different question from "was this descriptor closed", and only the second one
    is the property.
    """

    def _capture(self, monkeypatch, config):
        """Record the descriptors handed out, and the ones handed back.

        `os.close` is patched process-wide for the duration of the test, so
        `closed` may carry descriptors belonging to anything else running in
        this worker. That is why the assertions test *membership* rather than
        the list's contents: a foreign close is noise, and a missing one is the
        leak.
        """
        from istota import storage

        seen: list[int] = []
        closed: list[int] = []
        real = storage.resolve_user_vault_path
        real_close = os.close

        def capturing(cfg, user_id):
            resolution = real(cfg, user_id)
            if resolution.location is not None and resolution.location.dir_fd is not None:
                seen.append(resolution.location.dir_fd)
            return resolution

        def recording_close(fd):
            closed.append(fd)
            return real_close(fd)

        monkeypatch.setattr(storage, "resolve_user_vault_path", capturing)
        monkeypatch.setattr(os, "close", recording_close)
        return seen, closed

    def test_the_descriptor_is_closed_on_the_success_path(
        self, ready, monkeypatch
    ):
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config, _path = ready
        seen, closed = self._capture(monkeypatch, config)

        assert sync_user(config, "alice").outcome == OUTCOME_OK
        assert len(seen) == 1
        assert seen[0] in closed

    def test_the_descriptor_is_closed_on_a_failure_path(
        self, tmp_path, secret_key, monkeypatch
    ):
        from istota.secrets_vault import VaultLocked, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        _write_vault(_user_root(config) / "config" / "vault.kdbx")
        _provision_passphrase(config, "the-wrong-passphrase-entirely")
        seen, closed = self._capture(monkeypatch, config)

        assert sync_user(config, "alice").outcome == VaultLocked.__name__
        assert len(seen) == 1
        assert seen[0] in closed

    def test_the_descriptor_is_closed_when_the_skip_short_circuits(
        self, ready, monkeypatch
    ):
        from istota.secrets_vault import OUTCOME_UNCHANGED, sync_user

        config, _path = ready
        sync_user(config, "alice")
        seen, closed = self._capture(monkeypatch, config)

        assert sync_user(config, "alice").outcome == OUTCOME_UNCHANGED
        assert len(seen) == 1
        assert seen[0] in closed


# ---------------------------------------------------------------------------
# sync_all
# ---------------------------------------------------------------------------


class TestSyncAll:
    def test_one_users_failure_does_not_cost_the_rest(self, tmp_path, secret_key):
        from istota import db
        from istota.secrets_vault import OUTCOME_OK, sync_all

        mount = tmp_path / "mount"
        for name in ("alice", "bob"):
            (mount / "Users" / name).mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        config = Config(
            db_path=db_path,
            temp_dir=tmp_path / "tmp",
            workspace_path=mount,
            users={
                "alice": UserConfig(
                    vault_path="vault.kdbx", vault_services=["karakeep"]
                ),
                "bob": UserConfig(
                    vault_path="vault.kdbx", vault_services=["karakeep"]
                ),
            },
        )
        # alice's is corrupt; bob's is real.
        (mount / "Users" / "alice" / "vault.kdbx").write_bytes(b"junk" * 64)
        _write_vault(mount / "Users" / "bob" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)
        secrets_store.set_secret(db_path, "bob", "vault", "passphrase", PASSPHRASE)

        results = {r.user_id: r for r in sync_all(config)}
        assert results["alice"].outcome != OUTCOME_OK
        assert results["bob"].outcome == OUTCOME_OK
        assert (
            secrets_store.get_secret(
                db_path, "bob", VAULT_ENTRY_SERVICE, "karakeep_api_key"
            )
            == API_KEY_VALUE
        )
        # alice's cycle never parsed, so nothing of hers was written *or*
        # deleted — containment is about the rows as well as the exception.
        assert (
            secrets_store.get_secret(
                db_path, "alice", VAULT_ENTRY_SERVICE, "karakeep_api_key"
            )
            is None
        )


    def test_a_raising_user_is_contained(self, ready, monkeypatch):
        from istota import secrets_vault

        config, _path = ready

        def _boom(cfg, user_id, *, force=False):
            raise RuntimeError("the sync blew up")

        monkeypatch.setattr(secrets_vault, "sync_user", _boom)
        results = secrets_vault.sync_all(config)
        assert len(results) == 1
        assert results[0].outcome == secrets_vault.OUTCOME_ERROR


# ---------------------------------------------------------------------------
# No value ever reaches a log record
# ---------------------------------------------------------------------------


class TestTheEnableGate:
    """What switches a cycle on now, and what each empty answer costs.

    The gate used to be a configured `vault_path`. It is now that **or** a
    stored passphrase, which is what lets a user turn a vault on by dropping a
    file in the folder and generating a passphrase, with nothing typed
    anywhere. Its shape is what keeps the folder listing off every user's tick:
    a user with neither is skipped before any file is touched.
    """

    def _folder(self, config: Config) -> Path:
        folder = _user_root(config) / config.bot_dir_name / "vault"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def test_a_file_in_the_folder_with_no_passphrase_is_not_configured(
        self, tmp_path, secret_key,
    ):
        """Half the pair is not a vault, and reporting it as a broken one
        would notify a user who has not finished switching it on."""
        from istota.secrets_vault import OUTCOME_NOT_CONFIGURED, sync_user

        config = _vault_config(tmp_path, vault_path="", services=[])
        _write_vault(self._folder(config) / "personal.kdbx")

        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_NOT_CONFIGURED
        assert result.transition is False

    def test_neither_half_touches_the_filesystem(
        self, tmp_path, secret_key, monkeypatch,
    ):
        """Every user by default, on every tick."""
        from istota import secrets_vault

        config = _vault_config(tmp_path, vault_path="", services=[])

        def _refuse(*args, **kwargs):  # pragma: no cover - the point is no call
            raise AssertionError("the vault folder was listed")

        monkeypatch.setattr(storage, "list_vault_files", _refuse)
        result = secrets_vault.sync_user(config, "alice")
        assert result.outcome == secrets_vault.OUTCOME_NOT_CONFIGURED

    def test_a_passphrase_and_one_file_in_the_folder_applies_it(
        self, tmp_path, secret_key,
    ):
        """The whole of the happy path: drop the file in, generate a
        passphrase, and nothing is stored to select anything."""
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config = _vault_config(tmp_path, vault_path="", services=["karakeep"])
        _write_vault(self._folder(config) / "personal.kdbx")
        _provision_passphrase(config)

        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_OK
        assert result.apply is not None
        assert result.apply.created == 2
        assert (
            secrets_store.get_secret(
                config.db_path, "alice", VAULT_ENTRY_SERVICE, "karakeep_api_key"
            )
            == API_KEY_VALUE
        )


    def test_a_passphrase_and_an_empty_folder_is_a_missing_file(
        self, tmp_path, secret_key,
    ):
        """The user had a vault and the file went away, which is worth saying."""
        from istota.secrets_vault import VaultMissing, sync_user

        config = _vault_config(tmp_path, vault_path="", services=[])
        self._folder(config)
        _provision_passphrase(config)

        result = sync_user(config, "alice")
        assert result.outcome == VaultMissing.__name__
        assert result.transition is True

    def test_several_files_and_no_choice_is_quiet(self, tmp_path, secret_key):
        """A question for the settings card, not a failure.

        Reporting it as one would push a notification at a user whose answer is
        one dropdown away, every cycle until they answer it.
        """
        from istota.secrets_vault import OUTCOME_NOT_CONFIGURED, sync_user

        config = _vault_config(tmp_path, vault_path="", services=[])
        folder = self._folder(config)
        _write_vault(folder / "personal.kdbx")
        _write_vault(folder / "work.kdbx")
        _provision_passphrase(config)

        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_NOT_CONFIGURED
        assert result.transition is False

    def test_the_stored_choice_decides_which_one_is_read(
        self, tmp_path, secret_key,
    ):
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config = _vault_config(tmp_path, vault_path="", services=["karakeep"])
        folder = self._folder(config)
        _write_vault(folder / "personal.kdbx", karakeep=False, ntfy=True)
        _write_vault(folder / "work.kdbx")
        storage.store_vault_file(config, "alice", "work.kdbx")
        _provision_passphrase(config)

        result = sync_user(config, "alice")
        assert result.outcome == OUTCOME_OK
        assert str(result.path).endswith("work.kdbx")

    def test_the_deployment_gate_sees_a_passphrase_alone(
        self, tmp_path, secret_key,
    ):
        """`any_vault_configured` is what the scheduler's interval gate reads,
        so a folder-configured user has to turn it on without a TOML line."""
        config = _vault_config(tmp_path, vault_path="", services=[])
        assert config.any_vault_configured() is False
        _provision_passphrase(config)
        assert config.any_vault_configured() is True


class TestNoValueIsLogged:
    def test_no_sync_log_record_carries_a_value(self, ready, caplog):
        from istota.secrets_vault import sync_user

        config, path = ready
        with caplog.at_level(logging.DEBUG):
            sync_user(config, "alice")
            _write_vault(path, ntfy=True)
            sync_user(config, "alice")

        needles = (API_KEY_VALUE, TOPIC_VALUE, PASSPHRASE, SECRET_KEY)
        for record in caplog.records:
            rendered = record.getMessage()
            for needle in needles:
                assert needle not in rendered, f"{needle!r} in {rendered!r}"
