"""ACF values on a post write: ``--acf-file``, ``--acf-set``, the ``$upload``
marker, ``--featured-image``, and the read-back comparison.

**Whole values** (spec §6.2). The values given are top-level fields; each one
named replaces that field's whole value, and a field not named is left alone,
since ACF writes only the fields in the ``acf`` object. ``--acf-set FIELD=JSON``
is applied over ``--acf-file``, field by field. A repeater or flexible-content
field is replaced as a whole list, so editing one row is a get, a change and a
write of the whole field.

**Uploads are explicit.** Anywhere in a value, ``{"$upload": "PATH", "alt":
..., "title": ..., "caption": ...}`` is replaced by the id of the attachment
uploaded from PATH. Nothing else is ever read as a path. PATH never passed the
argv stamp, so it is resolved here against `_hostpath.egress_roots()` with
`memory_refusal` applied (sandbox.md: content-supplied components are
re-resolved), then opened by `media.open_upload`, all in the precheck before
the vault fetch. Every upload of a call happens before the post write; a
failure stops the write and lists what was uploaded.

**Fences come off.** A string copied whole out of a fenced read (``get
--output``, ``options get``) is unwrapped to its body before it is sent, and one
still carrying a marker or its redaction is refused (`unwrap_markers`).

**Where a field must be.** A type whose collection schema has no ``acf``
property has no field group with "Show in REST API" on, and a name not in that
schema's ``properties`` is a field WordPress would drop. Both refuse with
``acf_not_in_rest`` before any write, which also drops the discovery cache so
a group switched on a moment ago is seen on the next call.
"""

from __future__ import annotations

import json
import re
from istota.untrusted import MARKER_REDACTION, has_marker, unframe_untrusted

from . import media
from .client import LABEL, WordPressError
from .common import read_text_file
from .discovery import ACF_NOTE, acf_schema

UPLOAD_KEY = "$upload"
#: Marker keys and the attachment field each one sets.
_MARKER_FIELDS = {"alt": "alt_text", "title": "title", "caption": "caption"}
MAX_ACF_BYTES = 8 * 1024 * 1024
_FIELD_RE = re.compile(r"\A[A-Za-z0-9_\-]{1,64}\Z")


class Slot:
    """Where an uploaded attachment's id goes, until the upload has run."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index

    def __repr__(self) -> str:
        return f"Slot({self.index})"


class Uploads:
    """The files one call uploads, deduplicated by path and metadata."""

    def __init__(self, cap: int, closers: list) -> None:
        self.cap = cap
        self.closers = closers
        self.sources: list[media.UploadSource] = []
        self._seen: dict[tuple, int] = {}

    def add(self, resolved: str, label: str, meta: dict) -> Slot:
        key = (resolved, tuple(sorted((k, v) for k, v in meta.items() if v is not None)))
        if key in self._seen:
            return Slot(self._seen[key])
        if len(self.sources) >= media.MAX_UPLOADS:
            raise WordPressError(
                f"More than {media.MAX_UPLOADS} files to upload in one call; split the write.",
                "validation_error",
            )
        source = media.open_upload(resolved, label, self.cap, meta)
        self.closers.append(source.close)
        self.sources.append(source)
        self._seen[key] = len(self.sources) - 1
        return Slot(self._seen[key])


def _marker(value: dict, where: str, uploads: Uploads) -> Slot:
    extra = sorted(set(value) - {UPLOAD_KEY, *_MARKER_FIELDS})
    if extra:
        raise WordPressError(
            f"An upload marker at {where} has keys it does not take: {', '.join(extra)}. "
            f"It takes {UPLOAD_KEY!r} and optionally alt, title and caption.",
            "validation_error",
        )
    path = value[UPLOAD_KEY]
    if not isinstance(path, str) or not path.strip():
        raise WordPressError(f"{UPLOAD_KEY} at {where} must be a file path.",
                             "validation_error")
    meta = {}
    for key, field in _MARKER_FIELDS.items():
        if key in value:
            if not isinstance(value[key], str):
                raise WordPressError(f"{key} at {where} must be a string.", "validation_error")
            meta[field] = value[key]
    resolved = media.egress_path(path, f"wordpress {UPLOAD_KEY} at {where}")
    return uploads.add(resolved, f"{UPLOAD_KEY} at {where}", meta)


def substitute_markers(value, uploads: Uploads, where: str = "acf"):
    """`value` with every ``$upload`` marker opened and replaced by a `Slot`."""
    if isinstance(value, dict):
        if UPLOAD_KEY in value:
            return _marker(value, where, uploads)
        return {k: substitute_markers(v, uploads, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_markers(v, uploads, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def unwrap_markers(value, where: str = "acf"):
    """`value` with every string that is exactly one fence replaced by its body.

    Reads fence every string the site wrote, and a model copies values out of
    them into writes. A string that is one whole fence is unwrapped; one that
    still holds a marker, or the text a marker was redacted to, is refused,
    since sending it would write that text to the site.
    """
    if isinstance(value, str):
        body = unframe_untrusted(value, LABEL)
        text = value if body is None else body
        if has_marker(text, LABEL) or MARKER_REDACTION in text:
            raise WordPressError(
                f"The value at {where} still carries an [UNTRUSTED WORDPRESS CONTENT] "
                f"marker or the text {MARKER_REDACTION!r}, and writing it would put that "
                f"on the site. Copy a fenced value whole, or give only the text between "
                f"the two markers.",
                "validation_error",
            )
        return text
    if isinstance(value, dict):
        return {k: unwrap_markers(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap_markers(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def fill(value, ids: dict[int, int]):
    """`value` with each `Slot` replaced by its uploaded attachment id."""
    if isinstance(value, Slot):
        return ids[value.index]
    if isinstance(value, dict):
        return {k: fill(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, ids) for v in value]
    return value


def parse_acf_set(pairs: list[str] | None) -> dict:
    out: dict = {}
    for pair in pairs or []:
        field, sep, raw = pair.partition("=")
        field = field.strip()
        if not sep or not _FIELD_RE.fullmatch(field):
            raise WordPressError(
                f"--acf-set takes FIELD=JSON with a field name; got {pair[:80]!r}.",
                "validation_error",
            )
        try:
            out[field] = json.loads(raw)
        except ValueError:
            raise WordPressError(
                f"--acf-set {field}: the value is JSON, so a string is quoted "
                f"(--acf-set {field}='\"text\"').",
                "validation_error",
            ) from None
    return out


def check(args, cap: int) -> None:
    """The precheck half: read the values, open every upload. Sets on `args`:

    ``acf_values`` (the merged top-level fields with `Slot`s, or None),
    ``uploads`` (an `Uploads`), and ``featured_slot`` (a `Slot` or None).
    """
    values: dict = {}
    if getattr(args, "acf_file", None):
        try:
            loaded = json.loads(read_text_file(args.acf_file, "--acf-file", MAX_ACF_BYTES))
        except ValueError:
            raise WordPressError("--acf-file is not JSON.", "validation_error") from None
        if not isinstance(loaded, dict):
            raise WordPressError("--acf-file must hold a JSON object of field names.",
                                 "validation_error")
        values.update(loaded)
    values.update(parse_acf_set(getattr(args, "acf_set", None)))

    uploads = Uploads(cap, args.closers)
    args.uploads = uploads
    args.deadline = media.call_deadline(args.config)
    args.featured_slot = None
    featured = getattr(args, "featured_image", None)
    if featured:
        args.featured_slot = uploads.add(featured, "--featured-image", {})
    values = unwrap_markers(values)
    args.acf_values = substitute_markers(values, uploads) if values else None


def check_schema(ctx, type_slug: str, values: dict | None) -> None:
    """Refuse ``acf_not_in_rest`` unless every field is in the type's REST schema."""
    if not values:
        return
    schema = acf_schema(ctx, type_slug)
    if schema is None:
        raise WordPressError(f"Type {type_slug!r}: {ACF_NOTE}", "acf_not_in_rest",
                             fields=sorted(values))
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    unknown = sorted(name for name in values if name not in props)
    if unknown:
        raise WordPressError(
            f"Type {type_slug!r} has no REST-visible ACF field named "
            f"{', '.join(unknown)}. Either the name is wrong (`describe --type "
            f"{type_slug}` lists the fields), or its field group has 'Show in REST "
            f"API' off.",
            "acf_not_in_rest", fields=unknown,
        )


def run_uploads(ctx, uploads: Uploads,
                deadline: float | None = None) -> tuple[dict[int, int], list[dict]]:
    """Upload every file: ``({slot index: attachment id}, report rows)``."""
    done = media.upload_all(ctx, uploads.sources, deadline)
    ids = {index: item["id"] for index, item in done.items()}
    report = [media.upload_report(uploads.sources[i], item) for i, item in done.items()]
    return ids, report


def same(sent, got) -> bool:
    """Whether a stored ACF value is the one sent, allowing for how ACF returns it.

    An image or file field sent as an id comes back as an object carrying that
    id under the ``standard`` REST format, and a number may come back as a
    string; neither is a change. A sent object is compared on its own keys,
    since a stored row carries the fields nobody wrote as nulls.
    """
    if isinstance(sent, dict):
        return isinstance(got, dict) and all(k in got and same(v, got[k]) for k, v in sent.items())
    if isinstance(sent, list):
        return (isinstance(got, list) and len(got) == len(sent)
                and all(same(a, b) for a, b in zip(sent, got)))
    if isinstance(sent, bool) or sent is None:
        return got == sent
    if isinstance(sent, int) and isinstance(got, dict):
        return got.get("id") == sent or got.get("ID") == sent
    if got == sent:
        return True
    return isinstance(got, (int, float, str)) and not isinstance(got, bool) and str(got) == str(sent)
