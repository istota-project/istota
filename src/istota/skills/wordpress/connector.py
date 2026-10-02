"""The connector verbs: `options get|update` and `network sites` (spec §3.3, §5.5).

Core REST cannot write an ACF options page or list a multisite network's sites,
so the istota-connector plugin (``integrations/wordpress/istota-connector/``)
registers both as Abilities API abilities, and these verbs are thin wrappers
over `generic.run_ability`, the path `abilities run` takes: the ability is read
first, its annotations decide the method, and a write goes through the same
gate. The plugin decides what is reachable (only fields in groups with "Show in
REST API" on) and who may reach it (``manage_options``, and ``manage_sites`` on
the main site); the skill only checks what it is about to send.

**connector_missing.** The ability's own read answers whether the plugin is
there: ``rest_no_route`` means the site has no Abilities API (WordPress 6.9 or
later), and a missing ability means the plugin is not active on this site.
That read is what `abilities run` makes anyway, so nothing extra is probed,
and a plugin installed a moment ago is seen at once rather than after the
discovery cache expires. A missing field ability on a site that has
``istota/options-get`` is the 0.1 plugin, answered ``connector_outdated``.

**options update** reads the page first, refuses a field the page does not
expose with ``acf_not_in_rest`` before the gate, names each field's current and
new value in the ``would`` line, uploads any ``$upload`` marker after the gate
(`acf.py`), and compares what the ability returns with what was sent.
"""

from __future__ import annotations

import re

from . import acf, media
from .acf import Slot
from .client import WordPressError, fence, fence_tree, selector
from .common import gate, int_or_none, lookup, quoted
from .discovery import FIELD_ABILITIES
from .generic import annotations, fetch_ability, run_ability, shown_json

OPTIONS_GET = "istota/options-get"
OPTIONS_UPDATE = "istota/options-update"
NETWORK_SITES = "istota/network-sites"

INSTALL = (
    "Build the plugin zip with scripts/build-wordpress-connector.sh in the istota "
    "repository and upload it in wp-admin under Plugins, Add New, Upload Plugin "
    "(network-activate it on a multisite network). See docs/features/wordpress.md."
)
#: What a site with the 0.1 plugin answers for the field abilities.
OUTDATED = (
    "The istota-connector plugin on this site is older than 0.2.0 and has no field "
    "editing. Rebuild the zip and upload it again (the install line)."
)
#: How many sites one `network sites` call asks for; the plugin caps it too.
NETWORK_SITES_LIMIT = 1000
#: An ACF options page's menu slug.
_PAGE_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_ACF_SELECTORS = frozenset({"acf_fc_layout"})


def connector_ability(ctx, name: str) -> dict:
    # `describe` caches whether the connector is there; when this read says
    # otherwise, the cache is dropped so the next `describe` agrees.
    cached = ctx.cache.get("describe")
    cached_says = cached.get("connector") if isinstance(cached, dict) else None
    try:
        ability = fetch_ability(ctx, name)
    except WordPressError as exc:
        if exc.reason in ("unknown_route", "not_found") and cached_says is True:
            ctx.cache.drop()
        if exc.reason == "unknown_route":
            raise WordPressError(
                "This site has no Abilities API (WordPress 6.9 or later), which the "
                "istota-connector plugin needs.",
                "connector_missing", install=INSTALL,
            ) from None
        if exc.reason == "not_found":
            if name in FIELD_ABILITIES and _has_ability(ctx, OPTIONS_GET):
                raise WordPressError(OUTDATED, "connector_outdated", install=INSTALL) from None
            raise WordPressError(
                f"The istota-connector plugin is not active on this site (it has no "
                f"ability {name}).",
                "connector_missing", install=INSTALL,
            ) from None
        raise
    if cached_says is False:
        ctx.cache.drop()
    return ability


def _has_ability(ctx, name: str) -> bool:
    try:
        fetch_ability(ctx, name)
    except WordPressError:
        return False
    return True


def run_connector(args, ctx, name: str, value, *, read: bool = False, **kwargs):
    ability = connector_ability(ctx, name)
    if read:
        notes = annotations(ability)
        # A read verb takes no --confirmed, so an ability that would need one
        # is not the plugin this skill was written against.
        if notes.get("readonly") is not True or notes.get("destructive") is True:
            raise WordPressError(
                f"The site's {name} is not marked read-only, so it is not the "
                f"istota-connector ability this verb expects. `abilities run {name}` "
                f"runs it with the user's confirmation.",
                "connector_mismatch",
            )
    try:
        result, _, _ = run_ability(args, ctx, name, ability, value, **kwargs)
    except WordPressError as exc:
        if exc.extra.get("wp_code") == "istota_not_multisite":
            raise WordPressError("This site is not a multisite network.",
                                 "not_multisite") from None
        raise
    if not isinstance(result, dict):
        raise WordPressError("The connector answered with something that is not an object.",
                             "bad_response")
    return result


def _fields(result: dict) -> dict:
    fields = result.get("fields")
    return fields if isinstance(fields, dict) else {}


def check_page(args) -> None:
    if not _PAGE_RE.fullmatch(args.page):
        raise WordPressError(
            f"--page takes an options page's slug, such as acf-options, not {quoted(args.page)}.",
            "validation_error",
        )


def cmd_options_get(args) -> dict:
    ctx = args.wp
    result = run_connector(args, ctx, OPTIONS_GET, {"page": args.page}, read=True)
    return {
        "status": "ok",
        **ctx.envelope(),
        "page": args.page,
        "fields": fence_tree(_fields(result), keep=_ACF_SELECTORS),
    }


# --------------------------------------------------------------------------- #
# options update
# --------------------------------------------------------------------------- #


def check_options_update(args) -> None:
    check_page(args)
    acf.check(args, media.upload_cap(args.config))
    if not args.acf_values:
        raise WordPressError("Name at least one field to write (--acf-file or --acf-set).",
                             "validation_error")


def _shown(value, uploads: acf.Uploads):
    """A value for the `would` line, an upload named by its path."""
    if isinstance(value, Slot):
        return f"(upload of {uploads.sources[value.index].path})"
    if isinstance(value, dict):
        return {k: _shown(v, uploads) for k, v in value.items()}
    if isinstance(value, list):
        return [_shown(v, uploads) for v in value]
    return value


def _readback(sent: dict, stored: dict) -> dict:
    dropped = sorted(name for name in sent if name not in stored)
    changed = sorted(name for name in sent
                     if name in stored and not acf.same(sent[name], stored[name]))
    notes = []
    if dropped or changed:
        notes.append("A field did not store as sent: WordPress may have rejected or "
                     "reformatted the value. `options get` shows what was saved.")
    return {"dropped": dropped, "changed": changed, "notes": notes}


def cmd_options_update(args) -> dict:
    ctx = args.wp
    page = args.page
    current = _fields(run_connector(args, ctx, OPTIONS_GET, {"page": page}, read=True))
    unknown = sorted(name for name in args.acf_values if name not in current)
    if unknown:
        raise WordPressError(
            f"The options page {quoted(page)} has no REST-visible field named "
            f"{', '.join(unknown)}. Either the name is wrong (`options get` lists the "
            f"fields), or its field group has 'Show in REST API' off.",
            "acf_not_in_rest", fields=unknown,
        )
    changes = "; ".join(
        # Cut, then fence once: fencing first lets the cut drop a closing marker.
        f"{name} from {fence(shown_json(current[name]))} to "
        f"{shown_json(_shown(value, args.uploads))}"
        for name, value in args.acf_values.items()
    )
    ability = connector_ability(ctx, OPTIONS_UPDATE)
    notes = annotations(ability)
    # The plugin marks its write neither readonly nor destructive, which keeps
    # it on POST with a JSON body and a would line that is not understated.
    if notes.get("readonly") is True or notes.get("destructive") is True:
        raise WordPressError(
            f"The site's {OPTIONS_UPDATE} is not marked the way the istota-connector "
            f"plugin marks it, so this verb will not run it.",
            "connector_mismatch",
        )
    hint = lookup(ctx, "options", "get", "--page", page)
    described = f"update the options page {quoted(page)}: {changes}"
    # Gated here, ahead of the uploads; `run_ability` asks the same question
    # again below and gets the same answer.
    gate(args, ctx, [described])
    upload_ids, report = acf.run_uploads(ctx, args.uploads, args.deadline)
    uploaded = [{"path": row["path"], "id": row["id"]} for row in report]
    if report:
        media.check_deadline(args.deadline, "writing the options page", uploaded)
    fields = acf.fill(args.acf_values, upload_ids)
    try:
        result, _, _ = run_ability(args, ctx, OPTIONS_UPDATE, ability,
                                   {"page": page, "fields": fields},
                                   described=described, always_gate=True, hint=hint)
    except WordPressError as exc:
        if uploaded:
            exc.extra["uploaded"] = uploaded
        raise
    stored = _fields(result) if isinstance(result, dict) else {}
    out = {
        "status": "ok",
        **ctx.envelope(),
        "page": page,
        "fields": fence_tree(stored, keep=_ACF_SELECTORS),
        "readback": _readback(fields, stored),
    }
    if report:
        out["uploads"] = report
    return out


# --------------------------------------------------------------------------- #
# network sites
# --------------------------------------------------------------------------- #


def _project_site(site: dict) -> dict:
    site_id = site.get("id")
    return {
        "id": site_id if isinstance(site_id, int) and not isinstance(site_id, bool) else None,
        "domain": selector(site.get("domain")),
        "path": selector(site.get("path")),
        "name": fence(site.get("name")),
        "public": site.get("public") is True,
        "archived": site.get("archived") is True,
        "deleted": site.get("deleted") is True,
    }


def cmd_network_sites(args) -> dict:
    ctx = args.wp
    result = run_connector(args, ctx, NETWORK_SITES, {"number": NETWORK_SITES_LIMIT}, read=True)
    sites = result.get("sites") if isinstance(result.get("sites"), list) else []
    rows = [_project_site(s) for s in sites if isinstance(s, dict)]
    return {
        "status": "ok",
        **ctx.envelope(),
        "count": len(rows),
        "total": int_or_none(result.get("total")),
        "sites": rows,
    }
