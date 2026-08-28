"""The stored Nextcloud OAuth pair dying is an event the user has to be told about.

Both deletion sites in `web_tokens` used to log a WARNING in the web process and
nothing else, so a dead credential produced two unrelated-looking symptoms — web
turns reposted by the bot instead of appearing as the user, and read markers that
never sync — with no signal anywhere a user could see. `get_access_token` returns
None on every failure path and never raises, which is what made the loss silent
rather than merely quiet (ISSUE-333).

The notification is the durable record: `connected_service` already exists for
exactly this ("a stored credential the remote rejected"), it is object-backed, and
its resolver closes the row when the pair comes back.

The discriminating assertion throughout is that a *transient* failure raises
nothing. A test that only asserts "a row appears when the token dies" passes
against an implementation that raises on every failed read, which would push the
user once a minute while Nextcloud reboots.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota import web_tokens
from istota.config import Config, UserConfig, WebConfig
from istota.notification_resolvers import connected_service

KEY = "x" * 64


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture(autouse=True)
def _reset_locks():
    web_tokens._refresh_locks.clear()
    yield
    web_tokens._refresh_locks.clear()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="cs",
            token_storage="encrypted",
        ),
    )
    db.init_db(cfg.db_path)
    return cfg


def _rows(config, source="connected_service"):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = ?", (source,),
        ).fetchall()


def _sends(monkeypatch, *, delivered=True):
    calls: list[tuple] = []

    def _send(cfg, user_id, text, **kwargs):
        calls.append((user_id, text, kwargs.get("purpose")))
        return delivered

    monkeypatch.setattr("istota.notifications.send_notification", _send)
    return calls


def _seed(config, *, expires_in=3600):
    web_tokens.store_tokens(
        config.db_path, "alice", "access-abc", "refresh-xyz", expires_in,
    )


def _responder(monkeypatch, status, body=None):
    """Point the refresh endpoint at a canned response."""
    def _post(url, **kwargs):
        return httpx.Response(
            status, json=body if body is not None else {},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("httpx.post", _post)


class _LockProbe:
    """Records whether the framework DB was write-lockable during the call."""

    def __init__(self, config):
        self.config = config
        self.free: list[bool] = []

    def check(self):
        try:
            with db.get_db(self.config.db_path, busy_timeout_ms=200) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            self.free.append(True)
        except Exception:
            self.free.append(False)


class TestTheRefreshRejectionRaisesIt:
    """`invalid_grant` — the OAuth app was revoked, or the refresh token aged out.

    This is the path the reported incident took: the row was gone and the settings
    card read `connected: false`, with nothing between the deletion and the user
    noticing weeks later that their messages were being reposted by the bot.
    """

    def test_a_rejected_refresh_writes_an_open_row_and_pushes(
        self, keyed, config, monkeypatch,
    ):
        _seed(config, expires_in=-10)  # forces the refresh
        _responder(monkeypatch, 400, {"error": "invalid_grant"})
        sent = _sends(monkeypatch)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) is None

        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["dedup_key"] == "service:nextcloud"
        assert rows[0]["object_id"] == "nextcloud"
        assert rows[0]["state"] == "open"
        # Delivered, not merely written: the login callback is the only writer of
        # this credential and an active user never revisits it, so nothing else
        # will ever notice.
        assert rows[0]["last_delivered_at"] is not None
        assert len(sent) == 1 and sent[0][0] == "alice"

    def test_the_row_is_gone_as_before(self, keyed, config, monkeypatch):
        """The self-heal still happens — the notification is added, not swapped in."""
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 400, {"error": "invalid_grant"})
        _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

        assert web_tokens.token_status(config.db_path, "alice") is None

    def test_a_401_is_treated_the_same(self, keyed, config, monkeypatch):
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 401)
        _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

        assert len(_rows(config)) == 1


class TestTheDiscriminatingNegatives:
    """A failure that is not the credential's death must stay quiet.

    Without these, an implementation that raises on every `None` return passes the
    tests above and pushes the user once per poll while Nextcloud is restarting.
    """

    def test_a_transient_5xx_raises_nothing(self, keyed, config, monkeypatch):
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 503)
        sent = _sends(monkeypatch)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) is None

        assert _rows(config) == []
        assert sent == []
        # And the row is kept for a later retry, as before.
        assert web_tokens.token_status(config.db_path, "alice") is not None

    def test_a_network_error_raises_nothing(self, keyed, config, monkeypatch):
        _seed(config, expires_in=-10)

        def _post(url, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr("httpx.post", _post)
        sent = _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

        assert _rows(config) == []
        assert sent == []

    def test_no_stored_row_at_all_raises_nothing(self, keyed, config, monkeypatch):
        """A user who never connected has lost nothing.

        `get_access_token` returns None here too, and it is the most common None
        of the four — every read for every user on a deployment where the feature
        is on and the user has not logged in since.
        """
        sent = _sends(monkeypatch)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) is None

        assert _rows(config) == []
        assert sent == []

    def test_a_healthy_read_raises_nothing(self, keyed, config, monkeypatch):
        _seed(config)
        sent = _sends(monkeypatch)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) == "access-abc"

        assert _rows(config) == []
        assert sent == []


class TestKeyRotationRaisesIt:
    """The second deletion site: the row is there and will not decrypt.

    Operationally distinct from a rejected refresh — the remedy is the operator's
    (restore the key) rather than the user's — but indistinguishable to the user,
    who sees the same two broken features. The reason travels on the row so the
    two can be told apart afterwards, which is the thing the incident could not do.
    """

    def test_an_undecryptable_row_writes_an_open_row(
        self, keyed, config, monkeypatch,
    ):
        _seed(config)
        monkeypatch.setenv(web_tokens._KEY_ENV_VAR, "y" * 64)  # rotated
        sent = _sends(monkeypatch)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) is None

        rows = _rows(config)
        assert len(rows) == 1 and rows[0]["state"] == "open"
        assert len(sent) == 1

    def test_the_two_causes_are_distinguishable_on_the_row(
        self, keyed, config, monkeypatch,
    ):
        """Which of the two fired was not recoverable after the fact — the entry
        says so explicitly. The stored reason is what makes it recoverable."""
        _seed(config)
        monkeypatch.setenv(web_tokens._KEY_ENV_VAR, "y" * 64)
        _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

        import json
        params = json.loads(_rows(config)[0]["params"])
        assert "key" in params["reason"].lower()


class TestTheRowClosesWhenTheCredentialComesBack:
    def test_storing_a_pair_closes_an_open_row(self, keyed, config, monkeypatch):
        """The login callback re-mints, and the warning must not outlive the fix."""
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 400)
        _sends(monkeypatch)
        web_tokens.get_access_token(config.db_path, config, "alice")
        assert _rows(config)[0]["state"] == "open"

        web_tokens.store_tokens(
            config.db_path, "alice", "new-access", "new-refresh", 3600,
        )

        assert _rows(config)[0]["state"] == "resolved"

    def test_a_first_ever_login_closes_nothing_and_does_not_fail(
        self, keyed, config,
    ):
        """No open row is the normal case; the close must be a no-op, not an error."""
        web_tokens.store_tokens(
            config.db_path, "alice", "a", "r", 3600,
        )

        assert _rows(config) == []
        assert web_tokens.token_status(config.db_path, "alice") is not None


class TestTheResolver:
    """The backstop behind the close path, per the source's own contract."""

    def test_it_renders_while_the_pair_is_missing(self, keyed, config):
        view = self._view(config)
        assert view is not None
        assert "Nextcloud" in view.title

    def test_it_returns_none_once_the_pair_is_back(self, keyed, config):
        _seed(config)
        assert self._view(config) is None

    def test_its_reconnect_action_is_a_safe_path(self, keyed, config):
        view = self._view(config)
        actions = [a for a in view.actions if a.id == "reconnect"]
        assert len(actions) == 1
        assert sources.is_safe_path(actions[0].href)

    def test_the_action_points_at_the_reconnect_route_not_the_settings_page(
        self, keyed, config,
    ):
        """The whole value of item 1 in the entry is that the user does not have
        to work out that a full logout is the remedy. A link to the settings page
        lands them back on the card that told them to log out."""
        view = self._view(config)
        href = next(a.href for a in view.actions if a.id == "reconnect")
        assert href == connected_service.RECONNECT_HREFS["nextcloud"]
        assert "reconnect" in href

    def _view(self, config):
        row = _row_for(config, "nextcloud")
        with db.get_db(config.db_path) as conn:
            return connected_service.RESOLVER.resolve(config, conn, row)


class TestGarminIsUnchanged:
    """The source served one service; adding a second must not move the first."""

    def test_garmin_keeps_its_settings_link(self):
        assert connected_service.RECONNECT_HREFS["garmin"] == "/settings"

    def test_garmin_keeps_its_label(self):
        assert connected_service.label_for("garmin") == "Garmin Connect"

    def test_an_unknown_service_still_renders_nothing(self, config):
        row = _row_for(config, "not-a-service")
        with db.get_db(config.db_path) as conn:
            assert connected_service.RESOLVER.resolve(config, conn, row) is None


def _row_for(config, service):
    """A NotificationRow standing for a stored row naming `service`."""
    from istota.notification_sources import NotificationRow

    return NotificationRow(
        id=1,
        user_id="alice",
        source=connected_service.SOURCE,
        dedup_key=connected_service.dedup_key(service),
        title=connected_service.title_for(service),
        body=connected_service.body_for(service),
        severity="warning",
        actionable=True,
        object_type=connected_service.OBJECT_TYPE,
        object_id=service,
        params={"service": service, "reason": ""},
        state="open",
        created_at="2026-08-28T00:00:00Z",
        updated_at="2026-08-28T00:00:00Z",
    )


class TestTheProducerHoldsNoWriteLock:
    """`raise_notification` opens its own connection and is only safe for a caller
    that holds none. `web_tokens` reaches the DB through `_connect`, which opens
    and closes around each statement — asserted rather than argued."""

    def test_the_framework_db_is_lockable_at_the_raise(
        self, keyed, config, monkeypatch,
    ):
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 400)
        _sends(monkeypatch)
        probe = _LockProbe(config)
        real_raise = store.raise_notification

        def _spy(cfg, user_id, **kwargs):
            probe.check()
            return real_raise(cfg, user_id, **kwargs)

        monkeypatch.setattr("istota.notification_store.raise_notification", _spy)

        web_tokens.get_access_token(config.db_path, config, "alice")

        assert probe.free == [True], (
            "web_tokens was holding the framework write lock at the raise"
        )


class TestTheRotationHole:
    """Nextcloud rotates the refresh token on every refresh and invalidates the
    old one, and the persist happens *after* the response is parsed. A failure
    there leaves the server holding a pair the DB does not — unrecoverable
    except by re-login (ISSUE-333, item 5).

    It used to fail two ways at once: silently, and by escaping. `_refresh` did
    not guard `store_tokens`, so a write failure propagated straight out of
    `get_access_token`, whose docstring says it never raises to callers.
    """

    def _refresh_then_fail_to_persist(self, config, monkeypatch):
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 200, {
            "access_token": "new-a", "refresh_token": "new-r", "expires_in": 3600,
        })

        def _boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(web_tokens, "store_tokens", _boom)

    def test_it_does_not_escape_into_the_caller(self, keyed, config, monkeypatch):
        """The contract this broke: `get_access_token` never raises."""
        self._refresh_then_fail_to_persist(config, monkeypatch)
        _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

    def test_the_in_flight_caller_still_gets_its_token(
        self, keyed, config, monkeypatch,
    ):
        """The freshly minted access token is handed back rather than discarded.

        It is valid for `expires_in` seconds and the caller has work to do now;
        only the *next* read degrades, which is what the notice is for. Returning
        None here would fail the in-flight request too, on a token that works.
        """
        self._refresh_then_fail_to_persist(config, monkeypatch)
        _sends(monkeypatch)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) == "new-a"

    def test_it_raises_the_reconnect_notice(self, keyed, config, monkeypatch):
        self._refresh_then_fail_to_persist(config, monkeypatch)
        sent = _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

        rows = _rows(config)
        assert len(rows) == 1 and rows[0]["state"] == "open"
        assert len(sent) == 1

    def test_it_drops_the_dead_row_so_the_card_stops_claiming_connected(
        self, keyed, config, monkeypatch,
    ):
        """The stored pair is already dead — the server invalidated the refresh
        token we just spent. Keeping it leaves the settings card saying
        "Connected" about a credential that can never work again."""
        self._refresh_then_fail_to_persist(config, monkeypatch)
        _sends(monkeypatch)

        web_tokens.get_access_token(config.db_path, config, "alice")

        assert web_tokens.token_status(config.db_path, "alice") is None

    def test_it_says_so_at_error_level(self, keyed, config, monkeypatch, caplog):
        """A WARNING is what the whole issue is about being unreadable. This one
        is unrecoverable without the user, so it is an error."""
        self._refresh_then_fail_to_persist(config, monkeypatch)
        _sends(monkeypatch)

        with caplog.at_level("ERROR", logger="istota.web_tokens"):
            web_tokens.get_access_token(config.db_path, config, "alice")

        assert any(r.levelname == "ERROR" for r in caplog.records)


class TestItNeverRaisesIntoTheCaller:
    """`get_access_token`'s contract is that it returns None and never raises.
    A notification failure must not be able to break a web request."""

    def test_a_broken_notification_store_does_not_propagate(
        self, keyed, config, monkeypatch,
    ):
        _seed(config, expires_in=-10)
        _responder(monkeypatch, 400)

        def _boom(*a, **k):
            raise sqlite3.OperationalError("no such table: notifications")

        monkeypatch.setattr("istota.notification_store.raise_notification", _boom)

        assert web_tokens.get_access_token(
            config.db_path, config, "alice",
        ) is None
        # And the credential self-heal still completed.
        assert web_tokens.token_status(config.db_path, "alice") is None
