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

**Three questions, one signature table.** The paragraph above is the inline
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

The third is `is_model_visible`, and it is a question about **somebody else's
API** rather than about this deployment: will a model provider accept a
content block of this media type. Anthropic's Messages API and its
OpenAI-compatible layer both document `image/jpeg`, `image/png`, `image/gif`
and `image/webp`, and nothing else. It exists because `sniff_decodable`'s
safety depends on a converter — `prepare_image_attachments` turns HEIF into
JPEG before a model ever sees it — and the `Read` tool has no such step: the
tool server holds no image library, deliberately (`.claude/rules/sandbox.md`).
Inheriting the staging predicate there shipped `image/heic` to a provider that
documents no such format, with the tool call reporting success (ISSUE-520).

**`MODEL_VISIBLE_MEDIA_TYPES` is its own literal and must not be derived from
`INLINE_MEDIA_TYPES`.** The two hold the same four values today and answer
different questions — one is what a browser will draw on istota's own origin,
the other is what a provider documents — so a future widening motivated by
browser support would silently widen what ships to the provider, and a
provider adding a format would silently widen what `/chat/files` serves
inline. They are equal by coincidence, and the coincidence is not a
derivation. A test holds each against its own reason rather than against the
other.

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
    "MODEL_VISIBLE_MEDIA_TYPES",
    "SNIFF_BYTES",
    "image_dimensions",
    "is_model_visible",
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

MODEL_VISIBLE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
"""What a model provider will accept in an image content block.

Spelled out rather than built from `INLINE_MEDIA_TYPES`, for the reason the
module docstring gives: the two are equal today and answer different
questions, so deriving either from the other makes a change motivated by one
silently apply to the other. This one's source is the provider's own
documented format list.
"""

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


def is_model_visible(media_type: object) -> bool:
    """Whether a model provider will accept an image block of this type.

    Takes the **media type** rather than the leading bytes, unlike its two
    siblings, because its caller has already sniffed: `Read` needs to know
    what the file is in order to name the format in its refusal, so it sniffs
    wide with `sniff_decodable` and asks this about the answer. A
    bytes-taking fourth predicate would walk the signature table twice and
    still not tell a HEIC apart from a text file, which is the distinction
    the refusal message is built on.

    `object` rather than `str | None`, and False for anything that is not a
    media type this module names — same contract as the sniffers, for the
    same reason. The safe direction here is refusing: a wrong False costs a
    visible refusal on a file the model can ask about another way, and a
    wrong True is a provider error on the *next* request, attributed to
    nothing.
    """
    if not isinstance(media_type, str):
        return False
    return media_type in MODEL_VISIBLE_MEDIA_TYPES.values()


def image_dimensions(data: object) -> tuple[int, int] | None:
    """`(width, height)` read out of an image's own header, or None.

    A third question for the same table, and the one place in `src/` that asks
    it. `Read`'s image arm names the pixel dimensions beside the media type so
    a model looking at a picture knows what coordinate space it is naming
    points in; nothing computes from the answer, so `None` is an ordinary
    outcome and not a failure.

    **Header parsing, never a decode**, for the reason the module docstring
    gives: this runs inside the tool server, which holds no image library and
    must not grow one. The four raster formats put their size at a fixed
    offset or one short walk away. HEIF does not — ISO-BMFF needs a real box
    walk to reach `ispe`, which is a bounded loop over attacker-supplied data
    written for a case nobody has asked for — so it answers `None` and the
    caller says nothing about its size.

    `data` is the whole file rather than `SNIFF_BYTES` of it: JPEG stores its
    size in a frame header that sits past any fixed prefix, after a run of
    segments whose length the file chooses. Takes `object` and never raises,
    like its siblings.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    for reader in (_png_size, _gif_size, _webp_size, _jpeg_size):
        size = reader(raw)
        if size is not None:
            return size
    return None


def _u16be(raw: bytes, at: int) -> int:
    return (raw[at] << 8) | raw[at + 1]


def _positive(width: int, height: int) -> tuple[int, int] | None:
    # A zero dimension is a malformed header rather than a picture, and a
    # caller that renders "0x480" is reporting a measurement it did not make.
    return (width, height) if width > 0 and height > 0 else None


def _png_size(raw: bytes) -> tuple[int, int] | None:
    """IHDR is the first chunk by specification, so the offsets are fixed."""
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    return _positive(width, height)


def _gif_size(raw: bytes) -> tuple[int, int] | None:
    """The logical screen descriptor, little-endian, right after the header."""
    if len(raw) < 10 or raw[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width = int.from_bytes(raw[6:8], "little")
    height = int.from_bytes(raw[8:10], "little")
    return _positive(width, height)


def _webp_size(raw: bytes) -> tuple[int, int] | None:
    """One of three chunk layouts, which is why WebP needs its own branch.

    `VP8X` carries a canvas size and is what an animated or alpha-bearing file
    starts with; `VP8 ` is lossy and puts the size behind a three-byte frame
    tag and a sync code; `VP8L` is lossless and packs both dimensions, minus
    one, into 28 bits. All three are stored minus nothing except `VP8X` and
    `VP8L`, which store minus one — the `+ 1`s below are that and not an
    off-by-one.
    """
    if len(raw) < 16 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        return None
    chunk = raw[12:16]
    if chunk == b"VP8X" and len(raw) >= 30:
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return _positive(width, height)
    if chunk == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return _positive(width, height)
    if chunk == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
        bits = int.from_bytes(raw[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return _positive(width, height)
    return None


#: Markers that open a frame header carrying the image's size. Every `SOF`
#: except the three that share the range and mean something else: `C4` is a
#: Huffman table, `C8` is a reserved JPEG extension and `CC` is an arithmetic
#: coding table.
_JPEG_SOF = frozenset(
    m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC)
)
#: Standalone markers — no length field follows them, so the walk steps by two
#: rather than reading a segment length that is not there.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    """Walk the segment chain to the first frame header.

    Bounded by the buffer rather than by a segment count: each step advances by
    at least two bytes and a non-advancing length ends the walk, so a crafted
    file cannot spin here.
    """
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    at = 2
    limit = len(raw)
    while at + 3 < limit:
        if raw[at] != 0xFF:
            return None
        marker = raw[at + 1]
        if marker == 0xFF:
            # A fill byte; the specification allows any number of them.
            at += 1
            continue
        if marker in _JPEG_STANDALONE:
            at += 2
            continue
        if marker == 0xDA:
            # Start of scan: the entropy-coded data begins and no frame header
            # follows it that this walk could reach cheaply.
            return None
        length = _u16be(raw, at + 2)
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if at + 9 > limit:
                return None
            height = _u16be(raw, at + 5)
            width = _u16be(raw, at + 7)
            return _positive(width, height)
        at += 2 + length
    return None
