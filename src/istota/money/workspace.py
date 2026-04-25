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


def _resolve_optional(
    config_dirs: list[Path], primary: str, legacy: str,
) -> Path | None:
    """Return the first existing file across ``config_dirs``.

    Search order is by filename preference, then by directory order: the
    primary filename is looked up in every dir before falling back to the
    legacy filename. So an ``INVOICING.md`` in any dir wins over an
    ``invoicing.toml`` in any dir.
    """
    for fname in (primary, legacy):
        for cd in config_dirs:
            p = cd / fname
            if p.exists():
                return p
    return None


def _config_search_dirs(
    workspace_root: Path, data_dir: Path, config_dir: Path | None,
) -> list[Path]:
    """Return the ordered list of dirs to search for module config files.

    With an explicit ``config_dir`` override, that's the only one consulted.
    Otherwise prefer ``{data_dir}/config/`` (module-local layout) and fall
    back to ``{workspace_root}/config/`` (configs colocated with USER.md /
    CRON.md / etc).
    """
    if config_dir is not None:
        return [Path(config_dir).resolve()]
    return [data_dir / "config", workspace_root / "config"]


def synthesize_user_context(
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    config_dir: Path | None = None,
    ledgers: list | None = None,
    db_path: Path | None = None,
) -> UserContext:
    """Build a money :class:`UserContext` rooted at a workspace dir.

    Defaults:
      * ``data_dir`` = ``{workspace_root}/money``
      * ``db_path`` = ``{data_dir}/data/money.db`` (matches the
        standalone ``money.cli`` convention)
      * ``ledgers`` = ``[{"name": "main", "path": "{data_dir}/ledgers/main.beancount"}]``
        when not supplied
      * config files are resolved out of ``{data_dir}/config/`` first,
        falling back to ``{workspace_root}/config/``. Pass ``config_dir``
        to force a single explicit location.

    ``ledgers`` accepts either dicts (``{"name": ..., "path": ...}``) or
    short-form strings — bare names get resolved to
    ``{data_dir}/ledgers/{name}.beancount``, mirroring
    :func:`money.cli._parse_user_context`.

    The function does not create any files on disk; the caller is
    responsible for ensuring the data dir and ledger files exist before any
    operation that needs them.
    """
    workspace_root = Path(workspace_root).resolve()
    if data_dir is None:
        data_dir = workspace_root / "money"
    else:
        data_dir = Path(data_dir).resolve()

    config_dirs = _config_search_dirs(workspace_root, data_dir, config_dir)

    if db_path is None:
        db_path = data_dir / "data" / "money.db"

    if ledgers is None:
        ledgers = [{
            "name": "main",
            "path": data_dir / "ledgers" / "main.beancount",
        }]
    else:
        normalized = []
        for entry in ledgers:
            if isinstance(entry, str):
                normalized.append({
                    "name": entry,
                    "path": data_dir / "ledgers" / f"{entry}.beancount",
                })
            else:
                normalized.append({
                    "name": entry.get("name", "default"),
                    "path": Path(entry["path"]),
                })
        ledgers = normalized

    invoicing_path = _resolve_optional(config_dirs, INVOICING_FILENAME, _LEGACY_INVOICING)
    tax_path = _resolve_optional(config_dirs, TAX_FILENAME, _LEGACY_TAX)
    monarch_path = _resolve_optional(config_dirs, MONARCH_FILENAME, _LEGACY_MONARCH)

    return UserContext(
        data_dir=data_dir,
        ledgers=ledgers,
        invoicing_config_path=invoicing_path,
        monarch_config_path=monarch_path,
        tax_config_path=tax_path,
        db_path=db_path,
    )


def list_workspace_features(
    workspace_root: Path,
    *,
    data_dir: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, bool]:
    """Return which money features the workspace has configured.

    Searches the same dirs as :func:`synthesize_user_context`. Useful for
    UI badges and quick gates without instantiating a full
    :class:`UserContext`.
    """
    workspace_root = Path(workspace_root).resolve()
    data_dir = Path(data_dir).resolve() if data_dir else workspace_root / "money"
    config_dirs = _config_search_dirs(workspace_root, data_dir, config_dir)
    return {
        "invoicing": (
            _resolve_optional(config_dirs, INVOICING_FILENAME, _LEGACY_INVOICING) is not None
        ),
        "tax": _resolve_optional(config_dirs, TAX_FILENAME, _LEGACY_TAX) is not None,
        "monarch": (
            _resolve_optional(config_dirs, MONARCH_FILENAME, _LEGACY_MONARCH) is not None
        ),
    }
