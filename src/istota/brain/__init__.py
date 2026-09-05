"""Brain abstraction — model invocation behind a single protocol.

The executor builds the prompt, env, and sandbox configuration, then hands
a BrainRequest to a Brain implementation. Brains own everything from
"compose the model call" through "produce a result + trace", *and* own
their own model namespace — canonical IDs, provider aliases, and how role
aliases like ``smart`` map to a real model. Consumers never reach into a
brain module's tables; they go through ``make_brain(config.brain)`` and
call ``.resolve_alias`` / ``.resolve_model_name`` / ``.list_aliases``.

Operator alias overrides (``[models.aliases]`` TOML) are provider-agnostic
and live globally in ``_roles.py`` — each brain consults the override
table at resolution time and routes the override target through its own
alias table.

Phase 1 ships a single brain (ClaudeCodeBrain) that wraps the `claude`
CLI. Future phases add direct-HTTP brains (OpenRouter, Anthropic) without
any change to the executor's per-task orchestration.
"""

from ._events import (
    ContextManagementEvent,
    ResultEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolEndEvent,
    ToolProgressEvent,
    ToolUseEvent,
    make_stream_parser,
    parse_stream_line,
)
import dataclasses
import logging
from collections.abc import Iterable

from ._aliases import CANONICAL_ROLES, EFFORT_LEVELS, is_portable_alias, split_effort
from ._fallback import (
    COOLDOWN_STOP_REASONS,
    PrimaryAvailabilityBreaker,
    TRIGGER_STOP_REASONS,
    effective_fallback_kind,
    get_availability_breaker,
    primary_brain_unavailable,
    report_brain_result,
    reset_availability_breaker,
)
from ._postures import (
    POSTURE_FAIL_CLEAN,
    POSTURE_PIN,
    POSTURE_SKIP,
    REGISTRY as TASK_POSTURES,
    TaskPosture,
    postures_by_name as task_postures_by_name,
)
from ._roles import (
    RoleTarget,
    get_alias_override_target,
    get_alias_overrides,
    get_portable_alias_names,
    set_alias_overrides,
)
from ._types import Brain, BrainConfig, BrainRequest, BrainResult, ImageInput
from .claude_code import (
    ClaudeCodeBrain,
    is_usage_limit_banner,
    is_usage_limit_error,
)
from .native import NativeBrain

logger = logging.getLogger(__name__)

# Every brain kind ``make_brain`` knows how to build. The routing resolver
# validates override targets against this set so a typo in
# ``[brain.source_type_overrides]`` falls back to the base kind instead of
# raising and wedging the task.
KNOWN_BRAIN_KINDS = frozenset({"claude_code", "native", "tmux_claude"})

# Display names for the kinds above, for a surface that offers a brain to a
# person rather than to code — today `/chat/commands`' `selectable_brains`.
# Here rather than in `web_app` so a second surface cannot invent its own
# spelling of the same three kinds. `web/src/routes/admin/+page.svelte` keeps a
# client-side copy for the admin payloads, which carry a bare kind.
BRAIN_KIND_LABELS = {
    "claude_code": "Claude Code",
    "native": "Native",
    "tmux_claude": "Tmux Claude",
}


BRAIN_CONFIG_BLOCK = {
    "claude_code": "claude_code",
    "native": "native",
    "tmux_claude": "tmux",
}
"""Which nested config block holds each kind's own settings.

``tmux_claude`` reads ``[brain.tmux]``, which is the one place kind and block
name disagree — the reason this is a table rather than `getattr(cfg, kind)`.
"""


def configured_default_model_effort(brain_config) -> tuple[str, str]:
    """The (model, effort) the configured kind runs when nothing pins one.

    A **lookup, not a construction** (ISSUE-418). The same answer is available
    from `make_brain(brain_config).default_model`, and that is what a caller
    already holding a brain should use — `web_app._admin_models_section` does,
    since it has constructed one anyway. This is for a caller that wants only to
    *report* the default and would otherwise construct a brain to ask: the
    scheduler's log-channel line, which runs per task, where building one costs
    a `claude` CLI version probe (a `tmux_brain cli_version_mismatch` WARNING
    each time — exactly the cost `web_app`'s per-kind `make_brain` loop already
    pays) and, for native, a provider client.

    Returns the value **unresolved**: it may be an alias, and resolving it needs
    the brain's own table. A caller rendering it for a human wants what the
    operator wrote; a caller putting it on the wire should go through the brain.

    Never raises: an unknown kind or a config missing the block answers
    ``("", "")``, which reads as "the backend's own default" everywhere.
    """
    block = BRAIN_CONFIG_BLOCK.get(getattr(brain_config, "kind", ""))
    if block is None:
        return ("", "")
    cfg = getattr(brain_config, block, None)
    model = (getattr(cfg, "model", "") or "").strip()
    effort = (getattr(cfg, "effort", "") or "").strip()
    return (model, effort)


def model_namespace_for_kind(kind) -> str | None:
    """The model namespace a brain kind resolves alias names in.

    A **lookup, not a construction** (ISSUE-417). `model_namespace` is a class
    attribute, so answering this needs no instance — and building one to ask is
    not free: `TmuxClaudeBrain.__init__` runs `_warn_cli_version_once`, which
    shells out to the installed `claude` and emits a
    `tmux_brain cli_version_mismatch` WARNING, so a pure question about a
    constant produced operator-facing noise. `web_app._brain_catalogue` asked it
    once per known kind on every catalogue fetch.

    Structured as `make_brain`'s ladder rather than a dict, so a fourth kind is
    added in two adjacent places and a class that has moved cannot leave a stale
    literal behind. `tmux_claude` is imported lazily for the reason `make_brain`
    imports it lazily.

    ``None`` for anything unbuildable, and every caller must read that as **not
    established** rather than as "the same namespace" — the safe direction for
    the crossing rule is to drop a pin whose portability could not be settled.
    Takes ``object``: callers pass a value off a database row or an argparse
    namespace, and this never raises.
    """
    if kind == "claude_code":
        return ClaudeCodeBrain.model_namespace
    if kind == "native":
        return NativeBrain.model_namespace
    if kind == "tmux_claude":
        from .tmux_claude import TmuxClaudeBrain

        return TmuxClaudeBrain.model_namespace
    return None


def make_brain(brain_config: BrainConfig) -> Brain:
    """Construct a brain instance from config.

    Raises ValueError on unknown brain.kind so misconfiguration fails loud
    at startup rather than silently picking the wrong implementation.
    """
    kind = brain_config.kind
    if kind == "claude_code":
        return ClaudeCodeBrain(getattr(brain_config, "claude_code", None))
    if kind == "native":
        from .native import NativeBrain

        return NativeBrain(brain_config.native)
    if kind == "tmux_claude":
        from .tmux_claude import TmuxClaudeBrain

        return TmuxClaudeBrain(getattr(brain_config, "tmux", None))
    raise ValueError(f"Unknown brain kind: {kind!r}")


def room_selectable_kinds(brain_config) -> frozenset[str]:
    """The brain kinds a room may pin on this deployment.

    ``[brain] room_selectable`` intersected with the kinds ``make_brain`` can
    build. Nothing else: a pinned room does not fall back, so there is no
    fallback kind to exclude — a room pinned to the kind the deployment already
    falls back to has nothing left to collide with.

    Never raises. A name that is not a buildable kind is dropped rather than
    offered to a user whose room would then fail to start, and a malformed
    setting yields the empty set, which is the safe direction: nothing is
    selectable.
    """
    configured = getattr(brain_config, "room_selectable", None) or []
    if isinstance(configured, (str, bytes)) or not isinstance(configured, Iterable):
        return frozenset()
    return frozenset(
        name for name in (str(entry).strip() for entry in configured)
        if name in KNOWN_BRAIN_KINDS
    )


def reachable_brain_kinds(brain_config) -> frozenset[str]:
    """Every brain kind a task on this deployment could run under.

    The base ``kind``, the ``[brain.source_type_overrides]`` targets and
    ``room_selectable``, plus the failover target of each kind that still has
    one. Config only: it reads no database, which is what lets the checks built
    on it stay pure functions of the loaded config. Asking the rooms table
    instead would make them database-dependent and would still answer wrongly
    for a room nobody has used yet; the operator's allowlist is what makes the
    answer knowable before any room exists. The cost is stated rather than
    hidden: allowlisting a kind gets that kind's checks even where no room has
    selected it, which is the safe direction.

    The failover fold runs **per kind** rather than once against
    ``brain_config``, because ``effective_fallback_kind`` reads a *resolved*
    config — it drops a target equal to the kind being resolved, so the answer
    can differ between the base kind and a routed one. As the two functions
    stand today the folded set comes out the same either way, since the only
    value the fold can yield is the single configured ``fallback`` and it is
    dropped exactly for the kind that already contributes itself. The per-kind
    shape is what keeps that an arithmetic coincidence rather than a dependency:
    a fallback rule that branches on kind again would break a single evaluation
    silently and leave a check SKIPped, with the operator learning about a
    missing binary from a failed task.

    ``room_selectable`` is deliberately outside the fold. An admitted room
    override clears ``fallback``, so a pinned room contributes its own kind and
    no failover target.

    The base ``kind`` is the one entry **not** filtered against
    ``KNOWN_BRAIN_KINDS``, deliberately: it is what the deployment says it will
    run, and a name ``make_brain`` cannot build is that function's ``ValueError``
    at start-up rather than something to silently drop here. Reporting it is
    what lets a check say "reachable: claude-kode" instead of asserting the
    deployment is native.

    Never raises, and the guards are broad on purpose. Two of the three reads
    reach code this module does not own — ``effective_fallback_kind`` coerces
    neither ``fallback`` nor ``kind``, and ``str(value)`` runs whatever
    ``__str__`` a mapping value brought with it — so a narrow ``except`` here is
    a contract that holds only for the inputs somebody thought of. A malformed
    config still yields the base kind, which is the safe direction: a check that
    runs where it need not is noise, one that skips where it was needed is a
    missing dependency nothing reports.
    """
    kind = str(getattr(brain_config, "kind", "") or "").strip()
    kinds = {kind} if kind else set()

    overrides = getattr(brain_config, "source_type_overrides", None) or {}
    try:
        targets = [str(value).strip() for value in overrides.values()]
    except Exception:  # noqa: BLE001 — a routing read must not raise into a check
        targets = []
    kinds |= {target for target in targets if target in KNOWN_BRAIN_KINDS}

    failover: set[str] = set()
    for one in kinds:
        # `replace` and the fallback read are inside one guard because the
        # second is where the raise actually comes from: `replace` cannot fail
        # on a real `BrainConfig` (no `__post_init__`, no `init=False` field),
        # while `effective_fallback_kind` calls `.strip()` on whatever
        # `fallback` holds. Note `replace` copies shallowly, so `routed` shares
        # every nested block with the original — read-only here, and it must
        # stay that way.
        try:
            routed = dataclasses.replace(brain_config, kind=one)
            target = effective_fallback_kind(routed)
        except Exception:  # noqa: BLE001 — same contract
            continue
        if target in KNOWN_BRAIN_KINDS:
            failover.add(target)

    return frozenset(kinds | failover | room_selectable_kinds(brain_config))


# Routing refusals already logged this process, so a static misconfiguration
# says so once instead of at caller rate (ISSUE-422). Every refusal below
# depends on a stored pin and the operator's config, neither of which changes
# between calls, so an undeduped line repeats for as long as the condition
# lasts: about 1440 a day for a `* * * * *` cron job with a refused pin, times
# however many times one task resolves its brain.
#
# The sentinel for "the cap notice has been logged" lives in the same set so
# one `clear()` resets the whole latch. It cannot collide with a real key,
# because `arm` is always one of the non-empty literals below.
_WARNED_REFUSALS: set[tuple[str, str, str]] = set()
_CAP_NOTICE_KEY = ("", "", "")

# `pinned` comes off `scheduled_jobs.brain`, a plain string field CRON.md can
# write, so both bounds are needed and neither implies the other: the count
# stops one durable entry per attacker-chosen value, and the per-axis width
# stops a bounded number of unbounded entries. The width also bounds the
# *pinned kind* as it is logged, since 256 unbounded lines fill a disk as
# surely as 1440 short ones — deliberately only that axis, because it is the
# model-writable one. `source_type` is framework-set from a small vocabulary
# and stays raw in the message; it is truncated in the key alone, which is
# what stops a long one from minting keys. Two values sharing a truncated
# prefix collapse to one key and one line, the deliberate cost of that bound.
#
# The count is one budget across all three arms rather than three. A flood on
# one arm therefore silences a condition first seen on another until restart —
# accepted, because reaching it takes 256 distinct admin-authored values while
# the legitimate ceiling is three kinds by eleven source types, and because
# the refusal itself is unaffected either way: only the log line goes.
_WARNED_REFUSAL_CAP = 256
_REFUSAL_SHOWN_CHARS = 80


def _as_text(value: object) -> str:
    """``value`` as a string, never raising.

    Takes ``object`` and coerces, for the reason ``reachable_brain_kinds``
    guards its own read of the same mapping: ``source_type_overrides`` is
    stringified by the config hook only on the ``load_config`` path, so a
    ``BrainConfig`` built any other way carries whatever TOML can spell.
    ``str(value)`` runs whatever ``__str__`` came with the value, which is
    what the guard is for — an exception here would leave ``resolve_brain_kind``
    raising over a log line, which is the failure mode it exists to prevent.
    """
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — a routing read must not raise into a task
        return f"<unprintable {type(value).__name__}>"


def _shown(value: object) -> str:
    """``_as_text`` bounded, marked where it was cut.

    For a dedup key and for a log line, never for a *lookup* key: truncating
    one would let an over-long ``source_type`` match an override it does not
    name.
    """
    text = _as_text(value)
    if len(text) <= _REFUSAL_SHOWN_CHARS:
        return text
    return text[: _REFUSAL_SHOWN_CHARS - 3] + "..."


def _refusal_is_unreported(arm: str, kind, source_type) -> bool:
    """Whether this refusal has not been logged yet in this process.

    ``arm`` is which refusal fired, and it is part of the key rather than
    decoration: a refused pin falls through to the source-type layer, so one
    call can refuse the same name twice for two different reasons with two
    different remedies, and keying on the name alone would silence the second.

    No lock. The scheduler resolves brains on worker threads, and the two
    outcomes of a race are one duplicate line or a cap overshot by a few —
    both cheaper than a lock on a resolution path, and the same trade
    ``scheduler._warn_once`` already makes.
    """
    key = (arm, _shown(kind), _shown(source_type if source_type else ""))
    if key in _WARNED_REFUSALS:
        return False
    if len(_WARNED_REFUSALS) >= _WARNED_REFUSAL_CAP:
        # Going quiet with no explanation would read as the refusals having
        # stopped, which is the opposite of what has happened.
        if _CAP_NOTICE_KEY not in _WARNED_REFUSALS:
            _WARNED_REFUSALS.add(_CAP_NOTICE_KEY)
            logger.warning(
                "brain routing: %d distinct refusals warned about this process; "
                "suppressing further routing refusal warnings until restart",
                _WARNED_REFUSAL_CAP,
            )
        return False
    _WARNED_REFUSALS.add(key)
    return True


def resolve_brain_kind(source_type, brain_config, override: str | None = None):
    """Return the BrainConfig to use for a task with the given source_type.

    Resolution order, highest first::

        override  >  [brain.source_type_overrides][source_type]  >  [brain] kind

    ``override`` is the task's own pinned kind (``tasks.brain``), and it has
    **two** producers: ``rooms.brain``, filled when the task was created, and
    ``scheduled_jobs.brain``, a CRON.md ``[[jobs]] brain`` field passed through
    by ``check_scheduled_jobs`` (ISSUE-419). This function is the sole
    enforcement point for both and deliberately learns nothing about which one
    it is holding — which is why the allowlist it applies is
    ``[brain] room_selectable`` for a job as much as for a room, and why there
    is no second list. The job pin carries a gate this one does not, at sync
    time: CRON.md is model-writable, so ``cron_loader.fj_brain_or_none`` drops
    the field for a non-admin before it ever reaches a row.

    An override wins outright when it names a buildable kind that the operator
    listed in ``[brain] room_selectable``. It sits above the source-type layer
    because the two answer different questions:
    ``source_type_overrides`` is an operator's gradual-rollout knob keyed on a
    lane, while a pin is an explicit human choice about one conversation or one
    job, and an explicit pick a lane rule silently overrode would be
    indistinguishable from a bug.

    Two refusals, each logged at WARNING and each falling through to the
    source-type layer: a kind ``make_brain`` cannot build, and a kind the
    operator does not offer. The second is what makes shortening
    ``room_selectable`` take effect at the next dispatch without anything having
    to rewrite stored rows. Neither ever wedges a task, which is the same
    contract an unknown ``source_type_overrides`` target already has.

    All three WARNINGs — those two and the unknown override target below — are
    logged **once per process per distinct condition** (``_refusal_is_unreported``).
    Each condition is a static fact about a stored row and the operator's
    config, so the line said nothing new on its second appearance and repeated
    for as long as the misconfiguration lasted.

    "Never wedges a task" is now true of the *types* as well, which it was not:
    a non-string ``source_type`` raised ``AttributeError`` on ``.strip()`` and
    an unhashable override target raised ``TypeError`` from the membership
    test. ``load_config``'s hook stringifies that mapping on both sides, so
    neither is reachable through it — but ``execute_task`` calls this
    unguarded, and ``reachable_brain_kinds`` already concedes the same values
    are untrusted. Both reads go through ``_as_text`` now.

    **An admitted override also turns availability failover off**, by returning
    a config with ``fallback`` cleared. The room, or the job, named *this*
    brain, so a task that cannot run on it fails with the primary's own reason
    rather than
    quietly answering from a different model — and the two failover asymmetries
    a routed kind would otherwise inherit disappear with it. The decision is
    made here rather than in the executor because it cannot be inferred
    downstream: a room pinned to the kind that is already the instance default
    resolves to a config equal to the unrouted one, so "was an override
    admitted" has to be recorded at the moment of admission. Pinning the default
    kind therefore still counts as pinning; the alternative is a rule with an
    exception, which is harder to explain and no less surprising.

    Returns ``brain_config`` unchanged (same object) when nothing applies, so
    callers can cheaply detect the common no-routing case.
    """
    pinned = (override or "").strip()
    if pinned:
        if pinned not in KNOWN_BRAIN_KINDS:
            if _refusal_is_unreported("unknown-pin", pinned, source_type):
                logger.warning(
                    "brain routing: unknown kind %r pinned for source_type %r; "
                    "ignoring it and resolving from config",
                    _shown(pinned), source_type,
                )
        elif pinned not in room_selectable_kinds(brain_config):
            if _refusal_is_unreported("unlisted-pin", pinned, source_type):
                logger.warning(
                    "brain routing: kind %r pinned for source_type %r is not in "
                    "[brain] room_selectable; ignoring it and resolving from "
                    "config",
                    _shown(pinned), source_type,
                )
        else:
            return dataclasses.replace(brain_config, kind=pinned, fallback="")

    overrides = getattr(brain_config, "source_type_overrides", None) or {}
    # Both reads are coerced, because neither value's type is this function's
    # to assume and both used to raise out of it. `load_config`'s hook
    # stringifies this mapping on both sides, so a `BrainConfig` that came
    # through it is unaffected — but one built any other way carries whatever
    # TOML can spell, and `execute_task` calls this unguarded. A non-string
    # `source_type` raised `AttributeError` on `.strip()`, and an *unhashable*
    # target raised `TypeError` from the membership test below, both out of a
    # function whose stated contract is that a routing typo never wedges a
    # task. `reachable_brain_kinds` reads the same mapping behind the same
    # concession, with a `try` instead. Coerced *after* the falsy test, so an
    # empty list still means "no override" rather than becoming `"[]"`.
    # `_as_text`, not `_shown`: truncating a lookup key would let an
    # over-long `source_type` match an override it does not name.
    target = overrides.get(_as_text(source_type if source_type else "").strip())
    if not target or target == brain_config.kind:
        return brain_config
    if not isinstance(target, str):
        target = _shown(target)
    if target not in KNOWN_BRAIN_KINDS:
        if _refusal_is_unreported("unknown-override", target, source_type):
            logger.warning(
                "brain routing: unknown kind %r mapped for source_type %r; "
                "falling back to %r",
                _shown(target), source_type, brain_config.kind,
            )
        return brain_config
    return dataclasses.replace(brain_config, kind=target)


__all__ = [
    "Brain",
    "BrainConfig",
    "BrainRequest",
    "BrainResult",
    "CANONICAL_ROLES",
    "ClaudeCodeBrain",
    "EFFORT_LEVELS",
    "COOLDOWN_STOP_REASONS",
    "ContextManagementEvent",
    "ImageInput",
    "BRAIN_KIND_LABELS",
    "KNOWN_BRAIN_KINDS",
    "NativeBrain",
    "POSTURE_FAIL_CLEAN",
    "POSTURE_PIN",
    "POSTURE_SKIP",
    "PrimaryAvailabilityBreaker",
    "RoleTarget",
    "TASK_POSTURES",
    "TRIGGER_STOP_REASONS",
    "TaskPosture",
    "is_portable_alias",
    "is_usage_limit_banner",
    "is_usage_limit_error",
    "primary_brain_unavailable",
    "report_brain_result",
    "reset_availability_breaker",
    "ResultEvent",
    "StreamEvent",
    "TextDeltaEvent",
    "TextEvent",
    "ThinkingDeltaEvent",
    "ThinkingEvent",
    "ToolEndEvent",
    "ToolProgressEvent",
    "ToolUseEvent",
    "effective_fallback_kind",
    "get_alias_override_target",
    "get_alias_overrides",
    "get_availability_breaker",
    "get_portable_alias_names",
    "configured_default_model_effort",
    "make_brain",
    "model_namespace_for_kind",
    "make_stream_parser",
    "parse_stream_line",
    "reachable_brain_kinds",
    "resolve_brain_kind",
    "room_selectable_kinds",
    "set_alias_overrides",
    "split_effort",
    "task_postures_by_name",
]
