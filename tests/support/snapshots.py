"""Both forms of a ``db_backup`` snapshot, for tests that read or plant one.

A run uploads ``<rel>.gz``; a snapshot from before #988 is a plain ``<rel>``,
and retention keeps those for a week. ``compressed`` gives the compressed form
whichever one a run left, so a test written against it reads the same before
and after the writer changed.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path


def compressed(dated: Path, rel: str) -> Path:
    """``dated/<rel>.gz``, compressing a plain ``dated/<rel>`` into it first."""
    plain = dated / rel
    packed = plain.with_name(plain.name + ".gz")
    if plain.is_file():
        with open(plain, "rb") as src, gzip.open(packed, "wb") as dst:
            shutil.copyfileobj(src, dst)
        plain.unlink()
    assert packed.is_file(), f"no snapshot of {rel} in {dated}"
    return packed


def compress_tree(dated: Path) -> list[Path]:
    """Every plain ``*.db`` under ``dated`` compressed in place."""
    return [compressed(dated, str(p.relative_to(dated))) for p in sorted(dated.rglob("*.db"))]


def plain(dated: Path, rel: str) -> Path:
    """``dated/<rel>`` as a plain file, decompressing ``<rel>.gz`` into it."""
    target = dated / rel
    packed = target.with_name(target.name + ".gz")
    if packed.is_file():
        with gzip.open(packed, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        packed.unlink()
    assert target.is_file(), f"no snapshot of {rel} in {dated}"
    return target


def read_copy(snapshot: Path, out: Path) -> Path:
    """A plain copy of ``snapshot`` (either form) at ``out``, the original kept."""
    if snapshot.name.endswith(".gz"):
        with gzip.open(snapshot, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(snapshot, out)
    return out
