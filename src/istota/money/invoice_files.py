"""Invoice PDFs below the user's root, reached without following a link.

The output tree (``{data_dir}/{invoice_output}/<year>/Invoice-*.pdf``) is in
the user's workspace, which a task can write, and the web routes read,
rename and delete files in it from a process that can read other users'
trees and its own keys (ISSUE-775). So the tree is pinned by descriptor:
the walk starts at the user root (``Config.workspace_root``, the one
component trusted to follow a link) and every directory below it, ``money``
and the bot directory included, is opened ``O_NOFOLLOW``. ``data_dir`` is
only a spelling here: it was checked when the context was built, and a link
swapped in since then would be followed by anything anchored at it.

``invoice_output_parts`` is the one rule for the ``invoice_output`` setting:
relative, with no ``..``. ``config_store.check_invoicing_scalars`` refuses
anything else on write, and the readers here refuse a value stored before.

Generation writes through the same rule: ``open_dir_for_write`` creates and
opens the year folder from the user root, and ``name_taken`` and
``remove_file`` ask and remove through that walk rather than by path.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

from istota.sandbox.attachment_source import open_regular_at

DEFAULT_INVOICE_OUTPUT = "invoices/generated"


def invoice_output_parts(invoice_output: str | None) -> tuple[str, ...] | None:
    """``invoice_output`` as components below ``data_dir``, or None if it leaves it."""
    text = invoice_output or DEFAULT_INVOICE_OUTPUT
    if "\0" in text:
        return None
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    return pure.parts


def _parts_below(path: Path, root: Path) -> tuple[str, ...] | None:
    target = PurePath(os.path.normpath(path))
    spellings = [PurePath(os.path.normpath(root))]
    try:
        spellings.append(PurePath(root.resolve()))
    except (OSError, RuntimeError):
        pass
    for spelling in spellings:
        try:
            return target.relative_to(spelling).parts
        except ValueError:
            continue
    return None


@dataclass
class InvoicePdf:
    """One PDF as a name in a directory; ``dir_fd`` pins the directory when set."""

    directory: Path
    name: str
    dir_fd: int | None = None

    @property
    def path(self) -> Path:
        return self.directory / self.name

    def _at(self, name: str) -> str:
        return name if self.dir_fd is not None else str(self.directory / name)

    def rename(self, new_name: str) -> InvoicePdf:
        os.rename(
            self._at(self.name), self._at(new_name),
            src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd,
        )
        return InvoicePdf(self.directory, new_name, self.dir_fd)

    def unlink(self) -> None:
        os.unlink(self._at(self.name), dir_fd=self.dir_fd)


class OutputTree:
    """The invoice output directory, pinned; closes every fd it opened."""

    def __init__(self, fd: int, path: Path):
        self.fd = fd
        self.path = path
        self._year_fds: dict[str, int | None] = {}
        self._names: list[tuple[str, str]] | None = None

    def close(self) -> None:
        for fd in self._year_fds.values():
            if fd is not None:
                os.close(fd)
        self._year_fds.clear()
        os.close(self.fd)

    def year_fd(self, year: str) -> int | None:
        if year not in self._year_fds:
            try:
                self._year_fds[year] = os.open(
                    year, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd,
                )
            except (OSError, ValueError):
                self._year_fds[year] = None
        return self._year_fds[year]

    def pdf_names(self) -> list[tuple[str, str]]:
        """Every ``(year, name)`` regular ``*.pdf`` in a plain year folder, sorted."""
        if self._names is not None:
            return self._names
        names: list[tuple[str, str]] = []
        try:
            with os.scandir(self.fd) as scan:
                years = sorted(e.name for e in scan if e.is_dir(follow_symlinks=False))
        except OSError:
            years = []
        for year in years:
            fd = self.year_fd(year)
            if fd is None:
                continue
            try:
                with os.scandir(fd) as scan:
                    found = sorted(
                        e.name for e in scan
                        if e.name.endswith(".pdf") and e.is_file(follow_symlinks=False)
                    )
            except OSError:
                continue
            names.extend((year, name) for name in found)
        self._names = names
        return names

    def _pdf(self, year: str, name: str) -> InvoicePdf | None:
        fd = self.year_fd(year)
        if fd is None:
            return None
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return InvoicePdf(self.path / year, name, fd)

    def stored_parts(self, pdf_path: str | None) -> tuple[str, str] | None:
        """A row's stored ``pdf_path`` as ``(year, name)`` in this tree, by spelling."""
        if not pdf_path:
            return None
        parts = _parts_below(Path(pdf_path), self.path)
        if parts is None or len(parts) != 2 or ".." in parts:
            return None
        return parts[0], parts[1]

    def find(self, number: int, pdf_path: str | None = None) -> InvoicePdf | None:
        """The row's stored PDF if it is a regular file here, else the first by name.

        The by-name lookup is ``find_invoice_pdf``'s: the first
        ``Invoice-<n:06d>-*.pdf`` in the sorted year folders.
        """
        stored = self.stored_parts(pdf_path)
        if stored is not None:
            pdf = self._pdf(*stored)
            if pdf is not None:
                return pdf
        prefix = f"Invoice-{number:06d}-"
        for year, name in self.pdf_names():
            if name.startswith(prefix):
                pdf = self._pdf(year, name)
                if pdf is not None:
                    return pdf
        return None

    def open_pdf(self, pdf: InvoicePdf) -> int | None:
        return open_regular_at(pdf.dir_fd, pdf.name) if pdf.dir_fd is not None else None


def _dir_parts(user_root: Path, directory: Path) -> tuple[str, ...]:
    parts = _parts_below(Path(directory), Path(user_root))
    if parts is None:
        raise ValueError(f"{directory} is outside the user's tree")
    return parts


def open_dir_for_write(user_root: Path, directory: Path) -> int:
    """An fd on ``directory``, created if missing, walked from ``user_root`` with no link followed.

    Raises ``ValueError`` for a directory not below ``user_root`` by spelling,
    and ``OSError`` when a component is a link or not a directory. The
    caller closes the fd.
    """
    from istota.workspace.writes import open_or_create_dirs  # noqa: PLC0415 - import cost

    return open_or_create_dirs(Path(user_root), _dir_parts(user_root, directory))


def _open_existing_dir(user_root: Path, directory: Path) -> int | None:
    from istota.skills._loader import open_overlay_dir  # noqa: PLC0415 - import cost

    parts = _parts_below(Path(directory), Path(user_root))
    if parts is None:
        return None
    return open_overlay_dir(Path(user_root), *parts)


def name_taken(user_root: Path, path: Path) -> bool:
    """Whether anything is at ``path``, asked through the no-follow walk.

    A directory the walk refuses reads as free: the publish through the same
    walk refuses it, and a by-path ``lexists`` would follow the link.
    """
    fd = _open_existing_dir(user_root, path.parent)
    if fd is None:
        return False
    try:
        os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)


def remove_file(user_root: Path, path: Path) -> None:
    """Remove ``path`` through the no-follow walk; a refused walk removes nothing."""
    fd = _open_existing_dir(user_root, path.parent)
    if fd is None:
        return
    try:
        os.unlink(path.name, dir_fd=fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(fd)


class _NoOutputTree:
    """A tree holding nothing: what a confined lookup sees when the walk refused."""

    def find(self, number: int, pdf_path: str | None = None) -> InvoicePdf | None:
        return None


NO_OUTPUT_TREE = _NoOutputTree()


@contextmanager
def open_output_tree(
    user_root: Path | None, data_dir: Path | None, invoice_output: str | None,
) -> Iterator[OutputTree | None]:
    """The pinned output tree, or None when it is missing, linked or outside the root."""
    from istota.skills._loader import open_overlay_dir  # noqa: PLC0415 - import cost

    tree = None
    output = invoice_output_parts(invoice_output)
    if user_root is not None and data_dir is not None and output is not None:
        below = _parts_below(Path(data_dir), Path(user_root))
        if below is not None:
            fd = open_overlay_dir(Path(user_root), *below, *output)
            if fd is not None:
                tree = OutputTree(fd, Path(data_dir).joinpath(*output))
    try:
        yield tree
    finally:
        if tree is not None:
            tree.close()
