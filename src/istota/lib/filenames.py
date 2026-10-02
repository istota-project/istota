"""The one rule for turning a name somebody else chose into a filename.

A sender, an uploading browser or a remote file can name a file anything,
including characters a filesystem, Nextcloud or a desktop sync client will not
take. ISSUE-593: an email attachment whose MIME filename carried a carriage
return was written into the inbox as given, Nextcloud answered the upload with
a 404, and rclone retried it for a day while the file existed only in the VFS
cache. Before this module, four callers each had their own allowlist and the
email path had none.

Two modes:

- **Readable** (the default) keeps ordinary Unicode and spaces, because inbox
  names are read by people. It replaces control and invisible formatting
  characters with a space and collapses whitespace runs, replaces the
  characters Nextcloud and Windows clients refuse (``\\ / : * ? " < > |``)
  with ``_``, and trims leading dots and spaces and trailing ones.
- **ASCII only** keeps ``[A-Za-z0-9._-]`` and replaces everything else with
  ``_``, for a name headed into an HTTP header or a store that wants it plain.

Either way the result is a single path component: the input is cut to its
basename on ``/`` and ``\\`` first, and nothing returned is empty, ``.`` or
``..``. That is a filename rule and **not** a containment check. A caller
taking a path whose traversal must be refused rather than renamed (the email
attachment writer, ISSUE-447) checks containment on the raw name first and
sanitises after.

The result is idempotent: the rule is applied until it stops changing the
name, because callers layer it (the inbound poll sanitises an attachment name
and the inbox writer sanitises it again).

stdlib-only leaf, never raises.
"""

from __future__ import annotations

import re
import unicodedata

DEFAULT_MAX_STEM = 120
DEFAULT_FALLBACK = "file"
#: A filesystem's limit on one component, in bytes.
MAX_NAME_BYTES = 255

# C0, DEL, C1, zero-width marks, the Unicode line and paragraph separators, the
# bidirectional overrides that can make a name read as a different extension,
# the byte-order mark, and lone surrogates, which cannot be encoded to UTF-8.
# Code points rather than literals, so no invisible character sits in the source.
_INVISIBLE_RANGES = (
    (0x00, 0x1F), (0x7F, 0x9F), (0x200B, 0x200F), (0x2028, 0x202E),
    (0x2060, 0x2069), (0xFEFF, 0xFEFF), (0xD800, 0xDFFF),
)
_INVISIBLE = {
    code: " " for first, last in _INVISIBLE_RANGES for code in range(first, last + 1)
}
_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE_RUN = re.compile(r"\s+")
_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_UNDERSCORE_RUN = re.compile(r"_{2,}")
_SEPARATOR = re.compile(r"[/\\]")
# An extension starts with a letter or digit, so a stem's trailing `_` is never
# mistaken for one on a second pass.
_EXTENSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}")
# Nextcloud refuses these as partial-upload names, which is the ISSUE-593
# failure again, so the extension is folded into the stem.
_REFUSED_EXTENSIONS = frozenset({"part", "filepart"})


def _clean_stem(stem: str, ascii_only: bool) -> str:
    if ascii_only:
        stem = _ASCII_UNSAFE.sub("_", stem)
        stem = _UNDERSCORE_RUN.sub("_", stem)
        return stem.strip("._")
    stem = _FORBIDDEN.sub("_", stem)
    stem = _WHITESPACE_RUN.sub(" ", stem)
    return stem.lstrip(" .").rstrip(" .")


def _parts_once(raw: str, ascii_only: bool, max_stem: int) -> tuple[str, str]:
    # Surrogates go before normalising, which cannot take them.
    name = unicodedata.normalize("NFC", raw.translate(_INVISIBLE))
    name = _SEPARATOR.split(name)[-1].translate(_INVISIBLE)

    stem, dot, ext = name.rpartition(".")
    if not dot or not _EXTENSION.fullmatch(ext):
        stem, ext = name, ""
    elif ext.lower() in _REFUSED_EXTENSIONS:
        stem, ext = f"{stem}_{ext}", ""
    elif not stem.strip(" ._"):
        # `.png` is an extension with no name, not a name with no extension.
        return "", f".{ext}"
    suffix = f".{ext}" if ext else ""

    stem = _clean_stem(stem, ascii_only)
    stem = _clean_stem(stem[:max(1, max_stem)], ascii_only)
    while stem and len((stem + suffix).encode("utf-8")) > MAX_NAME_BYTES:
        stem = _clean_stem(stem[:-1], ascii_only)
    return stem, suffix


def filename_parts(
    raw: object,
    *,
    ascii_only: bool = False,
    max_stem: int = DEFAULT_MAX_STEM,
) -> tuple[str, str]:
    """``(stem, suffix)`` of the sanitised name, the suffix with its dot.

    The stem may come back empty, for a caller that supplies its own name in
    that case (web chat uses a random one). Most callers want `safe_filename`.
    """
    text = "" if raw is None else str(raw)
    parts = _parts_once(text, ascii_only, max_stem)
    # Every pass only removes or replaces characters, so this settles in one or
    # two; the bound is there so a mistake in the rule cannot hang a poll.
    for _ in range(8):
        if not parts[0]:
            break
        again = _parts_once("".join(parts), ascii_only, max_stem)
        if again == parts:
            break
        parts = again
    return parts


def safe_filename(
    raw: object,
    *,
    ascii_only: bool = False,
    max_stem: int = DEFAULT_MAX_STEM,
    fallback: str = DEFAULT_FALLBACK,
) -> str:
    """`raw` as one filename every storage backend here accepts.

    `fallback` is used when nothing of the stem survives. Its stem replaces the
    empty one and its suffix is used only when `raw` had none, so ``???.pdf``
    with ``fallback="document.bin"`` is ``document.pdf``. It is assumed safe.
    """
    stem, suffix = filename_parts(raw, ascii_only=ascii_only, max_stem=max_stem)
    if stem:
        return stem + suffix
    fb_stem, dot, fb_ext = fallback.rpartition(".")
    if not dot or not fb_stem:
        fb_stem, fb_ext = fallback, ""
    return fb_stem + (suffix or (f".{fb_ext}" if fb_ext else ""))
