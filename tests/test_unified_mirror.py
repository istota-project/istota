"""Mirror fan-out via output_target="room".

`room` expands at resolve time by the room's live bindings (not a static alias):
the origin delivery plus a push mirror to every non-origin binding whose view of
the room lives in a store we don't own (`TransportCapabilities.room_view ==
"external"`). A `"canonical"` binding (web) is skipped — writing the canonical
`messages` row is already that surface's delivery, so a push would double-post.
"""

import pytest

from istota import db
from istota.config import Config
from istota.transport._types import TransportCapabilities
from istota.transport.registry import TransportRegistry, make_registry
from istota.transport.routing import (
    Destination,
    parse_output_target,
    resolve_delivery_plan,
)


class _CanonicalTalk:
    """A stand-in Talk transport declaring the *opposite* room_view from the
    real one, so a test can tell "read the registry you were handed" apart from
    "rebuild an identical one from config"."""

    name = "talk"
    capabilities = TransportCapabilities(room_view="canonical")


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    return cfg


def _task(**kwargs):
    defaults = dict(
        id=1, status="pending", source_type="web", user_id="alice",
        prompt="x", conversation_token=None, priority=5,
        attempt_count=0, max_attempts=3,
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


class TestParseRoom:
    def test_room_parses_to_single_destination(self):
        assert parse_output_target("room") == [Destination("room")]


class TestIsCanonicalRoomView:
    """The public form of the `room_view` question, which the scheduler uses to
    pick the destinations whose delivery *is* the canonical row."""

    def test_web_is_canonical_and_talk_is_not(self, config):
        from istota.transport.routing import is_canonical_room_view

        config.talk.enabled = True
        registry = make_registry(config)
        assert is_canonical_room_view(config, registry, "web") is True
        assert is_canonical_room_view(config, registry, "talk") is False

    def test_a_non_room_surface_is_not_canonical(self, config):
        from istota.transport.routing import is_canonical_room_view

        registry = make_registry(config)
        assert is_canonical_room_view(config, registry, "ntfy") is False
        assert is_canonical_room_view(config, registry, "repl") is False

    def test_an_unresolvable_surface_is_not_canonical(self, config):
        """False, not an exception — and False is the safe answer: it keeps the
        destination in the push lane rather than silently dropping it on the
        assumption that writing a row already covered it."""
        from istota.transport.routing import is_canonical_room_view

        registry = make_registry(config)
        assert is_canonical_room_view(config, registry, "matrix") is False


class TestRoomViewCapability:
    """Every shipped surface declares where its view of a room is stored.

    The map is spelled out rather than derived so a *new* transport fails this
    test until someone decides which answer it gives. A surface that silently
    defaults to None is one the room fan-out will never mirror to, which is the
    failure mode this whole axis exists to make impossible to reach by accident.
    """

    EXPECTED = {
        "talk": "external",       # transcript in Nextcloud — needs a real push
        "web": "canonical",       # transcript is `messages` — the row is the delivery
        "email": None,            # a delivery target, not a view of a room
        "ntfy": None,
        "istota_file": None,
        "repl": None,
    }

    def test_every_registered_surface_declares_room_view(self, config):
        config.talk.enabled = True
        config.email.enabled = True
        registry = make_registry(config)
        assert set(registry.names()) == set(self.EXPECTED), (
            "a transport was added or removed without deciding its room_view"
        )
        actual = {
            name: registry.get(name).capabilities.room_view
            for name in registry.names()
        }
        assert actual == self.EXPECTED

    def test_room_view_is_orthogonal_to_surface_class(self, config):
        """Web is a stream surface *and* a canonical room view; Talk is a push
        surface *and* an external room view. The two axes agree today by
        coincidence, and the fan-out must read the room one."""
        config.talk.enabled = True
        registry = make_registry(config)
        web = registry.get("web").capabilities
        talk = registry.get("talk").capabilities
        assert (web.surface_class, web.room_view) == ("stream", "canonical")
        assert (talk.surface_class, talk.room_view) == ("push", "external")
        # repl is the counterexample that keeps them from being one field: a
        # stream surface that is not a room view at all.
        repl = registry.get("repl").capabilities
        assert (repl.surface_class, repl.room_view) == ("stream", None)


class TestRoomExpansion:
    def test_web_origin_mirrors_to_bound_talk(self, config):
        # web-origin room bound to talk: origin stream + talk push mirror.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-1", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-1", "web", "web-alice-1")
            db.add_room_binding(conn, "web-alice-1", "talk", "talktok9")
        task = _task(source_type="web", conversation_token="web-alice-1",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        surfaces = {(d.surface, d.channel, d.kind) for d in plan}
        assert ("web", "stream", "stream") in surfaces
        assert ("talk", "talktok9", "push") in surfaces
        talk_dest = next(d for d in plan if d.surface == "talk")
        assert talk_dest.mirror is True

    def test_web_only_room_mirrors_nowhere(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-2", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-2", "web", "web-alice-2")
        task = _task(source_type="web", conversation_token="web-alice-2",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["web"]
        assert plan[0].kind == "stream"

    def test_email_reply_into_dual_bound_room_pushes_to_talk(self, config):
        """An email reply routed by `room` reaches the room's Talk binding.

        The live failure this covers: the reply's stored origin descriptor named
        `web:<token>` for a room bound to *both* surfaces, so Talk — where the
        user had watched the whole exchange — showed nothing at all. `email` is
        not a room surface, so the Talk binding is a non-origin push and the
        email leg is the origin delivery. The web binding is skipped as a
        `room_view == "canonical"` surface: that room's assistant row is written
        by `_store_room_turn`, and a push would render it a second time as a
        system note.
        """
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm1", "alice", origin="web")
            db.add_room_binding(conn, "rm1", "web", "rm1")
            db.add_room_binding(conn, "rm1", "talk", "rm1")
        task = _task(source_type="email", conversation_token="rm1",
                     output_target="room,email")
        plan = resolve_delivery_plan(config, task, None)
        assert ("talk", "rm1", "push") in {
            (d.surface, d.channel, d.kind) for d in plan
        }
        assert [d.surface for d in plan].count("email") == 1
        assert not [d for d in plan if d.surface == "web" and d.kind == "push"]

    def test_talk_origin_does_not_push_to_web_binding(self, config):
        # talk-origin room bound to web: web's room_view is "canonical", so no
        # mirror push — the web view renders Talk turns from the shared store.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk")
            db.add_room_binding(conn, "cpz", "talk", "cpz")
            db.add_room_binding(conn, "cpz", "web", "cpz")
        task = _task(source_type="talk", conversation_token="cpz",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["talk"]
        assert plan[0].channel == "cpz"

    def test_binding_with_no_live_transport_is_kept_not_suppressed(self, config):
        """Talk bound but disabled in config: the destination survives expansion.

        `_room_view` can't read capabilities off a transport that was never
        registered, and "unresolvable" must read as "not a canonical view" —
        which is why the skip tests for `"canonical"` rather than keeping only
        `"external"`. The destination resolves to a normal push against the
        binding's own surface_ref and fails at delivery, exactly as it did under
        the old `_STREAM_SURFACES` name check. Skipping here instead would make
        the mirror disappear at plan time with nothing logged.
        """
        config.talk.enabled = False
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-4", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-4", "web", "web-alice-4")
            db.add_room_binding(conn, "web-alice-4", "talk", "offtok")
        task = _task(source_type="web", conversation_token="web-alice-4",
                     output_target="room")
        plan = resolve_delivery_plan(config, task, None)
        assert ("talk", "offtok", "push") in {
            (d.surface, d.channel, d.kind) for d in plan
        }

    def test_expansion_reads_the_registry_it_was_handed(self, config):
        """The passed registry is what's consulted, not one rebuilt from config.

        Pinned with a stub whose `talk` declares `room_view="canonical"`: handed
        through, the Talk binding is skipped; drop the argument and the
        `make_registry(config)` fallback calls it `"external"` and the mirror
        reappears. The disagreement is the whole point — a stub that agreed
        would pass whether or not the argument was wired, since the fallback
        builds exactly the registry the one production caller passes.
        """
        config.talk.enabled = True
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-5", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-5", "web", "web-alice-5")
            db.add_room_binding(conn, "web-alice-5", "talk", "talktok5")
        task = _task(source_type="web", conversation_token="web-alice-5",
                     output_target="room")
        real = make_registry(config)
        assert "talk" in {
            d.surface for d in resolve_delivery_plan(config, task, real)
        }
        stub = TransportRegistry({
            **{n: real.get(n) for n in real.names() if n != "talk"},
            "talk": _CanonicalTalk(),
        })
        assert "talk" not in {
            d.surface for d in resolve_delivery_plan(config, task, stub)
        }

    def test_room_expands_by_live_bindings_not_static_alias(self, config):
        # Same task, before vs after adding a talk binding: the plan changes.
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-3", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-3", "web", "web-alice-3")
        task = _task(source_type="web", conversation_token="web-alice-3",
                     output_target="room")
        before = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in before] == ["web"]
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, "web-alice-3", "talk", "newtalk")
        after = resolve_delivery_plan(config, task, None)
        assert {d.surface for d in after} == {"web", "talk"}


class TestOriginDescriptor:
    """What a send stamps on `sent_emails.origin_target`."""

    def test_a_registered_room_is_named_as_a_room(self, config):
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-9", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-9", "web", "web-alice-9")
            task = _task(source_type="web", conversation_token="web-alice-9")
            assert origin_descriptor(task, conn) == "room:web-alice-9"

    def test_a_talk_room_is_named_as_a_room_too(self, config):
        """Whatever surface the send went out from. Recording the leg is what
        threw away the fact that the destination was a room at all."""
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "tk9", "alice", origin="talk")
            db.add_room_binding(conn, "tk9", "talk", "tk9")
            task = _task(source_type="talk", conversation_token="tk9")
            assert origin_descriptor(task, conn) == "room:tk9"

    def test_a_promoted_rooms_surface_ref_resolves_to_its_canonical_token(
        self, config,
    ):
        """The Talk binding's ref is not the canonical token, and the stored
        descriptor has to name the room rather than one of its aliases."""
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-alice-10", "alice", origin="web")
            db.add_room_binding(conn, "web-alice-10", "web", "web-alice-10")
            db.add_room_binding(conn, "web-alice-10", "talk", "PromotedTok")
            task = _task(source_type="talk", conversation_token="PromotedTok")
            assert origin_descriptor(task, conn) == "room:web-alice-10"

    def test_an_email_continuation_finds_a_promoted_rooms_talk_ref(self, config):
        """The cross-surface case a surface-scoped lookup cannot answer.

        An email continuation's `conversation_token` is whatever the originating
        send recorded — on a promoted room, the Talk ref — while its own surface
        is `email`, which owns no bindings at all. Resolving under the task's own
        surface therefore always misses, and the room reads as unregistered, so
        the send stamps a single-surface descriptor for a room bound to two.
        """
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-canon", "alice", origin="web")
            db.add_room_binding(conn, "web-canon", "web", "web-canon")
            db.add_room_binding(conn, "web-canon", "talk", "TalkRef77")
            task = _task(source_type="email", conversation_token="TalkRef77")
            assert origin_descriptor(task, conn) == "room:web-canon"

    def test_a_talk_delivery_token_can_name_the_room(self, config):
        """A task whose channel is a synthetic email-thread hash can still carry
        the real room separately; stamping the surface form for it would write
        exactly the single-leg descriptor this stage stops writing."""
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "tk-real", "alice", origin="talk")
            db.add_room_binding(conn, "tk-real", "talk", "tk-real")
            task = _task(
                source_type="talk", conversation_token="0123456789abcdef",
                talk_delivery_token="tk-real",
            )
            assert origin_descriptor(task, conn) == "room:tk-real"

    def test_an_unregistered_dm_keeps_the_surface_form(self, config):
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            task = _task(source_type="talk", conversation_token="dmtoken")
            assert origin_descriptor(task, conn) == "talk:dmtoken"

    def test_an_archived_room_keeps_the_surface_form(self, config):
        from istota.transport.routing import origin_descriptor

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "gone", "alice", origin="talk")
            db.add_room_binding(conn, "gone", "talk", "gone")
            conn.execute(
                "UPDATE rooms SET archived = 1 WHERE token = ?", ("gone",),
            )
            task = _task(source_type="talk", conversation_token="gone")
            assert origin_descriptor(task, conn) == "talk:gone"

    def test_without_a_connection_it_cannot_name_a_room(self, config):
        """The pre-rooms behaviour, kept so a caller with no connection in scope
        still stamps something that routes."""
        from istota.transport.routing import origin_descriptor

        task = _task(source_type="web", conversation_token="web-alice-9")
        assert origin_descriptor(task) == "web:web-alice-9"


class TestExplicitRoomToken:
    def test_room_with_a_token_expands_that_room_not_the_tasks_own(self, config):
        """The case the bare `room` form cannot express: an email reply whose
        own conversation_token is a synthetic thread hash, carrying a stored
        `room:<token>` descriptor that names where the conversation lives."""
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm-elsewhere", "alice", origin="web")
            db.add_room_binding(conn, "rm-elsewhere", "web", "rm-elsewhere")
            db.add_room_binding(conn, "rm-elsewhere", "talk", "talk-elsewhere")
        task = _task(
            source_type="email", conversation_token="0123456789abcdef",
            output_target="room:rm-elsewhere,email",
        )
        plan = resolve_delivery_plan(config, task, None)
        surfaces = {(d.surface, d.channel) for d in plan}
        assert ("talk", "talk-elsewhere") in surfaces
        assert ("email", None) in surfaces

    def test_naming_a_room_and_one_of_its_legs_delivers_once(self, config):
        """`resolve_delivery_plan` dedups on (surface, channel) after
        resolution, which is what stops a double-post here.

        The task's own channel is deliberately *not* the room, so the Talk leg
        can only reach the plan through the explicit `room:<token>` — otherwise
        this passes without the token being read at all and pins nothing.
        """
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm-dup", "alice", origin="web")
            db.add_room_binding(conn, "rm-dup", "web", "rm-dup")
            db.add_room_binding(conn, "rm-dup", "talk", "tk-dup")
        task = _task(
            source_type="email", conversation_token="0123456789abcdef",
            output_target="room:rm-dup,talk:tk-dup",
        )
        plan = resolve_delivery_plan(config, task, None)
        talk_legs = [d for d in plan if d.surface == "talk"]
        assert len(talk_legs) == 1
        assert talk_legs[0].channel == "tk-dup"


class TestExternalIdLedger:
    def test_set_and_detect_external_id(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "r", "alice", origin="web")
            mid = db.add_message(
                conn, "r", role="assistant", body="bot reply",
                origin_surface="web", task_id=1,
            )
            db.set_message_external_id(conn, mid, "talk", "8888")
            assert db.message_has_external_id(conn, "r", "talk", "8888") is True
            assert db.message_has_external_id(conn, "r", "talk", "9999") is False
