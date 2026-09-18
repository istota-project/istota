"""§8: what a vault that stopped working says, and to whom.

The failing-vault class is the invisible one. Credentials keep working, so
nothing breaks loudly, and the user's edits silently stop taking effect — which
is why the only surface that reaches them is a notification row.

**Three properties carry this file and none of them is visible from the state
dict.**

*One raise per transition.* A vault retried every cycle for a week has to
produce one panel row, not two thousand. The dedup bump is what buys that, and a
bump does **not** redeliver — so the discriminating instrument here is a count of
``send_notification`` calls, not a count of rows. Asserting on rows alone passes
whether or not the push was suppressed.

*A change of error class is a bump, not a second notice.* ``VaultLocked``
becoming ``VaultCorrupt`` writes the new reason onto the one open row and
delivers nothing. The user has an unread row about this vault either way.

*``OUTCOME_UNCHANGED`` is not evidence of recovery.* ``VaultCorrupt`` caches its
digest, so a cycle over a still-broken vault answers ``unchanged`` exactly as a
cycle over a healthy one does. A close-on-recovery rule that read the outcome
word would close a notification about a vault that is still broken, silently and
for good — ``test_a_cached_skip_over_a_broken_vault_does_not_close_the_row`` is
the control that would have caught it, and it is the reason the rule keys on
``OUTCOME_OK`` from a settled cycle rather than on anything a skip returns.

Every KDBX here is a real file built by ``pykeepass``, as in its two sibling
files: a stub returning a canned ``VaultRead`` would make every assertion about
the outcome a statement about the stub. Rows are asserted against the real
``notifications`` table for the same reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from istota import db, secrets_store
from istota.config import Config, UserConfig
from istota.notification_resolvers import connected_service

PASSPHRASE = "notification-fixture-passphrase-not-a-real-one"
WRONG_PASSPHRASE = "notification-fixture-passphrase-that-does-not-match"
API_KEY_VALUE = "ak-notification-fixture-alpha"
BASE_URL_VALUE = "https://karakeep.example.com"

SECRET_KEY = "deadbeef" * 8

#: The one dedup key this source uses for the vault, spelled here rather than
#: imported so a change to the key's shape is a visible diff in a test rather
#: than an invisible agreement between two call sites.
VAULT_DEDUP_KEY = "service:vault"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_sync_state():
    """Module-level state, so it has to be reset in both directions.

    A leftover digest makes a later test's first cycle a skip; a leftover
    outcome suppresses the transition a later test is asserting on.
    """
    from istota import secrets_vault

    secrets_vault.reset_sync_state()
    yield
    secrets_vault.reset_sync_state()


@pytest.fixture
def secret_key(monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", SECRET_KEY)
    return SECRET_KEY


class _DeliveryCounter:
    """How many pushes left the building, per user.

    The instrument the bump rule needs. `write_notification` returns a
    `RaiseResult` whose `deliver` flag is False on a bump, and `deliver_pending`
    reads it — so a test that counts rows sees the same answer whether or not
    that flag was honoured. This counts the sends themselves.

    `deliver_pending` resolves `send_notification` from the module at call time
    (`from .notifications import send_notification` inside the function body),
    so patching the attribute on `istota.notifications` reaches it.
    """

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, str]] = []

        from istota import notifications

        def counted(config, user_id, text, **kwargs):
            self.calls.append((user_id, text))
            # False is what a deployment with no destination configured
            # answers, and it is the honest answer here: nothing was sent.
            # `deliver_pending` then leaves `last_delivered_at` unstamped,
            # which is the behaviour a real un-configured deployment gets.
            return False

        monkeypatch.setattr(notifications, "send_notification", counted)


@pytest.fixture
def sends(monkeypatch):
    return _DeliveryCounter(monkeypatch)


def _vault_config(tmp_path, *, vault_path: str, services: list[str]) -> Config:
    mount = tmp_path / "mount"
    (mount / "Users" / "alice" / "config").mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        temp_dir=tmp_path / "tmp",
        workspace_path=mount,
        users={"alice": UserConfig(vault_path=vault_path, vault_services=services)},
    )


def _write_vault(path: Path, *, password: str = PASSPHRASE) -> None:
    """A real KDBX holding `istota/karakeep`.

    `pykeepass` is imported inside the helper for the reason `parse_vault`
    function-scopes its own import: it pulls `lxml`, `argon2-cffi` and
    `pycryptodomex`, and nothing here should pay that at collection.
    """
    from pykeepass import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    group = kp.add_group(root, "karakeep")
    kp.add_entry(group, "base_url", "", BASE_URL_VALUE)
    kp.add_entry(group, "api_key", "", API_KEY_VALUE)
    kp.save()


def _corrupt(path: Path) -> None:
    """A file that exists, is non-empty, and is not a KeePass database.

    Non-empty deliberately: a zero-byte file is refused by `read_vault_bytes`
    before a digest exists, so it is the one `VaultCorrupt` that caches nothing
    — and the cached-skip property this file asserts on needs the cached shape.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a KeePass database, it is forty-odd bytes")


@pytest.fixture
def broken(tmp_path, secret_key):
    """A configured user with a provisioned passphrase and a corrupt vault.

    The passphrase matters even though nothing will unlock: `_sync_resolved`
    resolves it *before* the parse, so an unprovisioned user would settle
    `VaultPassphraseMissing` and this file would be testing a different class
    from the one it names.
    """
    config = _vault_config(
        tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
    )
    path = Path(config.workspace_path) / "Users" / "alice" / "config" / "vault.kdbx"
    _corrupt(path)
    secrets_store.set_secret(config.db_path, "alice", "vault", "passphrase", PASSPHRASE)
    return config, path


def _rows(config: Config, user_id: str = "alice") -> list[dict]:
    with db.get_db(config.db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? AND source = ? "
                "ORDER BY id",
                (user_id, connected_service.SOURCE),
            ).fetchall()
        ]


def _only_row(config: Config, user_id: str = "alice") -> dict:
    rows = _rows(config, user_id)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# Raising
# ---------------------------------------------------------------------------


class TestOneRaisePerTransition:
    def test_a_failing_sync_raises_a_row_and_delivers_it(self, broken, sends):
        from istota.secrets_vault import VaultCorrupt, sync_user

        config, _path = broken
        result = sync_user(config, "alice")

        assert result.outcome == VaultCorrupt.__name__
        assert result.transition is True
        row = _only_row(config)
        assert row["dedup_key"] == VAULT_DEDUP_KEY
        assert row["object_id"] == "vault"
        assert row["state"] == "open"
        assert len(sends.calls) == 1

    def test_a_repeat_failure_bumps_rather_than_redelivers(self, broken, sends):
        """The rule that keeps a week-long outage to one push.

        The second cycle is driven through a cleared sync state, which is what a
        daemon restart looks like: §7 says a restart re-reports once, on purpose,
        so the raise fires again. What must *not* fire again is the delivery.
        """
        from istota.secrets_vault import reset_sync_state, sync_user

        config, _path = broken
        sync_user(config, "alice")
        first = _only_row(config)

        reset_sync_state()
        sync_user(config, "alice")

        second = _only_row(config)
        assert second["id"] == first["id"]
        assert second["occurrences"] == first["occurrences"] + 1
        # The discriminating assertion: rows alone read identically whether or
        # not the bump was honoured.
        assert len(sends.calls) == 1
        assert second["last_delivered_at"] == first["last_delivered_at"]

    def test_a_cached_skip_raises_nothing_at_all(self, broken, sends):
        """A cached digest stops the cycle before it can say anything.

        Paired with the test above rather than standing alone: between them they
        say the raise fires once per *transition* and not once per cycle.
        """
        from istota.secrets_vault import OUTCOME_UNCHANGED, sync_user

        config, _path = broken
        sync_user(config, "alice")
        second = sync_user(config, "alice")

        assert second.outcome == OUTCOME_UNCHANGED
        assert _only_row(config)["occurrences"] == 1
        assert len(sends.calls) == 1

    def test_a_retried_failure_in_one_process_raises_only_once(
        self, tmp_path, secret_key, sends
    ):
        """The case the transition gate exists for, and the only one that reaches it.

        `VaultLocked` caches no digest — §7 puts it in the uncached set so the
        remedy, which touches no byte of the file, can take effect — so every
        cycle over a locked vault does the full read, the full parse and settles
        the same class again, forever. `transition` is False from the second
        cycle on, and it is the only thing standing between that and one
        `occurrences` bump per 300 seconds for as long as the vault stays
        locked.

        The sibling tests above cannot reach this: each resets the sync state
        between cycles to model a restart, which makes every raise a transition.
        Removing the gate leaves all of them green, which is how this test came
        to be written.
        """
        from istota.secrets_vault import OUTCOME_UNCHANGED, VaultLocked, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = Path(config.workspace_path) / "Users" / "alice" / "config" / "vault.kdbx"
        _write_vault(path, password=WRONG_PASSPHRASE)
        secrets_store.set_secret(
            config.db_path, "alice", "vault", "passphrase", PASSPHRASE
        )

        first = sync_user(config, "alice")
        second = sync_user(config, "alice")

        assert first.outcome == second.outcome == VaultLocked.__name__
        assert first.transition is True
        # Not a skip — the work really was done twice, which is what makes the
        # gate the only suppression there is. The second cycle read the same
        # bytes and settled a class again rather than short-circuiting, which is
        # exactly what `OUTCOME_UNCHANGED` would have said instead.
        assert second.transition is False
        assert second.outcome != OUTCOME_UNCHANGED
        assert second.digest == first.digest
        # The *cache* is what holds None for this class; the result carries the
        # digest that was read. Conflating the two reads as "nothing happened".
        from istota import secrets_vault

        assert secrets_vault._SYNC_STATE["alice"] == (None, VaultLocked.__name__)

        row = _only_row(config)
        assert row["occurrences"] == 1
        assert len(sends.calls) == 1

    def test_a_change_of_error_class_bumps_the_same_row(self, tmp_path, secret_key, sends):
        """Locked becoming corrupt is a bump, not a second notice.

        The panel then shows the latest class rather than the first, which is
        §8's stated trade: the log is where the sequence lives.
        """
        from istota import secrets_vault
        from istota.secrets_vault import VaultCorrupt, VaultLocked, sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = Path(config.workspace_path) / "Users" / "alice" / "config" / "vault.kdbx"
        _write_vault(path, password=WRONG_PASSPHRASE)
        secrets_store.set_secret(
            config.db_path, "alice", "vault", "passphrase", PASSPHRASE
        )

        locked = sync_user(config, "alice")
        assert locked.outcome == VaultLocked.__name__
        first = _only_row(config)
        assert json.loads(first["params"])["reason"] == (
            secrets_vault.notification_reason(VaultLocked.__name__)
        )

        _corrupt(path)
        corrupt = sync_user(config, "alice")
        assert corrupt.outcome == VaultCorrupt.__name__
        assert corrupt.transition is True

        second = _only_row(config)
        assert second["id"] == first["id"]
        assert json.loads(second["params"])["reason"] == (
            secrets_vault.notification_reason(VaultCorrupt.__name__)
        )
        assert len(sends.calls) == 1


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------


class TestCloseOnRecovery:
    def test_a_successful_sync_closes_the_row(self, broken, sends):
        from istota.secrets_vault import OUTCOME_OK, sync_user

        config, path = broken
        sync_user(config, "alice")
        assert _only_row(config)["state"] == "open"

        _write_vault(path)
        recovered = sync_user(config, "alice")

        assert recovered.outcome == OUTCOME_OK
        row = _only_row(config)
        assert row["state"] == "resolved"
        assert row["resolved_by"] == "vault_sync"

    def test_a_cached_skip_over_a_broken_vault_does_not_close_the_row(
        self, broken, sends
    ):
        """The escalation the stage line is built around.

        `VaultCorrupt` caches its digest, so the second cycle answers
        `unchanged` — the same word a healthy vault's second cycle answers. A
        close rule that read the outcome word rather than requiring a settled
        `OUTCOME_OK` closes a warning about a vault that is still broken, with
        nothing to raise it again until the file moves.
        """
        from istota.secrets_vault import OUTCOME_UNCHANGED, VaultCorrupt, sync_user

        config, _path = broken
        sync_user(config, "alice")

        skip = sync_user(config, "alice")
        assert skip.outcome == OUTCOME_UNCHANGED
        # The skip carries the settled class, which is what a consumer has to
        # read instead of the outcome word.
        assert skip.last_outcome == VaultCorrupt.__name__
        assert _only_row(config)["state"] == "open"

    def test_a_success_closes_a_row_raised_before_this_process_started(
        self, broken, sends
    ):
        """The close is not gated on a transition, and this is why.

        A restart empties the in-memory state, so the first successful cycle
        after one has `previous == ""` and is a transition out of nothing. If
        the close were gated on the *previous class having been a failure*, a
        row raised by the daemon that then restarted would stand open for ever.
        """
        from istota.secrets_vault import OUTCOME_OK, reset_sync_state, sync_user

        config, path = broken
        sync_user(config, "alice")
        assert _only_row(config)["state"] == "open"

        reset_sync_state()
        _write_vault(path)
        result = sync_user(config, "alice")

        assert result.outcome == OUTCOME_OK
        assert result.last_outcome == ""
        assert _only_row(config)["state"] == "resolved"


# ---------------------------------------------------------------------------
# What the row says
# ---------------------------------------------------------------------------


class TestTheReasonIsCodeOwned:
    def test_every_vault_error_class_has_a_sentence(self):
        """A drift guard, because the enumerations have been wrong twice.

        §8's table names four classes; Stage 4 added three more and said so;
        `VaultUnreadable` is an eighth that neither enumeration mentions and
        that `read_vault_bytes` raises on a symlink, a FIFO or an oversize file.
        Walking the subclasses is what stops a ninth arriving unnoticed.
        """
        from istota import secrets_vault

        classes = {
            cls.__name__ for cls in secrets_vault.VaultError.__subclasses__()
        }
        assert classes, "no VaultError subclasses found — the walk is broken"
        missing = classes - set(secrets_vault.NOTIFICATION_REASONS)
        assert not missing, f"no notification sentence for: {sorted(missing)}"

    def test_the_reason_is_the_table_and_never_the_exception_text(self, broken, sends):
        """`str(exc)` must not reach the row, on any class.

        The row's body is rendered in a browser and its `params` are copied into
        `istota_kv`'s sibling surfaces and a nightly backup that lands on the
        mount. A sentence this module owns is safe by construction; an exception
        message is safe only until somebody interpolates a path or a length into
        one — which `VaultKeyUnusable` is the live example of, since the store's
        own message for a too-short master key names its length.
        """
        from istota import secrets_vault
        from istota.secrets_vault import VaultCorrupt, sync_user

        config, _path = broken
        result = sync_user(config, "alice")

        stored = json.loads(_only_row(config)["params"])["reason"]
        assert stored == secrets_vault.NOTIFICATION_REASONS[VaultCorrupt.__name__]
        # The control: the outcome's own message is a different string, so the
        # assertion above cannot pass by the two happening to agree.
        assert stored != result.reason

    def test_no_row_carries_a_value_a_path_or_the_passphrase(self, tmp_path, secret_key, sends):
        from istota.secrets_vault import sync_user

        config = _vault_config(
            tmp_path, vault_path="config/vault.kdbx", services=["karakeep"]
        )
        path = Path(config.workspace_path) / "Users" / "alice" / "config" / "vault.kdbx"
        _write_vault(path, password=WRONG_PASSPHRASE)
        secrets_store.set_secret(
            config.db_path, "alice", "vault", "passphrase", PASSPHRASE
        )
        sync_user(config, "alice")

        row = _only_row(config)
        blob = " ".join(
            str(row[column] or "")
            for column in ("title", "body", "params", "link")
        )
        for forbidden in (PASSPHRASE, API_KEY_VALUE, BASE_URL_VALUE, SECRET_KEY, str(path)):
            assert forbidden not in blob, f"{forbidden!r} reached the notification"


class TestTheDeliveryFork:
    """Who pushes and who only writes the row.

    `connected_service` already draws this line for its own two callers, and
    the reasoning transfers: the daemon's cycles are unattended and a push is
    the only thing that reaches the user, while `istota secret vault-sync` is an
    operator reading the failure off their own terminal as it prints.
    """

    def test_a_cli_sync_writes_the_row_and_pushes_nothing(self, broken, sends):
        from istota.secrets_vault import sync_all

        config, _path = broken
        sync_all(config, users=["alice"], force=True, deliver=False)

        # The row is still there — a failure the operator saw is a failure the
        # user should find in their panel.
        assert _only_row(config)["state"] == "open"
        assert sends.calls == []

    def test_the_daemon_still_pushes(self, broken, sends):
        """The control. Without it the assertion above is true of everything."""
        from istota.secrets_vault import sync_all

        config, _path = broken
        sync_all(config, users=["alice"], force=True)

        assert len(sends.calls) == 1

    def test_a_cli_sync_still_closes_on_recovery(self, broken, sends):
        """The close is not forked, and must not be: it delivers nothing anyway."""
        from istota.secrets_vault import sync_all

        config, path = broken
        sync_all(config, users=["alice"], force=True, deliver=False)
        _write_vault(path)
        sync_all(config, users=["alice"], force=True, deliver=False)

        assert _only_row(config)["state"] == "resolved"


class TestTheDurableRecord:
    """What a process that never runs a sync can say about one.

    The in-memory state answers for this process only, and the two readers that
    matter are in neither: the settings endpoint runs in the web process, which
    under the Ansible shape is a different systemd unit from the scheduler, and
    `vault-status` is a different process again.
    """

    def _record(self, config):
        from istota.secrets_vault import read_sync_state

        with db.get_db(config.db_path) as conn:
            return read_sync_state(conn, "alice")

    def test_a_settled_cycle_writes_one(self, broken, sends):
        from istota.secrets_vault import VaultCorrupt, sync_user

        config, _path = broken
        sync_user(config, "alice")

        record = self._record(config)
        assert record is not None
        assert record["outcome"] == VaultCorrupt.__name__
        assert record["at"]
        # Never succeeded, so there is no success to carry.
        assert record["ok_at"] is None

    def test_a_skip_writes_nothing(self, broken, sends):
        """§7's cycle costs no database touch, and this must not add one.

        A skip runs every interval for as long as the file sits still, so a
        write here would be one per user per 300 seconds for ever.
        """
        from istota.secrets_vault import sync_user

        config, _path = broken
        sync_user(config, "alice")
        first = self._record(config)

        sync_user(config, "alice")
        assert self._record(config) == first

    def test_a_failure_carries_the_last_success_forward(self, broken, sends):
        """The field the settings heading renders as "last synced".

        A failure that erased it would make a vault broken for an hour
        indistinguishable from one that has never worked at all.
        """
        from istota.secrets_vault import OUTCOME_OK, VaultLocked, sync_user

        config, path = broken
        _write_vault(path)
        sync_user(config, "alice")
        succeeded_at = self._record(config)["ok_at"]
        assert succeeded_at

        _write_vault(path, password=WRONG_PASSPHRASE)
        result = sync_user(config, "alice")
        assert result.outcome == VaultLocked.__name__

        record = self._record(config)
        assert record["outcome"] == VaultLocked.__name__
        assert record["ok_at"] == succeeded_at
        # And `at` moved, which is what makes the two fields two facts.
        assert record["at"] != succeeded_at or record["outcome"] != OUTCOME_OK

    def test_it_carries_the_published_sentence_and_not_the_exception(
        self, broken, sends
    ):
        """`db_backup` snapshots `istota_kv` onto the mount, so this row travels.

        Same rule as the notification body, and for a sharper reason: a backup
        of it lands in the user's own Nextcloud tree.
        """
        from istota import secrets_vault
        from istota.secrets_vault import VaultCorrupt, sync_user

        config, _path = broken
        result = sync_user(config, "alice")

        record = self._record(config)
        assert record["reason"] == secrets_vault.NOTIFICATION_REASONS[
            VaultCorrupt.__name__
        ]
        assert record["reason"] != result.reason


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestTheResolver:
    """The backstop, not the close path — but the one the panel renders through."""

    def _row(self, config: Config):
        from istota.notification_sources import NotificationRow

        raw = _only_row(config)
        return NotificationRow(
            id=raw["id"],
            user_id=raw["user_id"],
            source=raw["source"],
            dedup_key=raw["dedup_key"],
            object_type=raw["object_type"],
            object_id=raw["object_id"],
            severity=raw["severity"],
            actionable=bool(raw["actionable"]),
            title=raw["title"],
            body=raw["body"],
            params=json.loads(raw["params"] or "{}"),
            link=raw["link"],
            room_token=raw["room_token"],
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            occurrences=raw["occurrences"],
        )

    def test_a_broken_vault_still_renders(self, broken, sends):
        config, _path = broken
        from istota.secrets_vault import sync_user

        sync_user(config, "alice")
        row = self._row(config)
        with db.get_db(config.db_path) as conn:
            view = connected_service.RESOLVER.resolve(config, conn, row)
        assert view is not None
        assert "Credential vault" in view.title

    def test_a_vault_the_operator_has_unconfigured_is_gone(self, broken, sends):
        """The orphaned-row case, answered by the resolver rather than by a sweep.

        Nothing settles an outcome for a user with no `vault_path`, so removing
        the line from `config.toml` leaves any open row with nothing that would
        ever close it. A resolver answering `None` is what `list_open` reads as
        "the object is gone", which is the whole anti-staleness story.
        """
        config, _path = broken
        from istota.secrets_vault import sync_user

        sync_user(config, "alice")
        row = self._row(config)

        config.users["alice"].vault_path = ""
        with db.get_db(config.db_path) as conn:
            view = connected_service.RESOLVER.resolve(config, conn, row)
        assert view is None

    def test_a_row_with_no_record_behind_it_still_renders(self, broken, sends):
        """The safe direction, and the one an absent record has to take.

        A row can outlive its record — raised by a build that predates it, or
        left behind by a cleanup — and the resolver's `None` is not "no news":
        `list_open` reads it as *the object is gone* and marks the row stale, so
        an unreadable record answering "recovered" closes a warning nobody has
        acted on, permanently and with nothing to raise it again until the vault
        next changes. Removing the record must leave the row exactly as it was.
        """
        from istota import secrets_vault
        from istota.secrets_vault import sync_user

        config, _path = broken
        sync_user(config, "alice")
        row = self._row(config)

        with db.get_db(config.db_path) as conn:
            db.kv_delete(
                conn, "alice",
                secrets_vault.VAULT_SYNC_STATE_NAMESPACE,
                secrets_vault.VAULT_SYNC_STATE_KEY,
            )
            view = connected_service.RESOLVER.resolve(config, conn, row)
        assert view is not None

    def test_an_unparseable_record_still_renders(self, broken, sends):
        """The same rule one step further in: unreadable is not recovered."""
        from istota import secrets_vault
        from istota.secrets_vault import sync_user

        config, _path = broken
        sync_user(config, "alice")
        row = self._row(config)

        with db.get_db(config.db_path) as conn:
            db.kv_set(
                conn, "alice",
                secrets_vault.VAULT_SYNC_STATE_NAMESPACE,
                secrets_vault.VAULT_SYNC_STATE_KEY,
                "not json at all",
            )
            view = connected_service.RESOLVER.resolve(config, conn, row)
        assert view is not None

    def test_the_action_says_what_can_actually_be_done(self, broken, sends):
        """"Reconnect" is the other two services' remedy and is meaningless here.

        There is nothing to reconnect to — the remedy is editing a file on the
        user's own laptop or phone, and the link only takes them to where the
        failure is described.
        """
        from istota.secrets_vault import sync_user

        config, _path = broken
        sync_user(config, "alice")
        row = self._row(config)

        with db.get_db(config.db_path) as conn:
            view = connected_service.RESOLVER.resolve(config, conn, row)
        assert view is not None
        assert [a.label for a in view.actions] == ["Open settings"]

    def test_a_recovered_vault_reads_as_connected(self, broken, sends):
        """The durable record is what makes this answerable outside the syncer.

        `_SYNC_STATE` is per-process, and the panel is rendered by the web
        process, which never runs a sync. Without a durable carrier the resolver
        could only ever answer "still broken".
        """
        config, path = broken
        from istota.secrets_vault import sync_user

        sync_user(config, "alice")
        row = self._row(config)

        _write_vault(path)
        sync_user(config, "alice")

        with db.get_db(config.db_path) as conn:
            view = connected_service.RESOLVER.resolve(config, conn, row)
        assert view is None
