"""Each converted site's new predicate answers what its literal answers.

This is the instrument for the model conversion, and it is a stronger one than
the Talk double: it enumerates every surface name and every shipped source type
and requires identical answers, where "no delivery test went red" is
circumstantial. Written and run **before** any site moves, because a test
written after the conversion proves only that the conversion is
self-consistent.

Six sites convert. Each one is a `Site` record below carrying the expression as
it reads today, the expression that replaces it, and both as callables. One
parametrized test compares them over a domain wide enough to cover what any of
the six can actually be handed — surface names, source types, the
destination-grammar names, the empty string a column default produces, and the
non-strings a database row or a parsed descriptor can hold.

**Restating the literal is the load-bearing weakness, and `TestTheSitesStillReadOneOfTheTwoForms`
is the answer to it.** A pinned copy of `("talk", "web")` proves nothing about
the site it claims to pin, so each record also names the module and the exact
text, and that test requires the module to contain either the literal (today) or
the accessor the conversion introduces (after Stage 6). A site rewritten into a
third form fails there rather than passing here against an expression nothing
reads any more.

The seventh site in the spec's own §C table — `commands`' `!confirm` transcript
write — is **not** converted, and `TestTheConfirmWriteIsNotEquivalent` is the
executable form of why: it gates on `_TRANSCRIPT_SURFACES`, which includes
email, so `is_room_member` there would stop an email `!confirm` recording an
exchange its own docstring calls a durable authorization record.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from istota import surfaces
from istota.surfaces import (
    is_room_member,
    is_room_view,
    origin_surface_for_source_type,
)

# The source-type enumeration, from the one place that states it. Eleven of the
# twelve are values `tasks.source_type` holds; `playbook` is a
# `memory_chunks.source_type` and is kept because an unrecognised value costs
# one case and must answer the same either way. See that module's comment.
from tests.test_surface_facts import SHIPPED_SOURCE_TYPES

SRC = Path(__file__).resolve().parent.parent / "src"

# Every surface name a registry can produce. Held equal to `surfaces.SURFACES`
# by `test_the_surface_enumeration_covers_the_table` below, so a surface added
# without a row here fails rather than going unchecked.
SURFACE_NAMES = ("email", "istota_file", "ntfy", "repl", "talk", "web")

# Names in the *destination grammar* rather than surface names.
# `parse_output_target` yields `Destination("room", <token>)` and
# `Destination("stream", None)`, and `_room_for_destination` dispatches on the
# first before it ever reaches the branch this file pins — a member check put
# ahead of that dispatch would drop every `room:<token>` descriptor, which is
# the descriptor ISSUE-247 exists for. They are in the domain because the
# *values* reach the site, not because the site treats them as surfaces.
GRAMMAR_NAMES = ("room", "stream")

# Four more strings reach these sites and are none of the above: the empty
# string the confirmation gate spells explicitly (`task.source_type or ""`) and
# that `_row_get(row, "origin_surface") or ""` produces in `web_app`; a name no
# deployment has; and the two near-misses that must answer as strangers,
# because the lookup is exact and no writer normalizes the column. They are
# spelled inline in the domain below, with their ids.
#
# `messages.origin_surface` is read raw and `Task.source_type` is nullable, so a
# reader is handed whatever the row held. Excludes the unhashable values, which
# are their own test: two of the six literals are frozensets and raise on them.
# The ids are spelled out rather than derived with `repr`, because an object's
# repr carries its address and the suite runs under xdist — a worker-dependent
# test id makes collection disagree between workers.
NON_STRINGS = [
    pytest.param(None, id="none"),
    pytest.param(0, id="int-zero"),
    pytest.param(1, id="int-one"),
    pytest.param(True, id="bool"),
    pytest.param(3.5, id="float"),
    pytest.param(object(), id="object"),
]

HASHABLE_DOMAIN = [
    *(pytest.param(v, id=f"surface-{v}") for v in SURFACE_NAMES),
    *(pytest.param(v, id=f"source-type-{v}") for v in SHIPPED_SOURCE_TYPES),
    *(pytest.param(v, id=f"grammar-{v}") for v in GRAMMAR_NAMES),
    pytest.param("", id="empty"),
    pytest.param("matrix", id="unknown-surface"),
    pytest.param("Talk", id="wrong-case"),
    pytest.param("talk ", id="trailing-space"),
    *NON_STRINGS,
]

# The bare values, for the tests that iterate rather than parametrize.
HASHABLE_VALUES = [p.values[0] for p in HASHABLE_DOMAIN]

# `list` and `dict` are unhashable, so `x in frozenset(...)` raises `TypeError`
# on them while `x in (…)` does not. Kept out of the equality domain and
# asserted separately: the conversion makes two sites *more* total than they
# were, which is a difference in the safe direction and worth stating rather
# than hiding behind a domain that avoids it.
UNHASHABLE_VALUES = ([], {})


@dataclass(frozen=True)
class Site:
    """One convertible site, with both spellings of its gate."""

    name: str                       # as the spec's §C table names it
    module: str                     # the file the expression lives in
    literal_text: str               # the expression as it reads before Stage 6
    accessor: str                   # the name the conversion introduces
    literal: Callable[[object], bool]
    predicate: Callable[[object], bool]


SITES = (
    Site(
        name="ingest.record_inbound room gate",
        module="istota/transport/ingest.py",
        # `room_surface = surface in ROOM_SURFACES and bool(room_token)` — the
        # `bool(room_token)` conjunct is not part of the question and does not
        # move; only the surface test converts.
        literal_text="surface in ROOM_SURFACES",
        accessor="is_room_member",
        literal=lambda v: v in frozenset({"talk", "web"}),
        predicate=is_room_member,
    ),
    Site(
        name="routing._room_for_destination binding branch",
        module="istota/transport/routing.py",
        # The branch that resolves a surface ref through `room_bindings` before
        # the registry is asked. Not the dispatch above it, which tests
        # `surface == "talk"` / `== "web"` / `== "room"` separately.
        literal_text='surface in ("talk", "web")',
        accessor="is_room_member",
        literal=lambda v: v in ("talk", "web"),
        predicate=is_room_member,
    ),
    Site(
        name="commands !steer transcript write",
        module="istota/commands.py",
        # Commented "Room surfaces only" at the site. Its neighbour in the same
        # file, `_record_confirm_exchange`, gates on a different literal and is
        # not this question — see `TestTheConfirmWriteIsNotEquivalent`.
        literal_text='ctx.surface in ("talk", "web")',
        accessor="is_room_member",
        literal=lambda v: v in ("talk", "web"),
        predicate=is_room_member,
    ),
    Site(
        name="web_app._user_row_display foreign marker",
        module="istota/web_app.py",
        # The only site that reads the predicate **negated**, to mark a row as
        # foreign to the room. Takes the raw `messages.origin_surface` column
        # with no source-type mapping: that column stores `task.source_type` for
        # assistant rows, so its domain is wider than surface names and mapping
        # it would be a category error. The branch is also gated on
        # `author_label`, which only email rows carry.
        literal_text="origin_surface not in ROOM_SURFACES",
        accessor="is_room_member",
        literal=lambda v: v not in frozenset({"talk", "web"}),
        predicate=lambda v: not is_room_member(v),
    ),
    Site(
        name="scheduler store_turn_message gate",
        module="istota/scheduler.py",
        # `task.source_type` unguarded here, where the confirmation gate below
        # spells `or ""`. The column is nullable, so None reaches both; the
        # tuple answers False for it and so does the mapping.
        literal_text='task.source_type in ("talk", "web")',
        accessor="origin_surface_for_source_type",
        literal=lambda v: v in ("talk", "web"),
        predicate=lambda v: is_room_member(origin_surface_for_source_type(v)),
    ),
    Site(
        name="scheduler confirmation mirror gate",
        module="istota/scheduler.py",
        # The one site asking the *view* question — "does this task's own origin
        # surface show it the question itself?" — rather than the ownership one,
        # and the only place the two axes could ever diverge. Read negated at
        # the site (`not (_talk_is_mirror and …)`), which is why the delivery
        # mapping must not be reused here: flipping a source type to a room
        # surface suppresses the prompt on the mirror leg.
        literal_text='(task.source_type or "") in ROOM_SURFACES',
        accessor="origin_surface_for_source_type",
        literal=lambda v: (v or "") in frozenset({"talk", "web"}),
        predicate=lambda v: is_room_view(origin_surface_for_source_type(v or "")),
    ),
)


def _site_ids(site: Site) -> str:
    return site.name


class TestEveryConvertedSiteAnswersWhatItAnsweredBefore:
    @pytest.mark.parametrize("site", SITES, ids=_site_ids)
    @pytest.mark.parametrize("value", HASHABLE_DOMAIN)
    def test_the_two_spellings_agree(self, site, value):
        assert site.literal(value) == site.predicate(value)

    @pytest.mark.parametrize("site", SITES, ids=_site_ids)
    def test_the_predicate_returns_a_real_bool(self, site):
        # Read straight into `if` and into `not` at the sites, so a truthy
        # record or a None would behave the same there and differently here.
        assert isinstance(site.predicate("talk"), bool)
        assert isinstance(site.predicate("email"), bool)

    @pytest.mark.parametrize("site", SITES, ids=_site_ids)
    @pytest.mark.parametrize("value", UNHASHABLE_VALUES, ids=["list", "dict"])
    def test_the_predicate_is_total_where_two_literals_were_not(self, site, value):
        # `ROOM_SURFACES` is a frozenset, so the ingest gate and the web_app
        # marker raise `TypeError` on an unhashable value today; the four tuple
        # literals do not. Every converted form answers, because
        # `surfaces._facts` type-checks before the lookup. A widening, in the
        # direction the sites already want — nothing feeds them a list, and if
        # something did, raising inside a room gate is the worse answer.
        assert isinstance(site.predicate(value), bool)

    def test_the_surface_enumeration_covers_the_table(self):
        # Vacuity guard. The domain above is a hand list, so a surface added to
        # `surfaces.SURFACES` without a line here would be compared by nothing
        # and every site would go on agreeing.
        assert set(SURFACE_NAMES) == set(surfaces.SURFACES)

    def test_the_domain_actually_separates_the_two_answers(self):
        # A domain on which every site answered False for every value would
        # make this whole file vacuous — six pairs of expressions agreeing that
        # nothing is a room. Both answers must appear, for every site.
        for site in SITES:
            assert {site.predicate(v) for v in HASHABLE_VALUES} == {True, False}, (
                site.name
            )
            assert {site.literal(v) for v in HASHABLE_VALUES} == {True, False}, (
                site.name
            )


class TestTheSitesStillReadOneOfTheTwoForms:
    """The tie between the pinned expression above and the code it claims to
    pin. Before the conversion the literal satisfies this; after it, the
    accessor does. A site rewritten into some third form satisfies neither, and
    then the equivalence above is comparing a copy against nothing.
    """

    @pytest.mark.parametrize("site", SITES, ids=_site_ids)
    def test_the_module_contains_the_literal_or_the_accessor(self, site):
        source = (SRC / site.module).read_text()
        assert site.literal_text in source or site.accessor in source, site.name

    @pytest.mark.parametrize("site", SITES, ids=_site_ids)
    def test_the_literal_names_one_site_while_it_is_there(self, site):
        # Each expression is unique in its module today, which is what makes
        # the pin above a pin on *that* site rather than on whichever of several
        # occurrences happened to match. After the conversion the count is zero
        # and this passes for the other reason.
        source = (SRC / site.module).read_text()
        assert source.count(site.literal_text) <= 1, site.name


class TestTheConfirmWriteIsNotEquivalent:
    """The seventh site in the spec's §C table, and the one that must not move.

    `_record_confirm_exchange` gates on `commands._TRANSCRIPT_SURFACES`,
    `("web", "talk", "email")` — the third of the three questions the same
    literals used to share, "may this surface deposit a `role='user'` row in a
    room at all", member plus guest. Converting it to `is_room_member` returns
    False for email and stops an email `!confirm` recording its exchange, in the
    phase whose whole claim is that it changes nothing.
    """

    def test_the_two_gates_differ_exactly_at_email(self):
        from istota.commands import _TRANSCRIPT_SURFACES

        differ = [
            v for v in SURFACE_NAMES
            if (v in _TRANSCRIPT_SURFACES) != is_room_member(v)
        ]
        assert differ == ["email"]

    def test_email_may_write_a_user_row_and_does_not_own_rooms(self):
        from istota.commands import _TRANSCRIPT_SURFACES

        assert "email" in _TRANSCRIPT_SURFACES
        assert is_room_member("email") is False
        # And the guest role is what says so positively, rather than the
        # absence of membership: email joins a room's transcript.
        assert surfaces.room_role("email") == "guest"

    def test_the_wider_question_is_not_a_reader_on_the_leaf(self):
        # There is deliberately no `records_room_turn()` accessor. `db.py`
        # declares the tuple once and `commands.py` imports it (Stage 6); the
        # set's domain is `source_type` values rather than surface names, and it
        # must track a migration DELETE holding a fourth value no surface table
        # will ever have. The two sets being equal today is a coincidence.
        assert not hasattr(surfaces, "records_room_turn")
