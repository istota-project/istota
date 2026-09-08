"""ISSUE-477. The room a bare `web` or bare `talk` destination lands in is a
setting, not a guess.

`user_profiles.default_room` holds one canonical room token, and the two
resolvers that already exist read it through one helper: `db.default_web_room`
on web, `notifications.resolve_conversation_token` on Talk. Everything that
resolves a bare surface goes through one of those two, so nothing here reaches
past them into a per-caller rule.
"""

from istota import db, user_profiles
from istota.config import Config, UserConfig


def _config(tmp_path) -> Config:
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config()
    config.db_path = db_path
    return config


def _own_room(conn, user_id, token, name):
    """A private registry room the user is the sole member of, with a web view."""
    db.register_room(conn, token, user_id, origin="web", name=name)
    db.add_room_member(conn, token, user_id)
    return db.ensure_web_chat_handle(conn, user_id, token, name)


def _promoted_room(conn, user_id, token, name, talk_ref):
    """A room with both a web view and a Talk binding."""
    handle = _own_room(conn, user_id, token, name)
    db.add_room_binding(conn, token, "talk", talk_ref)
    return handle


def _set_default_room(config, user_id, token):
    user_profiles.ensure_profile(config.db_path, user_id, display_name=user_id)
    return user_profiles.update_profile(config.db_path, user_id, default_room=token)


class TestTheConfiguredRoomIsHonouredByEveryBareWebCaller:
    """The rung lives in `db.default_web_room`, so the six bare-`web` callers
    inherit it rather than each resolving their own way. Parametrised over the
    callers on purpose: a seventh added later that resolves for itself fails
    here instead of quietly disagreeing."""

    def _setup(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-pinned", "notes")
        # The heuristic would answer `general` — it is the oldest handle.
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice").token == general.token
        _set_default_room(config, "alice", "room-pinned")
        return config, general

    def test_the_lookup_itself(self, tmp_path):
        config, _ = self._setup(tmp_path)
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice").token == "room-pinned"

    def test_the_provisioning_entry_point(self, tmp_path):
        config, _ = self._setup(tmp_path)
        with db.get_db(config.db_path) as conn:
            assert db.ensure_default_web_chat_room(conn, "alice").token == "room-pinned"

    def test_default_web_room_token(self, tmp_path):
        from istota.transport.web import default_web_room_token

        config, _ = self._setup(tmp_path)
        assert default_web_room_token(config, "alice") == "room-pinned"

    def test_the_bare_web_alert_route(self, tmp_path):
        from istota.notifications import send_notification

        config, general = self._setup(tmp_path)
        config.users["alice"] = UserConfig(routing={"alert": "web"})
        assert send_notification(config, "alice", "boom", purpose="alert") is True
        with db.get_db(config.db_path) as conn:
            assert [m.body for m in db.list_system_messages(conn, "room-pinned")] == ["boom"]
            assert db.list_system_messages(conn, general.token) == []

    def test_the_bare_web_log_route(self, tmp_path):
        from istota.notifications import effective_log_destinations

        config, _ = self._setup(tmp_path)
        config.users["alice"] = UserConfig(routing={"log": "web"})
        dests = effective_log_destinations(config, "alice")
        assert [(d.surface, d.channel) for d in dests] == [("web", "room-pinned")]

    def test_web_transport_resolve_target(self, tmp_path):
        from istota.transport.web import WebTransport

        config, _ = self._setup(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="scheduled",
            user_id="alice", prompt="hi", conversation_token=None,
        )
        assert WebTransport(config).resolve_target(task) == "room-pinned"

    def test_room_for_destination(self, tmp_path):
        from istota.transport.routing import Destination, _room_for_destination

        config, _ = self._setup(tmp_path)
        with db.get_db(config.db_path) as conn:
            room = _room_for_destination(
                conn, config, "alice", Destination("web", None),
            )
        assert room == "room-pinned"


class TestTheConfiguredRoomAnswersPerSurface:
    """One token, two surfaces. It answers for the surface it actually has a
    view on, and for no other — a web-only room is not a Talk conversation."""

    def test_a_web_only_room_is_ignored_by_a_bare_talk(self, tmp_path):
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _own_room(conn, "alice", "room-web-only", "notes")
        _set_default_room(config, "alice", "room-web-only")
        config.users["alice"] = UserConfig()
        # No Talk binding, so the Talk ladder falls past it and finds nothing.
        assert resolve_conversation_token(config, "alice") is None

    def test_a_promoted_room_answers_for_both(self, tmp_path):
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "shared notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig()

        # Talk gets the binding's ref (the Nextcloud conversation id), not the
        # canonical token — that is what a `talk:` leaf carries.
        assert resolve_conversation_token(config, "alice") == "nc12345"
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice").token == "room-both"

    def test_the_helper_returns_the_surface_ref(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        with db.get_db(config.db_path) as conn:
            assert db.configured_delivery_room(conn, "alice", "web") == "room-both"
            assert db.configured_delivery_room(conn, "alice", "talk") == "nc12345"

    def test_unset_is_none_on_both_surfaces(self, tmp_path):
        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            assert db.configured_delivery_room(conn, "alice", "web") is None
            assert db.configured_delivery_room(conn, "alice", "talk") is None


class TestTheTalkRungSitsBelowTheProvisionedChannels:
    """Talk's ladder has provisioned rungs above its guess, so the setting
    replaces the guess and not the provisioning. Putting it higher would move
    every existing Talk user's alerts out of their alerts channel on upgrade."""

    def test_alerts_channel_still_wins(self, tmp_path):
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig(alerts_channel="nc-alerts")
        assert resolve_conversation_token(config, "alice") == "nc-alerts"

    def test_an_explicit_talk_route_still_wins(self, tmp_path):
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig(routing={"alert": "talk:nc-explicit"})
        assert resolve_conversation_token(config, "alice") == "nc-explicit"

    def test_it_beats_the_briefing_token(self, tmp_path):
        from istota.config import BriefingConfig
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig(
            briefings=[BriefingConfig(name="morning", cron="0 7 * * *", conversation_token="nc-brief")],
        )
        assert resolve_conversation_token(config, "alice") == "nc12345"


class TestAConfiguredRoomThatIsGoneFallsBackToTheHeuristic:
    """The user pointed at a room. Inventing a different one under the same
    setting is worse than falling back visibly, so nothing is resurrected or
    recreated — the heuristic simply answers again."""

    def test_a_deleted_room_falls_back(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-pinned", "notes")
        _set_default_room(config, "alice", "room-pinned")
        with db.get_db(config.db_path) as conn:
            handle = [r for r in db.list_web_chat_rooms(conn, "alice")
                      if r.token == "room-pinned"][0]
            assert db.delete_web_chat_room(conn, handle.id, "alice") is True
            assert db.default_web_room(conn, "alice").token == general.token
            # Nothing was recreated under the configured token.
            assert db.get_room(conn, "room-pinned") is None

    def test_an_archived_room_falls_back(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-pinned", "notes")
        _set_default_room(config, "alice", "room-pinned")
        with db.get_db(config.db_path) as conn:
            db.set_room_archived(conn, "room-pinned", True)
            assert db.default_web_room(conn, "alice").token == general.token

    def test_a_room_the_user_is_no_longer_in_falls_back(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            db.register_room(conn, "room-theirs", "bob", origin="talk", name="team")
            db.add_room_member(conn, "room-theirs", "alice")
            db.ensure_web_chat_handle(conn, "alice", "room-theirs", "team")
        _set_default_room(config, "alice", "room-theirs")
        with db.get_db(config.db_path) as conn:
            db.remove_room_member(conn, "room-theirs", "alice")
            assert db.default_web_room(conn, "alice").token == general.token

    def test_an_archived_room_falls_back_on_talk_too(self, tmp_path):
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig(
            briefings=[],
        )
        with db.get_db(config.db_path) as conn:
            db.set_room_archived(conn, "room-both", True)
        assert resolve_conversation_token(config, "alice") is None

    def test_a_configured_room_the_user_hid_is_still_delivered_to(self, tmp_path):
        """Hiding a room you pinned is contradictory, and the two answers
        available are both bad: ignoring the pin silently, or delivering
        somewhere the user cannot see. The provisioning entry point takes the
        third — put the room back, exactly as it un-hides the heuristic's
        fallback today."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            pinned = _own_room(conn, "alice", "room-pinned", "notes")
            db.update_web_chat_room(conn, pinned.id, archived=True)
        _set_default_room(config, "alice", "room-pinned")
        with db.get_db(config.db_path) as conn:
            handle = db.ensure_default_web_chat_room(conn, "alice")
        assert handle.token == "room-pinned"
        assert handle.archived is False


class TestTheBackfill:
    """Every existing profile is backfilled with whatever the heuristic answers
    for it today, so nothing moves on upgrade and the room a user already
    receives alerts in becomes the room they are shown. Leaving it empty would
    be quieter now and worse later: the first archive of a `general` would move
    delivery silently, which is the ISSUE-473 complaint arriving again through
    the setting meant to end it."""

    def test_it_pins_the_room_the_heuristic_answers_today(self, tmp_path):
        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            conn.execute("UPDATE user_profiles SET default_room = ''")
            conn.execute("DELETE FROM _migration_state WHERE name = 'default_room_v1'")

        db.init_db(config.db_path)

        profile = user_profiles.get_profile(config.db_path, "alice")
        assert profile.default_room == general.token

    def test_delivery_does_not_move_when_general_is_later_archived(self, tmp_path):
        """The regression the backfill exists for: without it, archiving
        `general` silently hands the default to whatever was minted next."""
        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-later", "later")
            conn.execute("UPDATE user_profiles SET default_room = ''")
            conn.execute("DELETE FROM _migration_state WHERE name = 'default_room_v1'")

        db.init_db(config.db_path)

        with db.get_db(config.db_path) as conn:
            db.update_web_chat_room(
                conn, db.list_web_chat_rooms(conn, "alice")[0].id, archived=True,
            )
            # The pin holds: delivery stays on `general` rather than sliding to
            # the next handle in creation order, which is what it did before the
            # setting existed. The lookup answers None because it may not write;
            # the provisioning entry point un-hides the room the user pinned.
            assert db.default_web_room(conn, "alice") is None
            handle = db.ensure_default_web_chat_room(conn, "alice")
        assert handle.token == general.token
        assert handle.token != "room-later"

    def test_an_account_with_no_qualifying_room_stays_empty(self, tmp_path):
        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM _migration_state WHERE name = 'default_room_v1'")

        db.init_db(config.db_path)

        profile = user_profiles.get_profile(config.db_path, "alice")
        assert profile.default_room == ""
        # And the provisioning path is untouched.
        with db.get_db(config.db_path) as conn:
            assert db.ensure_default_web_chat_room(conn, "alice").name == "general"

    def test_it_runs_once(self, tmp_path):
        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            conn.execute("UPDATE user_profiles SET default_room = ''")
            conn.execute("DELETE FROM _migration_state WHERE name = 'default_room_v1'")

        db.init_db(config.db_path)
        user_profiles.update_profile(config.db_path, "alice", default_room="")
        db.init_db(config.db_path)

        # A user who deliberately cleared the setting keeps it cleared.
        assert user_profiles.get_profile(config.db_path, "alice").default_room == ""


class TestTheProfileField:
    def test_it_round_trips(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        assert user_profiles.get_profile(db_path, "alice").default_room == ""
        user_profiles.update_profile(db_path, "alice", default_room="room-1")
        assert user_profiles.get_profile(db_path, "alice").default_room == "room-1"


class TestBothSurfacesNameTheSameRoom:
    """The contract a surface-agnostic setting has to keep. The first two cases
    are the review's find: `default_web_room` can only return a room that
    already has a usable web view, so consulting it before the raw column let
    the heuristic answer over a pinned room — web delivering to `general` while
    Talk delivered to the pinned room."""

    def test_a_talk_only_room_is_honoured_on_web_too(self, tmp_path):
        from istota.notifications import resolve_conversation_token
        from istota.transport.web import default_web_room_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")  # a `general` exists
            db.register_room(conn, "talk-room", "alice", origin="talk", name="notes")
            db.add_room_member(conn, "talk-room", "alice")
            db.add_room_binding(conn, "talk-room", "talk", "nc999")
        _set_default_room(config, "alice", "talk-room")
        config.users["alice"] = UserConfig()

        # The provisioning entry point mints the missing web view rather than
        # letting the heuristic win.
        assert default_web_room_token(config, "alice") == "talk-room"
        assert resolve_conversation_token(config, "alice") == "nc999"

    def test_the_non_provisioning_lookup_answers_none_rather_than_guessing(self, tmp_path):
        # It may not write, so it cannot make the view — but falling through to
        # the heuristic would deliver into a room the user did not choose.
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            db.register_room(conn, "talk-room", "alice", origin="talk", name="notes")
            db.add_room_member(conn, "talk-room", "alice")
        _set_default_room(config, "alice", "talk-room")
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice") is None

    def test_a_hidden_pinned_room_is_never_named_by_the_non_provisioning_path(self, tmp_path):
        """`_room_for_destination` "answers None where delivery would invent or
        resurface a room", and a hidden handle is a room the user cannot see."""
        from istota.transport.routing import Destination, _room_for_destination

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            pinned = _own_room(conn, "alice", "room-pinned", "notes")
            db.update_web_chat_room(conn, pinned.id, archived=True)
        _set_default_room(config, "alice", "room-pinned")
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice") is None
            assert _room_for_destination(
                conn, config, "alice", Destination("web", None),
            ) is None

    def test_a_dismissed_pinned_room_is_recovered_only_by_the_provisioning_path(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-pinned", "notes")
            db.dismiss_room(conn, "room-pinned", "alice")
        _set_default_room(config, "alice", "room-pinned")
        with db.get_db(config.db_path) as conn:
            assert db.default_web_room(conn, "alice") is None
            handle = db.ensure_default_web_chat_room(conn, "alice")
            assert handle.token == "room-pinned"
            assert db.is_room_dismissed(conn, "room-pinned", "alice") is False

    def test_the_minted_handle_carries_the_registry_name(self, tmp_path):
        # Not the literal "room": `room_display_name` is the one rule for a
        # room's name since ISSUE-474.
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "talk-room", "alice", origin="talk", name="notes")
            db.add_room_member(conn, "talk-room", "alice")
        _set_default_room(config, "alice", "talk-room")
        with db.get_db(config.db_path) as conn:
            assert db.ensure_default_web_chat_room(conn, "alice").name == "notes"


class TestTheResolverTakesTheCallersConnection:
    """`transport.routing._room_for_destination` holds a connection and its own
    comment forbids taking a second one on the same database. The rung reads, so
    a nested connection would not deadlock under WAL — but it would be opened at
    the 30s busy timeout on a path reached per destination per message."""

    def test_it_uses_a_connection_that_is_passed(self, tmp_path):
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            # No second connection is opened: the one held is the one read.
            assert resolve_conversation_token(config, "alice", conn) == "nc12345"

    def test_the_talk_branch_of_room_for_destination_resolves_the_pin(self, tmp_path):
        from istota.transport.routing import Destination, _room_for_destination

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig()
        with db.get_db(config.db_path) as conn:
            assert _room_for_destination(
                conn, config, "alice", Destination("talk", None),
            ) == "room-both"


class TestTheBackfillLeavesTalkAlone:
    """The backfill computes the *web* guess. Pinning a Talk-bound room would
    insert it into the Talk ladder above the briefing token and move that user's
    alerts on upgrade — which is what this migration exists to prevent."""

    def _profile_with_room(self, config, *, talk_bound):
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "solo-1", "alice", origin="talk", name="notes")
            db.add_room_member(conn, "solo-1", "alice")
            if talk_bound:
                db.add_room_binding(conn, "solo-1", "talk", "nc-solo")
            db.ensure_web_chat_handle(conn, "alice", "solo-1", "notes")
            conn.execute("UPDATE user_profiles SET default_room = ''")
            conn.execute("DELETE FROM _migration_state WHERE name = 'default_room_v1'")

    def test_a_talk_bound_room_is_not_pinned(self, tmp_path):
        from istota.config import BriefingConfig
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        self._profile_with_room(config, talk_bound=True)
        config.users["alice"] = UserConfig(
            briefings=[BriefingConfig(
                name="morning", cron="0 7 * * *", conversation_token="nc-brief",
            )],
        )
        before = resolve_conversation_token(config, "alice")

        db.init_db(config.db_path)

        assert user_profiles.get_profile(config.db_path, "alice").default_room == ""
        assert resolve_conversation_token(config, "alice") == before == "nc-brief"

    def test_a_web_only_room_is_still_pinned(self, tmp_path):
        config = _config(tmp_path)
        self._profile_with_room(config, talk_bound=False)

        db.init_db(config.db_path)

        assert user_profiles.get_profile(
            config.db_path, "alice",
        ).default_room == "solo-1"

    def test_a_missing_unrelated_table_does_not_mark_the_migration_done(self, tmp_path):
        """Scoped to `user_profiles`, whose absence means a fresh install. Any
        other missing table is a half-restored database, and marking the
        migration done there would leave every profile '' forever."""
        config = _config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
        # Called directly rather than through `init_db`, which recreates the
        # table before the backfill would see it missing.
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            conn.execute("UPDATE user_profiles SET default_room = ''")
            conn.execute("DELETE FROM _migration_state WHERE name = 'default_room_v1'")
            conn.execute("ALTER TABLE room_dismissals RENAME TO room_dismissals_gone")

            db._migrate_default_room(conn)

            marked = conn.execute(
                "SELECT 1 FROM _migration_state WHERE name = 'default_room_v1'"
            ).fetchone()
            assert marked is None

            # And it succeeds once the table is back, rather than being wedged.
            conn.execute("ALTER TABLE room_dismissals_gone RENAME TO room_dismissals")
            db._migrate_default_room(conn)
            assert conn.execute(
                "SELECT 1 FROM _migration_state WHERE name = 'default_room_v1'"
            ).fetchone() is not None
