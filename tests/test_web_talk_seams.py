"""`web_app`'s own Talk calls, through the strict double.

Until now the double reached the daemon and not the web process. `web_app.py`
constructs `TalkClient(...)` directly in seven places — each with a per-user
OAuth bearer token, so there is no `get_talk_client` to patch — and two of them
are the paths most exposed to ISSUE-400: `_chat_promote_to_talk`, which *creates*
the divergence between a room's canonical token and its Talk ref, and
`_post_as_user`, which posts a web turn to that ref at ingest. A green suite said
nothing about either.

So every room here is a `promoted_room`, where the two tokens differ. On a plain
Talk room they are equal and a misroute is invisible by construction, which is
the whole reason the bug survived three call sites.

**Nothing here asserts "no exception was raised".** `_mirror_web_turn_as_user`,
`_push_read_to_talk` and `_delete_from_talk` each swallow everything, and
`UnknownTalkRoom` is swallowed exactly as the real 404 is — so the evidence is
`fake_talk.calls`, the returned value, and the external-id stamp.

The other half of the file is the 401 retry. `_post_as_user` force-refreshes the
token once and tries again, and a double whose only unhappy answer was
`UnknownTalkRoom` would collapse that into a misroute and quietly delete the
coverage. `bearer_rejections` is what keeps the two apart; see
`tests/support/talk_double.py`.
"""

import pytest

from istota import db, web_tokens
from istota.config import NextcloudConfig, WebConfig

from .support.rooms import promoted_room

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

pytestmark = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

KEY = "m" * 64
STALE = "stale-at"
FRESH = "fresh-at"


@pytest.fixture
def web_app_module(monkeypatch, make_config, db_path):
    """`istota.web_app` with `_config` pointed at the double's own database.

    The module global is how every one of these functions finds its config —
    they run inside a request, not with one passed in — so a test that forgets
    it gets a silent early return rather than a failure.
    """
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)
    import istota.web_app as mod

    config = make_config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        web=WebConfig(
            enabled=True,
            oauth2_provider="https://nc.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="s",
            session_secret_key="k",
            token_storage="encrypted",
        ),
    )
    # The rooms poll builds a bearer client of its own; off, so the ledger below
    # holds only what the path under test did.
    config.web.chat.talk_read_sync_interval = 0
    monkeypatch.setattr(mod, "_config", config)
    _clear_module_state(mod)
    yield mod
    _clear_module_state(mod)


def _clear_module_state(mod) -> None:
    """The four process-lifetime globals these paths touch.

    `_bg_tasks` is here for parity with the two neighbouring web fixtures
    rather than for a leak that exists: nothing in this file reaches
    `_fire_and_forget`, since every test drives a coroutine directly instead of
    going through an endpoint. A stale entry would be a task from a closed
    event loop, which is worth never inheriting.
    """
    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()
    mod._token_degraded_logged.clear()
    mod._bg_tasks.clear()


@pytest.fixture
def room(db_path):
    """A promoted room: `canonical` is a `web-…` token Talk has never heard of."""
    with db.get_db(db_path) as conn:
        return promoted_room(conn, "alice")


def _store(db_path, access=STALE):
    web_tokens.store_tokens(db_path, "alice", access, "rt", 3600)


def _user_turn(db_path, room_token, task_id):
    with db.get_db(db_path) as conn:
        return db.add_message(
            conn, room_token, role="user", body="hello from web",
            origin_surface="web", task_id=task_id,
        )


def _stamp(db_path, message_id):
    with db.get_db(db_path) as conn:
        return db.get_message_external_id(conn, message_id, "talk")


def _refresh_to(monkeypatch, replacement):
    """Make a forced refresh hand back `replacement`, ordinary reads unchanged."""
    real = web_tokens.get_access_token
    refreshed = []

    def _spy(db_path, config, user_id, *, force_refresh=False):
        if force_refresh:
            refreshed.append(user_id)
            return replacement
        return real(db_path, config, user_id)

    monkeypatch.setattr(web_tokens, "get_access_token", _spy)
    return refreshed


class TestThePostAsUserMirror:
    """ISSUE-400's shape in the web process: the turn goes to the room's Talk
    ref, and the double refuses the canonical token it used to be given."""

    async def test_it_posts_to_the_talk_ref_and_stamps_the_turn(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        _store(db_path, "live-at")
        message_id = _user_turn(db_path, room.canonical, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", room.canonical, "hello from web", 7,
        )

        assert fake_talk_web.refusals == []
        assert [(c.method, c.token) for c in fake_talk_web.calls] == [
            ("send_message", room.talk_ref),
        ]
        # The stamp is the second half: a post the double accepted but that the
        # product failed to record leaves the scheduler reposting it as the bot.
        assert _stamp(db_path, message_id) == str(fake_talk_web.sent_ids[-1])

    async def test_the_post_carries_the_webmirror_reference_id(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        _store(db_path, "live-at")
        message_id = _user_turn(db_path, room.canonical, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", room.canonical, "hello from web", 7,
        )

        assert fake_talk_web.sent_id_for(f"istota:webmirror:{message_id}") == (
            fake_talk_web.sent_ids[-1]
        )

    async def test_the_client_is_the_users_own_short_lived_one(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """A bot-authored mirror would misrepresent who wrote the turn, and the
        5 s bound is what keeps this out of the send's latency budget."""
        _store(db_path, "live-at")
        _user_turn(db_path, room.canonical, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", room.canonical, "hello from web", 7,
        )

        assert [(c.bearer_token, c.timeout) for c in
                fake_talk_web.constructions] == [("live-at", 5)]
        # And it is closed again. Six of the seven sites do this in a `finally`;
        # the seventh is the subject of `TestTheMessageDelete`'s pinned gap.
        assert fake_talk_web.closes == 1

    async def test_an_unbound_room_reaches_talk_at_all(
        self, fake_talk_web, web_app_module, db_path,
    ):
        """A web-only room has no Talk leg; the scheduler's attributed repost
        covers it. Asserted so a future binding lookup that answers the wrong
        room cannot hide behind "nothing happened, as expected"."""
        with db.get_db(db_path) as conn:
            handle = db.create_web_chat_room(conn, "alice", "web only")
        _store(db_path, "live-at")
        _user_turn(db_path, handle.token, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", handle.token, "hello", 7,
        )

        assert fake_talk_web.calls == []
        assert fake_talk_web.constructions == []


class TestThe401Retry:
    """The behaviour the construction-site patch had to preserve.

    `_post_as_user` force-refreshes once on a 401 and tries again. Every
    assertion here is on the second attempt actually happening with the *new*
    credential — a double that answered a stale token with `UnknownTalkRoom`
    would leave all of this looking like one misrouted call.
    """

    async def test_a_401_forces_a_refresh_and_retries_once(
        self, fake_talk_web, web_app_module, db_path, room, monkeypatch,
    ):
        _store(db_path, STALE)
        fake_talk_web.bearer_rejections[STALE] = 401
        refreshed = _refresh_to(monkeypatch, FRESH)
        message_id = _user_turn(db_path, room.canonical, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", room.canonical, "hello from web", 7,
        )

        assert refreshed == ["alice"], "a 401 did not force a refresh"
        assert [c.bearer_token for c in fake_talk_web.constructions] == [
            STALE, FRESH,
        ]
        assert [(c.token, c.status, c.bearer_token)
                for c in fake_talk_web.calls] == [
            (room.talk_ref, 401, STALE),
            (room.talk_ref, None, FRESH),
        ]
        # And it landed: a retry that posts nowhere is not a recovery.
        assert _stamp(db_path, message_id) == str(fake_talk_web.sent_ids[-1])
        assert fake_talk_web.refusals == [], "a stale token is not a misroute"

    async def test_it_retries_only_once(
        self, fake_talk_web, web_app_module, db_path, room, monkeypatch,
    ):
        """A token the provider keeps refusing must not become a retry loop."""
        _store(db_path, STALE)
        fake_talk_web.bearer_rejections = {STALE: 401, FRESH: 401}
        _refresh_to(monkeypatch, FRESH)
        message_id = _user_turn(db_path, room.canonical, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", room.canonical, "hello from web", 7,
        )

        assert len(fake_talk_web.auth_failures) == 2
        assert _stamp(db_path, message_id) is None

    async def test_a_403_is_not_retried(
        self, fake_talk_web, web_app_module, db_path, room, monkeypatch,
    ):
        """Only 401 says "this token is stale". A 403 is an answer about the
        room, and a fresh token cannot change it."""
        _store(db_path, STALE)
        fake_talk_web.bearer_rejections[STALE] = 403
        refreshed = _refresh_to(monkeypatch, FRESH)
        message_id = _user_turn(db_path, room.canonical, task_id=7)

        await web_app_module._mirror_web_turn_as_user(
            "alice", room.canonical, "hello from web", 7,
        )

        assert refreshed == []
        assert len(fake_talk_web.constructions) == 1
        assert _stamp(db_path, message_id) is None


class TestThePromotePath:
    """The function that *creates* a promoted room, checked by the rule.

    `create_conversation` mints a token bound to nothing, so the
    `add_participant` and seed `send_message` that follow are refused unless the
    product persisted the binding first — which is exactly the ordering its own
    comment says is load-bearing.
    """

    @pytest.fixture
    def web_room(self, db_path):
        with db.get_db(db_path) as conn:
            return db.create_web_chat_room(conn, "alice", "notes")

    async def test_it_binds_the_conversation_it_created(
        self, fake_talk_web, web_app_module, db_path, web_room,
    ):
        status, payload = await web_app_module._chat_promote_to_talk(
            "alice", web_room.id,
        )

        assert status == "ok"
        created = fake_talk_web.created_tokens[-1]
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, web_room.token, "talk")
        assert binding.surface_ref == created
        assert payload["talk_token"] == created

    async def test_the_participant_and_seed_post_name_that_conversation(
        self, fake_talk_web, web_app_module, web_room,
    ):
        await web_app_module._chat_promote_to_talk("alice", web_room.id)

        created = fake_talk_web.created_tokens[-1]
        assert [(c.method, c.token) for c in fake_talk_web.calls] == [
            ("create_conversation", None),
            ("add_participant", created),
            ("send_message", created),
        ]
        assert fake_talk_web.refusals == []

    async def test_no_call_names_the_rooms_canonical_token(
        self, fake_talk_web, web_app_module, web_room,
    ):
        """The one that would 404 in production, and the reason this room shape
        exists: `web_room.token` is a `web-…` string Nextcloud never issued."""
        await web_app_module._chat_promote_to_talk("alice", web_room.id)

        assert fake_talk_web.calls_to(web_room.token) == []

    async def test_the_promote_client_is_the_bot(
        self, fake_talk_web, web_app_module, web_room,
    ):
        """The conversation is owned by the bot account, not by the requester —
        the user is added to it as a participant."""
        await web_app_module._chat_promote_to_talk("alice", web_room.id)

        assert [c.bearer_token for c in fake_talk_web.constructions] == [None]

    async def test_an_already_bound_room_is_left_alone(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """The liveness probe answers `live` because the binding names a
        conversation the double knows, so nothing is created or replaced."""
        with db.get_db(db_path) as conn:
            handle = db.get_web_chat_room_by_token(conn, room.canonical)

        status, _payload = await web_app_module._chat_promote_to_talk(
            "alice", handle.id,
        )

        assert status == "live"
        assert [(c.method, c.token) for c in fake_talk_web.calls] == [
            ("get_conversation_info", room.talk_ref),
        ]
        assert fake_talk_web.created_tokens == []


class TestTheReadPush:
    async def test_it_marks_the_talk_ref_read(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        _store(db_path, "live-at")

        assert await web_app_module._push_read_to_talk(
            "alice", room.canonical,
        ) is True
        assert [(c.method, c.token) for c in fake_talk_web.calls] == [
            ("mark_conversation_read", room.talk_ref),
        ]
        assert fake_talk_web.closes == 1

    async def test_a_401_is_recovered_here_too(
        self, fake_talk_web, web_app_module, db_path, room, monkeypatch,
    ):
        """`mark_conversation_read` swallows by default, so `_mark_read_as_user`
        passes `raise_on_error=True` to see the status. The double reproduces
        both halves; without the second it could not drive this at all."""
        _store(db_path, STALE)
        fake_talk_web.bearer_rejections[STALE] = 401
        _refresh_to(monkeypatch, FRESH)

        assert await web_app_module._push_read_to_talk(
            "alice", room.canonical,
        ) is True
        assert [c.bearer_token for c in fake_talk_web.calls] == [STALE, FRESH]


class TestTheMessageDelete:
    async def test_the_bot_is_tried_after_the_user_is_refused(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """Talk lets only the author delete, so a 403 as the user is the
        ordinary answer for a bot-authored message and the fallback is the
        product's whole point. Both attempts name the Talk ref.

        The bot leg reaches the double through the *construction* patch, not
        through `fake_talk`'s `async_runtime` one: `get_talk_client` builds its
        singleton with a lazy `from .talk import TalkClient`, so the class patch
        catches it on the way past. Established by removing the third patch and
        watching this stay green — which is why the pin for that patch is a
        direct one in `tests/test_support_talk_double.py` rather than this test.
        """
        _store(db_path, "live-at")
        fake_talk_web.bearer_rejections["live-at"] = 403

        await web_app_module._delete_from_talk("alice", room.talk_ref, "5150")

        assert [(c.method, c.token, c.bearer_token, c.status)
                for c in fake_talk_web.calls] == [
            ("delete_message", room.talk_ref, "live-at", 403),
            ("delete_message", room.talk_ref, None, None),
        ]

    async def test_the_user_delete_stands_when_it_works(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        _store(db_path, "live-at")

        await web_app_module._delete_from_talk("alice", room.talk_ref, "5150")

        assert [c.bearer_token for c in fake_talk_web.calls] == ["live-at"]

    async def test_the_user_client_is_closed_on_the_way_out(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """The user-scoped client is built per call, so this path owns its
        lifetime and nothing else can end it.

        `delete_message` opens an `httpx.AsyncClient` behind the client, and
        once `_delete_from_talk` returns no reference to it survives — so a
        construction not matched by an `aclose()` is a connection pool leaked on
        every web message delete in a Talk-bound room, silently, because the
        whole function is best-effort and reports nothing (ISSUE-403).

        Both halves are the assertion. `closes == 1` alone is equally true of a
        second client built and closed while the first was dropped, and
        `constructions == 1` alone says nothing about the leak.

        The construction count is 1 rather than 2 because `fake_talk` patches
        `async_runtime.get_talk_client` as well, so the bot leg is handed the
        instance and constructs nothing. Without that patch the real factory
        runs, builds through the patched class, and the number is 2 here for a
        reason that has nothing to do with this property.
        """
        _store(db_path, "live-at")

        await web_app_module._delete_from_talk("alice", room.talk_ref, "5150")

        assert fake_talk_web.calls, "nothing ran, so this proves nothing"
        assert len(fake_talk_web.constructions) == 1
        assert fake_talk_web.closes == 1

    async def test_the_pooled_bot_client_is_left_open(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """The complement, and why the count above is 1 rather than "one per
        client this path used".

        The bot leg comes from `async_runtime.get_talk_client`, a process-wide
        singleton whose lifetime belongs to the runtime's cleanup hook; closing
        it here would take every later Talk call in the process with it. So the
        `finally` covers the user client alone, and the fix for ISSUE-403 is
        wrong in the other direction if this number moves.

        The 403 is what drives both legs at once, which also makes this the
        case where the user client is closed on a path where its own call
        *failed* — the leak was on every outcome, not only the happy one.
        """
        _store(db_path, "live-at")
        fake_talk_web.bearer_rejections["live-at"] = 403

        await web_app_module._delete_from_talk("alice", room.talk_ref, "5150")

        assert [c.bearer_token for c in fake_talk_web.calls] == ["live-at", None]
        assert len(fake_talk_web.constructions) == 1
        assert fake_talk_web.closes == 1

    async def test_the_client_is_closed_when_the_call_raises_outright(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """The third of `_attempt`'s three exits, and the one the other two
        cases cannot reach.

        A 403 leaves through `except httpx.HTTPStatusError`; a success leaves
        through the `return` inside the `try`. An unbound ref raises
        `UnknownTalkRoom` from the double's own check, which lands in the bare
        `except Exception` — the arm that catches a connection failure, which is
        exactly the state a leaked pool is most likely to be in. Both legs take
        it, so the bot leg is asked and the user client is still closed once.
        """
        _store(db_path, "live-at")

        await web_app_module._delete_from_talk("alice", "no-such-room", "5150")

        assert [c.bearer_token for c in fake_talk_web.refusals] == ["live-at", None]
        assert len(fake_talk_web.constructions) == 1
        assert fake_talk_web.closes == 1

    async def test_a_non_numeric_id_reaches_no_client(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """Paired in one test, because `calls == []` alone is equally true of a
        feature switched off, a missing token or an unconfigured Nextcloud —
        any fixture regression would leave this green while its siblings go
        red. The second half is the control: same room, same credential, an id
        that parses."""
        _store(db_path, "live-at")

        await web_app_module._delete_from_talk("alice", room.talk_ref, "not-an-id")
        assert fake_talk_web.calls == []

        await web_app_module._delete_from_talk("alice", room.talk_ref, "5150")
        assert [c.method for c in fake_talk_web.calls] == ["delete_message"]


class TestTheReadPull:
    async def test_it_advances_the_cursor_from_the_talk_ref(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """`list_conversations` carries no token, so the misroute risk is on the
        *other* side: the pull matches Talk's answer against each room's
        binding, and matching on the canonical token would advance nothing."""
        web_app_module._config.web.chat.talk_read_sync_interval = 60
        _store(db_path, "live-at")
        fake_talk_web.conversations = [
            {"token": room.talk_ref, "unreadMessages": 0},
        ]
        with db.get_db(db_path) as conn:
            db.add_room_member(conn, room.canonical, "alice")
            msg_id = db.add_message(
                conn, room.canonical, role="assistant", body="x",
                origin_surface="talk", task_id=None,
                external_ids={"talk": "99"},
            )

        await web_app_module._pull_talk_read_state("alice")

        assert [c.method for c in fake_talk_web.calls] == ["list_conversations"]
        with db.get_db(db_path) as conn:
            assert db.get_room_read_state(
                conn, room.canonical, "web", "alice",
            ) == msg_id

    async def test_it_advances_nothing_for_a_conversation_it_is_not_bound_to(
        self, fake_talk_web, web_app_module, db_path, room,
    ):
        """The negative half, with the canonical token as the impostor: Talk
        answers about a token that is not this room's binding, so the cursor
        must stay put. Without it the test above passes on a lookup that
        matched anything at all."""
        web_app_module._config.web.chat.talk_read_sync_interval = 60
        _store(db_path, "live-at")
        fake_talk_web.conversations = [
            {"token": room.canonical, "unreadMessages": 0},
        ]
        with db.get_db(db_path) as conn:
            db.add_room_member(conn, room.canonical, "alice")
            db.add_message(
                conn, room.canonical, role="assistant", body="x",
                origin_surface="talk", task_id=None,
                external_ids={"talk": "99"},
            )

        await web_app_module._pull_talk_read_state("alice")

        with db.get_db(db_path) as conn:
            assert db.get_room_read_state(
                conn, room.canonical, "web", "alice",
            ) == 0
