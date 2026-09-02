"""The leaf and the transports declare the same room facts.

Two places state what role a surface plays in the room model, deliberately.
`TransportCapabilities` carries `room_view`, `inbound_room_role` and
`user_turn_mirror` because somebody adding a surface is looking at the transport
class, and `istota.surfaces` carries the same three because the readers that ask
the question — `web_app._user_row_display` in the web process, the scheduler's
two gates — have no `Config` and no instantiated transport to read them from,
and must not get a different answer on a deployment where the surface is
switched off.

Duplication with a test holding it in step is the trade the spec chose over an
indirection at the declaration site, and it is the arrangement
`sandbox_cache_sweeper` already uses against `executor`'s cache directory names
and `usage_render.py` against `usageFormat.ts`. This file is the other half of
that trade: without it the two copies drift and the leaf is a guess.
"""

import pytest

from istota import surfaces
from istota.config import Config
from istota.transport import make_registry
from istota.transport._types import TransportCapabilities

# Every surface name a registry can produce. Restated rather than derived from
# `surfaces.SURFACES`, so dropping a row from the leaf shortens the enumeration
# instead of failing it — the coverage test below is what holds this list and
# the leaf equal, and it is the one that goes red when a surface is added.
EVERY_SURFACE = ("email", "istota_file", "ntfy", "repl", "talk", "web")


@pytest.fixture(scope="module")
def transports():
    """Every transport, instantiated. Talk and email are the two config-gated
    ones, so enabling both is what makes the registry produce them all."""
    config = Config()
    config.talk.enabled = True
    config.email.enabled = True
    registry = make_registry(config)
    return {name: registry.get(name) for name in registry.names()}


class TestTheTwoDeclarationsAgree:
    @pytest.mark.parametrize("name", EVERY_SURFACE)
    def test_room_view_agrees(self, transports, name):
        assert transports[name].capabilities.room_view == surfaces.room_view(name)

    @pytest.mark.parametrize("name", EVERY_SURFACE)
    def test_inbound_room_role_agrees(self, transports, name):
        # The leaf spells this one `room_role`: it has no outbound half to
        # distinguish it from, where the capability record sits beside
        # `room_view` and needs the direction saying.
        assert (
            transports[name].capabilities.inbound_room_role
            == surfaces.room_role(name)
        )

    @pytest.mark.parametrize("name", EVERY_SURFACE)
    def test_user_turn_mirror_agrees(self, transports, name):
        assert (
            transports[name].capabilities.user_turn_mirror
            == surfaces.user_turn_mirror(name)
        )

    def test_the_enumeration_covers_the_registry_and_the_leaf(self, transports):
        # Vacuity guard for the three tests above: each is parametrized over a
        # hand list, so a surface missing from it is checked by nothing. The
        # equality is stated in both directions because the two failures read
        # differently — a name here with no transport is a stale list, a
        # transport with no name here is an unchecked surface.
        assert set(EVERY_SURFACE) == set(transports)
        assert set(EVERY_SURFACE) == set(surfaces.SURFACES)


class TestTheCapabilityRecordIsInternallyConsistent:
    def test_a_mirror_mode_is_only_declared_on_an_external_room_view(
        self, transports,
    ):
        # `user_turn_mirror` is a refinement of `room_view == "external"`, not
        # an independent axis: a surface that is not an external room view is
        # never a fan-out target, so a mirror mode declared there names
        # behaviour no path can perform. The leaf asserts the same thing over
        # its own table (`test_surface_facts.py`); the two records are what
        # would disagree.
        for name, transport in transports.items():
            caps = transport.capabilities
            if caps.user_turn_mirror is not None:
                assert caps.room_view == "external", name

    def test_a_room_view_is_declared_by_a_room_member(self, transports):
        # True of every surface today and not a law — `is_room_member` and
        # `is_room_view` are separate questions and the scheduler's confirmation
        # gate is where they could diverge. Here to make a declaration that
        # breaks it a deliberate act rather than a typo.
        for name, transport in transports.items():
            caps = transport.capabilities
            if caps.room_view is not None:
                assert caps.inbound_room_role == "member", name


class TestTheDefaults:
    def test_an_undeclared_surface_is_no_part_of_the_room_model(self):
        # `ntfy`, `istota_file` and `repl` declare none of the three and rely on
        # these defaults, which is why they are asserted rather than assumed.
        # The conservative answer is also the safe one at every site the leaf
        # feeds: an unrecognised surface neither owns a room nor shows one.
        caps = TransportCapabilities()
        assert caps.room_view is None
        assert caps.inbound_room_role is None
        assert caps.user_turn_mirror is None

    @pytest.mark.parametrize("name", ["ntfy", "istota_file", "repl"])
    def test_the_non_room_surfaces_declare_nothing(self, transports, name):
        caps = transports[name].capabilities
        assert (caps.room_view, caps.inbound_room_role, caps.user_turn_mirror) == (
            None, None, None,
        )
