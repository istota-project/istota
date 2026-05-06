"""One-shot importer: legacy ``invoicing.toml`` / ``tax.toml`` / ``monarch.toml``
→ per-user money DB.

Mirrors :mod:`istota.feeds._migrate`. Runs idempotently on first touch from
``resolve_for_user``, the web routes, the CLI and the skill facade (via
:func:`ensure_initialised`).

Each section (invoicing / tax / monarch) is migrated independently and gated
on its own ``schema_meta`` sentinel — a workspace with only ``monarch.toml``
gets the monarch sentinel set and leaves the other two unset.
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from istota.money import config_store
from istota.money.cli import UserContext


logger = logging.getLogger(__name__)


_SECTIONS = ("invoicing", "tax", "monarch")
_SENTINEL_KEYS = {
    "invoicing": "invoicing_legacy_imported_at",
    "tax": "tax_legacy_imported_at",
    "monarch": "monarch_legacy_imported_at",
}
_TOML_FILENAMES = {
    "invoicing": ("invoicing.toml", "INVOICING.md"),
    "tax": ("tax.toml", "TAX.md"),
    "monarch": ("monarch.toml", "MONARCH.md"),
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_search_dirs(ctx: UserContext) -> list[Path]:
    """Mirror :func:`workspace._config_search_dirs`'s default order.

    ``{data_dir}/config/`` first, then ``{workspace_root}/config/`` (which
    is ``data_dir.parent / "config"`` in the default workspace layout).
    """
    data_dir = Path(ctx.data_dir)
    return [data_dir / "config", data_dir.parent / "config"]


def _find_legacy_file(ctx: UserContext, section: str) -> Path | None:
    primary, legacy = _TOML_FILENAMES[section]
    for fname in (primary, legacy):
        for cd in _config_search_dirs(ctx):
            p = cd / fname
            if p.is_file():
                return p
    return None


def _read_toml(path: Path) -> dict:
    if path.suffix.lower() == ".md":
        from istota.money._config_io import read_toml_config
        return read_toml_config(path)
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _section_already_populated(db_path: Path, section: str) -> bool:
    if section == "invoicing":
        return config_store.has_invoicing_data(db_path)
    if section == "tax":
        return config_store.has_tax_data(db_path)
    if section == "monarch":
        return config_store.has_monarch_data(db_path)
    return False


_LOCK_KEY_PREFIX = "_migrate_lock_"


def _claim_lock(db_path: Path, section: str) -> bool:
    """Race-safe transient lock. Returns True if we won the race.

    Used to serialize first-touch migration of a section. The lock is
    cleared by :func:`_finalize_migration` (success) or
    :func:`_release_lock` (parse / save failure) so a follow-up run can
    retry from a clean slate.
    """
    config_store.init_db(db_path)
    key = _LOCK_KEY_PREFIX + section
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (key, _iso_now()),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def _release_lock(db_path: Path, section: str) -> None:
    key = _LOCK_KEY_PREFIX + section
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (key,))
        conn.commit()


def _finalize_migration(db_path: Path, section: str) -> None:
    """Atomically: drop the lock and set the real sentinel.

    Called only after the importer + file rename succeeded. If we crash
    after the importer wrote rows but before this call, the next process
    sees the lock + populated tables and bails harmlessly via the
    populated-table check; the operator can clear the lock by retrying
    once the underlying issue is fixed.
    """
    sentinel_key = _SENTINEL_KEYS[section]
    lock_key = _LOCK_KEY_PREFIX + section
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (lock_key,))
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (sentinel_key, _iso_now()),
        )
        conn.commit()


def _sentinel_present(db_path: Path, section: str) -> bool:
    return config_store.get_meta(db_path, _SENTINEL_KEYS[section]) is not None


def _rename_imported(path: Path) -> Path:
    target = path.with_name(path.name + ".imported")
    try:
        path.rename(target)
    except OSError as exc:
        logger.warning(
            "money_legacy_rename_failed path=%s error=%s", path, exc,
        )
        return path
    return target


def _import_invoicing(db_path: Path, data: dict) -> dict:
    cfg = config_store.invoicing_config_from_toml_dict(data)
    config_store.save_invoicing(db_path, cfg, replace_collections=True)
    return {
        "companies": len(cfg.companies),
        "clients": len(cfg.clients),
        "services": len(cfg.services),
    }


def _import_tax(db_path: Path, data: dict) -> dict:
    cfg = config_store.tax_config_from_toml_dict(data)
    config_store.save_tax(db_path, cfg, replace_collections=True)
    return {
        "tax_year": cfg.tax_year,
        "patterns": len(cfg.se_income_accounts) + len(cfg.se_expense_accounts),
        "year_rates": 1 if any(v is not None for v in (
            cfg.federal_brackets, cfg.ca_brackets,
            cfg.federal_standard_deduction, cfg.ca_standard_deduction,
            cfg.ss_wage_base, cfg.ss_rate,
        )) else 0,
    }


def _import_monarch(db_path: Path, data: dict) -> dict:
    cfg = config_store.monarch_config_from_toml_dict(data, secrets=None)
    config_store.save_monarch(db_path, cfg, replace_collections=True)
    return {"profiles": len(cfg.profiles)}


_IMPORTERS = {
    "invoicing": _import_invoicing,
    "tax": _import_tax,
    "monarch": _import_monarch,
}


def _migrate_section(ctx: UserContext, section: str) -> dict | None:
    """Migrate one section's TOML file. Returns summary or ``None``."""
    db_path = Path(ctx.db_path)
    config_store.init_db(db_path)

    if _sentinel_present(db_path, section):
        legacy_path = _find_legacy_file(ctx, section)
        if legacy_path is not None:
            logger.warning(
                "money_legacy_present_but_already_imported section=%s path=%s "
                "— file is no longer read; delete it",
                section, legacy_path,
            )
        return None

    legacy_path = _find_legacy_file(ctx, section)
    if legacy_path is None:
        return None

    if _section_already_populated(db_path, section):
        logger.warning(
            "money_legacy_present_but_db_populated section=%s path=%s "
            "— DB already has rows; delete or merge manually",
            section, legacy_path,
        )
        return None

    if not _claim_lock(db_path, section):
        # Another worker won the race.
        return None

    try:
        try:
            parsed = _read_toml(legacy_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "money_legacy_unparseable section=%s path=%s error=%s",
                section, legacy_path, exc,
            )
            _release_lock(db_path, section)
            return None

        try:
            summary = _IMPORTERS[section](db_path, parsed)
        except Exception:
            # Importer failed; release the lock so the next run can retry.
            _release_lock(db_path, section)
            raise

        new_path = _rename_imported(legacy_path)
        _finalize_migration(db_path, section)
    except Exception:
        raise

    logger.info(
        "money_legacy_imported section=%s path=%s renamed=%s summary=%s",
        section, legacy_path, new_path, summary,
    )
    return {
        "section": section,
        "path": str(legacy_path),
        "renamed_to": str(new_path),
        **summary,
    }


def migrate_legacy_workspace_config(ctx: UserContext) -> dict | None:
    """Run all three section migrations. Returns a dict summarizing results.

    Returns ``None`` only when nothing happened across every section.
    """
    summaries: dict[str, Any] = {}
    for section in _SECTIONS:
        result = _migrate_section(ctx, section)
        if result is not None:
            summaries[section] = result
    return summaries or None


def ensure_initialised(ctx: UserContext) -> None:
    """Wire up a money workspace for use.

    Creates the data + ledgers dirs (idempotent), initialises the DB schema,
    and runs the legacy migration (which itself no-ops on subsequent runs).
    Safe to call from every entry point.
    """
    data_dir = Path(ctx.data_dir)
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    (data_dir / "ledgers").mkdir(parents=True, exist_ok=True)

    db_path = ctx.db_path or (data_dir / "data" / "money.db")
    config_store.init_db(db_path)
    if ctx.db_path is None:
        ctx.db_path = db_path

    migrate_legacy_workspace_config(ctx)
