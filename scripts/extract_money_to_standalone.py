#!/usr/bin/env python3
"""Build a standalone moneyman tree from the in-istota `money` package.

Produces a directory laid out like the public ``github.com/muinyc/moneyman``
repo: ``src/moneyman/``, ``tests/``, ``web/src/lib/`` and ``web/src/routes/``
flattened (no ``money/`` subdir), plus a minimal ``pyproject.toml``.

Use as:
    extract_money_to_standalone.py --dest /tmp/moneyman-extract

The destination is overwritten (excluding any ``.git`` directory inside it,
so callers can pre-clone the public repo and run this against the worktree).
The result is what you'd ``git add -A`` and tag for a release.

This script is intentionally side-effect-light: it does not run git, push,
or tag. CI/release scripts wrap it with whatever workflow they need.

Constraints enforced:
  * ``src/money/`` must have zero ``istota.*`` imports (run
    ``check_money_isolation.sh`` first; this script refuses on imports).
  * In-istota-only files (``routes.py``, ``jobs.py``, ``workspace.py``) are
    excluded — they wire the package into istota's web/scheduler and have
    no standalone meaning.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files inside src/money/ that are istota-integration only.
EXCLUDE_FROM_MONEY = {"routes.py", "jobs.py", "workspace.py"}

# Frontend files in web/src/{lib,routes}/money/ all flatten into the
# standalone repo's web/src/{lib,routes}/. The standalone moneyman repo's
# web app is its own SvelteKit app rooted at /moneyman, not under /istota.
FRONTEND_PAIRS = (
    ("web/src/lib/money", "web/src/lib"),
    ("web/src/routes/money", "web/src/routes"),
)

# Tests live under tests/money/ in istota; the extract puts them at the top
# of tests/ to match the standalone layout.
TESTS_FROM = "tests/money"
TESTS_TO = "tests"

PYPROJECT_TEMPLATE = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "moneyman"
version = "{version}"
description = "Standalone accounting service: Beancount, invoicing, Monarch sync."
requires-python = ">=3.11"
dependencies = ["tomli", "click"]

[project.optional-dependencies]
beancount = ["beancount>=3.0.0", "beanquery>=0.1.0"]
invoicing = ["weasyprint>=62.0"]
monarch = ["monarchmoneycommunity"]
api = ["fastapi", "uvicorn[standard]", "httpx"]
web = ["fastapi", "uvicorn[standard]", "authlib", "itsdangerous"]
dev = ["pytest", "pytest-asyncio"]

[project.scripts]
moneyman = "moneyman.cli:cli"

[tool.hatch.build.targets.wheel]
packages = ["src/moneyman"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
"""


def _check_isolation() -> None:
    """Refuse to extract if src/money/ imports istota.*"""
    src_money = REPO_ROOT / "src" / "money"
    pat = re.compile(r"^\s*(?:from|import)\s+istota(?:\.|\s|$)", re.MULTILINE)
    bad = []
    for py in src_money.rglob("*.py"):
        text = py.read_text()
        if pat.search(text):
            bad.append(py)
    if bad:
        print("ERROR: src/money/ has istota.* imports:", file=sys.stderr)
        for p in bad:
            print(f"  {p.relative_to(REPO_ROOT)}", file=sys.stderr)
        sys.exit(1)


def _rewrite_imports(text: str) -> str:
    """Rewrite `money.X` references back to `moneyman.X`."""
    # `from money.foo import ...` and `from money import ...`
    text = re.sub(
        r"^(\s*from\s+)money(\.|\s)",
        r"\1moneyman\2",
        text,
        flags=re.MULTILINE,
    )
    # `import money` and `import money.foo`
    text = re.sub(
        r"^(\s*import\s+)money(\.|\s|$)",
        r"\1moneyman\2",
        text,
        flags=re.MULTILINE,
    )
    # `patch("money.foo.bar")` and similar — common in tests
    text = re.sub(r'(["\'])money(\.[A-Za-z_])', r"\1moneyman\2", text)
    # Logger names: `logging.getLogger("money")` / `"money.foo"`
    # already covered by the previous quoted-string rule.
    return text


def _rewrite_frontend(text: str) -> str:
    """Rewrite SvelteKit imports to flatten /money/ subpaths."""
    # `from '$lib/money/foo'` → `from '$lib/foo'`
    text = re.sub(r"\$lib/money/", "$lib/", text)
    # base path: in standalone the app is mounted at /moneyman, not /istota
    # The frontend already constructs `${base}/money/api${path}`, so when the
    # standalone shell sets base = "/moneyman", URLs become /moneyman/money/api...
    # which is wrong. Rewrite `/money/api` → `/api` for the standalone case.
    text = text.replace("${base}/money/api", "${base}/api")
    return text


def _copy_tree(src: Path, dst: Path, *, exclude: set[str] | None = None) -> int:
    """Copy src → dst (rmtree-ing dst first), optionally skipping basenames.

    Returns the number of files copied.
    """
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        if exclude and p.name in exclude:
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        n += 1
    return n


def extract(dest: Path, version: str = "0.2.0") -> None:
    _check_isolation()
    dest = dest.resolve()
    git_dir = dest / ".git"
    git_existed = git_dir.exists()

    # Wipe everything except .git
    if dest.exists():
        for child in dest.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        dest.mkdir(parents=True)

    # 1. src/moneyman/ — money package with istota-integration files removed
    src_money = REPO_ROOT / "src" / "money"
    dst_pkg = dest / "src" / "moneyman"
    n_py = _copy_tree(src_money, dst_pkg, exclude=EXCLUDE_FROM_MONEY)
    for py in dst_pkg.rglob("*.py"):
        py.write_text(_rewrite_imports(py.read_text()))

    # 2. tests/ — flatten tests/money/ to top-level tests/
    src_tests = REPO_ROOT / TESTS_FROM
    dst_tests = dest / TESTS_TO
    n_tests = _copy_tree(src_tests, dst_tests)
    for py in dst_tests.rglob("*.py"):
        py.write_text(_rewrite_imports(py.read_text()))

    # 3. web/ — flatten money/ subpaths
    n_web = 0
    for src_rel, dst_rel in FRONTEND_PAIRS:
        src = REPO_ROOT / src_rel
        dst = dest / dst_rel
        if not src.exists():
            continue
        # Don't rmtree dst here; multiple pairs target the same parents.
        dst.mkdir(parents=True, exist_ok=True)
        for p in src.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            if target.suffix in {".ts", ".js", ".svelte"}:
                target.write_text(_rewrite_frontend(target.read_text()))
            n_web += 1

    # 4. pyproject.toml
    (dest / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version=version)
    )

    # 5. README placeholder
    (dest / "README.md").write_text(
        "# moneyman\n\n"
        "Standalone accounting service: Beancount ledger ops, invoicing, "
        "Monarch sync.\n\n"
        "This repo is a periodic snapshot extracted from the istota monorepo. "
        "Active development happens upstream; PRs should be opened there.\n\n"
        "## Install\n\n"
        "```\n"
        "uv sync --all-extras\n"
        "```\n\n"
        "## Run\n\n"
        "```\n"
        "MONEYMAN_CONFIG=config.toml uv run moneyman --help\n"
        "```\n"
    )

    msg = (
        f"Extracted: {n_py} py file(s) → src/moneyman/, "
        f"{n_tests} test file(s) → tests/, {n_web} web file(s) → web/."
    )
    if git_existed:
        msg += " (.git preserved)"
    print(msg)
    print(f"Output: {dest}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dest", required=True, type=Path,
                   help="Destination directory (overwritten, .git preserved)")
    p.add_argument("--version", default="0.2.0",
                   help="Version string to write into pyproject.toml")
    args = p.parse_args(argv)
    extract(args.dest, version=args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
