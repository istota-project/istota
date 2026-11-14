"""Relay route revocation: the queue written with a device's deletion, and the sweep that empties it.

`RevokingRelay` follows istota-relay's `DELETE {base}/v1/routes/{id}` (its
`src/relay/app.py:delete_route` and `enroll.revoke`): either capability as the
bearer, 204 for a route revoked now or already gone, 401 for a capability that
is not the route's, and 429 with `retry_after` when the per-source bearer
limit refuses the request.
"""

import logging
import sqlite3

import httpx
import pytest

from istota import db, doctor
from istota.config import Config, PushConfig
from istota.push import apns, devices, relay_client, revocations
from tests.test_push_relay_sending import (  # noqa: F401
    CAPABILITY, OTHER_CAPABILITY, RELAY_URL, _enroll, relay, route_id, row,
)

NOW = 2_000_000_000
DAY = 86_400


class RevokingRelay:
    """The relay's revoke endpoint. Answers are queued as (status, body, headers); default 204."""

    def __init__(self):
        self.capabilities = {}  # route_id -> capability, for routes still active
        self.requests = []
        self.answers = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = "/istota/v1/routes/"
        path = request.url.path
        assert request.method == "DELETE" and path.startswith(prefix), f"{request.method} {request.url}"
        assert request.content == b""
        if self.answers:
            status, body, headers = self.answers.pop(0)
            if isinstance(body, dict):
                return httpx.Response(status, json=body, headers=headers)
            return httpx.Response(status, content=body, headers=headers)
        route = path[len(prefix):]
        active = self.capabilities.get(route)
        if active is None:
            return httpx.Response(204)
        if request.headers.get_list("authorization") != [f"Bearer {active}"]:
            return httpx.Response(401, json={"result": "unauthorized_route"}, headers={"www-authenticate": "Bearer"})
        del self.capabilities[route]
        return httpx.Response(204)

    def answer(self, status, body=b"", headers=None):
        self.answers.append((status, body, headers or {}))


@pytest.fixture
def revoking(relay, monkeypatch):  # noqa: F811
    mock = RevokingRelay()
    # Sends still reach the relay fixture's mock; revocations reach this one.
    route = lambda request: mock(request) if request.method == "DELETE" else relay.mock(request)  # noqa: E731
    client = httpx.Client(transport=httpx.MockTransport(route), follow_redirects=False)
    monkeypatch.setattr(relay_client, "_client", lambda: client)
    clock = {"now": NOW}
    monkeypatch.setattr(revocations.time, "time", lambda: clock["now"])
    monkeypatch.setattr(devices.time, "time", lambda: clock["now"])
    relay.revoker = mock
    relay.clock = clock
    relay.enroll_route = lambda token="ab" * 32, route=1, **kw: _enroll_route(relay, token, route, **kw)
    yield relay
    client.close()


def _enroll_route(state, token, route, **kw):
    device_id = _enroll(state, token, route, **kw)
    state.revoker.capabilities[route_id(route)] = kw.get("capability", CAPABILITY)
    return device_id


def queued(state) -> list[dict]:
    with db.get_db(state.config.db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM push_relay_revocations ORDER BY created_at, route_id")]


def delete(state, device_id, user="alice"):
    with db.get_db(state.config.db_path) as conn:
        return devices.delete_device(conn, device_id, user, relay_url=state.config.push.relay_url)


def sweep(state):
    revocations.run_relay_revocations(state.config)


# --- The schema ----------------------------------------------------------------------


def _shape(conn):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(push_relay_revocations)")]
    indexes = [r[1] for r in conn.execute("PRAGMA index_list(push_relay_revocations)")]
    return columns, "idx_push_relay_revocations_due" in indexes


def test_the_table_and_its_index_exist_on_a_fresh_and_an_upgraded_database(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    expected = (["route_id", "credential_ref", "relay_url", "next_attempt_at", "attempts",
                 "expires_at", "last_error", "created_at"], True)
    with db.get_db(path) as conn:
        assert _shape(conn) == expected
        conn.execute("DROP TABLE push_relay_revocations")
        for _ in range(2):
            db._migrate_push_tables(conn)
        assert _shape(conn) == expected


# --- Queueing ------------------------------------------------------------------------


def test_deleting_a_relay_device_queues_its_route_and_keeps_the_file(revoking):
    device_id = revoking.enroll_route()
    assert delete(revoking, device_id)
    assert row(revoking, device_id) is None
    assert queued(revoking) == [{
        "route_id": route_id(1), "credential_ref": f"{device_id}.cap", "relay_url": RELAY_URL,
        "next_attempt_at": NOW, "attempts": 0, "expires_at": NOW + 7 * DAY, "last_error": None,
        "created_at": NOW,
    }]
    assert (revoking.directory / f"{device_id}.cap").read_text("ascii") == CAPABILITY


def test_deleting_a_device_without_a_route_queues_nothing(revoking):
    with db.get_db(revoking.config.db_path) as conn:
        device_id = devices.register_device(conn, "alice", "ef" * 32, "production", auth_method="nextcloud",
                                            auth_epoch=0)
    assert delete(revoking, device_id)
    assert queued(revoking) == []


def test_a_session_deletion_queues_every_relay_route_it_removes(revoking):
    first = revoking.enroll_route()
    second = revoking.enroll_route("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET auth_session_id = 'login-1'")
        devices.delete_session_devices(conn, "alice", "login-1", relay_url=RELAY_URL)
    assert row(revoking, first) is None and row(revoking, second) is None
    assert [(r["route_id"], r["credential_ref"]) for r in queued(revoking)] == [
        (route_id(1), f"{first}.cap"), (route_id(2), f"{second}.cap")]


def _refuse_queue_inserts(state):
    with db.get_db(state.config.db_path) as conn:
        conn.execute("CREATE TRIGGER refuse_revocation BEFORE INSERT ON push_relay_revocations "
                     "BEGIN SELECT RAISE(ABORT, 'refused'); END")


def test_a_device_deletion_whose_queue_insert_fails_deletes_nothing(revoking):
    device_id = revoking.enroll_route()
    _refuse_queue_inserts(revoking)
    assert not delete(revoking, device_id)
    assert row(revoking, device_id) is not None
    assert queued(revoking) == []


def test_a_session_deletion_whose_queue_insert_fails_deletes_nothing(revoking):
    device_id = revoking.enroll_route()
    _refuse_queue_inserts(revoking)
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET auth_session_id = 'login-1'")
    with pytest.raises(sqlite3.Error):
        with db.get_db(revoking.config.db_path) as conn:
            devices.delete_session_devices(conn, "alice", "login-1", relay_url=RELAY_URL)
    assert row(revoking, device_id) is not None
    assert queued(revoking) == []


def test_a_deletion_inside_the_callers_transaction_rolls_back_with_it(revoking):
    device_id = revoking.enroll_route()
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert devices.delete_device(conn, device_id, "alice", relay_url=RELAY_URL)
        conn.rollback()
    assert row(revoking, device_id) is not None
    assert queued(revoking) == []


def test_a_re_registration_with_a_new_route_queues_the_old_one_with_its_own_file(revoking):
    device_id = revoking.enroll_route()
    revoking.enroll_route(route=2, capability=OTHER_CAPABILITY)
    assert row(revoking, device_id)["relay_route_id"] == route_id(2)
    entry, = queued(revoking)
    ref = f"revoke-{route_id(1)}.cap"
    assert (entry["route_id"], entry["credential_ref"], entry["relay_url"]) == (route_id(1), ref, RELAY_URL)
    assert (revoking.directory / ref).read_text("ascii") == CAPABILITY
    assert (revoking.directory / f"{device_id}.cap").read_text("ascii") == OTHER_CAPABILITY
    assert oct((revoking.directory / ref).stat().st_mode & 0o777) == oct(0o600)


def test_a_re_registration_with_the_same_route_queues_nothing(revoking):
    revoking.enroll_route()
    revoking.enroll_route()
    assert queued(revoking) == []


def test_a_re_registration_that_drops_the_route_queues_it(revoking):
    device_id = revoking.enroll_route()
    with db.get_db(revoking.config.db_path) as conn:
        devices.register_device(conn, "alice", "ab" * 32, "production", auth_method="nextcloud", auth_epoch=0,
                                credentials_dir=revoking.directory, relay_url=RELAY_URL)
    entry, = queued(revoking)
    assert entry["route_id"] == route_id(1)
    assert not (revoking.directory / f"{device_id}.cap").exists()
    assert (revoking.directory / entry["credential_ref"]).read_text("ascii") == CAPABILITY


def test_a_re_registration_that_fails_leaves_no_queue_entry_and_no_file(revoking, monkeypatch):
    revoking.enroll_route()

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(devices, "write_relay_capability", refuse)
    with db.get_db(revoking.config.db_path) as conn:
        assert devices.register_device(
            conn, "alice", "ab" * 32, "production", auth_method="nextcloud", auth_epoch=0,
            relay={"route_id": route_id(2), "send_capability": OTHER_CAPABILITY, "lease_expires_at": 1},
            credentials_dir=revoking.directory, relay_url=RELAY_URL) is None
    assert queued(revoking) == []
    assert not (revoking.directory / f"revoke-{route_id(1)}.cap").exists()


def test_an_invalid_device_from_the_relay_is_deleted_without_a_revocation(revoking):
    device_id = revoking.enroll_route()
    revoking.mock.answer(410, {"result": "invalid_device", "apns_status": 410, "apns_reason": "Unregistered",
                               "invalidated_at": (NOW + 60) * 1000})
    from tests.test_push_relay_sending import send_one
    send_one(revoking, device_id)
    assert row(revoking, device_id) is None
    assert queued(revoking) == []
    assert not (revoking.directory / f"{device_id}.cap").exists()


# --- The sweep -------------------------------------------------------------------------


def test_the_sweep_revokes_with_the_stored_capability_and_drops_the_entry(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    sweep(revoking)
    request, = revoking.revoker.requests
    assert str(request.url) == f"{RELAY_URL}/v1/routes/{route_id(1)}"
    assert request.headers["authorization"] == f"Bearer {CAPABILITY}"
    assert request.extensions["timeout"] == {"connect": 7, "read": 7, "write": 7, "pool": 7}
    assert route_id(1) not in revoking.revoker.capabilities
    assert queued(revoking) == []
    assert not (revoking.directory / f"{device_id}.cap").exists()


def test_a_replaced_route_is_revoked_and_the_live_capability_is_kept(revoking):
    device_id = revoking.enroll_route()
    revoking.enroll_route(route=2, capability=OTHER_CAPABILITY)
    sweep(revoking)
    assert route_id(1) not in revoking.revoker.capabilities
    assert revoking.revoker.capabilities[route_id(2)] == OTHER_CAPABILITY
    assert queued(revoking) == []
    assert sorted(p.name for p in revoking.directory.iterdir()) == [f"{device_id}.cap"]


def test_a_401_drops_the_entry(revoking):
    device_id = revoking.enroll_route()
    revoking.revoker.capabilities[route_id(1)] = OTHER_CAPABILITY  # not the capability we hold
    delete(revoking, device_id)
    sweep(revoking)
    assert queued(revoking) == []
    assert not (revoking.directory / f"{device_id}.cap").exists()


def test_a_route_already_gone_is_a_204_and_drops_the_entry(revoking):
    device_id = revoking.enroll_route()
    del revoking.revoker.capabilities[route_id(1)]
    delete(revoking, device_id)
    sweep(revoking)
    assert len(revoking.revoker.requests) == 1
    assert queued(revoking) == []


@pytest.mark.parametrize("attempts,delay", [(0, 30), (1, 60), (2, 120), (5, 960), (9, 15_360), (10, 21_600),
                                            (40, 21_600)])
def test_backoff_doubles_from_thirty_seconds_to_six_hours(attempts, delay):
    assert revocations.backoff(attempts) == delay


@pytest.mark.parametrize("answer", [
    (503, b'{"error": "unavailable"}', {}),
    (500, b'{"error": "internal"}', {}),
    (404, b"", {}),
    (200, b"{}", {}),
    (307, b"", {"location": "https://elsewhere.example.com/v1/routes/x"}),
    (429, b"x" * 5000, {}),
])
def test_an_unexpected_answer_keeps_the_entry_and_backs_off(revoking, answer):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    revoking.revoker.answer(*answer)
    sweep(revoking)
    entry, = queued(revoking)
    assert (entry["attempts"], entry["next_attempt_at"]) == (1, NOW + 30)
    assert entry["last_error"].startswith(f"{answer[0]} ")
    assert len(revoking.revoker.requests) == 1
    assert (revoking.directory / f"{device_id}.cap").exists()


def test_the_second_failure_waits_twice_as_long(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    revoking.revoker.answer(503)
    revoking.revoker.answer(503)
    sweep(revoking)
    sweep(revoking)  # not due yet: no request
    assert len(revoking.revoker.requests) == 1
    revoking.clock["now"] = NOW + 30
    sweep(revoking)
    entry, = queued(revoking)
    assert (entry["attempts"], entry["next_attempt_at"]) == (2, NOW + 30 + 60)


def test_a_rate_limit_longer_than_the_backoff_is_honoured_and_ends_the_sweep(revoking):
    first = revoking.enroll_route()
    second = revoking.enroll_route("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    delete(revoking, first)
    revoking.clock["now"] = NOW + 1
    delete(revoking, second)
    revoking.revoker.answer(429, {"result": "rate_limited", "retry_after": 900}, {"retry-after": "900"})
    sweep(revoking)
    assert len(revoking.revoker.requests) == 1
    entries = {e["route_id"]: e for e in queued(revoking)}
    assert (entries[route_id(1)]["attempts"], entries[route_id(1)]["next_attempt_at"]) == (1, NOW + 1 + 900)
    assert entries[route_id(1)]["last_error"] == "429 rate_limited"
    assert entries[route_id(2)]["attempts"] == 0


def test_a_rate_limit_shorter_than_the_backoff_keeps_the_backoff(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("UPDATE push_relay_revocations SET attempts = 3")
    revoking.revoker.answer(429, {"result": "rate_limited", "retry_after": 5})
    sweep(revoking)
    assert queued(revoking)[0]["next_attempt_at"] == NOW + 240


def test_an_absurd_retry_after_is_bounded_by_the_entry_lifetime(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    revoking.revoker.answer(429, {"result": "rate_limited", "retry_after": 10 ** 30})
    sweep(revoking)
    assert queued(revoking)[0]["next_attempt_at"] == NOW + 7 * DAY


def test_an_unreachable_relay_ends_the_sweep_after_one_attempt(revoking, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    first = revoking.enroll_route()
    second = revoking.enroll_route("cd" * 32, route=2, capability=OTHER_CAPABILITY)
    delete(revoking, first)
    delete(revoking, second)
    calls = []

    def unreachable(request):
        calls.append(request)
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(unreachable))
    monkeypatch.setattr(relay_client, "_client", lambda: client)
    sweep(revoking)
    assert len(calls) == 1
    attempts = sorted(e["attempts"] for e in queued(revoking))
    assert attempts == [0, 1]
    failed = [e for e in queued(revoking) if e["attempts"] == 1][0]
    assert failed["last_error"] == "0 relay_unreachable"
    for secret in (CAPABILITY, OTHER_CAPABILITY, route_id(1), route_id(2)):
        assert secret not in caplog.text


def test_a_redirect_is_not_followed_even_by_a_client_that_would(revoking, monkeypatch):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    seen = []

    def redirecting(request):
        seen.append(str(request.url))
        if request.url.host == "relay.example.com":
            return httpx.Response(307, headers={"location": "https://elsewhere.example.com/x"})
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(redirecting), follow_redirects=True)
    monkeypatch.setattr(relay_client, "_client", lambda: client)
    sweep(revoking)
    assert seen == [f"{RELAY_URL}/v1/routes/{route_id(1)}"]
    assert queued(revoking)[0]["last_error"] == "307 unexpected_status"


def test_an_entry_past_its_lifetime_is_dropped_without_a_request(revoking, caplog):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    revoking.clock["now"] = NOW + 7 * DAY
    sweep(revoking)
    assert revoking.revoker.requests == []
    assert queued(revoking) == []
    assert not (revoking.directory / f"{device_id}.cap").exists()


@pytest.mark.parametrize("damage", ["missing", "garbage", "no_url", "bad_url"])
def test_an_entry_that_cannot_be_sent_is_dropped(revoking, damage):
    device_id = revoking.enroll_route()
    relay_url = {"no_url": "", "bad_url": "http://relay.example.com/istota"}.get(damage, RELAY_URL)
    with db.get_db(revoking.config.db_path) as conn:
        devices.delete_device(conn, device_id, "alice", relay_url=relay_url)
    cap = revoking.directory / f"{device_id}.cap"
    if damage == "missing":
        cap.unlink()
    elif damage == "garbage":
        cap.write_text("not a capability")
    sweep(revoking)
    assert revoking.revoker.requests == []
    assert queued(revoking) == []
    assert not cap.exists()


def test_a_sweep_sends_at_most_twenty(revoking):
    for n in range(1, 24):
        revoking.enroll_route(f"{n:02x}" * 32, route=n, capability=CAPABILITY)
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("UPDATE push_devices SET auth_session_id = 'login-1'")
        devices.delete_session_devices(conn, "alice", "login-1", relay_url=RELAY_URL)
    sweep(revoking)
    assert len(revoking.revoker.requests) == 20
    assert len(queued(revoking)) == 3
    sweep(revoking)
    assert queued(revoking) == []


def test_entries_not_yet_due_are_left_alone(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("UPDATE push_relay_revocations SET next_attempt_at = ?", (NOW + 1,))
    sweep(revoking)
    assert revoking.revoker.requests == []
    assert len(queued(revoking)) == 1


def test_the_sweep_uses_the_url_stored_with_the_entry(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    revoking.config.push.relay_url = "https://other.example.com/istota"
    sweep(revoking)
    assert str(revoking.revoker.requests[0].url).startswith(f"{RELAY_URL}/v1/routes/")


def test_the_sweep_still_revokes_after_a_switch_to_direct_delivery(revoking):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    revoking.config.push.delivery = "direct"
    revoking.config.push.enabled = False
    sweep(revoking)
    assert len(revoking.revoker.requests) == 1
    assert queued(revoking) == []


def test_the_sweep_never_calls_apns_and_survives_a_missing_table(revoking, caplog):
    caplog.set_level(logging.WARNING)
    assert apns.send is not None  # the relay fixture turns any APNs call into a failure
    with db.get_db(revoking.config.db_path) as conn:
        conn.execute("DROP TABLE push_relay_revocations")
    sweep(revoking)
    assert "revocation" in caplog.text


def test_the_dispatch_gate_runs_the_sweep(revoking, monkeypatch):
    from istota.scheduler import build_interval_gates

    calls = []
    monkeypatch.setattr(revocations, "run_relay_revocations", lambda config: calls.append(config))
    gate = {g.name: g for g in build_interval_gates(revoking.config)}["push-dispatch"]
    gate.run(0.0)
    assert calls == [revoking.config]


def test_the_table_holds_no_capability(revoking):
    device_id = revoking.enroll_route()
    revoking.enroll_route(route=2, capability=OTHER_CAPABILITY)
    delete(revoking, device_id)
    with db.get_db(revoking.config.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert CAPABILITY not in dump and OTHER_CAPABILITY not in dump


# --- Doctor ------------------------------------------------------------------------------


def test_doctor_warns_about_revocations_pending_for_more_than_a_day(revoking, monkeypatch):
    device_id = revoking.enroll_route()
    delete(revoking, device_id)
    monkeypatch.setattr(doctor.time, "time", lambda: NOW + DAY - 1)
    assert doctor.check_push_relay(revoking.config, False).status == doctor.OK
    monkeypatch.setattr(doctor.time, "time", lambda: NOW + DAY)
    result = doctor.check_push_relay(revoking.config, False)
    assert result.status == doctor.WARN
    assert "1 route revocation" in result.detail
    assert route_id(1) not in result.detail and result.remedy


def test_doctor_counts_nothing_without_a_database(tmp_path):
    push = PushConfig(enabled=True, content="private", delivery="relay", relay_url=RELAY_URL,
                      push_credentials_dir=str(tmp_path / "caps"))
    assert doctor._relay_revocations_pending(Config(db_path=tmp_path / "none.db", push=push)) == 0
