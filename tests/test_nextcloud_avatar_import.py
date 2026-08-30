"""The Nextcloud avatar import: `fetch_avatar` and the scheduler job.

Stage 4 of the profile-icons spec. Two halves that fail in different ways:

`fetch_avatar` has to tell a *custom* avatar from the coloured letter Nextcloud
generates for a user who set none. It cannot do that from the body — both are
PNGs of the same shape — so it reads a response header, and every branch of
that reading gets a case here, including the one where the header is absent
altogether. Getting it wrong imports Nextcloud's version of our own initial
chip, indistinguishable at the call site from a real photograph.

`check_avatar_import` enumerates **`config.users`**, and the first test in
`TestTheUserSet` is the regression test for the defect that made the feature
dead: derive the set from `user_avatars` instead and the users who need the
first import — the ones with no row — are exactly the ones excluded.
"""

from __future__ import annotations

import io
import logging
import sqlite3

import httpx
import pytest
from PIL import Image

from istota import avatars, db, scheduler
from istota.config import Config, NextcloudConfig, SchedulerConfig, WebConfig
from istota.nextcloud import avatars as nc_avatars
from istota.nextcloud._http import OcsError


# --- fixtures ---------------------------------------------------------------


def _png(size=(500, 500), color=(120, 130, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _response(status: int, *, content: bytes = b"", headers: dict | None = None):
    """A real `httpx.Response`, so header lookups are genuinely case-insensitive.

    A `MagicMock` with a plain dict for `headers` would pass a lookup that
    matched the spelling in the test and fail against the one Nextcloud sends,
    which is the single fact this file exists to pin down.
    """
    return httpx.Response(status, content=content, headers=headers or {})


CUSTOM = {nc_avatars.CUSTOM_AVATAR_HEADER: "1", "ETag": '"abc"'}
GENERATED = {nc_avatars.CUSTOM_AVATAR_HEADER: "0", "ETag": '"gen"'}


@pytest.fixture
def nc_config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="istota",
            app_password="secret",
        ),
    )


@pytest.fixture(autouse=True)
def _reset_absent_header_log():
    """The "header absent" line is once per *process*, so it leaks across tests."""
    nc_avatars.reset_absent_header_log()
    yield
    nc_avatars.reset_absent_header_log()


class _Recorder:
    """Stands in for `httpx.get`, recording what was asked and answering a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def headers(self) -> dict:
        return self.calls[-1][1].get("headers") or {}


@pytest.fixture
def http(monkeypatch):
    def _install(*answers):
        recorder = _Recorder(*answers)
        monkeypatch.setattr(nc_avatars.httpx, "get", recorder)
        return recorder

    return _install


# --- fetch_avatar -----------------------------------------------------------


class TestFetchAvatar:
    def test_a_custom_avatar_comes_back_as_an_image_to_import(self, nc_config, http):
        body = _png()
        http(_response(200, content=body, headers=CUSTOM))

        got = nc_avatars.fetch_avatar(nc_config, "alice")

        assert isinstance(got, nc_avatars.RemoteAvatar)
        assert got.image == body
        assert got.etag == '"abc"'

    def test_a_falsy_header_is_a_negative_result_not_an_image(self, nc_config, http):
        http(_response(200, content=_png(), headers=GENERATED))

        got = nc_avatars.fetch_avatar(nc_config, "alice")

        assert isinstance(got, nc_avatars.NoCustomAvatar)
        assert got.etag == '"gen"'
        assert got.header_seen is True

    def test_an_absent_header_degrades_to_the_negative_result(self, nc_config, http):
        """Degrading to today's behaviour is right; degrading to a picture that
        is wrong is not. A Nextcloud that does not send the header at all means
        every user would import a coloured letter."""
        http(_response(200, content=_png(), headers={"ETag": '"x"'}))

        got = nc_avatars.fetch_avatar(nc_config, "alice")

        assert isinstance(got, nc_avatars.NoCustomAvatar)
        assert got.header_seen is False

    def test_the_absent_header_is_logged_once_per_process(
        self, nc_config, http, caplog
    ):
        http(_response(200, content=_png(), headers={}))

        with caplog.at_level(logging.WARNING, logger="istota.nextcloud.avatars"):
            for uid in ("alice", "bob", "carol"):
                nc_avatars.fetch_avatar(nc_config, uid)

        named = [
            r for r in caplog.records
            if nc_avatars.CUSTOM_AVATAR_HEADER.lower() in r.getMessage().lower()
        ]
        assert len(named) == 1, "one line per process, not one per user per tick"

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_spellings(self, nc_config, http, value):
        http(_response(200, content=_png(),
                       headers={nc_avatars.CUSTOM_AVATAR_HEADER: value}))
        assert isinstance(nc_avatars.fetch_avatar(nc_config, "a"),
                          nc_avatars.RemoteAvatar)

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_spellings(self, nc_config, http, value):
        http(_response(200, content=_png(),
                       headers={nc_avatars.CUSTOM_AVATAR_HEADER: value}))
        assert isinstance(nc_avatars.fetch_avatar(nc_config, "a"),
                          nc_avatars.NoCustomAvatar)

    def test_304_learns_nothing(self, nc_config, http):
        http(_response(304))
        assert nc_avatars.fetch_avatar(nc_config, "alice", etag='"abc"') is None

    def test_404_learns_nothing(self, nc_config, http):
        """A user Nextcloud does not know. Not a failure to log per tick."""
        http(_response(404, content=b"nope"))
        assert nc_avatars.fetch_avatar(nc_config, "ghost") is None

    def test_a_server_error_raises_so_a_caller_can_tell_it_from_a_no_op(
        self, nc_config, http
    ):
        http(_response(500, content=b"boom"))
        with pytest.raises(OcsError):
            nc_avatars.fetch_avatar(nc_config, "alice")

    def test_a_connection_failure_raises(self, nc_config, http):
        http(httpx.ConnectError("no route"))
        with pytest.raises(OcsError):
            nc_avatars.fetch_avatar(nc_config, "alice")

    def test_an_unconfigured_nextcloud_raises_rather_than_guessing_a_host(
        self, tmp_path, http
    ):
        recorder = http(_response(200, content=_png(), headers=CUSTOM))
        with pytest.raises(OcsError):
            nc_avatars.fetch_avatar(Config(db_path=tmp_path / "t.db"), "alice")
        assert recorder.calls == []

    def test_if_none_match_is_sent_only_when_an_etag_is_stored(self, nc_config, http):
        recorder = http(_response(304))
        nc_avatars.fetch_avatar(nc_config, "alice", etag='"abc"')
        assert recorder.headers.get("If-None-Match") == '"abc"'

        recorder = http(_response(200, content=_png(), headers=CUSTOM))
        nc_avatars.fetch_avatar(nc_config, "alice")
        assert "If-None-Match" not in recorder.headers

    def test_the_url_names_the_user_and_the_size_and_quotes_the_uid(
        self, nc_config, http
    ):
        recorder = http(_response(200, content=_png(), headers=CUSTOM))
        nc_avatars.fetch_avatar(nc_config, "a b/c", size=192)
        url = recorder.calls[0][0]
        assert url == "https://cloud.example.com/index.php/avatar/a%20b%2Fc/192"

    def test_a_truthy_header_with_an_empty_body_is_a_failure_not_a_negative(
        self, nc_config, http
    ):
        """Recording "no custom avatar" for a server that just said there is one
        writes a lie into the store, and the ETag beside it stops the next tick
        looking again."""
        http(_response(200, content=b"", headers=CUSTOM))
        with pytest.raises(OcsError):
            nc_avatars.fetch_avatar(nc_config, "alice")


# --- the scheduler job ------------------------------------------------------


def _import_config(tmp_path, *, users=("alice",), **overrides):
    settings = dict(
        db_path=tmp_path / "test.db",
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com", username="istota", app_password="s",
        ),
        web=WebConfig(enabled=True, avatar_import_from_nextcloud=True),
        scheduler=SchedulerConfig(avatar_import_interval=21600),
        users={u: None for u in users},
    )
    settings.update(overrides)
    return Config(**settings)


@pytest.fixture
def import_config(tmp_path, db_path):
    def _make(**overrides):
        return _import_config(tmp_path, **overrides)

    return _make


def _fetches(monkeypatch, answers):
    """Route `fetch_avatar` through a per-user script, recording the etags seen."""
    calls: list[tuple[str, str]] = []

    def _fake(config, uid, *, size=192, etag="", timeout=10.0):
        calls.append((uid, etag))
        answer = answers[uid] if isinstance(answers, dict) else answers
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(scheduler.nc_avatars, "fetch_avatar", _fake)
    return calls


class TestTheUserSet:
    def test_a_user_with_no_avatar_row_at_all_is_imported_on_the_first_tick(
        self, import_config, db_path, monkeypatch
    ):
        """The regression test for the defect that made this feature dead.

        The job's user set is `config.users`. Read it off `user_avatars` instead
        and a user with no row — precisely the user who needs the first import —
        is never asked about, so nothing is ever imported on any deployment.
        """
        config = import_config(users=("alice",))
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM user_avatars").fetchone()[0] == 0

        _fetches(monkeypatch,
                 nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'))

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            stored = avatars.get_user_avatar(conn, "alice")
        assert stored is not None
        assert stored.source == avatars.SOURCE_NEXTCLOUD

    def test_every_configured_user_is_asked_about(
        self, import_config, monkeypatch
    ):
        config = import_config(users=("alice", "bob", "carol"))
        calls = _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))

        scheduler.check_avatar_import(config)

        assert sorted(uid for uid, _ in calls) == ["alice", "bob", "carol"]


class TestTheImport:
    def test_what_it_fetched_is_normalized_like_any_upload(
        self, import_config, db_path, monkeypatch
    ):
        """An image from Nextcloud is not more trusted than one from a browser."""
        config = import_config()
        _fetches(monkeypatch,
                 nc_avatars.RemoteAvatar(image=_png(size=(500, 500)), etag='"e1"'))

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            stored = avatars.get_user_avatar(conn, "alice")
        assert stored is not None
        assert stored.mime == avatars.NORMALIZED_MIME
        with Image.open(io.BytesIO(stored.image)) as img:
            assert img.size == (avatars.AVATAR_EDGE, avatars.AVATAR_EDGE)
            assert img.format == "WEBP"

    def test_the_stored_etag_is_the_one_the_remote_named(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config()
        _fetches(monkeypatch, nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'))

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            assert avatars.import_probe_state(conn) == {"alice": '"e1"'}

    def test_the_next_tick_revalidates_with_that_etag(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config()
        _fetches(monkeypatch, nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'))
        scheduler.check_avatar_import(config)

        calls = _fetches(monkeypatch, None)
        scheduler.check_avatar_import(config)

        assert calls == [("alice", '"e1"')]

    def test_a_304_leaves_the_stored_picture_and_its_etag_alone(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config()
        _fetches(monkeypatch, nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'))
        scheduler.check_avatar_import(config)
        with db.get_db(db_path) as conn:
            before = avatars.get_user_avatar(conn, "alice")

        _fetches(monkeypatch, None)
        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            after = avatars.get_user_avatar(conn, "alice")
            assert avatars.import_probe_state(conn) == {"alice": '"e1"'}
        assert after is not None and before is not None
        assert after.content_hash == before.content_hash

    def test_an_upload_is_not_disturbed(self, import_config, db_path, monkeypatch):
        """The import writes the `nextcloud` row. Precedence does the rest."""
        config = import_config()
        upload, digest = avatars.normalize(
            _png(color=(200, 10, 10)), declared_format=None, max_bytes=10_000_000,
        )
        with db.get_db(db_path) as conn:
            avatars.put_user_avatar(
                conn, "alice", source=avatars.SOURCE_UPLOAD,
                image=upload, content_hash=digest,
            )

        _fetches(monkeypatch, nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'))
        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            shown = avatars.get_user_avatar(conn, "alice")
        assert shown is not None
        assert shown.source == avatars.SOURCE_UPLOAD
        assert shown.content_hash == digest


class TestTheNegativeResult:
    def test_a_generated_avatar_writes_a_probe_row_and_no_bytes(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config()
        _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            assert avatars.get_user_avatar(conn, "alice") is None
            assert avatars.user_avatar_hash(conn, "alice") is None
            assert avatars.import_probe_state(conn) == {"alice": '"g"'}
            row = conn.execute(
                "SELECT image FROM user_avatars WHERE user_id = 'alice'"
            ).fetchone()
            assert row["image"] is None

    def test_the_next_tick_sends_if_none_match_for_the_probe(
        self, import_config, monkeypatch
    ):
        config = import_config()
        _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))
        scheduler.check_avatar_import(config)

        calls = _fetches(monkeypatch, None)
        scheduler.check_avatar_import(config)

        assert calls == [("alice", '"g"')]

    def test_a_removed_nextcloud_avatar_does_not_blank_what_was_imported(
        self, import_config, db_path, monkeypatch
    ):
        """Documented behaviour: the imported copy stays. A user who wants it
        gone uploads their own."""
        config = import_config()
        _fetches(monkeypatch, nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'))
        scheduler.check_avatar_import(config)

        _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))
        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            assert avatars.get_user_avatar(conn, "alice") is not None


class TestTheGates:
    def test_a_local_storage_backend_is_a_no_op(
        self, import_config, monkeypatch
    ):
        config = import_config(nextcloud=NextcloudConfig(url=""))
        calls = _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))

        assert scheduler.check_avatar_import(config) == []
        assert calls == []

    def test_a_deployment_with_no_web_surface_is_a_no_op(
        self, import_config, monkeypatch
    ):
        """An avatar renders in the web UI and nowhere else.

        With `[web] enabled = false` there is no reader for these rows, so a
        tick would spend one Nextcloud request per configured user every six
        hours on bytes nothing will ever serve. It also has to agree with
        `doctor`: `web.avatar_import` SKIPs with "web interface disabled", the
        way every other `web.*` check does, and a check reporting SKIP for work
        the daemon is doing anyway is worse than no check.
        """
        config = import_config(
            web=WebConfig(enabled=False, avatar_import_from_nextcloud=True)
        )
        calls = _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))

        assert scheduler.check_avatar_import(config) == []
        assert calls == []

    def test_the_import_switch_is_re_checked_here_not_only_at_the_loop(
        self, import_config, monkeypatch
    ):
        config = import_config(
            web=WebConfig(enabled=True, avatar_import_from_nextcloud=False)
        )
        calls = _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))

        assert scheduler.check_avatar_import(config) == []
        assert calls == []

    def test_a_zero_interval_is_a_no_op(self, import_config, monkeypatch):
        config = import_config(scheduler=SchedulerConfig(avatar_import_interval=0))
        calls = _fetches(monkeypatch, nc_avatars.NoCustomAvatar(etag='"g"'))

        assert scheduler.check_avatar_import(config) == []
        assert calls == []


class TestFailureIsPerUser:
    def test_one_unreachable_account_does_not_end_the_tick(
        self, import_config, db_path, monkeypatch, caplog
    ):
        config = import_config(users=("alice", "bob"))
        _fetches(monkeypatch, {
            "alice": OcsError("boom", 500, None, "/index.php/avatar/alice/192"),
            "bob": nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'),
        })

        with caplog.at_level(logging.WARNING):
            outcomes = scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            assert avatars.get_user_avatar(conn, "bob") is not None
            assert avatars.get_user_avatar(conn, "alice") is None
        assert {o.user_id: o.action for o in outcomes} == {
            "alice": scheduler.AVATAR_IMPORT_FAILED,
            "bob": scheduler.AVATAR_IMPORT_IMPORTED,
        }

    def test_an_undecodable_remote_image_fails_that_user_only(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config(users=("alice", "bob"))
        _fetches(monkeypatch, {
            "alice": nc_avatars.RemoteAvatar(image=b"<svg/>", etag='"e1"'),
            "bob": nc_avatars.RemoteAvatar(image=_png(), etag='"e2"'),
        })

        outcomes = scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            assert avatars.get_user_avatar(conn, "alice") is None
            assert avatars.get_user_avatar(conn, "bob") is not None
        assert {o.user_id: o.action for o in outcomes}["alice"] == (
            scheduler.AVATAR_IMPORT_FAILED
        )


class TestNoTransactionAcrossAFetch:
    def test_a_concurrent_writer_is_not_locked_out_while_a_fetch_is_in_flight(
        self, import_config, db_path, monkeypatch
    ):
        """The loop is fetch, then one short write, then the next user.

        Wrapping the whole per-user loop in one `with db.get_db(...)` would hold
        the framework write lock for the length of one Nextcloud timeout, which
        stalls every writer in the daemon and in the web process. The second
        connection here waits two seconds and then raises, so an implementation
        that holds the transaction fails rather than taking thirty.
        """
        config = import_config(users=("alice", "bob"))
        observed: list[str] = []

        def _fake(cfg, uid, *, size=192, etag="", timeout=10.0):
            with db.get_db(db_path, busy_timeout_ms=2000) as other:
                other.execute(
                    "INSERT OR REPLACE INTO shared_kv "
                    "(namespace, key, value, written_by, updated_at) "
                    "VALUES ('t', ?, '1', 'test', datetime('now'))",
                    (uid,),
                )
                other.commit()
            observed.append(uid)
            return nc_avatars.RemoteAvatar(image=_png(), etag='"e1"')

        monkeypatch.setattr(scheduler.nc_avatars, "fetch_avatar", _fake)

        scheduler.check_avatar_import(config)

        assert sorted(observed) == ["alice", "bob"]


class TestTheRecordedState:
    def test_a_tick_records_what_it_saw_for_the_doctor_check(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config(users=("alice", "bob"))
        _fetches(monkeypatch, {
            "alice": nc_avatars.RemoteAvatar(image=_png(), etag='"e1"'),
            "bob": nc_avatars.NoCustomAvatar(etag='"g"'),
        })

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            state = avatars.read_import_state(conn)
        assert state is not None
        assert state["users"] == 2
        assert state["imported"] == 1
        assert state["no_custom"] == 1
        assert state["failed"] == 0
        assert state["header"] == avatars.HEADER_SEEN
        assert state["at"]

    def test_a_tick_that_learned_nothing_does_not_claim_the_header_was_absent(
        self, import_config, db_path, monkeypatch
    ):
        """Every user answered 304, so no response could have carried the header.
        Reporting that as "absent" would page an operator about a Nextcloud that
        is answering correctly."""
        config = import_config()
        _fetches(monkeypatch, None)

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            state = avatars.read_import_state(conn)
        assert state is not None
        assert state["header"] == avatars.HEADER_UNOBSERVED

    def test_a_missing_header_is_recorded_so_doctor_can_say_so(
        self, import_config, db_path, monkeypatch
    ):
        config = import_config()
        _fetches(
            monkeypatch,
            nc_avatars.NoCustomAvatar(etag='"g"', header_seen=False),
        )

        scheduler.check_avatar_import(config)

        with db.get_db(db_path) as conn:
            state = avatars.read_import_state(conn)
        assert state is not None
        assert state["header"] == avatars.HEADER_ABSENT

    def test_the_state_lives_in_a_namespace_the_model_may_not_touch(self):
        from istota.kv_namespaces import is_reserved_namespace

        assert is_reserved_namespace(avatars.IMPORT_STATE_NAMESPACE)


class TestTheCounts:
    def test_pictures_and_probes_are_counted_apart(self, db_conn: sqlite3.Connection):
        image, digest = avatars.normalize(
            _png(), declared_format=None, max_bytes=10_000_000
        )
        avatars.put_user_avatar(
            db_conn, "alice", source=avatars.SOURCE_NEXTCLOUD,
            image=image, content_hash=digest, remote_etag='"e1"',
        )
        avatars.touch_import_probe(db_conn, "bob", remote_etag='"g"')
        avatars.put_user_avatar(
            db_conn, "carol", source=avatars.SOURCE_UPLOAD,
            image=image, content_hash=digest,
        )

        counts = avatars.import_counts(db_conn)

        assert counts == {"imported": 1, "probes": 1}
