"""What still points at an invoicing record, and whether it may be deleted.

Shared by the web routes and ``istota money client|company|service remove``.
It lives outside ``config_store`` because the references being counted are not
in the config DB at all: work entries are TOML files in the user's workspace,
so the scan needs a ``data_dir`` the store never sees.

The three guards differ in strictness, matching how badly the absence corrupts
things:

- **service** — refused while any work entry names it. Deletion breaks time in
  both directions: ``build_line_items`` skips an entry whose service is
  missing, so future work goes unbilled, *and* the invoice list rebuilds its
  totals from live config, so every past invoice containing such an entry
  re-renders short.
- **entity** — refused while a client names it, a work entry pins it, it is the
  stored default, or it is the effective default blank-entity clients fall back
  to. The failure lands on a generated PDF carrying a different legal entity's
  name, address and payment instructions, with nothing on it saying so.
- **client** — allowed. Invoice grouping is by the entry's client key, so
  entries and invoices survive and only the display name degrades to the raw
  key. The count comes back so the caller can say what it cost.

A scan that cannot complete refuses the delete for the two strict kinds:
proceeding would delete blind, which is the failure the guards exist to
prevent. The soft client case degrades to an unknown count instead, since
refusing there would strand a user behind a broken year file for a delete that
destroys nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ReferenceScan:
    """Outcome of a reference scan.

    ``blocked_reason`` is set when the delete must be refused; ``scan_failed``
    when the work store could not be read at all (a distinct outcome, since it
    means the counts are unknown rather than zero).
    """

    references: dict = field(default_factory=dict)
    blocked_reason: str | None = None
    scan_failed: str | None = None

    @property
    def allowed(self) -> bool:
        return self.blocked_reason is None and self.scan_failed is None


def _work_entries(data_dir: Path | None) -> tuple[list, list[str]]:
    """Work entries plus the year files whose rows could not be read.

    An absent ``data_dir`` (a user who has never invoiced) is zero references
    rather than a blocked delete.
    """
    from istota.money.work import load_work_entries, quarantined_years

    if not data_dir:
        return [], []
    entries = load_work_entries(data_dir)
    return entries, sorted(quarantined_years(data_dir))


def _quarantine_reason(kind: str, key: str, quarantined: list[str]) -> str:
    return (
        f"cannot tell what references {kind} '{key}': "
        f"{', '.join(quarantined)} has an entry this version can't read, and a "
        "row it names would not be counted. Fix the row by hand and retry."
    )


def service_references(
    db_path: Path | str, data_dir: Path | None, key: str,
) -> ReferenceScan:
    try:
        entries, quarantined = _work_entries(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("config_delete_reference_scan_failed kind=service error=%s", exc)
        return ReferenceScan(scan_failed=str(exc))

    matching = [e for e in entries if e.service == key]
    refs = {
        "work_entries": len(matching),
        "invoices": len({e.invoice for e in matching if e.invoice}),
        "quarantined": quarantined,
    }
    if quarantined:
        return ReferenceScan(refs, blocked_reason=_quarantine_reason("service", key, quarantined))
    if matching:
        plural = "y" if len(matching) == 1 else "ies"
        return ReferenceScan(
            refs,
            blocked_reason=f"service '{key}' is used by {len(matching)} work entr{plural}",
        )
    return ReferenceScan(refs)


def client_references(
    db_path: Path | str, data_dir: Path | None, key: str,
) -> ReferenceScan:
    """Count a client's work entries. Never blocks — the delete is the soft case.

    Matching is case-insensitive because ``add_work_entry`` lowercases the
    client it stores, so a legacy mixed-case config key would otherwise report
    zero for entries it plainly owns.
    """
    try:
        entries, quarantined = _work_entries(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("config_delete_reference_scan_failed kind=client error=%s", exc)
        return ReferenceScan(references={}, scan_failed=str(exc))

    wanted = key.lower()
    matching = [e for e in entries if e.client.lower() == wanted]
    return ReferenceScan({"work_entries": len(matching), "quarantined": quarantined})


def entity_references(
    db_path: Path | str, data_dir: Path | None, key: str,
) -> ReferenceScan:
    """What would break if this entity went away.

    Four distinct ways an entity is depended on:

    - ``clients`` — clients naming it explicitly.
    - ``work_entries`` — entries pinning it. ``resolve_entity`` checks
      ``entry.entity`` *first*, ahead of the client's, so an entry pinned here
      silently re-bills under a different entity once this one is gone.
    - ``default_entity`` — it is the *stored* default. Deliberately the stored
      scalar and not ``load_invoicing``'s derived one, which falls back to the
      first company and would make a fresh user's only entity permanently
      undeletable.
    - ``default_for_clients`` — clients with a blank ``entity``, which means
      "bill under the default". The effective default is read from
      ``cfg.company.key``: that is the object ``resolve_entity`` actually falls
      back to, and it is *not* the stored scalar when the stored one names no
      existing company (an ordinary outcome of migrating a TOML with clients
      but no ``[companies]`` block). Trusting the scalar there let the entity
      every client really bills under be deleted while the guard reported zero.
    """
    from istota.money import config_store

    cfg = config_store.load_invoicing(db_path)
    clients = sorted(k for k, c in cfg.clients.items() if c.entity == key)
    stored_default = config_store.get_invoicing_setting(db_path, "default_entity")
    effective_default = cfg.company.key if cfg.companies else ""
    fallback_clients = (
        sum(1 for c in cfg.clients.values() if not c.entity)
        if effective_default == key
        else 0
    )

    try:
        entries, quarantined = _work_entries(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("config_delete_reference_scan_failed kind=entity error=%s", exc)
        return ReferenceScan(scan_failed=str(exc))

    pinned = [e for e in entries if e.entity == key]
    refs = {
        "clients": clients,
        "work_entries": len(pinned),
        "default_entity": stored_default == key,
        "default_for_clients": fallback_clients,
        "quarantined": quarantined,
    }

    if quarantined:
        return ReferenceScan(refs, blocked_reason=_quarantine_reason("entity", key, quarantined))
    if clients:
        return ReferenceScan(refs, blocked_reason=(
            f"entity '{key}' is used by {len(clients)} client(s): {', '.join(clients)}"
        ))
    if pinned:
        plural = "y" if len(pinned) == 1 else "ies"
        return ReferenceScan(refs, blocked_reason=(
            f"entity '{key}' is pinned by {len(pinned)} work entr{plural}"
        ))
    if stored_default == key:
        return ReferenceScan(refs, blocked_reason=f"entity '{key}' is the default entity")
    if fallback_clients:
        return ReferenceScan(refs, blocked_reason=(
            f"entity '{key}' is the default {fallback_clients} client(s) bill under "
            "— give them an explicit entity first"
        ))
    return ReferenceScan(refs)
