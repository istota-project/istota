"""Shared-block source resolver — pre-made curated content from the shared_kv store.

A ``shared_block`` source reads a module-owned shared block from the shared_kv
store and contributes it to a briefing block, so expensive shared generation
(headlines, a markets summary, a newsletter digest) runs *once globally* and
every user's briefing reads the pre-made artifact instead of regenerating it.

The **writer chooses granularity** via the stored JSON shape:

* ``{"items": [{"title","summary","url"}, …]}`` (or a bare JSON list) → the
  reader's block **synthesizes** the items (share the fetch, not the prose).
* ``{"text": "…"}`` (or a bare JSON string) → the section text is **spliced**
  near-verbatim into a ``structured`` block (share the synthesis too).

Fail-soft like every source: a missing/stale/malformed value yields an empty
result with a provenance note (the block is omitted), never an exception.

The ``shared_block`` sugar takes ``{"name": "world-headlines", "max_age_hours":
12}`` and resolves to the shared_kv read at namespace ``briefing_shared_blocks``,
key ``name``. Trust is a property of the stored value (an admin-written
``trusted`` flag), never chosen by the consuming user.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from istota.briefings.sources import GatheredSource, SourceContext


logger = logging.getLogger(__name__)


# The namespace the module-owned shared blocks write into (Stage 5). The
# `shared_block` sugar keys off the block name here.
SHARED_BLOCK_NAMESPACE = "briefing_shared_blocks"


def _parse_updated_at(raw: str | None) -> datetime | None:
    """Parse a SQLite ``datetime('now')`` value (naive UTC) into an aware dt."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # ISO with fractional seconds / offset
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_hours(updated_at: datetime | None, now: datetime | None) -> float | None:
    if updated_at is None:
        return None
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (ref - updated_at).total_seconds() / 3600.0


def _normalize_items(raw_items: list) -> list[dict]:
    """Coerce a stored items list into the rss-shaped dicts ``_render_source``
    expects. Non-dict members become a bare ``{title}``."""
    out: list[dict] = []
    for item in raw_items:
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append({"title": str(item)})
    return out


def _read(namespace: str, key: str, ctx: SourceContext):
    """Return the shared KV row dict or None. No gate on reads."""
    from istota import db

    if ctx.conn is None:
        return None, "(shared_block source skipped — no DB connection)"
    row = db.shared_kv_get(ctx.conn, namespace, key)
    return row, None


def _resolve(
    *,
    namespace: str,
    key: str,
    max_age_hours: float,
    title: str,
    ctx: SourceContext,
    missing_note: str | None = None,
) -> GatheredSource:
    if not namespace or not key:
        return GatheredSource(
            kind="shared_block", title=title,
            provenance="(shared_block source missing namespace/key)", ok=False,
        )

    row, err = _read(namespace, key, ctx)
    if err:
        return GatheredSource(kind="shared_block", title=title, provenance=err, ok=False)
    if row is None:
        note = missing_note or f"(no shared KV at {namespace}/{key})"
        return GatheredSource(kind="shared_block", title=title, provenance=note, ok=False)

    # Freshness: stale-but-present content is dropped, not shown, so a wedged
    # generator degrades to an omitted block rather than yesterday's headlines.
    updated_at = _parse_updated_at(row.get("updated_at"))
    age = _age_hours(updated_at, ctx.now)
    if max_age_hours and age is not None and age > max_age_hours:
        return GatheredSource(
            kind="shared_block", title=title,
            provenance=f"(stale: {namespace}/{key} written {int(age)}h ago)",
            ok=False,
        )

    try:
        parsed = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return GatheredSource(
            kind="shared_block", title=title,
            provenance=f"(malformed KV value at {namespace}/{key})", ok=False,
        )

    # Trust is a property of the content's writer (an admin), stored in the
    # value — never chosen by the consuming user.
    trusted = bool(parsed.get("trusted", False)) if isinstance(parsed, dict) else False

    items: list[dict] = []
    text = ""
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        items = _normalize_items(parsed["items"])
    elif isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
        text = parsed["text"]
    elif isinstance(parsed, str):
        text = parsed
    elif isinstance(parsed, list):
        items = _normalize_items(parsed)
    else:
        return GatheredSource(
            kind="shared_block", title=title,
            provenance=f"(unusable KV shape at {namespace}/{key})", ok=False,
        )

    if not items and not text.strip():
        return GatheredSource(
            kind="shared_block", title=title,
            provenance=f"(empty KV content at {namespace}/{key})", ok=False,
        )

    age_str = f", written {int(age)}h ago" if age is not None else ""
    by = row.get("written_by") or "?"
    provenance = f"shared KV {namespace}/{key}{age_str} by {by}"

    return GatheredSource(
        kind="shared_block", title=title, items=items, text=text,
        provenance=provenance, untrusted=not trusted,
    )


def resolve_shared_block(config: dict, ctx: SourceContext) -> GatheredSource:
    """Read a module-owned shared block by name.

    ``{"name": "world-headlines", "max_age_hours": 12}`` maps to a shared read at
    namespace ``briefing_shared_blocks``, key ``name``. If the referenced block
    isn't configured *and* has no stored value, the provenance note says so (a
    stale reference reads more clearly than a bare "no shared KV").
    """
    name = config.get("name") or config.get("key") or ""
    if not name:
        return GatheredSource(
            kind="shared_block", title="Shared block",
            provenance="(shared_block source missing name)", ok=False,
        )
    max_age_hours = float(config.get("max_age_hours", 0) or 0)
    title = config.get("title") or name

    # Flag an unknown name only when it's neither a configured/DB-defined block
    # NOR a live shared_kv key. A custom-published key (from a publish_shared_kv
    # job) has no shared_block_configs definition but is a perfectly valid live
    # key — never flag it "unknown".
    known = _shared_block_names(ctx)
    missing_note = None
    if known is not None and name not in known and not _shared_kv_key_exists(ctx, name):
        missing_note = f"(unknown shared block '{name}')"

    return _resolve(
        namespace=SHARED_BLOCK_NAMESPACE, key=name,
        max_age_hours=max_age_hours, title=title, ctx=ctx,
        missing_note=missing_note,
    )


def _shared_kv_key_exists(ctx: SourceContext, name: str) -> bool:
    """Whether a live value exists at briefing_shared_blocks/<name>.

    Lets a custom-published key (no definition row) resolve without an "unknown"
    provenance note. Fail-soft: no conn / DB error → False (falls back to the
    configured-names check only).
    """
    if ctx.conn is None:
        return False
    try:
        from istota import db
        return db.shared_kv_get(ctx.conn, SHARED_BLOCK_NAMESPACE, name) is not None
    except Exception:  # noqa: BLE001 - fail-soft
        return False


def _shared_block_names(ctx: SourceContext) -> set[str] | None:
    """The configured shared-block names, or None if the config has none/absent
    (Stage 5 config attribute; guarded so Stage 4 works standalone)."""
    blocks = getattr(ctx.app_config, "briefing_shared_blocks", None)
    if not blocks:
        return None
    names = set()
    for b in blocks:
        name = getattr(b, "name", None)
        if name:
            names.add(name)
    return names or None
