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


def make_brain(brain_config: BrainConfig) -> Brain:
    """Construct a brain instance from config.

    Raises ValueError on unknown brain.kind so misconfiguration fails loud
    at startup rather than silently picking the wrong implementation.
    """
    kind = brain_config.kind
    if kind == "claude_code":
        return ClaudeCodeBrain()
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


def resolve_brain_kind(source_type, brain_config, override: str | None = None):
    """Return the BrainConfig to use for a task with the given source_type.

    Resolution order, highest first::

        override  >  [brain.source_type_overrides][source_type]  >  [brain] kind

    ``override`` is the task's own pinned kind (``tasks.brain``, filled from
    ``rooms.brain`` when the task was created). It wins outright when it names a
    buildable kind that the operator listed in ``[brain] room_selectable``. It
    sits above the source-type layer because the two answer different questions:
    ``source_type_overrides`` is an operator's gradual-rollout knob keyed on a
    lane, while a room's brain is an explicit human choice about one
    conversation, and an explicit pick a lane rule silently overrode would be
    indistinguishable from a bug.

    Two refusals, each logged at WARNING and each falling through to the
    source-type layer: a kind ``make_brain`` cannot build, and a kind the
    operator does not offer. The second is what makes shortening
    ``room_selectable`` take effect at the next dispatch without anything having
    to rewrite stored rows. Neither ever wedges a task, which is the same
    contract an unknown ``source_type_overrides`` target already has.

    **An admitted override also turns availability failover off**, by returning
    a config with ``fallback`` cleared. The room named *this* brain, so a task
    that cannot run on it fails with the primary's own reason rather than
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
            logger.warning(
                "brain routing: unknown kind %r pinned for source_type %r; "
                "ignoring it and resolving from config",
                pinned, source_type,
            )
        elif pinned not in room_selectable_kinds(brain_config):
            logger.warning(
                "brain routing: kind %r pinned for source_type %r is not in "
                "[brain] room_selectable; ignoring it and resolving from config",
                pinned, source_type,
            )
        else:
            return dataclasses.replace(brain_config, kind=pinned, fallback="")

    overrides = getattr(brain_config, "source_type_overrides", None) or {}
    target = overrides.get((source_type or "").strip())
    if not target or target == brain_config.kind:
        return brain_config
    if target not in KNOWN_BRAIN_KINDS:
        logger.warning(
            "brain routing: unknown kind %r mapped for source_type %r; "
            "falling back to %r",
            target, source_type, brain_config.kind,
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
    "make_brain",
    "make_stream_parser",
    "parse_stream_line",
    "resolve_brain_kind",
    "room_selectable_kinds",
    "set_alias_overrides",
    "split_effort",
    "task_postures_by_name",
]
