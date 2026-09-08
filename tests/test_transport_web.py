"""Tests for the web chat delivery surface (WebTransport) — ISSUE-121.

Web chat is now a user-routable delivery surface: alerts, the verbose execution
log, and notifications routed to ``web`` post an unsolicited message into the
user's room. WebTransport.deliver writes a ``web_chat_messages`` row; the
interactive task path is unchanged (still a stream over task_events).
"""

from __future__ import annotations

from istota import db
from istota.async_runtime import run_coro
from istota.config import Config
from istota.transport._types import DeliveryOptions
from istota.transport.web import WebTransport, default_web_room_token


def _config(tmp_path) -> Config:
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config()
    config.db_path = db_path
    return config


def _task(user_id: str, token: str | None = None) -> db.Task:
    return db.Task(
        id=1, status="completed", source_type="scheduled",
        user_id=user_id, prompt="hi", conversation_token=token,
    )


class TestCapabilities:
    def test_user_routable_and_stream(self):
        caps = WebTransport(Config()).capabilities
        assert caps.user_routable is True
        assert caps.surface_class == "stream"
        # Non-edit: the log path delivers one final summary, not a tool stream.
        assert caps.supports_edit is False
        assert caps.supports_progress_ack is False

    def test_construction_does_no_io(self):
        # make_registry builds this with no DB path set; must not touch the DB.
        WebTransport(Config())


class TestDefaultRoomToken:
    def test_provisions_and_returns_general(self, tmp_path):
        config = _config(tmp_path)
        token = default_web_room_token(config, "alice")
        assert token and token.startswith("web-alice-")
        with db.get_db(config.db_path) as conn:
            rooms = db.list_web_chat_rooms(conn, "alice")
        assert [r.name for r in rooms] == ["general"]

    def test_idempotent(self, tmp_path):
        config = _config(tmp_path)
        assert default_web_room_token(config, "alice") == default_web_room_token(
            config, "alice"
        )

    def test_no_db_path_returns_none(self):
        config = Config()
        config.db_path = None
        assert default_web_room_token(config, "alice") is None


class TestDeliver:
    def test_appends_message_to_room(self, tmp_path):
        config = _config(tmp_path)
        token = default_web_room_token(config, "alice")
        transport = WebTransport(config)

        msg_id = run_coro(transport.deliver(token, "heads up"))
        assert isinstance(msg_id, int)

        # Notifications now land in the canonical messages store (role=system).
        with db.get_db(config.db_path) as conn:
            msgs = db.list_system_messages(conn, token)
            room = db.get_room(conn, token)
        assert len(msgs) == 1
        assert msgs[0].body == "heads up"
        assert msgs[0].role == "system"
        assert room.user_id == "alice"

    def test_carries_title_from_options(self, tmp_path):
        config = _config(tmp_path)
        token = default_web_room_token(config, "alice")
        run_coro(WebTransport(config).deliver(
            token, "body", options=DeliveryOptions(title="Alert"),
        ))
        with db.get_db(config.db_path) as conn:
            msgs = db.list_system_messages(conn, token)
        assert msgs[0].title == "Alert"

    def test_owner_derived_from_room_not_caller(self, tmp_path):
        # A token belonging to bob is the source of truth: the message lands in
        # bob's room regardless of a different user's task being passed.
        config = _config(tmp_path)
        bob_token = default_web_room_token(config, "bob")
        run_coro(WebTransport(config).deliver(
            bob_token, "x", task=_task("alice"),
        ))
        with db.get_db(config.db_path) as conn:
            msgs = db.list_system_messages(conn, bob_token)
            assert len(msgs) == 1
            assert db.get_room(conn, bob_token).user_id == "bob"

    def test_unknown_token_with_no_task_drops(self, tmp_path):
        config = _config(tmp_path)
        # No room exists for this token and no task → no user to attribute to.
        assert run_coro(WebTransport(config).deliver("web-ghost-000", "x")) is None
        with db.get_db(config.db_path) as conn:
            assert db.list_system_messages(conn, "web-ghost-000") == []

    def test_missing_room_drops_even_with_task(self, tmp_path):
        config = _config(tmp_path)
        # Room tokens are minted at room creation, so a token with no room row is
        # a *deleted* room (never a pending one). Delivery must drop + WARN rather
        # than insert an orphan row that can never render — even with a task
        # present (e.g. an email reply routed to a since-deleted origin room).
        msg_id = run_coro(WebTransport(config).deliver(
            "web-gone-000", "x", task=_task("carol"),
        ))
        assert msg_id is None
        with db.get_db(config.db_path) as conn:
            assert db.list_system_messages(conn, "web-gone-000") == []

    def test_empty_target_with_no_task_returns_none(self, tmp_path):
        config = _config(tmp_path)
        assert run_coro(WebTransport(config).deliver("", "x")) is None


class TestInteractiveResultNotPushed:
    """The critical invariant: a source_type="web" task's own result still
    streams over task_events — registering WebTransport must not make
    resolve_delivery_plan push it via deliver()."""

    def test_web_task_result_resolves_to_stream_only(self, tmp_path):
        from istota.transport import make_registry
        from istota.transport.routing import plan_has_surface, resolve_delivery_plan
        config = _config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target="web",
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        # Stream, not push — nothing for the scheduler to deliver via WebTransport.
        assert all(d.kind == "stream" for d in plan)


class TestResolveTarget:
    def test_resolves_default_room(self, tmp_path):
        config = _config(tmp_path)
        token = WebTransport(config).resolve_target(_task("alice"))
        assert token and token.startswith("web-alice-")


class TestEdit:
    def test_edit_is_noop(self, tmp_path):
        config = _config(tmp_path)
        assert run_coro(WebTransport(config).edit("web-x", 1, "y")) is None


class TestTheDefaultRoomIsOneTheUserIsAloneIn:
    """ISSUE-473. `ensure_default_web_chat_room` documents two rules for the room
    a bare `web` route lands in — never a room another person reads, never a
    machine-owned log/alerts channel — and applied them only on the branch that
    runs when the user has no handles at all. Every other time it returned the
    oldest handle unfiltered: the `general` room until the user archives it, and
    whatever is next in creation order from then on.
    """

    def _shared_room(self, conn, user_id, token, name="team", other="bob"):
        db.register_room(conn, token, other, origin="talk", name=name)
        db.add_room_member(conn, token, user_id)
        return db.ensure_web_chat_handle(conn, user_id, token, name)

    def test_a_room_someone_else_reads_is_never_the_default(self, tmp_path):
        from istota.config import UserConfig
        from istota.notifications import send_notification

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            self._shared_room(conn, "alice", "shared-1")
            # The user hides the room they never use, and the shared one becomes
            # the oldest handle they have.
            db.update_web_chat_room(conn, general.id, archived=True)
            assert db.default_web_room(conn, "alice") is None

        # A private alert must not be posted in front of bob. Falling back to the
        # registry scan brings the user's own room back rather than borrowing his.
        config.users["alice"] = UserConfig(routing={"alert": "web"})
        assert send_notification(config, "alice", "boom", purpose="alert") is True
        with db.get_db(config.db_path) as conn:
            assert db.list_system_messages(conn, "shared-1") == []
            bodies = [m.body for m in db.list_system_messages(conn, general.token)]
        assert bodies == ["boom"]

    def test_a_log_or_alerts_channel_is_never_the_default(self, tmp_path):
        from istota import user_profiles

        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            for token, name in (("chan-log", "logs"), ("chan-alerts", "alerts")):
                db.register_room(conn, token, "alice", origin="talk", name=name)
                db.ensure_web_chat_handle(conn, "alice", token, name)
            conn.execute(
                "UPDATE user_profiles SET log_channel = ?, alerts_channel = ? "
                "WHERE user_id = ?",
                ("chan-log", "chan-alerts", "alice"),
            )
            db.update_web_chat_room(conn, general.id, archived=True)
            assert db.default_web_room(conn, "alice") is None

    def test_a_room_the_user_hid_is_never_the_default(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            db.register_room(conn, "solo-1", "alice", origin="talk", name="notes")
            db.ensure_web_chat_handle(conn, "alice", "solo-1", "notes")
            db.dismiss_room(conn, "solo-1", "alice")
            db.update_web_chat_room(conn, general.id, archived=True)
            assert db.default_web_room(conn, "alice") is None

    def test_a_handle_whose_room_is_gone_is_never_the_default(self, tmp_path):
        # A handle with no `rooms` row is a deleted room, and `deliver` drops into
        # it with a warning. Naming it as the default guarantees that drop.
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            db.ensure_web_chat_handle(conn, "alice", "web-alice-ghost", "ghost")
            db.update_web_chat_room(conn, general.id, archived=True)
            assert db.default_web_room(conn, "alice") is None

    def test_the_users_own_room_is_still_the_default(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            self._shared_room(conn, "alice", "shared-1")
            assert db.default_web_room(conn, "alice").token == general.token

    def test_the_lookup_creates_nothing(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice") is None
            assert db.list_web_chat_rooms(conn, "alice") == []

    def test_a_room_the_registry_archived_is_never_the_default(self, tmp_path):
        # `archive_orphaned_talk_rooms` sets `rooms.archived` when the bot leaves
        # a Nextcloud conversation and leaves the per-user handle alone, so
        # without this the default can be a room that is invisible in web and
        # dead on Talk — which `deliver` writes into perfectly happily.
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            db.register_room(conn, "orphan-1", "alice", origin="talk", name="old")
            db.ensure_web_chat_handle(conn, "alice", "orphan-1", "old")
            db.set_room_archived(conn, "orphan-1", True)
            db.update_web_chat_room(conn, general.id, archived=True)
            assert db.default_web_room(conn, "alice") is None

    def test_a_room_whose_one_member_is_somebody_else_is_never_the_default(
        self, tmp_path,
    ):
        # A handle outlives membership, so counting members admits a room the
        # user is not in. The rule is "nobody but me", not "at most one".
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            db.register_room(conn, "bobs-1", "bob", origin="talk", name="bob")
            db.ensure_web_chat_handle(conn, "alice", "bobs-1", "bob")
            db.update_web_chat_room(conn, general.id, archived=True)
            assert db.default_web_room(conn, "alice") is None

    def test_the_fallback_takes_the_oldest_room_not_the_liveliest(self, tmp_path):
        # The fallback mints the handle a bare `web` route then lands on for
        # good, and it reads `list_member_rooms`, which is activity-ordered — so
        # unsorted it makes a permanent delivery target out of whichever room the
        # user last spoke in.
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            for token, name in (("solo-1", "first"), ("solo-2", "second")):
                db.register_room(conn, token, "alice", origin="talk", name=name)
            db.add_message(
                conn, "solo-2", role="system", body="hello", origin_surface="web",
            )
            assert [r.token for r in db._default_room_candidates(conn, "alice")] == [
                "solo-1", "solo-2",
            ]
            assert db.ensure_default_web_chat_room(conn, "alice").token == "solo-1"

    def test_a_hidden_room_comes_back_rather_than_a_second_one_appearing(
        self, tmp_path,
    ):
        # The lookup skips a hidden room; the fallback may still un-hide it. An
        # alert has to go somewhere, and the user's own room — visible again — is
        # a better answer than a duplicate `general` or a dropped alert.
        from istota.config import UserConfig
        from istota.notifications import send_notification

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            db.update_web_chat_room(conn, general.id, archived=True)

        config.users["alice"] = UserConfig(routing={"alert": "web"})
        assert send_notification(config, "alice", "boom", purpose="alert") is True
        with db.get_db(config.db_path) as conn:
            rooms = db.list_web_chat_rooms(conn, "alice")
            bodies = [m.body for m in db.list_system_messages(conn, general.token)]
        assert [r.token for r in rooms] == [general.token]
        assert bodies == ["boom"]

    def test_the_transcript_resolver_applies_the_same_filter(self, tmp_path):
        # `_room_for_destination` must not provision, so it kept a second copy of
        # the "oldest handle" rule and drifted from this one. It agrees on which
        # rooms qualify and, having no fallback, answers None where the delivery
        # resolver would invent or resurface a room.
        from istota.transport.routing import Destination, _room_for_destination

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            self._shared_room(conn, "alice", "shared-1")
            assert _room_for_destination(
                conn, config, "alice", Destination("web", None),
            ) == general.token
            db.update_web_chat_room(conn, general.id, archived=True)
            assert _room_for_destination(
                conn, config, "alice", Destination("web", None),
            ) is None
