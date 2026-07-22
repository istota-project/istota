"""Module-owned shared briefing blocks (shared-kv-curated-content spec, Stage 5).

A *shared block* is a one-block briefing generated **once globally** (no user)
under the reserved ``__system__`` identity. Its rendered section content is
written into the ``shared_kv`` store at namespace ``briefing_shared_blocks`` /
key ``<name>``, where per-user briefings read it via a ``shared_block`` (or
``kv``) source — collapsing the N-way duplicate fetch + synthesis of shared
content (world headlines, a markets summary) to one generation total.

Generation runs sleep-cycle-style: the same user-agnostic gather the per-user
pipeline uses, then a single non-streaming, no-sandbox Brain call for just that
section's content (not the ``{subject, body}`` briefing envelope).

Only user-agnostic source kinds are allowed — ``browse`` (a frontpage is
byte-identical for everyone), ``markets`` (same quotes for everyone), and
``email`` (the *shared/unowned* pool). All other kinds — the personal built-ins
(``calendar``/``todos``/``reminders``/``notes``), ``kv``/``shared_block`` (no
chaining), and **``rss``** (which needs a real feeds user; a shared rss block
would disseminate one person's subscriptions to everyone) — are dropped with a
WARNING at generation time.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from istota.briefings.generate import _render_source
from istota.briefings.models import ALLOWED_SHARED_SOURCE_KINDS
from istota.briefings.sources import GatheredSource, SourceContext, resolve_source


logger = logging.getLogger(__name__)


# The reserved system identity for shared-block generation. Used for both the
# gather SourceContext and the shared_kv `written_by` audit column, so module-
# owned generation has no dependency on any admin being configured.
SYSTEM_IDENTITY = "__system__"

# Source kinds a shared block may use — genuinely user-agnostic only. Canonical
# definition lives in ``briefings.models`` (imported so the web/route/CLI layers
# don't pull this generation module); re-exported here for existing callers.
__all__ = ["ALLOWED_SHARED_SOURCE_KINDS", "SYSTEM_IDENTITY", "run_shared_block"]

# Section-only Brain call: no tools, no streaming, no sandbox (sleep-cycle shape).
_GENERATION_TIMEOUT_SECONDS = 180


def _alert_brain_unavailable(config, label: str, reason: str) -> None:
    """Fire one operator alert when shared-block generation trips the breaker.

    One-shot per cooldown window (``report_brain_result`` returns the reason
    only on the closed→open transition). Surfaces that shared blocks are now
    serving stale content (last-known-good) so the operator knows the morning
    briefing's shared section is N hours behind, not fresh (ISSUE-181).
    """
    try:
        from istota.notifications import send_operator_alert

        send_operator_alert(
            config,
            f"⏸️ Shared block '{label}' paused — primary brain unavailable "
            f"({reason}). Briefings will keep serving the last-known-good "
            f"content until the primary recovers.",
        )
    except Exception:
        logger.debug("shared block brain-unavailable alert failed", exc_info=True)


def _allowed_sources(block_def) -> list[dict]:
    """Return the block's usable sources, dropping user-specific kinds with a
    WARNING. Each source is a ``{"kind", "config"}`` dict."""
    usable: list[dict] = []
    for src in block_def.sources or []:
        if not isinstance(src, dict):
            continue
        kind = src.get("kind", "")
        if kind not in ALLOWED_SHARED_SOURCE_KINDS:
            logger.warning(
                "shared block %r: source kind %r is not user-agnostic; dropping",
                block_def.name, kind,
            )
            continue
        usable.append(src)
    return usable


def _gather_shared(config, sources: list[dict], now: datetime | None) -> list[GatheredSource]:
    """Gather the block's user-agnostic sources concurrently, fail-soft.

    Each worker opens its own framework connection (SQLite conns aren't
    thread-safe) so the email shared-pool resolver has DB access for ownership
    resolution. ``SourceContext.user_id`` is the reserved system identity — none
    of the allowed kinds needs a real user.
    """
    def _one(src: dict) -> GatheredSource:
        kind = src.get("kind", "")
        cfg = src.get("config") or {}
        try:
            from istota import db as framework_db

            with framework_db.get_db(config.db_path) as tconn:
                tctx = SourceContext(
                    app_config=config, user_id=SYSTEM_IDENTITY, conn=tconn, now=now,
                )
                return resolve_source(kind, cfg, tctx)
        except Exception as e:  # noqa: BLE001 — fail-soft, mirrors resolve_source
            logger.warning("shared block gather: %s source failed: %s", kind, e)
            return GatheredSource(
                kind=kind, title=kind, provenance=f"({kind} source error)", ok=False,
            )

    if not sources:
        return []
    # Reassemble by source index, not completion order — a structured block's
    # verbatim concatenation (``_assemble_verbatim``) splices sources in this
    # order, so it must match the configured source order regardless of which
    # worker finishes first. Mirrors the per-user pipeline's slot dict.
    by_index: dict[int, GatheredSource] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
        futures = {pool.submit(_one, s): i for i, s in enumerate(sources)}
        for fut in as_completed(futures):
            by_index[futures[fut]] = fut.result()
    return [by_index[i] for i in range(len(sources))]


def _section_directive(block_def) -> str:
    """The synthesis directive for a ``synthesis`` block.

    ``structured`` shared blocks no longer route through the brain (see
    ``_assemble_verbatim`` / ``run_shared_block``), so the former structured
    branch here is dead for shared blocks and has been removed — the default is
    the synthesis wording.
    """
    if block_def.directive:
        return block_def.directive
    return (
        "Synthesize the sources below into one coherent section. Group related "
        "items and lead with what's new."
    )


def _assemble_verbatim(gathered: list[GatheredSource]) -> str | None:
    """Concatenate a structured block's gathered source text verbatim.

    Sources are reassembled in configured source order (``_gather_shared``
    reassembles its pool results by source index, so a multi-source structured
    block splices deterministically in the order authored). Each source's
    ``text`` is used as-is; a source
    that returned ``items`` instead of ``text`` (no shared source kind does today
    — markets/browse/email all return ``text``) is rendered as a simple bullet
    list so verbatim still has something to splice. Returns ``None`` when nothing
    was produced, so the caller keeps the prior value (last-known-good).
    """
    parts: list[str] = []
    for gs in gathered:
        if not gs.ok or gs.is_empty:
            continue
        if gs.text and gs.text.strip():
            parts.append(gs.text.strip())
        elif gs.items:
            bullets = "\n".join(
                f"- {it.get('title') or it.get('summary') or ''}".rstrip()
                for it in gs.items
                if isinstance(it, dict)
            ).strip()
            if bullets:
                parts.append(bullets)
    if not parts:
        return None
    return "\n\n".join(parts)


def _gather_block(block_def, config, now: datetime | None) -> list[GatheredSource]:
    """Resolve a block's usable sources and return only the non-empty ones.

    Returns ``[]`` when the block declares no usable (user-agnostic) source or
    every source gathered nothing — the caller then skips the write and keeps the
    prior shared_kv value (last-known-good).
    """
    usable = _allowed_sources(block_def)
    if not usable:
        logger.warning(
            "shared block %r: no usable (user-agnostic) sources; skipping",
            block_def.name,
        )
        return []
    gathered = _gather_shared(config, usable, now)
    return [g for g in gathered if g.ok and not g.is_empty]


def _build_synthesis_prompt(block_def, non_empty: list[GatheredSource]) -> str:
    """Build the section-only synthesis prompt from pre-gathered sources."""
    title = block_def.title or block_def.name
    lines = [
        f"Produce ONE briefing section titled '{title}'.",
        _section_directive(block_def),
        "",
        "Sources:",
    ]
    for gs in non_empty:
        lines.append("")
        lines.append(_render_source(gs))
    lines += [
        "",
        "Output ONLY the section body as plain text — do NOT emit a title or "
        f"header line for '{title}' (the consuming briefing supplies the section "
        "title from its block, so a header here would duplicate it). Do NOT "
        "output JSON, a subject line, preamble, commentary, or code fences. Do "
        "NOT send emails or use any tools.",
    ]
    return "\n".join(lines)


def assemble_shared_block_input(block_def, config, *, now: datetime | None = None) -> str | None:
    """Build the section-only synthesis prompt for a shared block.

    Returns the prompt string, or ``None`` when the block has no usable sources
    or every source gathered nothing (all-empty) — the caller then skips the
    write and keeps the prior shared_kv value (last-known-good). Thin wrapper
    over :func:`_gather_block` + :func:`_build_synthesis_prompt`; used by the
    ``synthesis`` path (and kept for its independent tests).
    """
    non_empty = _gather_block(block_def, config, now)
    if not non_empty:
        return None
    return _build_synthesis_prompt(block_def, non_empty)


def _run_section_brain(config, prompt: str, label: str) -> tuple[bool, str]:
    """Run a privileged, text-only section synthesis through the configured brain.

    Mirrors the sleep cycle's brain call: no tools, no streaming, no sandbox.
    Uses the ``general`` role alias. Returns (success, text).

    ISSUE-181: like the sleep cycle, this calls the primary brain directly, so
    it consults the shared availability breaker before paying for a doomed call
    and feeds its own failures back in — a ``usage_limit`` opens the breaker
    (arming one operator alert) so the next shared-block cycle skips instead of
    re-attempting, and a success closes it. The ``structured`` path never
    reaches here (no brain call), so only ``synthesis`` blocks are affected.
    """
    from istota.brain import BrainRequest, make_brain
    from istota.brain import primary_brain_unavailable, report_brain_result

    # Non-essential task policy (ISSUE-181): skip when the primary is degraded.
    available, _reason = primary_brain_unavailable(config.brain)
    if not available:
        logger.info(
            "shared block %s skipped — primary brain unavailable (cooling down)",
            label,
        )
        return False, ""

    brain = make_brain(config.brain)
    req = BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=Path(config.temp_dir) if config.temp_dir else Path("/tmp"),
        env=dict(os.environ),
        timeout_seconds=_GENERATION_TIMEOUT_SECONDS,
        model=brain.resolve_model_name("general"),
        streaming=False,
        on_progress=None,
        cancel_check=None,
        on_pid=None,
        sandbox_wrap=None,
        result_file=None,
    )
    try:
        result = brain.execute(req)
    except Exception as e:  # noqa: BLE001
        logger.error("shared block %s brain error: %s", label, e)
        return False, ""
    # Feed the result into the shared breaker (one-shot alert on open).
    _opened_reason = report_brain_result(result, config.brain)
    if _opened_reason:
        _alert_brain_unavailable(config, label, _opened_reason)
    if not result.success or result.stop_reason in ("timeout", "not_found"):
        logger.error(
            "shared block %s failed (stop_reason=%s): %s",
            label, result.stop_reason, (result.result_text or "")[:200],
        )
        return False, ""
    return True, (result.result_text or "").strip()


def run_shared_block(block_def, config, *, now: datetime | None = None) -> dict | None:
    """Generate a shared block's content. Returns the shared_kv value dict
    (``{"text": ..., "trusted": <bool>}``) or ``None`` to skip the write.

    Branches on ``render_mode``:

    * ``structured`` → concatenate the gathered source text **verbatim**, with
      **no** Brain call (self-formatting data like the markets quote table is
      stored as-is — zero LLM passes).
    * ``synthesis`` (default) → run the section-only synthesis Brain over the
      gathered sources.

    ``None`` means no usable sources / all-empty gather (keep prior content —
    last-known-good) or, for ``synthesis``, a failed Brain call. The ``trusted``
    flag flows from the block definition into the stored value; the read side
    honors it and never a consuming user.
    """
    non_empty = _gather_block(block_def, config, now)
    if not non_empty:
        return None

    trusted = bool(getattr(block_def, "trusted", False))

    if block_def.render_mode == "structured":
        text = _assemble_verbatim(non_empty)
        if not text:
            return None
        return {"text": text, "trusted": trusted}

    prompt = _build_synthesis_prompt(block_def, non_empty)
    ok, text = _run_section_brain(config, prompt, block_def.name)
    if not ok or not text:
        return None
    return {"text": text, "trusted": trusted}
