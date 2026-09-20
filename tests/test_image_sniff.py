"""What counts as a raster istota will render on its own origin.

The predicate is the whole of the security argument behind serving a workspace
file `inline` instead of `attachment`, so the cases that matter most are the
misses: an SVG, an HTML document and anything that only *looks* like an image
because of where it sits in a filename.

The second half of the file is about the module's *other* question — what the
image pipeline can decode — and the two predicates are held apart deliberately:
`sniff_raster` must keep refusing HEIC, because that answer is what keeps
`/chat/files` narrow.
"""

import pytest

from istota.image_attachments import IMAGE_EXTENSIONS
from istota.image_sniff import (
    DECODABLE_MEDIA_TYPES,
    EXTENSION_BY_MEDIA_TYPE,
    INLINE_MEDIA_TYPES,
    SNIFF_BYTES,
    image_dimensions,
    sniff_decodable,
    sniff_raster,
)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
GIF87 = b"GIF87a\x01\x00\x01\x00\x80\x00\x00"
GIF89 = b"GIF89a\x01\x00\x01\x00\x80\x00\x00"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00"


HITS = [
    ("png", PNG, "image/png"),
    ("jpeg", JPEG, "image/jpeg"),
    ("jpeg minimal", b"\xff\xd8\xff", "image/jpeg"),
    ("gif87a", GIF87, "image/gif"),
    ("gif89a", GIF89, "image/gif"),
    ("webp", WEBP, "image/webp"),
]

MISSES = [
    ("empty", b""),
    ("truncated png signature", b"\x89PNG\r\n"),
    ("png signature with a wrong byte", b"\x89PNG\r\n\x1a\x0a"[:7] + b"\x00"),
    ("jpeg preceded by whitespace", b"  \xff\xd8\xff\xe0"),
    ("jpeg two bytes only", b"\xff\xd8"),
    ("riff container that is not webp", b"RIFF\x24\x00\x00\x00WAVEfmt "),
    ("riff truncated before the form type", b"RIFF\x24\x00\x00\x00WEB"),
    ("svg document", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
    ("svg with an xml declaration", b'<?xml version="1.0"?>\n<svg xmlns="http://ww'),
    ("html starting with a comment", b"<!-- hi --><html><body>x</body></html>"),
    ("html doctype", b"<!DOCTYPE html>\n<html>"),
    ("plain text", b"a,b\n1,2\n"),
    ("gif with the wrong version", b"GIF88a\x01\x00\x01\x00"),
    ("bmp", b"BM\x36\x00\x00\x00\x00\x00\x00\x00"),
    ("tiff little endian", b"II\x2a\x00\x08\x00\x00\x00"),
    ("pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
    ("heif", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"),
]


@pytest.mark.parametrize("name,head,expected", HITS, ids=[c[0] for c in HITS])
def test_admitted_signatures(name, head, expected):
    assert sniff_raster(head) == expected


@pytest.mark.parametrize("name,head", MISSES, ids=[c[0] for c in MISSES])
def test_everything_else_misses(name, head):
    assert sniff_raster(head) is None


def test_a_polyglot_is_decided_by_its_first_bytes():
    """A file that is a valid PNG and also parses as HTML is a PNG. The header
    is what makes that safe: an explicit `image/png` plus `nosniff` leaves the
    browser no route to reinterpret it as a document."""
    assert sniff_raster(PNG + b"<script>alert(1)</script>") == "image/png"


def test_html_carrying_a_png_signature_later_is_not_an_image():
    assert sniff_raster(b"<html>" + PNG) is None


def test_a_head_shorter_than_sniff_bytes_is_still_decided():
    """The route reads at most SNIFF_BYTES; a small file yields fewer."""
    assert sniff_raster(PNG[:8]) == "image/png"
    assert sniff_raster(WEBP[:12]) == "image/webp"


def test_extra_bytes_past_the_signature_change_nothing():
    assert sniff_raster(PNG + b"\x00" * 4096) == "image/png"


def test_sniff_bytes_covers_every_signature():
    """SNIFF_BYTES is what the caller reads, so it has to be at least as long
    as the longest signature — WebP's, which needs 12."""
    assert SNIFF_BYTES >= 12
    for _name, head, expected in HITS:
        assert sniff_raster(head[:SNIFF_BYTES]) == expected


def test_media_types_are_the_four_declared():
    assert set(INLINE_MEDIA_TYPES.values()) == {
        "image/png", "image/jpeg", "image/gif", "image/webp",
    }


def test_svg_is_not_in_the_inline_set():
    """SVG is a script-bearing document. It is XML text, so it matches no
    signature — which is the point, not an accident of the table."""
    assert "image/svg+xml" not in INLINE_MEDIA_TYPES.values()


@pytest.mark.parametrize(
    "bad", [None, "\x89PNG\r\n\x1a\n", 42, [], {}],
    ids=["none", "str", "int", "list", "dict"],
)
def test_never_raises_on_a_non_bytes_head(bad):
    """A leaf that never raises: the caller is a download route, and a
    traceback there is a 500 on a file the user owns."""
    assert sniff_raster(bad) is None


def test_a_bytearray_and_a_memoryview_are_accepted():
    """A caller reading from a file may hand back either."""
    assert sniff_raster(bytearray(PNG)) == "image/png"
    assert sniff_raster(memoryview(PNG)) == "image/png"


def test_a_memoryview_that_is_not_a_byte_view_is_refused():
    """`bytes(mv)` reinterprets rather than raising when itemsize is not 1, so
    a view over wider items whose leading bytes happen to match would otherwise
    be answered as an image."""
    import array

    buf = array.array("I", [0x474E5089, 0x0A1A0A0D])
    wide = memoryview(buf)
    assert wide.itemsize != 1
    # The reinterpretation is real — this is what the guard is refusing.
    assert bytes(wide).startswith(b"\x89PNG\r\n\x1a\n")
    assert sniff_raster(wide) is None
    # Cast back to bytes and it is admitted, so the guard is about the view's
    # shape rather than about the buffer.
    assert sniff_raster(wide.cast("B")) == "image/png"


def test_a_non_contiguous_memoryview_is_refused():
    strided = memoryview(bytearray(PNG + PNG))[::2]
    assert not strided.c_contiguous
    assert sniff_raster(strided) is None


# --------------------------------------------------------------------------
# The decodable question
# --------------------------------------------------------------------------

HEIC_HEADER = b"\x00\x00\x00\x1cftypheic\x00\x00\x00\x00mif1heic"
HEIF_HEADER = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic"
MP4_HEADER = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
QUICKTIME_HEADER = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  "
THREE_GP_HEADER = b"\x00\x00\x00\x18ftyp3gp5\x00\x00\x00\x00"


def _encoded_heic() -> bytes:
    """A real HEIC, generated rather than committed.

    A committed binary would be an opaque blob in a public repository, and
    producing one needs `pillow-heif` either way — which is a core dependency,
    so it is in the lean `--extra test` install this suite runs against.

    **It is not a substitute for a device file.** What it pins is that the
    brand allowlist matches what the encoder in this tree produces; nobody here
    has inspected an iPhone photo, which is why the allowlist covers eight
    brands rather than the one this fixture exercises.
    """
    import io

    pillow_heif = pytest.importorskip("pillow_heif", reason="HEIF encoder absent")
    from PIL import Image

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buf, format="HEIF")
    return buf.getvalue()


DECODABLE_HITS = [
    ("png", PNG, "image/png"),
    ("jpeg", JPEG, "image/jpeg"),
    ("gif87a", GIF87, "image/gif"),
    ("gif89a", GIF89, "image/gif"),
    ("webp", WEBP, "image/webp"),
    ("heic", HEIC_HEADER, "image/heic"),
    ("heif", HEIF_HEADER, "image/heif"),
]


@pytest.mark.parametrize(
    "name,head,expected", DECODABLE_HITS, ids=[c[0] for c in DECODABLE_HITS]
)
def test_the_pipeline_can_decode_these(name, head, expected):
    assert sniff_decodable(head) == expected


def test_an_encoder_produced_heic_is_admitted():
    """The library case, end to end: what `pillow-heif` writes here is what
    `image_attachments` opens, so the allowlist has to admit it."""
    assert sniff_decodable(_encoded_heic()) == "image/heic"


@pytest.mark.parametrize(
    "name,head",
    [
        ("mp4", MP4_HEADER),
        ("quicktime", QUICKTIME_HEADER),
        ("3gp", THREE_GP_HEADER),
        ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
        ("pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
        ("bare ftyp with no brand", b"\x00\x00\x00\x0cftyp"),
        ("ftyp at the wrong offset", b"xftypheic\x00\x00\x00\x00"),
        ("empty", b""),
    ],
    ids=[
        "mp4", "quicktime", "3gp", "svg", "pdf", "bare-ftyp", "wrong-offset",
        "empty",
    ],
)
def test_everything_else_is_not_decodable(name, head):
    """MP4, 3GP and QuickTime are the same ISO-BMFF container as HEIC, so a
    bare `ftyp` test would have typed a video as an image and spent a Pillow
    decode on it. Only the major brand at offset 8 admits a file."""
    assert sniff_decodable(head) is None


def test_the_inline_answer_still_refuses_heic():
    """The control for the two-predicate split. `/chat/files` must keep the
    narrow answer — browser support for HEIF is not universal, and an inline
    type that does not draw is worse than an attachment that does. If both
    predicates admitted a HEIC, one function would do and the split bought
    nothing."""
    assert sniff_raster(HEIC_HEADER) is None
    assert sniff_raster(HEIF_HEADER) is None
    assert sniff_raster(_encoded_heic()) is None
    assert "image/heic" not in INLINE_MEDIA_TYPES.values()
    assert "image/heif" not in INLINE_MEDIA_TYPES.values()


def test_the_decodable_set_is_the_inline_four_plus_the_heif_pair():
    assert set(DECODABLE_MEDIA_TYPES.values()) == set(
        INLINE_MEDIA_TYPES.values()
    ) | {"image/heic", "image/heif"}


def test_every_extension_it_names_survives_the_downstream_screen():
    """`prepare_image_attachments` screens candidates by suffix, so a staged
    file named from this table and not in that set is skipped in silence —
    the model answers without the image and without knowing one was sent.
    Pinned here rather than in the module, which imports nothing."""
    assert set(EXTENSION_BY_MEDIA_TYPE) == set(DECODABLE_MEDIA_TYPES.values())
    assert set(EXTENSION_BY_MEDIA_TYPE.values()) <= IMAGE_EXTENSIONS


def test_sniff_bytes_covers_the_major_brand():
    """A brand sits at 8..12, inside the 32 the existing comment anticipated."""
    assert SNIFF_BYTES >= 12
    for _name, head, expected in DECODABLE_HITS:
        assert sniff_decodable(head[:SNIFF_BYTES]) == expected


@pytest.mark.parametrize(
    "bad", [None, "\x89PNG\r\n\x1a\n", 42, [], {}],
    ids=["none", "str", "int", "list", "dict"],
)
def test_the_decodable_predicate_never_raises_either(bad):
    assert sniff_decodable(bad) is None


def test_the_decodable_predicate_takes_the_same_buffer_types():
    assert sniff_decodable(bytearray(HEIC_HEADER)) == "image/heic"
    assert sniff_decodable(memoryview(HEIC_HEADER)) == "image/heic"


class TestImageDimensions:
    """Header parsing, never a decode: this runs inside the tool server, which
    holds no image library and must not grow one."""

    @staticmethod
    def _encode(fmt, size, **kwargs):
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", size, (9, 8, 7)).save(buffer, format=fmt, **kwargs)
        return buffer.getvalue()

    @pytest.mark.parametrize("fmt,kwargs", [
        ("PNG", {}),
        ("JPEG", {}),
        ("JPEG", {"progressive": True}),
        ("GIF", {}),
        ("WEBP", {}),
        ("WEBP", {"lossless": True}),
    ])
    @pytest.mark.parametrize("size", [(1, 1), (137, 89), (1439, 812)])
    def test_it_agrees_with_pillow(self, fmt, kwargs, size):
        data = self._encode(fmt, size, **kwargs)
        assert image_dimensions(data) == size

    def test_a_jpeg_with_a_long_comment_before_the_frame_still_parses(self):
        # The size sits behind a run of segments whose length the file chooses,
        # which is why this takes the whole file rather than a fixed prefix.
        data = self._encode("JPEG", (200, 120), comment=b"x" * 20000)
        assert image_dimensions(data) == (200, 120)

    @pytest.mark.parametrize("data", [
        None,
        "not bytes",
        b"",
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"\x89PNG\r\n\x1a\n",
        b"GIF89a",
        b"RIFF\x00\x00\x00\x00WEBP",
        b"\xff\xd8",
        b"\xff\xd8\xff\xfe\x00\x02",
    ])
    def test_anything_it_cannot_read_is_none_rather_than_a_raise(self, data):
        assert image_dimensions(data) is None

    def test_a_heif_answers_none_rather_than_guessing(self):
        # ISO-BMFF needs a real box walk to reach `ispe`, which is a bounded
        # loop over attacker-supplied data written for a case nobody has asked
        # for. The caller says nothing about the size instead.
        heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 64
        assert sniff_decodable(heic[:SNIFF_BYTES]) == "image/heic"
        assert image_dimensions(heic) is None

    def test_a_zero_dimension_header_is_refused(self):
        import struct

        forged = bytearray(self._encode("PNG", (64, 48)))
        forged[16:24] = struct.pack(">II", 64, 0)
        assert image_dimensions(bytes(forged)) is None

    def test_a_jpeg_that_reaches_its_scan_without_a_frame_stops(self):
        # The walk is bounded by the buffer: each step advances by at least two
        # bytes, and start-of-scan ends it rather than reading entropy-coded
        # data as a segment length.
        data = b"\xff\xd8" + b"\xff\xda\x00\x02" + b"\x00" * 1000
        assert image_dimensions(data) is None
