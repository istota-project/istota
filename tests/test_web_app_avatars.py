"""The avatar HTTP surface: who may see a face, and what a cache may keep.

Stage 2 of the profile-icons spec. The store and the decode path are covered by
`tests/test_avatars.py`; what is specific here is the boundary — the
authorization predicate on someone else's picture, the five-branch cache table,
and the two size checks that stand in front of the decoder.

Two properties carry most of the weight and neither is visible by reading the
handler:

**The three 404s are one 404.** "No avatar", "no shared room" and "no such user"
must be indistinguishable, or an authenticated caller can walk the deployment's
user list one id at a time and an avatar is a face. Asserted on the whole
answer, not on the status.

**A co-member's picture is never `immutable`.** Membership is revoked by code
that already exists (`db.remove_room_member`, room teardown), and a year-long
cache entry for a face makes the grant unrevokable — which would make the
predicate above ornamental the moment it started mattering.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

from PIL import Image

from istota import avatars, db
from istota.avatars import AvatarError
from istota.config import Config, SiteConfig, UserConfig, WebConfig

ORIGIN = {"origin": "https://example.com"}

pytestmark = _needs_web_deps


# --- fixtures ---------------------------------------------------------------


def _png(size=(500, 300), color=(120, 130, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _noisy_png(size) -> bytes:
    """A PNG whose byte length is roughly its pixel count.

    A flat colour encodes to a few hundred bytes at any dimension, so a size
    test written against `_png` measures the encoder rather than the cap.
    """
    width, height = size
    buf = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(width * height * 3)).save(buf, "PNG")
    return buf.getvalue()


def _make_config(tmp_path, db_path):
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={
            "alice": UserConfig(display_name="Alice"),
            "bob": UserConfig(display_name="Bob"),
            "carol": UserConfig(display_name="Carol"),
        },
        web=WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mock_oauth = MagicMock()
    mock_oauth.nextcloud = MagicMock()
    mod._oauth = mock_oauth
    return mod.app


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(tmp_path, db_path):
    return _make_config(tmp_path, db_path)


@pytest.fixture
def app(config):
    return _patch_app(config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username="alice"):
    """Log `client` in as `username`. One client is one browser session."""
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
        "user_id": username,
    })
    await client.get("/istota/callback", follow_redirects=False)


@pytest.fixture
async def alice(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        await _login(c, "alice")
        yield c


@pytest.fixture
async def bob(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        await _login(c, "bob")
        yield c


def _share_room(db_path, token, *users):
    with db.get_db(db_path) as conn:
        for user_id in users:
            db.add_room_member(conn, token, user_id)


async def _upload(client, data=None, name="face.png", content_type="image/png"):
    return await client.put(
        "/istota/api/settings/avatar",
        files={"file": (name, data if data is not None else _png(), content_type)},
        headers=ORIGIN,
    )


def _seed(db_path, user_id, source=avatars.SOURCE_UPLOAD, raw=None):
    """Put a normalized avatar straight in the store, bypassing the route."""
    image, digest = avatars.normalize(
        raw if raw is not None else _png(), declared_format=None,
        max_bytes=10 * 1024 * 1024,
    )
    with db.get_db(db_path) as conn:
        avatars.put_user_avatar(
            conn, user_id, source=source, image=image, content_hash=digest,
        )
    return image, digest


# --- serving the caller's own picture ---------------------------------------


class TestUploadAndServeSelf:
    async def test_upload_returns_the_normalized_hash_and_mime(self, alice, db_path):
        resp = await _upload(alice)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["hash"]) == 64
        assert body["mime"] == avatars.NORMALIZED_MIME
        assert body["bytes"] > 0
        with db.get_db(db_path) as conn:
            stored = avatars.get_user_avatar(conn, "alice")
        assert stored is not None
        assert stored.source == avatars.SOURCE_UPLOAD
        assert stored.content_hash == body["hash"]
        assert len(stored.image) == body["bytes"]

    async def test_matching_version_as_the_subject_is_immutable(self, alice):
        digest = (await _upload(alice)).json()["hash"]
        resp = await alice.get(f"/istota/api/avatars/user/alice?v={digest}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == avatars.NORMALIZED_MIME
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["cache-control"] == (
            "private, max-age=31536000, immutable"
        )
        assert Image.open(io.BytesIO(resp.content)).format == "WEBP"

    async def test_no_version_is_no_cache_and_carries_an_etag(self, alice):
        digest = (await _upload(alice)).json()["hash"]
        resp = await alice.get("/istota/api/avatars/user/alice")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "private, no-cache"
        assert digest in resp.headers["etag"]

    async def test_conditional_request_is_304_carrying_the_same_cache_control(
        self, alice,
    ):
        await _upload(alice)
        first = await alice.get("/istota/api/avatars/user/alice")
        second = await alice.get(
            "/istota/api/avatars/user/alice",
            headers={"if-none-match": first.headers["etag"]},
        )
        assert second.status_code == 304
        assert second.content == b""
        # A 304 that omits Cache-Control leaves the stored entry on whatever
        # freshness it was first cached with, and the branches do not agree.
        assert second.headers["cache-control"] == first.headers["cache-control"]
        assert second.headers["etag"] == first.headers["etag"]

    async def test_conditional_request_with_a_matching_version_stays_immutable(
        self, alice,
    ):
        digest = (await _upload(alice)).json()["hash"]
        url = f"/istota/api/avatars/user/alice?v={digest}"
        first = await alice.get(url)
        second = await alice.get(url, headers={"if-none-match": first.headers["etag"]})
        assert second.status_code == 304
        assert "immutable" in second.headers["cache-control"]

    async def test_a_stale_version_serves_the_current_bytes(self, alice):
        await _upload(alice)
        current = await alice.get("/istota/api/avatars/user/alice")
        stale = await alice.get("/istota/api/avatars/user/alice?v=" + "0" * 64)
        assert stale.status_code == 200
        assert stale.content == current.content
        # The server never sends stale bytes, and never invites a year of them.
        assert stale.headers["cache-control"] == "private, no-cache"

    async def test_self_is_served_with_no_shared_room(self, alice):
        # The self case does not consult `shares_room_with`, so a user in no
        # room at all still sees their own face.
        digest = (await _upload(alice)).json()["hash"]
        resp = await alice.get(f"/istota/api/avatars/user/alice?v={digest}")
        assert resp.status_code == 200

    async def test_an_unauthenticated_caller_gets_401(self, client, db_path):
        _seed(db_path, "alice")
        resp = await client.get("/istota/api/avatars/user/alice")
        assert resp.status_code == 401


# --- someone else's picture -------------------------------------------------


class TestCoMemberVisibility:
    async def test_a_co_member_is_served_but_never_immutable(
        self, alice, bob, db_path,
    ):
        digest = (await _upload(alice)).json()["hash"]
        _share_room(db_path, "rm1", "alice", "bob")

        resp = await bob.get(f"/istota/api/avatars/user/alice?v={digest}")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "private, max-age=300"
        # The whole point: a revocable grant cannot be cached as immutable.
        assert "immutable" not in resp.headers["cache-control"]

    async def test_a_co_member_with_no_version_still_gets_no_cache(
        self, alice, bob, db_path,
    ):
        await _upload(alice)
        _share_room(db_path, "rm1", "alice", "bob")
        resp = await bob.get("/istota/api/avatars/user/alice")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "private, no-cache"

    async def test_revoking_membership_closes_the_endpoint(
        self, alice, bob, db_path,
    ):
        await _upload(alice)
        _share_room(db_path, "rm1", "alice", "bob")
        assert (await bob.get("/istota/api/avatars/user/alice")).status_code == 200

        with db.get_db(db_path) as conn:
            db.remove_room_member(conn, "rm1", "bob")
        assert (await bob.get("/istota/api/avatars/user/alice")).status_code == 404

    async def test_the_three_404s_are_indistinguishable(self, bob, db_path):
        """Not visible, does not exist, and has no picture are one answer.

        A distinguishable refusal turns the endpoint into a user-directory
        oracle, one id at a time. Compared on the whole answer rather than on
        the status, since a differing body or freshness leaks the same fact.
        """
        _seed(db_path, "alice")            # a real user bob cannot see
        _share_room(db_path, "rm1", "bob", "carol")  # a co-member with no picture

        not_visible = await bob.get("/istota/api/avatars/user/alice")
        no_such_user = await bob.get("/istota/api/avatars/user/nobody")
        no_picture = await bob.get("/istota/api/avatars/user/carol")

        answers = [not_visible, no_such_user, no_picture]
        assert [r.status_code for r in answers] == [404, 404, 404]
        assert len({r.content for r in answers}) == 1
        assert len({r.headers.get("cache-control") for r in answers}) == 1
        # A short negative cache, not `no-store`: a room holding one
        # avatar-less member would otherwise cost a 404 on every page load.
        assert not_visible.headers["cache-control"] == "private, max-age=30"

    async def test_a_stranger_cannot_confirm_a_hash_by_sending_it(
        self, alice, bob, db_path,
    ):
        digest = (await _upload(alice)).json()["hash"]
        resp = await bob.get(f"/istota/api/avatars/user/alice?v={digest}")
        assert resp.status_code == 404

    async def test_a_stranger_gets_no_304_from_a_conditional_request(
        self, alice, bob, db_path,
    ):
        digest = (await _upload(alice)).json()["hash"]
        resp = await bob.get(
            "/istota/api/avatars/user/alice",
            headers={"if-none-match": f'"{digest}"'},
        )
        # A 304 where a stranger gets a 404 would confirm both the user and
        # their picture, which is the oracle the 404s exist to close.
        assert resp.status_code == 404


class TestSharesRoomWith:
    def test_true_only_for_a_room_in_common(self, db_path):
        _share_room(db_path, "rm1", "alice", "bob")
        _share_room(db_path, "rm2", "carol")
        with db.get_db(db_path) as conn:
            assert db.shares_room_with(conn, "alice", "bob") is True
            assert db.shares_room_with(conn, "bob", "alice") is True
            assert db.shares_room_with(conn, "alice", "carol") is False
            assert db.shares_room_with(conn, "alice", "nobody") is False

    def test_a_user_in_no_room_shares_nothing(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.shares_room_with(conn, "alice", "bob") is False

    def test_a_hidden_room_still_counts_as_shared(self, db_path):
        """Hiding a room in web is not leaving the conversation.

        `room_dismissals` is a per-user display tombstone, and the Talk poll
        re-adds a dropped `room_members` row the next time it registers the
        room — so a `remove_room_member` on a Talk-origin room is not a durable
        revocation, whatever the caller intended. The predicate is deliberately
        the wider one, because it is the true one: the two people are still in
        that Talk conversation together, seeing each other's names and presence
        there. Pinned so the next reader finds a decision rather than a gap.
        """
        with db.get_db(db_path) as conn:
            db.add_room_member(conn, "rm1", "alice")
            db.add_room_member(conn, "rm1", "bob")
            db.remove_room_member(conn, "rm1", "bob")
            db.dismiss_room(conn, "rm1", "bob")
            assert db.shares_room_with(conn, "alice", "bob") is False
            # What the next poll tick does.
            db.add_room_member(conn, "rm1", "bob")
            assert db.shares_room_with(conn, "alice", "bob") is True


class TestTheAcceptListIsStatedOnce:
    def test_the_typescript_copy_equals_the_python_one(self):
        """Two languages, one list, and nothing else holding them equal.

        Same treatment `usage_render.py` and `usageFormat.ts` get: the client
        offers what the server accepts, so a format added on one side and not
        the other leaves the picker narrower than the endpoint (a format the
        user cannot choose) or wider (one they find out about after uploading).
        """
        source = (
            Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "api.ts"
        ).read_text()
        match = re.search(r"export const AVATAR_ACCEPT = '([^']+)'", source)
        assert match, "AVATAR_ACCEPT is not declared as a single-quoted literal"
        assert match.group(1) == avatars.ACCEPT_ATTRIBUTE


# --- the bot icon -----------------------------------------------------------


class TestBotAvatar:
    def _seed_bot(self, db_path):
        image, digest = avatars.normalize(
            _png(), declared_format=None, max_bytes=10 * 1024 * 1024,
        )
        with db.get_db(db_path) as conn:
            avatars.put_bot_avatar(conn, image=image, content_hash=digest)
        return image, digest

    async def test_any_authenticated_user_may_fetch_it(self, bob, db_path):
        image, digest = self._seed_bot(db_path)
        resp = await bob.get(f"/istota/api/avatars/bot?v={digest}")
        assert resp.status_code == 200
        assert resp.content == image
        assert resp.headers["content-type"] == avatars.NORMALIZED_MIME
        # Not revocable, so it keeps the long cache.
        assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"

    async def test_no_icon_is_the_same_negative_cache(self, bob):
        resp = await bob.get("/istota/api/avatars/bot")
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "private, max-age=30"

    async def test_it_is_not_public(self, client):
        assert (await client.get("/istota/api/avatars/bot")).status_code == 401


# --- upload bounds ----------------------------------------------------------


class TestUploadBounds:
    async def test_a_declared_length_over_the_cap_is_refused_before_the_decode(
        self, alice, config, monkeypatch,
    ):
        import istota.web_app as mod

        config.web.max_avatar_kb = 1
        called = []
        monkeypatch.setattr(
            avatars, "normalize",
            lambda *a, **k: called.append(1) or (b"", ""),
        )
        data = _noisy_png((120, 120))
        # Over the stream limit, so the declared length alone settles it.
        assert len(data) > 1024 + mod._AVATAR_MULTIPART_ALLOWANCE
        resp = await _upload(alice, data=data)
        assert resp.status_code == 413
        assert called == []
        assert "error" in resp.json()

    async def test_a_missing_content_length_is_refused(self, alice, config):
        async def _chunks():
            yield b"whatever"

        resp = await alice.put(
            "/istota/api/settings/avatar",
            content=_chunks(),
            headers={**ORIGIN, "content-type": "multipart/form-data; boundary=x"},
        )
        assert resp.status_code == 413

    async def test_a_declared_length_over_the_limit_reads_no_chunk_at_all(self):
        """The route-level test above cannot tell the two checks apart.

        Both refuse before `normalize`, so dropping the header check leaves it
        green. This is the property that check exists for, asserted where it
        is visible: nothing is pulled off the stream.
        """
        import istota.web_app as mod

        pulled = []

        class _Req:
            headers = {"content-length": "1000000"}

            async def stream(self):
                pulled.append(1)
                yield b"x"

        with pytest.raises(AvatarError) as excinfo:
            await mod._read_bounded_body(_Req(), 16)
        assert excinfo.value.status == 413
        assert pulled == []

    async def test_the_stream_stops_at_the_cap_and_reads_no_further(self):
        """A declared length is a claim; the running total is the enforcement."""
        import istota.web_app as mod

        pulled = []

        class _Req:
            headers = {"content-length": "8"}

            async def stream(self):
                for i in range(10):
                    pulled.append(i)
                    yield b"0123456789"

        with pytest.raises(AvatarError) as excinfo:
            await mod._read_bounded_body(_Req(), 16)
        assert excinfo.value.status == 413
        # Refused on the second chunk; the remaining eight are never read.
        assert len(pulled) < 10

    async def test_a_part_over_the_cap_is_refused_by_the_second_check(
        self, alice, config,
    ):
        import istota.web_app as mod

        # Under the stream limit (cap + multipart allowance) and over the cap
        # itself, so this can only be caught by the byte check on the part.
        config.web.max_avatar_kb = 1
        data = _noisy_png((40, 40))
        assert 1024 < len(data) < 1024 + mod._AVATAR_MULTIPART_ALLOWANCE
        resp = await _upload(alice, data=data)
        assert resp.status_code == 413

    async def test_an_svg_is_415(self, alice):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>'
        resp = await _upload(alice, data=svg, name="face.svg", content_type="image/svg+xml")
        assert resp.status_code == 415
        assert "error" in resp.json()

    async def test_a_non_multipart_body_is_refused(self, alice):
        resp = await alice.put(
            "/istota/api/settings/avatar",
            content=_png(),
            headers={**ORIGIN, "content-type": "image/png"},
        )
        assert resp.status_code == 400

    async def test_a_malformed_multipart_body_is_refused_not_a_500(self, alice):
        """The parser raises past Starlette's own exception type.

        `MultiPartParser.parse` wraps only `MultiPartException`; the underlying
        `python_multipart` parser raises `MultipartParseError`, which is a
        different class and escapes. Any authenticated caller reaches it with a
        content type that declares a boundary the body does not have — which is
        also what a truncated proxy write looks like — and the answer was a 500
        with a traceback, carrying none of the `{error}` shape the client reads.
        """
        resp = await alice.put(
            "/istota/api/settings/avatar",
            content=b"garbage-not-multipart",
            headers={**ORIGIN, "content-type": "multipart/form-data; boundary=x"},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    async def test_a_client_disconnect_is_not_a_500(self):
        """An upload the user cancels is the ordinary case, not the exotic one.

        `Request.stream()` raises `ClientDisconnect`, which the route does not
        catch, so an aborted upload logged a traceback per attempt.
        """
        import istota.web_app as mod
        from starlette.requests import ClientDisconnect

        class _Req:
            headers = {"content-length": "8"}

            async def stream(self):
                yield b"0123"
                raise ClientDisconnect()

        with pytest.raises(AvatarError) as excinfo:
            await mod._read_bounded_body(_Req(), 4096)
        assert excinfo.value.status == 400

    async def test_a_multipart_body_with_no_file_part_is_refused(self, alice):
        resp = await alice.put(
            "/istota/api/settings/avatar",
            files={"other": ("x.png", _png(), "image/png")},
            headers=ORIGIN,
        )
        assert resp.status_code == 400

    async def test_a_missing_origin_is_403(self, alice):
        resp = await alice.put(
            "/istota/api/settings/avatar",
            files={"file": ("face.png", _png(), "image/png")},
        )
        assert resp.status_code == 403

    async def test_an_unauthenticated_upload_is_401(self, client):
        resp = await _upload(client)
        assert resp.status_code == 401


class TestDecodeIsSerialized:
    """A per-request memory budget, enforced as a one-at-a-time decode.

    `Image.open` decompresses a PNG's text chunks before any byte or pixel
    ceiling can see them, at up to Pillow's own 64 MiB budget, so the peak for
    one upload is far above what the 4 MB cap suggests. The route cannot lower
    Pillow's globals (they are shared with the executor's attachment pre-shrink
    running in another thread), so it bounds how many decodes can be paying that
    peak at once instead.

    The two tests below are a pair. `peak == 1` holds trivially if the two
    requests simply never overlap, so the control runs the identical arrangement
    against a two-worker executor and requires `peak == 2`: without it the
    assertion could be measuring the scheduler rather than the budget.
    """

    async def _peak_concurrency(self, alice, bob, monkeypatch):
        live = 0
        peak = 0
        lock = threading.Lock()
        real = avatars.normalize
        # Both decodes must be in flight together for a widened pool to show
        # it, so the first one waits for the second to arrive rather than
        # sleeping a hopeful interval.
        both_in = threading.Barrier(2, timeout=5)

        def _slow(*args, **kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                try:
                    both_in.wait()
                except threading.BrokenBarrierError:
                    # One worker: the second decode cannot arrive until this
                    # one returns, which is the property under test.
                    pass
                return real(*args, **kwargs)
            finally:
                with lock:
                    live -= 1

        monkeypatch.setattr(avatars, "normalize", _slow)
        results = await asyncio.gather(_upload(alice), _upload(bob))
        assert [r.status_code for r in results] == [200, 200]
        return peak

    async def test_two_uploads_never_decode_at_once(self, alice, bob, monkeypatch):
        assert await self._peak_concurrency(alice, bob, monkeypatch) == 1

    async def test_the_control_reaches_two_on_a_wider_pool(
        self, alice, bob, monkeypatch,
    ):
        import istota.web_app as mod

        with ThreadPoolExecutor(max_workers=2) as pool:
            monkeypatch.setattr(mod, "_avatar_decode_executor", pool)
            assert await self._peak_concurrency(alice, bob, monkeypatch) == 2


# --- removal ----------------------------------------------------------------


class TestDelete:
    async def test_it_removes_the_upload_row(self, alice, db_path):
        await _upload(alice)
        resp = await alice.delete("/istota/api/settings/avatar", headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert (await alice.get("/istota/api/avatars/user/alice")).status_code == 404

    async def test_it_is_idempotent(self, alice):
        resp = await alice.delete("/istota/api/settings/avatar", headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False

    async def test_it_reveals_an_imported_nextcloud_picture(self, alice, db_path):
        _, imported = _seed(
            db_path, "alice", source=avatars.SOURCE_NEXTCLOUD,
            raw=_png(color=(4, 8, 16)),
        )
        await _upload(alice)
        await alice.delete("/istota/api/settings/avatar", headers=ORIGIN)

        resp = await alice.get("/istota/api/avatars/user/alice")
        assert resp.status_code == 200
        assert imported in resp.headers["etag"]

    async def test_it_touches_no_other_source(self, alice, db_path):
        _seed(db_path, "alice", source=avatars.SOURCE_NEXTCLOUD)
        resp = await alice.delete("/istota/api/settings/avatar", headers=ORIGIN)
        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            assert avatars.get_user_avatar(conn, "alice") is not None

    async def test_a_missing_origin_is_403(self, alice):
        assert (await alice.delete("/istota/api/settings/avatar")).status_code == 403


# --- /me --------------------------------------------------------------------


class TestMeCarriesTheHashes:
    async def test_both_are_null_when_nothing_is_stored(self, alice):
        me = (await alice.get("/istota/api/me")).json()
        assert me["avatars"] == {"user": None, "bot": None}

    async def test_the_user_hash_appears_after_an_upload(self, alice):
        digest = (await _upload(alice)).json()["hash"]
        me = (await alice.get("/istota/api/me")).json()
        assert me["avatars"]["user"] == digest

    async def test_the_bot_hash_is_the_stored_icon(self, alice, db_path):
        image, digest = avatars.normalize(
            _png(), declared_format=None, max_bytes=10 * 1024 * 1024,
        )
        with db.get_db(db_path) as conn:
            avatars.put_bot_avatar(conn, image=image, content_hash=digest)
        me = (await alice.get("/istota/api/me")).json()
        assert me["avatars"]["bot"] == digest

    async def test_it_answers_when_the_tables_are_missing(self, alice, db_path):
        # `/me` is the identity every page reads. A framework database that
        # predates the migration must cost the decoration, not the route.
        with db.get_db(db_path) as conn:
            conn.execute("DROP TABLE user_avatars")
            conn.execute("DROP TABLE bot_avatar")
        resp = await alice.get("/istota/api/me")
        assert resp.status_code == 200
        assert resp.json()["avatars"] == {"user": None, "bot": None}


# --- the admin bot-icon writes ----------------------------------------------
#
# Stage 5. The first mutating admin routes in `web_app.py`, though not in the
# app: they copy `briefings/routes.py`'s `require_admin` + `verify_origin`
# pair. What is asserted here is that the gate is real on both verbs, that the
# body takes the same two size checks the user's own upload does rather than a
# second implementation of them, and that a refusal stores nothing.


@pytest.fixture
async def admin(config, alice):
    """Alice, with the deployment naming her an admin.

    `_user_is_web_admin` reads `_config.admin_users` per request, so mutating
    the config the app already holds is enough and the order the two fixtures
    resolve in does not matter.
    """
    config.admin_users = {"alice"}
    return alice


async def _put_bot_icon(client, data=None, name="icon.png", content_type="image/png",
                        headers=ORIGIN):
    return await client.put(
        "/istota/api/admin/avatar",
        files={"file": (name, data if data is not None else _png(), content_type)},
        headers=headers,
    )


def _stored_bot_icon(db_path):
    with db.get_db(db_path) as conn:
        return avatars.get_bot_avatar(conn)


class TestAdminBotIconWrites:
    async def test_an_admin_sets_it_and_every_caller_can_fetch_it(
        self, admin, bob, db_path,
    ):
        resp = await _put_bot_icon(admin)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["hash"]) == 64
        assert body["mime"] == avatars.NORMALIZED_MIME
        assert body["bytes"] > 0

        served = await bob.get(f"/istota/api/avatars/bot?v={body['hash']}")
        assert served.status_code == 200
        assert served.content == _stored_bot_icon(db_path).image

    async def test_a_non_admin_may_not_set_it(self, bob, db_path):
        assert (await _put_bot_icon(bob)).status_code == 403
        assert _stored_bot_icon(db_path) is None

    async def test_an_anonymous_caller_may_not_set_it(self, client, db_path):
        assert (await _put_bot_icon(client)).status_code == 401
        assert _stored_bot_icon(db_path) is None

    async def test_a_missing_origin_is_refused(self, admin, db_path):
        assert (await _put_bot_icon(admin, headers={})).status_code == 403
        assert _stored_bot_icon(db_path) is None

    async def test_the_last_writer_wins(self, admin, db_path):
        first = (await _put_bot_icon(admin)).json()["hash"]
        second = (await _put_bot_icon(admin, data=_png(color=(9, 200, 30)))).json()["hash"]
        assert first != second
        assert _stored_bot_icon(db_path).content_hash == second
        with db.get_db(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM bot_avatar").fetchone()
        assert rows["n"] == 1

    async def test_a_declared_length_over_the_cap_never_reaches_the_decoder(
        self, admin, config, monkeypatch, db_path,
    ):
        # The admin route must take `_read_avatar_upload`'s two checks rather
        # than a second copy of them: the cap has to bite before the body
        # exists in memory, and `len(raw)` is too late.
        import istota.avatars as avatars_mod
        calls = []
        monkeypatch.setattr(
            avatars_mod, "normalize",
            lambda *a, **k: calls.append(1) or (b"", ""),
        )
        config.web.max_avatar_kb = 1
        resp = await _put_bot_icon(admin, data=_noisy_png((200, 200)))
        assert resp.status_code == 413
        assert resp.json()["error"]
        assert calls == []
        assert _stored_bot_icon(db_path) is None

    async def test_an_svg_is_refused_and_stores_nothing(self, admin, db_path):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>'
        resp = await _put_bot_icon(admin, data=svg, name="icon.svg",
                                   content_type="image/svg+xml")
        assert resp.status_code == 415
        assert resp.json()["error"]
        assert _stored_bot_icon(db_path) is None

    async def test_the_bot_hash_reaches_me(self, admin):
        digest = (await _put_bot_icon(admin)).json()["hash"]
        me = (await admin.get("/istota/api/me")).json()
        assert me["avatars"]["bot"] == digest


class TestAdminBotIconClear:
    async def test_an_admin_clears_it(self, admin, db_path):
        await _put_bot_icon(admin)
        resp = await admin.delete("/istota/api/admin/avatar", headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert _stored_bot_icon(db_path) is None

    async def test_it_is_idempotent(self, admin):
        resp = await admin.delete("/istota/api/admin/avatar", headers=ORIGIN)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False

    async def test_a_non_admin_may_not_clear_it(self, admin, bob, db_path):
        await _put_bot_icon(admin)
        assert (await bob.delete("/istota/api/admin/avatar",
                                 headers=ORIGIN)).status_code == 403
        assert _stored_bot_icon(db_path) is not None

    async def test_a_missing_origin_is_refused(self, admin, db_path):
        await _put_bot_icon(admin)
        assert (await admin.delete("/istota/api/admin/avatar")).status_code == 403
        assert _stored_bot_icon(db_path) is not None

    async def test_it_leaves_users_pictures_alone(self, admin, db_path):
        _seed(db_path, "bob")
        await _put_bot_icon(admin)
        await admin.delete("/istota/api/admin/avatar", headers=ORIGIN)
        with db.get_db(db_path) as conn:
            assert avatars.get_user_avatar(conn, "bob") is not None


# --- the login page's mark --------------------------------------------------
#
# The server-rendered login card inlines the bot icon as a `data:` URI rather
# than gaining a second route for one image on one page. That is explicitly not
# a privacy measure — the same page already serves the favicon and the sigil to
# an unauthenticated browser off the static mount — so what these assert is the
# two things that *are* load-bearing: the blob is not read out of SQLite per
# request, and the icon does not inherit the sigil's light-theme inversion,
# which would render a photograph as a negative.


@pytest.fixture(autouse=True)
def _clear_login_icon_memo():
    import istota.web_app as mod
    mod._login_icon_memo = None
    yield
    mod._login_icon_memo = None


def _seed_bot_icon(db_path, color=(120, 130, 140)):
    image, digest = avatars.normalize(
        _png(color=color), declared_format=None, max_bytes=10 * 1024 * 1024,
    )
    with db.get_db(db_path) as conn:
        avatars.put_bot_avatar(conn, image=image, content_hash=digest)
    return image, digest


class TestTheLoginPageMark:
    async def test_the_sigil_renders_when_no_icon_is_set(self, client):
        html = (await client.get("/istota/login")).text
        assert "/istota/octopus-sigil.webp" in html
        assert "data:image/webp;base64," not in html

    async def test_the_icon_is_inlined_when_one_is_set(self, client, db_path):
        import base64
        image, _ = _seed_bot_icon(db_path)
        html = (await client.get("/istota/login")).text
        assert "/istota/octopus-sigil.webp" not in html
        expected = base64.b64encode(image).decode("ascii")
        assert f"data:image/webp;base64,{expected}" in html

    async def test_the_inlined_icon_opts_out_of_the_light_theme_inversion(
        self, client, db_path,
    ):
        # `.mark` is inverted for the light theme because the sigil is a flat
        # near-white silhouette. Running a photograph through that filter
        # produces a negative — the same reason `Avatar.svelte` applies none.
        _seed_bot_icon(db_path)
        html = (await client.get("/istota/login")).text
        assert 'class="mark icon"' in html
        assert ":root[data-theme='light'] .mark.icon { filter: none; }" in html

    async def test_the_blob_is_read_once_across_two_renders(
        self, client, db_path, monkeypatch,
    ):
        _seed_bot_icon(db_path)
        import istota.avatars as avatars_mod
        blob_reads = []
        real = avatars_mod.get_bot_avatar
        monkeypatch.setattr(
            avatars_mod, "get_bot_avatar",
            lambda conn: blob_reads.append(1) or real(conn),
        )
        first = (await client.get("/istota/login")).text
        second = (await client.get("/istota/login")).text
        assert first == second
        assert len(blob_reads) == 1

    async def test_a_new_icon_re_encodes(self, client, db_path):
        _seed_bot_icon(db_path)
        first = (await client.get("/istota/login")).text
        image, _ = _seed_bot_icon(db_path, color=(2, 4, 8))
        second = (await client.get("/istota/login")).text
        assert first != second
        import base64
        assert base64.b64encode(image).decode("ascii") in second

    async def test_a_read_failure_falls_back_to_the_sigil(self, client, db_path):
        # Nothing on the login path may raise on account of decoration.
        with db.get_db(db_path) as conn:
            conn.execute("DROP TABLE bot_avatar")
        resp = await client.get("/istota/login")
        assert resp.status_code == 200
        assert "/istota/octopus-sigil.webp" in resp.text

    async def test_the_error_card_carries_the_same_mark(self, client, db_path):
        import base64
        import istota.web_app as mod
        image, _ = _seed_bot_icon(db_path)
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            side_effect=RuntimeError("boom"),
        )
        resp = await client.get("/istota/callback", follow_redirects=False)
        assert resp.status_code >= 400
        assert base64.b64encode(image).decode("ascii") in resp.text
