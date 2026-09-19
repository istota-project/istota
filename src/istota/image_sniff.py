"""Which bytes istota will render as an image on its own origin.

`/chat/files` serves a file out of the caller's own workspace, and that
workspace holds user- and model-authored HTML and SVG. Serving those inline
would execute them on the app's origin against the session cookie that just
authorized the read, which is why every response there was `attachment`. The
narrow exception is a raster: a response carrying an explicit
`Content-Type: image/png` derived from a PNG signature, plus
`X-Content-Type-Options: nosniff`, cannot be reinterpreted as a document by any
browser. A file that is both a valid PNG and valid HTML is served as
`image/png` and stays an image.

**Decided from the first bytes, never from the name.** The extension is a
caller-supplied string on a file the model wrote; `avatars.py` already refuses
to trust one. An SVG named `.png` is the case that settles it, and it is a
test.

**Four formats**, matching what `avatars.ACCEPTED_FORMATS` admits minus HEIF.
HEIF is left out deliberately: browser support is not universal, and an inline
type that does not draw is worse than an attachment that does. SVG is XML text
and matches no signature here, which is the point of sniffing rather than
mapping a suffix.

**Two questions, one signature table.** The paragraph above is the inline
question and `sniff_raster` is its answer; it is unchanged and must stay that
way. `sniff_decodable` answers a wider and separate one — *can the image
pipeline decode this* — for a caller staging bytes off a messaging surface
into `image_attachments.prepare_image_attachments`. It admits HEIC and HEIF
beside the four, because `IMAGE_EXTENSIONS` already accepts them,
`_OUTPUT_FORMAT_BY_SOURCE` already maps them to JPEG and `pillow-heif` is a
core dependency — so refusing one here would be the staging sniff breaking a
path that already works. Widening `sniff_raster` instead would change what
`/chat/files` serves inline, on a browser-support argument nobody revisited.
Each caller asks the question it means.

**The HEIF arm is a brand allowlist, never a bare `ftyp` test.** An ISO-BMFF
file carries a box length at 0, `ftyp` at 4 and its major brand at 8 — and
MP4, 3GP and QuickTime are the same container with brands like `isom`, `mp42`
and `qt  `. Matching `ftyp` alone would type a video as an image and spend a
Pillow decode on it. Only the **major** brand is read: a compatible-brands
scan is a bounded loop over attacker-supplied data and is deliberately not
written for a case nobody has observed. A file outside the list sniffs as
`None`, which costs its sender a visible refusal rather than a wrong answer.

**No decode.** A magic-number test must not open the file with an image
library: `web_app.py` already runs its avatar decode on a serialized
single-worker executor because Pillow's peak memory is not bounded by the byte
cap it enforces, and a download route must not join that queue.

A leaf rather than a function inside `web_app.py`, so the skill side can hold
the same predicate about what counts as an inline-servable image without a
second copy of the table. stdlib-only, imports nothing, never raises — the
caller is a download route, where a traceback is a 500 on a file the user owns.
"""

from __future__ import annotations

__all__ = [
    "DECODABLE_MEDIA_TYPES",
    "EXTENSION_BY_MEDIA_TYPE",
    "INLINE_MEDIA_TYPES",
    "SNIFF_BYTES",
    "sniff_decodable",
    "sniff_raster",
]


INLINE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

DECODABLE_MEDIA_TYPES: dict[str, str] = {
    **INLINE_MEDIA_TYPES,
    "heic": "image/heic",
    "heif": "image/heif",
}
"""What `sniff_decodable` can answer. The inline four plus the HEIF family."""

EXTENSION_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}
"""The suffix a sniffed file has to be given to survive downstream screening.

`prepare_image_attachments` screens candidates by `Path(candidate).suffix`
against `IMAGE_EXTENSIONS`, and an image whose suffix is not in that set is
skipped in silence — the model then answers without the image and without
knowing one was sent. So a caller that has sniffed bytes names its own copy
from this table rather than from anything the file arrived wearing. The
relation `set(values()) <= image_attachments.IMAGE_EXTENSIONS` is pinned by
test rather than by an import, because this module imports nothing.
"""

# What a caller has to read off the front of the file. WebP needs 12 (`RIFF` at
# 0 and `WEBP` at 8); the rest need 8 or fewer. Rounded up, so a caller reading
# this many bytes never has to change when a format is added.
SNIFF_BYTES: int = 32

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")
_RIFF_SIGNATURE = b"RIFF"
_WEBP_FORM_TYPE = b"WEBP"

_FTYP_BOX_TYPE = b"ftyp"
_HEIF_MAJOR_BRANDS: dict[bytes, str] = {
    # The HEIF-family major brands. `heic`/`heix` are HEVC-coded images,
    # `hevc`/`hevx` image sequences, `heim`/`heis`/`hevm`/`hevs` the
    # scalable and multiview variants; `mif1`/`msf1` are the codec-agnostic
    # image and image-sequence brands a generic HEIF encoder writes.
    b"heic": DECODABLE_MEDIA_TYPES["heic"],
    b"heix": DECODABLE_MEDIA_TYPES["heic"],
    b"hevc": DECODABLE_MEDIA_TYPES["heic"],
    b"hevx": DECODABLE_MEDIA_TYPES["heic"],
    b"heim": DECODABLE_MEDIA_TYPES["heic"],
    b"heis": DECODABLE_MEDIA_TYPES["heic"],
    b"hevm": DECODABLE_MEDIA_TYPES["heic"],
    b"hevs": DECODABLE_MEDIA_TYPES["heic"],
    b"mif1": DECODABLE_MEDIA_TYPES["heif"],
    b"msf1": DECODABLE_MEDIA_TYPES["heif"],
}


def _as_head_bytes(head: object) -> bytes | None:
    """The leading bytes as `bytes`, or None for anything that is not them.

    Shared by both predicates so the never-raises contract has one
    implementation: a second copy is how one of the two grows a caller that
    hands it `None` and gets an `AttributeError` instead of a miss.
    """
    if isinstance(head, memoryview):
        # `bytes(mv)` on a view whose itemsize is not 1 *reinterprets* the
        # underlying buffer rather than raising, so a view of an `array("I")`
        # whose leading four bytes happen to match would be answered as an
        # image. A non-contiguous view is not the file's leading bytes either.
        if head.itemsize != 1 or not head.c_contiguous:
            return None
        return head.tobytes()
    if isinstance(head, bytearray):
        return bytes(head)
    if isinstance(head, bytes):
        return head
    return None


def _raster_type(head: bytes) -> str | None:
    """The four inline formats, decided at offset zero."""
    if head.startswith(_PNG_SIGNATURE):
        return INLINE_MEDIA_TYPES["png"]
    if head.startswith(_JPEG_SIGNATURE):
        return INLINE_MEDIA_TYPES["jpeg"]
    if head.startswith(_GIF_SIGNATURES):
        return INLINE_MEDIA_TYPES["gif"]
    # A RIFF container holds WAVE and AVI too, so the form type at offset 8 is
    # the half that says it is an image.
    if head.startswith(_RIFF_SIGNATURE) and head[8:12] == _WEBP_FORM_TYPE:
        return INLINE_MEDIA_TYPES["webp"]
    return None


def _heif_type(head: bytes) -> str | None:
    """The HEIF family, decided by the ISO-BMFF major brand at offset 8."""
    if head[4:8] != _FTYP_BOX_TYPE:
        return None
    return _HEIF_MAJOR_BRANDS.get(head[8:12])


def sniff_raster(head: object) -> str | None:
    """The media type of a raster istota will render inline, or None.

    `head` is the first bytes of the file — at least `SNIFF_BYTES` where the
    file is that long, fewer where it is not. Any signature that does not match
    at offset zero is None: a leading space before a JPEG signature is a miss,
    because a browser given `image/jpeg` would not draw it either.

    **`object` rather than `bytes`, deliberately.** The module's contract is
    that it never raises, and a `bytes`-annotated parameter is a promise the
    type checker keeps and the runtime does not — `None.startswith` is an
    `AttributeError`, which on this caller is a 500 on a file the user owns.
    The annotation is the only thing standing between a future caller's
    `Optional[bytes]` and that, so the guard is the contract rather than
    defensive padding. It also takes `bytearray` and `memoryview`, which is
    what a caller reading into a preallocated buffer hands back — the same
    reasoning `kv_namespaces.is_reserved_namespace` and the `surfaces.py`
    readers state for their own widened signatures.

    **HEIF is not in this answer and must not be added to it.** Browser
    support is not universal and an inline type that does not draw is worse
    than an attachment that does; `sniff_decodable` is where a caller asking
    the other question goes.
    """
    data = _as_head_bytes(head)
    if data is None:
        return None
    return _raster_type(data)


def sniff_decodable(head: object) -> str | None:
    """The media type of anything the image pipeline can decode, or None.

    `sniff_raster`'s four plus HEIC and HEIF. The caller is staging bytes that
    arrived over a messaging surface for `image_attachments`, which converts
    HEIF to JPEG already — so the question is what Pillow can open here, not
    what a browser will draw on istota's own origin.

    Same contract as its sibling: offset zero, no decode, `object` rather than
    `bytes`, and it never raises.
    """
    data = _as_head_bytes(head)
    if data is None:
        return None
    return _raster_type(data) or _heif_type(data)
