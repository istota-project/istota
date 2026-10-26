"""Istota's own calendar: `sharing/events.py` and the feed route.

The events are written by deferred ops the calendar skill queues and the
scheduler applies, so the op file is model-writable: the apply step takes the
user from the task, refuses a UID another user owns, and applies at most
`[sharing] events_per_task` entries whatever the file holds. The feed is a
public route behind a bearer token stored as its digest, with its own limiter.

Real database throughout; the route is mounted on a bare FastAPI app with the
two dependencies the web app overrides.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config, SiteConfig

icalendar = pytest.importorskip("icalendar")

from istota.sharing import events  # noqa: E402


@pytest.fixture
def config(db_path, tmp_path):
    return Config(db_path=db_path, temp_dir=tmp_path / "tmp", site=SiteConfig(hostname="bot.example.com"),
                  bot_name="Istota")


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _task(conn, user="alice", **kwargs):
    task_id = db.create_task(conn, "put it on my calendar", user, source_type="cli", **kwargs)
    return db.get_task(conn, task_id)


def _create(uid, summary="Tax review", start="2026-10-29T13:00:00Z", end="2026-10-29T15:00:00Z", **extra):
    fields = {"summary": summary, "dtstart": start, "dtend": end, "all_day": False, **extra}
    return events.upsert_op(uid, create=True, fields=fields)


def _rows(conn, user="alice"):
    return events.events_for_user(conn, user, include_cancelled=True)


UID = "0b7a7f9e-3a55-4c39-9a0e-0d6f1c2b9e41@bot.example.com"


class TestApply:
    def test_create_update_cancel_bumps_the_sequence(self, conn, config):
        task = _task(conn)
        events.apply_ops(conn, config, task, [_create(UID)])
        (row,) = _rows(conn)
        assert (row.uid, row.sequence, row.status, row.user_id) == (UID, 0, "confirmed", "alice")
        assert row.created_by_task == task.id

        events.apply_ops(conn, config, task, [events.upsert_op(
            UID, create=False, fields={"summary": "Tax review, moved",
                                       "dtstart": "2026-10-30T13:00:00Z", "dtend": "2026-10-30T15:00:00Z"})])
        (row,) = _rows(conn)
        assert (row.sequence, row.summary, row.dtstart) == (1, "Tax review, moved", "2026-10-30T13:00:00Z")

        events.apply_ops(conn, config, task, [events.cancel_op(UID)])
        (row,) = _rows(conn)
        assert (row.sequence, row.status) == (2, "cancelled")
        assert row.cancelled_at is not None

    def test_identity_comes_from_the_task_not_the_file(self, conn, config):
        op = _create(UID)
        op["user_id"] = "bob"
        events.apply_ops(conn, config, _task(conn), [op])
        assert [r.user_id for r in _rows(conn)] == ["alice"]
        assert _rows(conn, "bob") == []

    def test_a_uid_owned_by_another_user_is_refused(self, conn, config):
        events.apply_ops(conn, config, _task(conn, "bob"), [_create(UID, summary="Bob's")])
        report = events.apply_ops(conn, config, _task(conn), [
            _create(UID, summary="Alice's"),
            events.upsert_op(UID, create=False, fields={"summary": "taken over"}),
            events.cancel_op(UID),
        ])
        assert report.applied == 0 and len(report.refused) == 3
        (row,) = _rows(conn, "bob")
        assert (row.summary, row.sequence, row.status) == ("Bob's", 0, "confirmed")
        assert _rows(conn) == []

    def test_the_cap_is_counted_again_at_apply(self, conn, config):
        config.sharing.events_per_task = 10
        ops = [_create(f"{n:04d}@bot.example.com", summary=f"Event {n}") for n in range(15)]
        report = events.apply_ops(conn, config, _task(conn), ops)
        assert (report.applied, report.dropped) == (10, 5)
        assert sorted(r.summary for r in _rows(conn)) == sorted(f"Event {n}" for n in range(10))

    def test_refused_entries_count_against_the_cap(self, conn, config):
        config.sharing.events_per_task = 2
        ops = [{"op": "nonsense"}, _create("a@bot.example.com"), _create("b@bot.example.com")]
        report = events.apply_ops(conn, config, _task(conn), ops)
        assert (report.applied, len(report.refused), report.dropped) == (1, 1, 1)

    def test_a_zero_cap_applies_nothing(self, conn, config):
        config.sharing.events_per_task = 0
        report = events.apply_ops(conn, config, _task(conn), [_create(UID)])
        assert report.applied == 0 and _rows(conn) == []

    @pytest.mark.parametrize("fields", [
        {"summary": "", "dtstart": "2026-10-29T13:00:00Z", "dtend": "2026-10-29T15:00:00Z", "all_day": False},
        {"summary": "x", "dtstart": "2026-10-29 13:00", "dtend": "2026-10-29T15:00:00Z", "all_day": False},
        {"summary": "x", "dtstart": "2026-10-29T15:00:00Z", "dtend": "2026-10-29T13:00:00Z", "all_day": False},
        {"summary": "x", "dtstart": "2026-10-29", "dtend": "2026-10-30T00:00:00Z", "all_day": True},
        {"summary": "x\r\nATTENDEE:mailto:x@example.org", "dtstart": "2026-10-29T13:00:00Z",
         "dtend": "2026-10-29T15:00:00Z", "all_day": False},
        {"summary": "x" * 600, "dtstart": "2026-10-29T13:00:00Z", "dtend": "2026-10-29T15:00:00Z", "all_day": False},
    ], ids=["empty", "not-utc", "backwards", "mixed-all-day", "line-break", "too-long"])
    def test_malformed_fields_are_refused(self, conn, config, fields):
        report = events.apply_ops(conn, config, _task(conn), [events.upsert_op(UID, create=True, fields=fields)])
        assert report.applied == 0 and report.refused and _rows(conn) == []

    def test_a_malformed_uid_is_refused(self, conn, config):
        report = events.apply_ops(conn, config, _task(conn), [_create("no at sign\r\nX")])
        assert report.applied == 0 and _rows(conn) == []

    def test_feed_delivery_is_recorded_on_the_row(self, conn, config, db_path):
        from istota import user_profiles

        user_profiles.ensure_profile(db_path, "alice", conn=conn)
        user_profiles.update_profile(db_path, "alice", conn=conn, calendar_delivery="feed")
        events.apply_ops(conn, config, _task(conn), [_create(UID)])
        assert _rows(conn)[0].invite_status == "feed"

    def test_invite_delivery_leaves_the_status_for_the_send(self, conn, config):
        config.email.enabled = True
        events.apply_ops(conn, config, _task(conn), [_create(UID)])
        assert _rows(conn)[0].invite_status is None

    def test_with_email_off_delivery_is_the_feed(self, conn, config):
        config.email.enabled = False
        assert events.effective_delivery(config, "invite") == "feed"
        events.apply_ops(conn, config, _task(conn), [_create(UID)])
        assert _rows(conn)[0].invite_status == "feed"


class TestTheDeferredHandler:
    def test_the_handler_applies_the_file_and_removes_it(self, conn, config, tmp_path):
        from istota.scheduler_deferred import _process_deferred_istota_events

        task = _task(conn)
        conn.commit()
        user_dir = tmp_path / "deferred"
        user_dir.mkdir()
        path = user_dir / f"task_{task.id}_istota_events.json"
        path.write_text(json.dumps([_create(f"{n}@bot.example.com") for n in range(15)]), encoding="utf-8")
        assert _process_deferred_istota_events(config, task, user_dir) == 10
        assert not path.exists()
        with db.get_db(config.db_path) as fresh:
            assert len(_rows(fresh)) == 10

    def test_the_suffix_is_known_so_a_retry_purges_it(self):
        from istota.scheduler_deferred import _KNOWN_DEFERRED_SUFFIXES

        assert "istota_events" in _KNOWN_DEFERRED_SUFFIXES


class TestTheFeed:
    def test_the_feed_is_utc_and_omits_cancelled_events(self, conn, config):
        task = _task(conn)
        events.apply_ops(conn, config, task, [
            _create(UID, location="Room 4"),
            _create("gone@bot.example.com", summary="Cancelled one"),
            events.cancel_op("gone@bot.example.com"),
            events.upsert_op("day@bot.example.com", create=True, fields={
                "summary": "Holiday", "dtstart": "2026-11-02", "dtend": "2026-11-03", "all_day": True}),
        ])
        _task_bob = _task(conn, "bob")
        events.apply_ops(conn, config, _task_bob, [_create("bob@bot.example.com", summary="Bob's")])
        body = events.render_feed(conn, config, "alice")
        cal = icalendar.Calendar.from_ical(body)
        assert str(cal["X-WR-CALNAME"]) == "Istota"
        found = {str(c["UID"]): c for c in cal.walk("VEVENT")}
        assert set(found) == {UID, "day@bot.example.com"}
        timed = found[UID]
        assert b"DTSTART:20261029T130000Z" in body
        assert str(timed["LOCATION"]) == "Room 4" and int(timed["SEQUENCE"]) == 0
        assert b"DTSTART;VALUE=DATE:20261102" in body
        assert b"VTIMEZONE" not in body

    def test_rendering_twice_gives_the_same_bytes(self, conn, config):
        events.apply_ops(conn, config, _task(conn), [_create(UID)])
        assert events.render_feed(conn, config, "alice") == events.render_feed(conn, config, "alice")


class TestTokens:
    def test_a_token_is_stored_as_its_digest_and_looked_up_by_it(self, conn):
        token = events.issue_token(conn, "alice")
        stored = conn.execute("SELECT token_sha256 FROM calendar_feed_tokens WHERE user_id = 'alice'").fetchone()[0]
        assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in stored
        assert events.user_for_token(conn, token) == "alice"

    def test_reset_replaces_the_old_token_at_once(self, conn):
        old = events.issue_token(conn, "alice")
        new = events.issue_token(conn, "alice")
        assert old != new
        assert events.user_for_token(conn, old) is None
        assert events.user_for_token(conn, new) == "alice"
        assert conn.execute("SELECT COUNT(*) FROM calendar_feed_tokens").fetchone()[0] == 1

    @pytest.mark.parametrize("token", ["", "short", "x" * 43, "a/" * 22, "A" * 43 + "\n"])
    def test_an_unknown_or_malformed_token_finds_nobody(self, conn, token):
        events.issue_token(conn, "alice")
        assert events.user_for_token(conn, token) is None

    def test_deleting_the_profile_takes_the_token_and_the_events(self, conn, config, db_path):
        from istota import user_profiles

        user_profiles.ensure_profile(db_path, "alice", conn=conn)
        events.apply_ops(conn, config, _task(conn), [_create(UID)])
        token = events.issue_token(conn, "alice")
        bob = events.issue_token(conn, "bob")
        conn.commit()
        assert user_profiles.delete_profile(db_path, "alice")
        assert events.user_for_token(conn, token) is None
        assert _rows(conn) == []
        assert events.user_for_token(conn, bob) == "bob"

    def test_revoking_ends_the_feed(self, conn):
        token = events.issue_token(conn, "alice")
        assert events.revoke_token(conn, "alice") is True
        assert events.user_for_token(conn, token) is None
        assert events.revoke_token(conn, "alice") is False


# --- The route ---------------------------------------------------------------

@pytest.fixture
def feed_client(config, db_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from istota.sharing import feed_routes

    feed_routes.reset_limits()
    holder = SimpleNamespace(address=None)
    app = FastAPI()
    app.include_router(feed_routes.router)
    app.dependency_overrides[feed_routes.current_config] = lambda: config
    app.dependency_overrides[feed_routes.client_address] = lambda: holder.address
    with TestClient(app, base_url="https://bot.example.com") as client:
        client.holder = holder
        yield client
    feed_routes.reset_limits()


@pytest.fixture
def alice_token(conn, config):
    events.apply_ops(conn, config, _task(conn), [_create(UID)])
    token = events.issue_token(conn, "alice")
    conn.commit()
    return token


class TestTheRoute:
    def test_the_feed_is_served_with_cache_headers(self, feed_client, alice_token):
        response = feed_client.get(f"/istota/calendar/{alice_token}.ics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert response.headers["cache-control"] == "private, max-age=300"
        assert response.headers["etag"]
        assert UID.encode() in response.content

    def test_an_unknown_token_is_a_404(self, feed_client, alice_token):
        other = "B" * 43
        for path in (f"/istota/calendar/{other}.ics", "/istota/calendar/nothing.ics",
                     f"/istota/calendar/{alice_token}", f"/istota/calendar/{alice_token}.txt"):
            assert feed_client.get(path).status_code == 404, path

    def test_a_reset_token_is_a_404_at_once(self, feed_client, alice_token, db_path):
        with db.get_db(db_path) as c:
            events.issue_token(c, "alice")
            c.commit()
        assert feed_client.get(f"/istota/calendar/{alice_token}.ics").status_code == 404

    def test_if_none_match_answers_304(self, feed_client, alice_token):
        first = feed_client.get(f"/istota/calendar/{alice_token}.ics")
        again = feed_client.get(f"/istota/calendar/{alice_token}.ics",
                                headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""

    def test_the_token_is_limited_per_hour(self, feed_client, alice_token):
        path = f"/istota/calendar/{alice_token}.ics"
        codes = [feed_client.get(path).status_code for _ in range(121)]
        assert codes[:120] == [200] * 120 and codes[120] == 429

    def test_an_address_is_limited_across_tokens(self, feed_client, alice_token):
        from istota.sharing import feed_routes

        feed_client.holder.address = "203.0.113.9"
        codes = [feed_client.get(f"/istota/calendar/{n:043d}.ics").status_code
                 for n in range(feed_routes.ADDRESS_MAX_HITS + 1)]
        assert set(codes[:-1]) == {404} and codes[-1] == 429
