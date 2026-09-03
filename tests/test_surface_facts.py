"""The room-model table and its readers (`istota.surfaces`).

Three properties, and the second is the one the leaf exists for. The readers
are total — a value off a database row is whatever the row held, so every
reader answers rather than raises. `origin_surface_for_source_type` answers the
*origin* question rather than the delivery question, which is the distinction
`transport.registry._surface_for_source_type` does not make and must not be
asked to. And the table covers every surface the registry can produce, so
adding a transport without filling in a record fails here rather than answering
"not a room surface" at seven call sites.

The seven converted sites read it now; the equivalence tests pinning each one
against the literal it replaces live in `tests/test_surface_model_equivalence.py`.
"""

import dataclasses
import inspect
import re

import pytest

from istota import surfaces
from istota.config import Config
from istota.transport import make_registry, registry as registry_module
from istota.transport.registry import _surface_for_source_type

# The source types the spec enumerates, and the set the readers are checked
# total over. Explicit rather than derived, because the point of the
# enumeration is to state what they are and check each one; a list generated
# from the same place the code reads would assert nothing.
#
# Eleven of these are values `tasks.source_type` actually holds. `playbook` is
# not — every `playbook` literal in `src/` is a `memory_chunks.source_type`
# (`executor._recall_playbooks`, `memory/sleep_cycle.py`) and no `create_task`
# call passes it. It is kept because an extra unrecognised value costs one
# parametrized case and must answer None either way. It is why the flip list
# below carries eight names against the seven `origin_surface_for_source_type`
# documents.
SHIPPED_SOURCE_TYPES = (
    "briefing", "cli", "doctor", "email", "heartbeat", "istota_file",
    "playbook", "repl", "scheduled", "subtask", "talk", "web",
)

# The five of those that name a surface a task can originate on.
ORIGIN_SURFACE_SOURCE_TYPES = ("email", "istota_file", "repl", "talk", "web")

# Values a reader can be handed by a caller that read a column, parsed JSON, or
# had nothing at all. None of them is a surface. The ids are spelled out rather
# than derived with `repr`, because an object's repr carries its address and
# the suite runs under xdist — a worker-dependent test id makes collection
# disagree between workers.
JUNK = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param("  ", id="blank"),
    pytest.param("Talk", id="wrong-case"),
    pytest.param("talk ", id="trailing-space"),
    pytest.param("matrix", id="unknown-surface"),
    pytest.param(0, id="int-zero"),
    pytest.param(1, id="int-one"),
    pytest.param(3.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param([], id="list"),
    pytest.param({}, id="dict"),
    pytest.param(object(), id="object"),
]

ALL_READERS = (
    surfaces.room_role,
    surfaces.room_view,
    surfaces.is_room_member,
    surfaces.is_room_view,
    surfaces.user_turn_mirror,
    surfaces.origin_surface_for_source_type,
)


class TestTheTable:
    def test_talk_is_an_external_room_member_that_mirrors_as_the_user(self):
        assert surfaces.room_role("talk") == "member"
        assert surfaces.room_view("talk") == "external"
        assert surfaces.user_turn_mirror("talk") == "as_user"

    def test_web_is_a_canonical_room_member(self):
        assert surfaces.room_role("web") == "member"
        assert surfaces.room_view("web") == "canonical"
        # Nothing mirrors *into* a canonical view: writing the row is the
        # delivery, so a mirror mode would render the turn twice.
        assert surfaces.user_turn_mirror("web") is None

    def test_email_is_a_guest_with_no_room_view(self):
        # The durable-place test: an email thread is reconstructed from
        # messages in mailboxes we cannot write into, so it is not a room view.
        # Guest is what ISSUE-136's "existence, never creation" rule looks like
        # as a field — email joins a room's transcript and never mints one.
        assert surfaces.room_role("email") == "guest"
        assert surfaces.room_view("email") is None
        assert surfaces.is_room_member("email") is False
        assert surfaces.is_room_view("email") is False

    @pytest.mark.parametrize("surface", ["ntfy", "istota_file", "repl"])
    def test_the_non_room_surfaces_answer_nothing(self, surface):
        assert surfaces.room_role(surface) is None
        assert surfaces.room_view(surface) is None
        assert surfaces.user_turn_mirror(surface) is None
        assert surfaces.is_room_member(surface) is False
        assert surfaces.is_room_view(surface) is False

    def test_a_mirror_mode_is_only_declared_on_an_external_room_view(self):
        # `user_turn_mirror` is a refinement of `room_view == "external"`, not
        # an independent axis. A record declaring one without the other means
        # something no fan-out path can act on.
        for name, facts in surfaces.SURFACES.items():
            if facts.user_turn_mirror is not None:
                assert facts.room_view == "external", name

    def test_a_room_view_is_also_a_room_member(self):
        # True of every surface today and not a law — the two are separate
        # questions and the confirmation gate is where they could diverge. If a
        # surface ever breaks this, that gate is what to re-read.
        for name, facts in surfaces.SURFACES.items():
            if facts.room_view is not None:
                assert facts.room_role == "member", name

    def test_the_records_are_frozen(self):
        # The record is bound outside the `raises` block on purpose: with the
        # subscript inside it, removing the `talk` key satisfies the assertion
        # with a KeyError and the test passes without ever exercising
        # `frozen=True`. Narrowed to FrozenInstanceError for the same reason.
        facts = surfaces.SURFACES["talk"]
        with pytest.raises(dataclasses.FrozenInstanceError):
            facts.room_role = "guest"  # type: ignore[misc]

    def test_a_record_cannot_be_half_filled(self):
        # No defaults on the dataclass: an empty record would satisfy the
        # key-coverage tests below while answering "not a room surface" at every
        # converted site, which is the exact failure those tests exist to catch.
        with pytest.raises(TypeError):
            surfaces.SurfaceRoomFacts()  # type: ignore[call-arg]


class TestTheTwoPredicatesAreSeparateReads:
    """Every shipped surface answers `is_room_member` and `is_room_view` the
    same way, so nothing above distinguishes them — a change routing one
    through the other would keep the whole file green. These are the only
    assertions that prove they read different fields, which is what the
    scheduler's confirmation gate depends on being true.
    """

    def test_a_member_with_no_view_is_not_a_room_view(self, monkeypatch):
        # Asserted before the patch rather than after it in a test of its own:
        # `SURFACES` is a mutable global every reader resolves per call, so a
        # row leaked by an earlier test follows the worker. A separate
        # "it was cleaned up" test would pass trivially whenever it ran first
        # or landed on another xdist worker.
        assert "imaginary" not in surfaces.SURFACES
        monkeypatch.setitem(
            surfaces.SURFACES, "imaginary",
            surfaces.SurfaceRoomFacts(
                room_role="member", room_view=None, user_turn_mirror=None,
            ),
        )
        assert surfaces.is_room_member("imaginary") is True
        assert surfaces.is_room_view("imaginary") is False

    def test_a_guest_with_an_external_view_is_a_room_view(self, monkeypatch):
        # The shape the spec's open question 2 asks about — "may create a room"
        # and "may write a turn into one" as two bits rather than an enum. No
        # surface is this today; the readers must still answer it apart.
        assert "imaginary" not in surfaces.SURFACES
        monkeypatch.setitem(
            surfaces.SURFACES, "imaginary",
            surfaces.SurfaceRoomFacts(
                room_role="guest", room_view="external",
                user_turn_mirror="attributed",
            ),
        )
        assert surfaces.is_room_member("imaginary") is False
        assert surfaces.is_room_view("imaginary") is True
        assert surfaces.user_turn_mirror("imaginary") == "attributed"


class TestTheReadersAreTotal:
    @pytest.mark.parametrize("reader", ALL_READERS, ids=lambda f: f.__name__)
    @pytest.mark.parametrize("value", JUNK)
    def test_junk_answers_rather_than_raises(self, reader, value):
        # Callers pass values straight off database rows and off model-written
        # JSON, so a non-string is a case to answer rather than to crash on.
        assert reader(value) in (None, False)

    def test_an_unknown_surface_is_conservative_everywhere(self):
        # The conservative answer is what every converted site wants: an
        # unrecognised surface neither owns a room nor shows one.
        assert surfaces.is_room_member("matrix") is False
        assert surfaces.is_room_view("matrix") is False
        assert surfaces.room_role("matrix") is None
        assert surfaces.room_view("matrix") is None
        assert surfaces.user_turn_mirror("matrix") is None

    def test_the_predicates_return_real_bools(self):
        # They are read straight into `if` and into `not` at the converted
        # sites; a truthy record or a None would read the same there and
        # differently in an assertion.
        assert surfaces.is_room_member("talk") is True
        assert surfaces.is_room_view("talk") is True
        assert surfaces.is_room_member(None) is False


class TestOriginSurfaceForSourceType:
    @pytest.mark.parametrize("source_type", SHIPPED_SOURCE_TYPES)
    def test_every_shipped_source_type_is_answered(self, source_type):
        answer = surfaces.origin_surface_for_source_type(source_type)
        if source_type in ORIGIN_SURFACE_SOURCE_TYPES:
            assert answer == source_type
        else:
            assert answer is None

    @pytest.mark.parametrize("source_type", SHIPPED_SOURCE_TYPES)
    def test_the_room_predicates_answer_todays_answer(self, source_type):
        # The two scheduler gates used to test `task.source_type` against a
        # `("talk", "web")` literal directly. Routing the same source type
        # through the leaf must not change either answer for any shipped value.
        origin = surfaces.origin_surface_for_source_type(source_type)
        expected = source_type in ("talk", "web")
        assert surfaces.is_room_member(origin) is expected
        assert surfaces.is_room_view(origin) is expected

    def test_it_is_not_the_delivery_mapping(self):
        # The defect this function exists to avoid, asserted rather than
        # described: `_surface_for_source_type` maps every non-surface source
        # type to "talk", so asking it the origin question flips eight of the
        # twelve values enumerated above from "no surface" to "a room surface"
        # — seven of them real task source types. At the confirmation gate
        # the predicate is negated, which is what suppressed the prompt on the
        # mirror leg for cron, briefing and heartbeat tasks. (The spec says six;
        # measured, it is eight — `istota_file` and `doctor` flip too.)
        flipped = [
            st for st in SHIPPED_SOURCE_TYPES
            if surfaces.is_room_member(_surface_for_source_type(st))
            != surfaces.is_room_member(surfaces.origin_surface_for_source_type(st))
        ]
        assert sorted(flipped) == [
            "briefing", "cli", "doctor", "heartbeat", "istota_file",
            "playbook", "scheduled", "subtask",
        ]

    def test_an_empty_source_type_originates_nowhere(self):
        # The confirmation gate reads `task.source_type or ""` (the
        # `store_turn_message` one reads the column bare), and the delivery
        # mapping sends "" to "talk". This is the ninth flip and the one a
        # column default can produce.
        assert surfaces.origin_surface_for_source_type("") is None
        assert _surface_for_source_type("") == "talk"

    def test_an_unknown_source_type_originates_nowhere(self):
        assert surfaces.origin_surface_for_source_type("totally-unknown") is None
        assert surfaces.origin_surface_for_source_type("") is None


class TestTheTableCoversTheRegistry:
    def test_every_surface_make_registry_can_produce_has_a_record(self):
        # Talk and email are the two config-gated transports, so enabling both
        # is what makes the registry produce every surface there is.
        config = Config()
        config.talk.enabled = True
        config.email.enabled = True
        registry = make_registry(config)
        assert set(registry.names()) == set(surfaces.SURFACES)

    def test_every_transport_make_registry_assigns_has_a_record(self):
        # The test above cannot see a transport added behind a *new* config
        # flag: it would be absent from `registry.names()` under this config and
        # absent from SURFACES, and the equality would stay green while a
        # deployed surface answered "not a room surface" at every converted
        # site. `.claude/rules/transport.md` names Matrix as the designed-for
        # next one. So read the assignments out of `make_registry`'s own source,
        # the shape `tests/test_lint_scope.py` uses to keep a hand list honest.
        source = inspect.getsource(registry_module.make_registry)
        assigned = set(re.findall(r'transports\[["\'](\w+)["\']\]\s*=', source))
        assert assigned, "found no transport assignments — the regex has rotted"
        missing = assigned - set(surfaces.SURFACES)
        assert not missing, (
            f"transports with no record in surfaces.SURFACES: {sorted(missing)}"
        )

    def test_a_disabled_transport_does_not_change_the_static_answer(self):
        # The whole reason the table is static rather than registry-derived:
        # `web_app._user_row_display` and the scheduler's gates ask what role a
        # surface plays, not whether this deployment has one running.
        config = Config()
        config.talk.enabled = False
        assert "talk" not in make_registry(config).names()
        assert surfaces.is_room_member("talk") is True
        assert surfaces.room_view("talk") == "external"
