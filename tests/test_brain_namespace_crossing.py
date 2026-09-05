"""A model pin only crosses when it actually changes namespace (ISSUE-417).

`config.model` was written for the deployment's default brain, and nothing
checked whether the brain that ran a task spoke the same model vocabulary. Two
paths asked a related question and neither asked the right one: the fallback
crossed **unconditionally**, and the room-pin / `source_type_overrides` paths
never crossed at all.

ISSUE-418 removed the second half structurally — a request carries a genuine
per-task pin or nothing, so an unpinned task reaches its brain's own default
with no namespace reasoning involved. What is left is the first half, and it is
wrong in the opposite direction: `claude_code -> tmux_claude` is a move *within*
the `anthropic` namespace, running the same `claude` binary, so `claude-opus-5`
is valid on the target — and the fallback dropped it anyway and told the user
their pin had been dropped, for no reason.

Measured before the fix, both directions:

    claude_code -> native       ns=openai_compat  dropped_pin='claude-opus-5'
    claude_code -> tmux_claude  ns=anthropic      dropped_pin='claude-opus-5'

The second line is the defect. The first must stay exactly as it is, which is
why every crossing test here has a same-namespace twin.
"""

import logging

import pytest

from istota.brain import (
    KNOWN_BRAIN_KINDS,
    make_brain,
    model_namespace_for_kind,
)
from istota.config import BrainConfig, Config
from istota.executor import (
    _pin_origin_namespace,
    _request_model,
    _resolve_crossing_model_effort,
)


class _Task:
    """Only the fields the resolver and the origin rule read.

    ``model_namespace`` defaults to None — "not recorded" — so every case in
    this file goes on exercising the inference it was written for. The recorded
    path is ISSUE-420's and is covered in
    `tests/test_brain_pin_origin_recorded.py`.
    """

    def __init__(self, model="", effort="", brain=None, source_type="",
                 model_namespace=None):
        self.model = model
        self.effort = effort
        self.brain = brain
        self.source_type = source_type
        self.model_namespace = model_namespace


def _brain(kind):
    return make_brain(BrainConfig(kind=kind))


class TestTheNamespaceLookup:
    """`model_namespace_for_kind` — a lookup, never a construction.

    Three sites answered this by building a brain: `commands._model_namespace`,
    `web_app._brain_catalogue` (once per known kind, on every catalogue fetch)
    and the executor. Constructing `TmuxClaudeBrain` runs `_warn_cli_version_once`,
    which shells out to the installed `claude` — so asking a pure question about
    a class attribute emitted a `tmux_brain cli_version_mismatch` WARNING, and on
    the native kind it builds a provider client.
    """

    def test_the_two_cli_brains_share_one_namespace(self):
        assert model_namespace_for_kind("claude_code") == "anthropic"
        assert model_namespace_for_kind("tmux_claude") == "anthropic"

    def test_native_reads_its_own(self):
        assert model_namespace_for_kind("native") == "openai_compat"

    def test_an_unknown_kind_is_not_established(self):
        """`None` means "could not be answered", never "the same namespace"."""
        assert model_namespace_for_kind("nope") is None
        assert model_namespace_for_kind("") is None
        assert model_namespace_for_kind(None) is None

    @pytest.mark.parametrize("kind", sorted(KNOWN_BRAIN_KINDS))
    def test_it_agrees_with_the_brain_it_describes(self, kind):
        """The lookup and the construction must not drift.

        Parametrized over every buildable kind, so a fourth brain that forgets
        the table fails here rather than in a namespace comparison that reads
        `None` and silently clears a pin.
        """
        assert model_namespace_for_kind(kind) == _brain(kind).model_namespace

    def test_it_constructs_nothing(self, monkeypatch):
        """The property this exists for, asserted rather than assumed.

        Both doors are shut, because patching `make_brain` alone cannot fail
        for the regression this names: the function reads class attributes and
        imports `TmuxClaudeBrain` directly, so a revert that constructs it
        *without* going through `make_brain` — which is the thing that shells
        out to `claude` — would stay green.
        """
        import istota.brain as brain_pkg
        from istota.brain import tmux_claude as tmux_mod

        def _boom(*a, **k):
            raise AssertionError("model_namespace_for_kind constructed a brain")

        monkeypatch.setattr(brain_pkg, "make_brain", _boom)
        monkeypatch.setattr(tmux_mod.TmuxClaudeBrain, "__init__", _boom)
        for kind in sorted(KNOWN_BRAIN_KINDS):
            assert model_namespace_for_kind(kind)


class TestAMoveWithinANamespaceKeepsThePin:
    """The reported defect. `claude_code -> tmux_claude` is not a crossing."""

    def test_a_canonical_id_survives(self):
        model, effort, dropped = _resolve_crossing_model_effort(
            _Task("claude-opus-5"), Config(), _brain("tmux_claude"), "high",
            origin_namespace="anthropic",
        )
        assert model == "claude-opus-5"
        assert dropped is None

    def test_the_effort_survives_with_it(self):
        _model, effort, _dropped = _resolve_crossing_model_effort(
            _Task("claude-opus-5"), Config(), _brain("tmux_claude"), "high",
            origin_namespace="anthropic",
        )
        assert effort == "high"

    def test_no_note_is_logged(self, caplog):
        """The user was being told about a drop that did not need to happen."""
        with caplog.at_level(logging.INFO):
            _resolve_crossing_model_effort(
                _Task("claude-opus-5"), Config(), _brain("tmux_claude"), "high",
                origin_namespace="anthropic",
            )
        assert not [r for r in caplog.records if "non-portable" in r.getMessage()]

    def test_a_provider_shortcut_resolves_rather_than_dropping(self):
        """`opus` is non-portable and still valid in its own namespace.

        It comes back **resolved**, not raw: the primary path is
        `brain.resolve_model_name(task.model)`, and a non-crossing pin has to
        come out exactly as that would have produced it. Returning the raw
        string hands the CLI an alias it does not accept.
        """
        from istota.brain.claude_code import OPUS

        model, _e, dropped = _resolve_crossing_model_effort(
            _Task("opus"), Config(), _brain("tmux_claude"), "",
            origin_namespace="anthropic",
        )
        assert model == OPUS
        assert dropped is None

    def test_a_portable_alias_keeps_its_effort_within_a_namespace(self):
        """The regression the raw-return caused, pinned.

        `smart` used to go through `resolve_alias` and arrive as a canonical id
        plus the alias's own effort. Handing back the raw string dropped both —
        on a previously correct path, taken exactly when the primary brain is
        already down.
        """
        from istota.brain._roles import set_alias_overrides

        set_alias_overrides({"smart": "opus:high"})
        try:
            model, effort, dropped = _resolve_crossing_model_effort(
                _Task("smart"), Config(), _brain("tmux_claude"), "",
                origin_namespace="anthropic",
            )
            assert model != "smart", "the alias reached the wire unresolved"
            assert effort == "high"
            assert dropped is None
        finally:
            set_alias_overrides({})

    def test_an_effort_suffix_is_not_passed_through(self):
        """`resolve_model_name` strips the modifier; the raw string did not."""
        model, _e, _d = _resolve_crossing_model_effort(
            _Task("claude-opus-5:high"), Config(), _brain("tmux_claude"), "",
            origin_namespace="anthropic",
        )
        assert model == "claude-opus-5"


class TestAMoveAcrossANamespaceStillDrops:
    """The half that was already right, and must stay byte-identical."""

    def test_a_non_portable_pin_drops(self):
        model, _e, dropped = _resolve_crossing_model_effort(
            _Task("claude-opus-5"), Config(), _brain("native"), "high",
            origin_namespace="anthropic",
        )
        assert model == ""
        assert dropped == "claude-opus-5"

    def test_a_portable_alias_re_resolves(self):
        """A portable intent crosses the boundary and is not reported dropped."""
        model, _e, dropped = _resolve_crossing_model_effort(
            _Task("smart"), Config(), _brain("native"), "",
            origin_namespace="anthropic",
        )
        assert dropped is None

    def test_an_unpinned_task_carries_nothing_either_way(self):
        for kind in ("native", "tmux_claude"):
            model, _e, dropped = _resolve_crossing_model_effort(
                _Task(""), Config(), _brain(kind), "high",
                origin_namespace="anthropic",
            )
            assert (model, dropped) == ("", None)


class TestAnUnknownOriginIsTreatedAsACrossing:
    """`None` must never compare equal to a real namespace.

    The safe direction is the one `commands._clear_pin_across_namespaces`
    already takes: a pin whose portability could not be established is dropped
    rather than sent to a wire that may not accept it.
    """

    def test_an_unknown_origin_drops_a_non_portable_pin(self):
        model, _e, dropped = _resolve_crossing_model_effort(
            _Task("claude-opus-5"), Config(), _brain("tmux_claude"), "",
            origin_namespace=None,
        )
        assert model == ""
        assert dropped == "claude-opus-5"

    def test_two_unknowns_do_not_compare_equal(self):
        """Both ends unknown is still not evidence they match."""
        model, _e, dropped = _resolve_crossing_model_effort(
            _Task("claude-opus-5"), Config(), _brain("tmux_claude"), "",
            origin_namespace=None,
        )
        assert dropped == "claude-opus-5"


class TestTheClearingRuleAnswersOnTheKind:
    """`commands._model_namespace` is a lookup now (ISSUE-417).

    It used to answer by constructing the brain, so a *known* kind this host
    could not currently build — a misconfigured `[brain.native]`, a missing
    `claude` — read as an unknown namespace and `_clear_pin_across_namespaces`
    dropped the room's model pin. It now answers with the kind's namespace, so a
    same-namespace change keeps the pin.

    The narrowing is deliberate: a namespace is a property of the kind rather
    than of this host's ability to construct the brain, and clearing a still
    valid pin costs the user their setting for a reason that has nothing to do
    with portability. `web_app._brain_catalogue` publishes the same answer, so
    the modal's prediction and the server's rule stay in step.
    """

    def test_a_known_kind_that_cannot_be_built_still_has_a_namespace(
        self, monkeypatch,
    ):
        import istota.commands as cmd
        from istota.config import BrainConfig

        def _boom(_cfg):
            raise RuntimeError("no claude on this host")

        monkeypatch.setattr(cmd, "make_brain", _boom)
        assert cmd._model_namespace(BrainConfig(kind="tmux_claude")) == "anthropic"

    def test_an_unknown_kind_is_still_not_established(self):
        import istota.commands as cmd
        from istota.config import BrainConfig

        assert cmd._model_namespace(BrainConfig(kind="nope")) is None


# A native model unmistakably not an anthropic id: an assertion below that
# happened to hold under both namespaces would say nothing.
NATIVE_MODEL = "qwen3-testbed-max"


def _routed_config(**brain_kwargs):
    """`claude_code` by default, with two lanes routed to native."""
    return Config(
        brain=BrainConfig(
            kind="claude_code",
            source_type_overrides={"scheduled": "native", "email": "native"},
            **brain_kwargs,
        )
    )


class TestTheOriginOfAnUnpinnedTask:
    """The unpinned branch asks the router, not `[brain] kind` (ISSUE-419).

    Both producers of a pin on a task with no `tasks.brain` resolve the name
    against the *lane's* brain before writing it: `check_scheduled_jobs` goes
    through `resolve_brain_kind("scheduled", ...)`, and a `!model` prefix in a
    room that pinned no brain goes through `commands.brain_for_room`, which is
    the same call. Reading `config.brain.kind` here answered with a namespace
    nothing had written in, so every pin on a routed lane read as a crossing.
    """

    def test_a_routed_lane_answers_with_the_routed_namespace(self):
        assert (
            _pin_origin_namespace(_Task("m", source_type="scheduled"), _routed_config())
            == "openai_compat"
        )

    def test_the_rule_is_the_lane_and_not_the_cron_lane(self):
        """Deliberately not `scheduled`.

        The branch is the one taken whenever `tasks.brain` is NULL, so this
        reaches email, briefing, heartbeat, subtask and web alike. That is the
        correct answer for all of them and is the point of the change rather
        than a surprise from it, which is what this case is here to say.
        """
        assert (
            _pin_origin_namespace(_Task("m", source_type="email"), _routed_config())
            == "openai_compat"
        )

    def test_an_unrouted_lane_on_a_routed_deployment_reads_the_base_kind(self):
        """The routing is per lane, so a lane with no entry is unchanged."""
        assert (
            _pin_origin_namespace(_Task("m", source_type="talk"), _routed_config())
            == "anthropic"
        )

    def test_a_deployment_with_no_routing_at_all_is_unchanged(self):
        assert (
            _pin_origin_namespace(
                _Task("m", source_type="scheduled"),
                Config(brain=BrainConfig(kind="claude_code")),
            )
            == "anthropic"
        )

    def test_a_non_string_source_type_does_not_raise(self):
        """`tasks.source_type` is TEXT with no CHECK and SQLite is dynamically
        typed, so `resolve_brain_kind`'s opening `.strip()` is reachable with a
        number on the row. The residue is the base kind — the answer this
        returned before routing was consulted — and never an exception into a
        request build.
        """
        task = _Task("m")
        task.source_type = 7
        assert _pin_origin_namespace(task, _routed_config()) == "anthropic"


class TestAPinnedTaskStillReadsTheColumn:
    """Unchanged by Stage 4, and the second case is a deferred question.

    A room's pin is written into `tasks.brain` at creation and `rooms.model` was
    resolved against that kind, so the column is the origin even where the
    routing would say otherwise.
    """

    def test_the_pin_outranks_the_lanes_routing(self):
        assert (
            _pin_origin_namespace(
                _Task("m", brain="native", source_type="talk"),
                _routed_config(room_selectable=["native"]),
            )
            == "openai_compat"
        )

    def test_a_pin_the_allowlist_now_refuses_still_reads_as_its_own_namespace(self):
        """The inference, which is what a row with no recorded namespace gets.

        ISSUE-420 fixed the live defect by recording the namespace beside the
        pin rather than by changing this branch, so what is asserted here is the
        fallback for a row written before that column existed — a real
        population on any upgraded deployment, and one whose answer deliberately
        did not move. A row that *does* carry a namespace never reaches this
        line; see `tests/test_brain_pin_origin_recorded.py`.

        With `native` dropped from `room_selectable`, `resolve_brain_kind`
        refuses the pin and the task runs `claude_code`, while this answers
        `openai_compat` and the crossing rule drops the model. For a
        `rooms.model` written *while* the pin was still admitted that is the
        documented ISSUE-417 intent and the right answer.

        For a **cron job it is never right**, because `check_scheduled_jobs`
        re-resolves the model on every fire against the *resolved* kind while
        writing the *raw* pin into `tasks.brain`. So `[[jobs]] brain = "native"`
        plus `model = "smart"` on a deployment that has not allowlisted native —
        and `room_selectable` is empty by default — stores an anthropic id
        beside a `native` pin, every fire, and has it dropped here, every fire.
        The job runs the deployment default model and nothing says so.

        Reading the *admitted* pin instead was rejected: it collapses this
        function to the target brain's own namespace and makes the primary-path
        crossing rule inert for pinned tasks too. Recording the namespace at the
        producer keeps both cases — a pin written while the kind was admitted
        and one written after — without this branch having to tell them apart,
        which it cannot, because they leave identical rows.
        """
        assert (
            _pin_origin_namespace(
                _Task("m", brain="native", source_type="talk"),
                _routed_config(room_selectable=[]),
            )
            == "openai_compat"
        )


class TestARoutedLanesPinIsNoLongerDroppedAsACrossing:
    """The outcome the origin rule exists for, asserted through `_request_model`.

    A cron job's `model` is resolved by the brain the lane runs, so on a
    `source_type_overrides` deployment the stored id is native's. Reading the
    base kind's namespace as the origin made that a crossing, found the id
    non-portable and dropped it — the operator's pin replaced by native's own
    default, logged at INFO and surfaced nowhere.
    """

    def test_a_routed_cron_pin_reaches_the_request(self):
        model = _request_model(
            _Task(NATIVE_MODEL, source_type="scheduled"),
            _routed_config(),
            _brain("native"),
        )
        assert model == NATIVE_MODEL

    def test_the_same_holds_on_a_routed_lane_that_is_not_cron(self):
        model = _request_model(
            _Task(NATIVE_MODEL, source_type="email"),
            _routed_config(),
            _brain("native"),
        )
        assert model == NATIVE_MODEL

    def test_nothing_is_logged_as_dropped(self, caplog):
        with caplog.at_level(logging.INFO):
            _request_model(
                _Task(NATIVE_MODEL, source_type="scheduled"),
                _routed_config(),
                _brain("native"),
            )
        assert not [r for r in caplog.records if "non-portable" in r.getMessage()]

    def test_an_unrouted_lane_on_the_same_deployment_still_drops(self):
        """The half that must not move: `talk` is not routed, so the pin really
        was written in the anthropic namespace and really cannot carry.
        """
        model = _request_model(
            _Task("claude-opus-5", source_type="talk"),
            _routed_config(),
            _brain("native"),
        )
        assert model == ""


class TestTheProducersThatDoNotMeetThePremise:
    """Residues of the lane rule, recorded rather than guarded.

    The unpinned branch assumes a name in `tasks.model` was resolved through the
    lane's own brain. Three producers do not do that, and each ends with a
    foreign id handed to a brain that passes an unknown name straight through.
    None is introduced by the lane rule — each was previously dropped, which was
    the right outcome from a rule that was wrong about why — and each is fixed at
    the producer rather than here.
    """

    def test_a_room_model_written_from_one_surface_is_read_on_another(self):
        """`rooms.model` is one column shared by every surface bound to a room.

        It is written against the writing surface's lane
        (`commands.brain_for_room(..., ctx.surface)`,
        `web_app._brain_for_room_token(token, "web")`) and was read here against
        the inbound one, so a room whose model was set from the web UI and whose
        next message arrived over Talk disagreed with itself wherever those two
        lanes route to different kinds.

        **Fixed by ISSUE-420's recorded namespace**, which is why this producer
        is no longer in the same state as the two below it: the writing surface
        stores the namespace it resolved in and `record_inbound` freezes it onto
        the task, so the inbound surface reads a fact. What is still asserted
        here is the residue for a row written *before* that column — the value
        was resolved in `anthropic` (web is unrouted, so it ran the base kind)
        and this answers `openai_compat`, so the id reaches native's wire
        unchallenged. Recorded, not asserted as correct.
        """
        config = Config(
            brain=BrainConfig(
                kind="claude_code", source_type_overrides={"talk": "native"},
            )
        )
        talk_task = _Task("claude-opus-5", source_type="talk")
        assert _pin_origin_namespace(talk_task, config) == "openai_compat"
        assert _request_model(talk_task, config, _brain("native")) == "claude-opus-5"

        # The same room once its namespace has been recorded: the fact wins, no
        # crossing is read, and the id the user chose survives.
        recorded = _Task(
            "claude-opus-5", source_type="talk", model_namespace="anthropic",
        )
        assert _pin_origin_namespace(recorded, config) == "anthropic"

    def test_a_subtask_inherits_a_model_resolved_in_the_parents_lane(self):
        """`scheduler_deferred` copies the parent's `model` onto a `subtask` row
        with `brain=task.brain`, which is NULL when the parent was unpinned. The
        child then reads its own lane, which is not where the name was written.
        """
        config = Config(
            brain=BrainConfig(
                kind="claude_code", source_type_overrides={"subtask": "native"},
            )
        )
        child = _Task("claude-opus-5", source_type="subtask")
        assert _pin_origin_namespace(child, config) == "openai_compat"
