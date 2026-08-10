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
