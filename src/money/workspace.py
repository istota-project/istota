"""Synthesize a money :class:`UserContext` from a workspace ``config/`` dir.

This is the post-migration loading path: a user's money config lives next to
their other istota config files as ``INVOICING.md``, ``TAX.md``,
``MONARCH.md`` (each holding a fenced ```toml block). Their data dir
defaults to ``{workspace_root}/money``, with the SQLite DB and ledger files
under it.

The legacy path — a top-level ``money.toml`` with ``[users.X]`` sections
pointing at separate ``*.toml`` config files — keeps working through
:func:`money.cli.load_context`. This module is the "workspace mode" loader
for callers that already know the user's workspace root.
"""

from pathlib import Path

from .cli import UserContext


INVOICING_FILENAME = "INVOICING.md"
TAX_FILENAME = "TAX.md"
MONARCH_FILENAME = "MONARCH.md"

# Legacy filenames that the workspace loader still accepts during the
# migration window (decision #5 in the migration spec).
_LEGACY_INVOICING = "invoicing.toml"
_LEGACY_TAX = "tax.toml"
_LEGACY_MONARCH = "monarch.toml"


def _resolve_optional(config_dir: Path, primary: str, legacy: str) -> Path | None:
    """Return the first existing file (primary, then legacy), or None."""
    p = config_dir / primary
    if p.exists():
        return p
    p = config_dir / legacy
    if p.exists():
        return p
    return None


def synthesize_user_context(
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    ledgers: list[dict] | None = None,
    db_path: Path | None = None,
) -> UserContext:
    """Build a money :class:`UserContext` rooted at a workspace dir.

    Defaults:
      * ``data_dir`` = ``{workspace_root}/money``
      * ``db_path`` = ``{data_dir}/moneyman.db``
      * ``ledgers`` = ``[{"name": "main", "path": "{data_dir}/ledger/main.beancount"}]``
        when not supplied (only used if the file exists)
      * config files are resolved out of ``{workspace_root}/config/``

    The function does not create any files on disk; the caller is
    responsible for ensuring the data dir and ledger files exist before any
    operation that needs them.
    """
    workspace_root = Path(workspace_root).resolve()
    config_dir = workspace_root / "config"
    if data_dir is None:
        data_dir = workspace_root / "money"
    else:
        data_dir = Path(data_dir).resolve()

    if db_path is None:
        db_path = data_dir / "moneyman.db"

    if ledgers is None:
        default_ledger = data_dir / "ledger" / "main.beancount"
        ledgers = [{"name": "main", "path": default_ledger}]
    else:
        ledgers = [
            {"name": l.get("name", "default"), "path": Path(l["path"])}
            for l in ledgers
        ]

    invoicing_path = _resolve_optional(config_dir, INVOICING_FILENAME, _LEGACY_INVOICING)
    tax_path = _resolve_optional(config_dir, TAX_FILENAME, _LEGACY_TAX)
    monarch_path = _resolve_optional(config_dir, MONARCH_FILENAME, _LEGACY_MONARCH)

    return UserContext(
        data_dir=data_dir,
        ledgers=ledgers,
        invoicing_config_path=invoicing_path,
        monarch_config_path=monarch_path,
        tax_config_path=tax_path,
        db_path=db_path,
    )


def list_workspace_features(workspace_root: Path) -> dict[str, bool]:
    """Return which money features the workspace has configured.

    Useful for UI badges and quick gates without instantiating a full
    UserContext.
    """
    config_dir = Path(workspace_root) / "config"
    return {
        "invoicing": (
            (config_dir / INVOICING_FILENAME).exists()
            or (config_dir / _LEGACY_INVOICING).exists()
        ),
        "tax": (
            (config_dir / TAX_FILENAME).exists()
            or (config_dir / _LEGACY_TAX).exists()
        ),
        "monarch": (
            (config_dir / MONARCH_FILENAME).exists()
            or (config_dir / _LEGACY_MONARCH).exists()
        ),
    }
