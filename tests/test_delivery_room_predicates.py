"""ISSUE-479. The four predicates that answer "can this room be delivered to"
share one core, and the arms they disagree about stay per-caller.

`db._live_room` is the shared core: the registry row exists and is not archived.
Everything else belongs to one caller — dismissal, because Talk has none and the
pin's recovery lives in the writer; membership, because the three callers that
ask want three different answers.

These are mostly **controls**. The fold is behaviour-preserving, so their job is
to fail if a later pass folds an arm into the core that does not belong there.
Each says which fold it is guarding against.
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
    db.register_room(conn, token, user_id, origin="web", name=name)
    db.add_room_member(conn, token, user_id)
    return db.ensure_web_chat_handle(conn, user_id, token, name)


def _promoted_room(conn, user_id, token, name, talk_ref):
    handle = _own_room(conn, user_id, token, name)
    db.add_room_binding(conn, token, "talk", talk_ref)
    return handle


def _set_default_room(config, user_id, token):
    user_profiles.ensure_profile(config.db_path, user_id, display_name=user_id)
    return user_profiles.update_profile(config.db_path, user_id, default_room=token)


class TestTheSharedCoreIsExistsAndNotArchived:
    """The pair every one of the four wants and none of them disagrees about.
    Asserted through the three public predicates rather than through
    `_live_room` alone, since a core nobody reaches is not shared."""

    def test_an_archived_room_is_refused_by_all_three(self, tmp_path):
        config = _config(tmp_path)
        _set_default_room(config, "alice", "room-x")
        with db.get_db(config.db_path) as conn:
            _own_room(conn, "alice", "room-x", "notes")
            db.set_room_archived(conn, "room-x", True)
            channels = db.channel_room_tokens(conn, "alice")

            assert db.visible_room(conn, "alice", "room-x") is None
            assert db.configured_default_room(conn, "alice") is None
            assert db._usable_as_delivery_default(
                conn, "alice", "room-x", channels,
            ) is False

    def test_a_room_that_does_not_exist_is_refused_by_all_three(self, tmp_path):
        config = _config(tmp_path)
        _set_default_room(config, "alice", "room-gone")
        with db.get_db(config.db_path) as conn:
            channels = db.channel_room_tokens(conn, "alice")

            assert db.visible_room(conn, "alice", "room-gone") is None
            assert db.configured_default_room(conn, "alice") is None
            assert db._usable_as_delivery_default(
                conn, "alice", "room-gone", channels,
            ) is False

    def test_a_live_room_passes_the_core_and_carries_its_row(self, tmp_path):
        # The core returns the `Room` rather than a bool for the reason
        # `visible_room` does: the picker takes the name off the same lookup.
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _own_room(conn, "alice", "room-x", "notes")
            room = db._live_room(conn, "room-x")
            assert room is not None
            assert room.name == "notes"


class TestTheDismissalArmStaysOutOfTheCore:
    """The decision ISSUE-479 was filed to make. `visible_room` counts a hide,
    the configured pin does not — it is asked one level down, per surface, and
    undone by the only writer in the group. Folding it into the core changes
    behaviour twice over, and these are the two tests that catch it."""

    def test_a_pinned_room_the_user_hid_still_answers_a_bare_talk(self, tmp_path):
        """Talk has no dismissal of its own: a hide is a fact about the web
        sidebar. A `visible_room`-based core would drop the pin here and send
        the alert to the briefing token or the auto-DM instead."""
        from istota.notifications import resolve_conversation_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _promoted_room(conn, "alice", "room-both", "notes", "nc12345")
            db.dismiss_room(conn, "room-both", "alice")
        _set_default_room(config, "alice", "room-both")
        config.users["alice"] = UserConfig()

        assert resolve_conversation_token(config, "alice") == "nc12345"
        with db.get_db(config.db_path) as conn:
            assert db.configured_delivery_room(conn, "alice", "talk") == "nc12345"

    def test_the_web_arm_still_declines_a_hidden_pin_for_the_writer(self, tmp_path):
        """The other half of the same split. Web *does* count the hide, one
        level down, because the recovery is a write and neither reader may take
        one. A core that answered `None` for the pin outright would leave
        `ensure_default_web_chat_room` nothing to recover from."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-pinned", "notes")
            db.dismiss_room(conn, "room-pinned", "alice")
        _set_default_room(config, "alice", "room-pinned")

        with db.get_db(config.db_path) as conn:
            # The two public predicates must **disagree** about this room, and
            # that disagreement is the fold's whole invariant: the picker's
            # question counts the hide, the pin's does not. A dismissal folded
            # into the shared core collapses them onto one answer, which no
            # signature change is needed to do and so nothing else would catch.
            assert db.visible_room(conn, "alice", "room-pinned") is None
            # The pin survives the core and the raw read; only the surface-aware
            # wrapper declines it.
            assert db.configured_default_room(conn, "alice") == "room-pinned"
            assert db.configured_delivery_room(conn, "alice", "web") is None
            assert db.default_web_room(conn, "alice") is None
            # And the writer recovers it rather than falling back to `general`.
            assert db.ensure_default_web_chat_room(conn, "alice").token == "room-pinned"


class TestTheHeuristicsOwnArmsDoNotLeakIntoThePin:
    """`_usable_as_delivery_default`'s other two exclusions keep a *guess* out of
    somewhere embarrassing. A pin is not a guess, so a shared core must not carry
    them — the picker offers both classes marked, and choosing one is deliberate.
    """

    def test_a_pinned_room_somebody_else_reads_is_honoured(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            general = db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-shared", "team")
            db.add_room_member(conn, "room-shared", "bob")
        _set_default_room(config, "alice", "room-shared")

        with db.get_db(config.db_path) as conn:
            channels = db.channel_room_tokens(conn, "alice")
            # The heuristic refuses it, and the pin overrides the heuristic.
            assert db._usable_as_delivery_default(
                conn, "alice", "room-shared", channels,
            ) is False
            assert db.configured_default_room(conn, "alice") == "room-shared"
            assert db.default_web_room(conn, "alice").token == "room-shared"
            assert general.token != "room-shared"

    def test_a_pinned_machine_owned_channel_room_is_honoured(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.ensure_default_web_chat_room(conn, "alice")
            _own_room(conn, "alice", "room-alerts", "alerts")
        user_profiles.ensure_profile(config.db_path, "alice", display_name="alice")
        user_profiles.update_profile(
            config.db_path, "alice", alerts_channel="room-alerts",
        )
        _set_default_room(config, "alice", "room-alerts")

        with db.get_db(config.db_path) as conn:
            channels = db.channel_room_tokens(conn, "alice")
            assert "room-alerts" in channels
            assert db._usable_as_delivery_default(
                conn, "alice", "room-alerts", channels,
            ) is False
            assert db.default_web_room(conn, "alice").token == "room-alerts"

    def test_the_picker_still_drops_a_room_the_sidebar_hides(self, tmp_path):
        """ISSUE-478's arm, restated against the folded core: `visible_room` is
        the picker's predicate and it keeps counting a hide."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _own_room(conn, "alice", "room-hidden", "notes")
            _own_room(conn, "alice", "room-open", "other")
            db.dismiss_room(conn, "room-hidden", "alice")

            assert db.visible_room(conn, "alice", "room-hidden") is None
            assert db.visible_room(conn, "alice", "room-open") is not None
