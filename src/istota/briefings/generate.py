"""Generation pipeline for the briefings module.

``assemble_briefing_input`` reads a briefing's blocks from the module DB,
resolves each block's sources (via ``sources/*``, fail-soft), and builds a
prompt grouped **by block** — per block: its title, directive, and the
normalized gathered content of its enabled sources tagged by provenance. The
model synthesizes each block into one coherent titled section and returns the
existing ``{subject, body}`` envelope.

``archive_briefing`` persists a rendered briefing to ``briefing_archive`` and
prunes past the retention window.

The single model invocation and the ``{subject, body}`` contract are unchanged
from the legacy path; only the *input assembly* differs (grouped by block, with
per-block synthesis directives).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from istota.briefings import db as briefings_db
from istota.briefings.models import BriefingBlock, BriefingsContext
from istota.briefings.sources import GatheredSource, SourceContext, resolve_source


logger = logging.getLogger(__name__)


@dataclass
class BriefingInput:
    """The assembled input for one briefing generation.

    ``prompt`` is the full model prompt (grouped by block). ``block_meta`` is
    per-block source provenance recorded on the archive row. ``rendered_blocks``
    is the count of non-empty blocks — 0 means nothing to say (caller may skip).
    """

    briefing_name: str
    prompt: str
    block_meta: dict = field(default_factory=dict)
    rendered_blocks: int = 0


def _now_in_tz(app_config, user_id: str, now: datetime | None):
    tz_str = "UTC"
    try:
        tz_str = app_config.resolve_user_timezone(user_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        tz = ZoneInfo(tz_str)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
        tz_str = "UTC"
    resolved = now.astimezone(tz) if now else datetime.now(tz)
    return resolved, tz_str


def _render_source(gs: GatheredSource) -> str:
    """Render one gathered source's content into prompt text."""
    header = f"--- source: {gs.title}"
    if gs.provenance:
        header += f" ({gs.provenance})"
    header += " ---"
    parts = [header]
    if gs.text.strip():
        parts.append(gs.text.strip())
    for item in gs.items:
        if "sender" in item:  # email
            parts.append(f"From {item.get('sender', '?')}: {item.get('subject', '')}")
            body = (item.get("body") or "").strip()
            if body:
                parts.append(body)
            parts.append("")
        elif "text" in item:  # todos
            parts.append(item["text"])
        else:  # rss / generic
            line = f"- {item.get('title', '(untitled)')}"
            summary = (item.get("summary") or "").strip()
            if summary:
                line += f" — {summary}"
            url = item.get("url")
            if url:
                # The canonical article URL for this item. When this story
                # makes it into the briefing, its citation should link here.
                line += f" [article: {url}]"
            parts.append(line)
    return "\n".join(parts)


def _default_directive(block: BriefingBlock) -> str:
    if block.render_mode == "structured":
        return (
            "Include this section's content as-is (pre-formatted). Do not "
            "reword numbers, quotes, or event details."
        )
    return (
        "Synthesize the sources below into one coherent section. Group related "
        "items, attribute across sources, and lead with what's new. When a "
        "source item carries an article URL (shown as '[article: <url>]'), link "
        "that source's attribution to the specific article it came from; use a "
        "plain-text source name when no URL is available."
    )


def assemble_briefing_input(
    ctx: BriefingsContext,
    briefing_name: str,
    app_config,
    *,
    conn=None,
    now: datetime | None = None,
) -> BriefingInput | None:
    """Assemble the block-grouped prompt for ``briefing_name``.

    ``conn`` is a *framework* DB connection threaded to the source resolvers
    (email ownership, Feeds gating). Returns ``None`` when the briefing has no
    blocks — the caller treats that as a task failure (blocks are the sole
    content model). Never raises on a single source failure (resolvers are
    fail-soft).
    """
    with briefings_db.connect(ctx.db_path) as bconn:
        blocks = briefings_db.list_blocks(bconn, briefing_name)

    if not blocks:
        return None

    resolved, tz_str = _now_in_tz(app_config, ctx.user_id, now)
    time_str = resolved.strftime("%Y-%m-%d %H:%M")
    is_morning = resolved.hour < 12
    mode = "morning" if is_morning else "evening"

    lines: list[str] = [
        f"Generate a {mode} briefing for user {ctx.user_id}.",
        f"Current time: {time_str} ({tz_str})",
        "",
        "Below are the briefing's content blocks, in order. Each block lists "
        "its sources' gathered content. Produce ONE section per non-empty "
        "block, titled with the block title (emoji-prefixed section header), "
        "in this exact order.",
    ]

    # Previous-briefing dedup, reusing the legacy digest store.
    try:
        from istota.skills.briefing import load_previous_briefing_digest

        previous = load_previous_briefing_digest(
            ctx.user_id, app_config, briefing_name or None,
        )
        if previous:
            lines += [
                "",
                "## Previous briefing (for reference)",
                previous,
                "",
                "The above was covered previously. Focus on new developments; "
                "revisit a story only for a material update, leading with what "
                "changed.",
            ]
    except Exception:  # noqa: BLE001
        pass

    # Gather every enabled source concurrently. Resolvers are network-bound
    # (browse frontpage fetch, IMAP) and fail-soft, so a slow source only
    # delays its own slot instead of serializing the whole briefing. Each
    # worker opens its OWN framework connection (SQLite connections aren't
    # thread-safe; WAL supports concurrent connections), so the caller's shared
    # `conn` is never touched off the calling thread.
    enabled_sources = [
        (bi, si, source)
        for bi, block in enumerate(blocks)
        for si, source in enumerate(block.sources)
        if source.enabled
    ]

    def _gather_one(kind: str, source_config: dict) -> GatheredSource:
        try:
            if conn is None:
                tctx = SourceContext(
                    app_config=app_config, user_id=ctx.user_id, conn=None, now=now,
                )
                return resolve_source(kind, source_config, tctx)
            from istota import db as framework_db

            with framework_db.get_db(app_config.db_path) as tconn:
                tctx = SourceContext(
                    app_config=app_config, user_id=ctx.user_id, conn=tconn, now=now,
                )
                return resolve_source(kind, source_config, tctx)
        except Exception as e:  # noqa: BLE001 — fail-soft, mirrors resolve_source
            logger.warning("briefings gather: %s source failed: %s", kind, e)
            return GatheredSource(
                kind=kind, title=kind, provenance=f"({kind} source error)", ok=False,
            )

    gathered_by_slot: dict[tuple[int, int], GatheredSource] = {}
    if enabled_sources:
        with ThreadPoolExecutor(max_workers=min(8, len(enabled_sources))) as pool:
            futures = {
                pool.submit(_gather_one, source.kind, source.config): (bi, si)
                for bi, si, source in enabled_sources
            }
            for fut in as_completed(futures):
                gathered_by_slot[futures[fut]] = fut.result()

    block_meta: dict = {}
    rendered = 0

    for bi, block in enumerate(blocks):
        gathered: list[GatheredSource] = [
            gathered_by_slot[(bi, si)]
            for si, source in enumerate(block.sources)
            if source.enabled
        ]

        non_empty = [g for g in gathered if g.ok and not g.is_empty]
        block_meta[block.title] = {
            "sources": len(block.sources),
            "gathered": len(non_empty),
            "kinds": [g.kind for g in gathered],
            "notes": [g.provenance for g in gathered if not g.ok],
        }
        if not non_empty:
            continue  # omit an all-empty block — no empty header

        rendered += 1
        lines.append("")
        lines.append(f"### Block: {block.title}")
        directive = (block.directive or "").strip() or _default_directive(block)
        lines.append(directive)
        opts = block.options or {}
        if opts.get("story_count"):
            lines.append(f"Target ~{opts['story_count']} items.")
        if opts.get("tone"):
            lines.append(f"Tone: {opts['tone']}.")
        for gs in non_empty:
            lines.append("")
            lines.append(_render_source(gs))

    lines += [
        "",
        "Format the briefing following the section format in the briefing "
        "skill reference. Use emoji-prefixed labels as section headers (not "
        "markdown headings). Output one section per block above, in the given "
        "order, omitting any block with no content. NO tables.",
        "",
        "CRITICAL: Your entire response must be a single JSON object with this "
        'exact format:\n{"subject": "<briefing subject>", "body": "<briefing '
        'content here>"}\n\n'
        "The body field contains the full briefing text with emoji section "
        "headers. Use \\n for newlines within the body string. Do NOT output "
        "anything outside the JSON object — no preamble, no commentary, no code "
        "fences. Do NOT send emails or use any email commands. Delivery is "
        "handled by the scheduler.",
    ]

    return BriefingInput(
        briefing_name=briefing_name,
        prompt="\n".join(lines),
        block_meta=block_meta,
        rendered_blocks=rendered,
    )


def archive_briefing(
    ctx: BriefingsContext,
    *,
    briefing_name: str,
    subject: str | None,
    body_md: str,
    task_id: int | None = None,
    block_meta: dict | None = None,
    delivered_to: list[str] | None = None,
    retention_days: int = 90,
    now: datetime | None = None,
) -> int | None:
    """Persist a rendered briefing to the archive + prune past retention.

    Best-effort: returns the archive row id, or ``None`` on failure (a failed
    archive write never blocks delivery — the task already delivered).
    """
    try:
        ctx.ensure_dirs()
        briefings_db.init_db(ctx.db_path)
        with briefings_db.connect(ctx.db_path) as conn:
            row_id = briefings_db.insert_archive(
                conn,
                briefing_name=briefing_name,
                subject=subject,
                body_md=body_md,
                task_id=task_id,
                block_meta=block_meta or {},
                delivered_to=delivered_to or [],
            )
            briefings_db.prune_archive(
                conn, briefing_name=briefing_name,
                retention_days=retention_days, now=now,
            )
            conn.commit()
            return row_id
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "briefings archive write failed for %s/%s: %s",
            ctx.user_id, briefing_name, e,
        )
        return None
