"""Which bytes are an MP4 video the GIF frame extractor opens, asked of the bytes.

WhatsApp sends every GIF, Giphy's included, as an MP4 `videoMessage` with
`gifPlayback` (ISSUE-647). The staged copy is named by the sidecar from the
declared mimetype, so what it is has to be read off its first bytes before it
is handed to a decoder.

**The sniff confirms; it does not choose the pipeline.** An ISO-BMFF file with
brand `isom` is as much audio as video, which `audio_sniff` says too, so the
surface's declared message type decides which pipeline a file may enter and
this module only answers whether the bytes match. The `ftyp` arm is decided on
the major brand alone for the same reason.

No decode. stdlib-only, imports nothing, never raises.
"""

from __future__ import annotations

__all__ = ["EXTENSION_BY_MEDIA_TYPE", "SNIFF_BYTES", "sniff_video"]

EXTENSION_BY_MEDIA_TYPE: dict[str, str] = {"video/mp4": "mp4"}
"""The suffix a sniffed file is given."""

#: The `ftyp` brand ends at 12; rounded up as `audio_sniff` does.
SNIFF_BYTES: int = 64

_FTYP_BOX_TYPE = b"ftyp"
_MP4_VIDEO_MAJOR_BRANDS = frozenset({
    b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1",
    b"M4V ", b"dash", b"3gp4", b"3gp5", b"3gp6",
})


def sniff_video(head: object) -> str | None:
    """`video/mp4` for an ISO-BMFF file with a video major brand, else None.

    Takes `object` so a caller's `None`, or anything that is not the file's
    leading bytes, is a miss rather than an exception.
    """
    if isinstance(head, (bytearray, memoryview)):
        if isinstance(head, memoryview) and (head.itemsize != 1 or not head.c_contiguous):
            return None
        head = bytes(head)
    if not isinstance(head, bytes):
        return None
    if head[4:8] == _FTYP_BOX_TYPE and head[8:12] in _MP4_VIDEO_MAJOR_BRANDS:
        return "video/mp4"
    return None
