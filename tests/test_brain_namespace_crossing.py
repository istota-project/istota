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
from istota.executor import _resolve_crossing_model_effort


class _Task:
    """Only the two fields the resolver reads."""

    def __init__(self, model="", effort=""):
        self.model = model
        self.effort = effort


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
