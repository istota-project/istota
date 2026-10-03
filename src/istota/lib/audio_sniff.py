"""Which bytes are audio the transcription pipeline decodes, asked of the bytes.

The caller stages a file that arrived over a messaging surface (a WhatsApp
voice note) and has to name the inbox copy so the executor's pre-transcription
pass picks it up. That pass screens by suffix against `AUDIO_EXTENSIONS`, so a
file given a suffix outside the set is skipped in silence and the model answers
a voice note it never heard. The suffix therefore comes from this table and the
file's own first bytes, never from the name or the mimetype it arrived with.

**The sniff confirms; it does not choose the pipeline.** Bytes can satisfy more
than one sniffer — an ISO-BMFF file with brand `isom` is as much a video as an
audio file — so the surface's declared message type decides which pipeline a
file may enter, and this module only answers whether the bytes match. That is
also why the `ftyp` arm is decided on the major brand alone: whether the MP4
holds a sound track or a picture is the message type's question.

**The MPEG arms share a prefix.** MP3 frame sync and ADTS both start with the
twelve bits `0xFFF`; the layer bits (bits 1 and 2 of byte 1) separate them —
`01` is MPEG layer III, `00` is ADTS. MPEG 2.5 frames carry only an eleven-bit
sync, which is why the MP3 test reads three leading ones in byte 1 rather than
four. Layers I and II are not admitted: nothing on the surfaces this serves
sends them.

**AMR is not admitted.** It is in neither `AUDIO_EXTENSIONS` nor the whisper
skill's `file_types`, and whether the deployed decoder opens it is unverified,
so an AMR note sniffs as `None` and takes the surface's unsupported reply.

**`AUDIO_EXTENSIONS` lives here** rather than in `executor`, which re-exports
it as `_AUDIO_EXTENSIONS`, because `transport` must not import `executor` and
needs the set too. It equals the whisper skill's `file_types` frontmatter, held
by test rather than by an import, since this module imports nothing.

No decode. stdlib-only, imports nothing, never raises.
"""

from __future__ import annotations

__all__ = [
    "AUDIO_EXTENSIONS",
    "EXTENSION_BY_MEDIA_TYPE",
    "SNIFF_BYTES",
    "sniff_audio",
]


AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {"mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"}
)
"""Suffixes the executor pre-transcribes. Equal to whisper's `file_types`."""

EXTENSION_BY_MEDIA_TYPE: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/aac": "aac",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "audio/webm": "webm",
}
"""The suffix a sniffed file is given. Every value is in `AUDIO_EXTENSIONS`.

`ogg` rather than `opus` for a voice note: the suffix names the container.
"""

# Enough for every signature here (the `ftyp` brand ends at 12, `WAVE` at 12),
# rounded up so a caller reading this many bytes never changes when a format is
# added.
SNIFF_BYTES: int = 64

_OGG_SIGNATURE = b"OggS"
_ID3_SIGNATURE = b"ID3"
_RIFF_SIGNATURE = b"RIFF"
_WAVE_FORM_TYPE = b"WAVE"
_FLAC_SIGNATURE = b"fLaC"
_EBML_SIGNATURE = b"\x1a\x45\xdf\xa3"
_FTYP_BOX_TYPE = b"ftyp"
_MP4_AUDIO_MAJOR_BRANDS = frozenset(
    {b"M4A ", b"M4B ", b"mp42", b"isom", b"iso2", b"mp41", b"dash"}
)


def _as_head_bytes(head: object) -> bytes | None:
    """The leading bytes as `bytes`, or None for anything that is not them.

    A memoryview whose items are not single bytes, or that is not contiguous,
    is not the file's leading bytes, and `bytes()` on one reinterprets rather
    than raising — the same rule `image_sniff` applies.
    """
    if isinstance(head, memoryview):
        if head.itemsize != 1 or not head.c_contiguous:
            return None
        return head.tobytes()
    if isinstance(head, bytearray):
        return bytes(head)
    if isinstance(head, bytes):
        return head
    return None


def _mpeg_type(head: bytes) -> str | None:
    """MP3 or ADTS, told apart by the layer bits after a shared sync."""
    if len(head) < 2 or head[0] != 0xFF:
        return None
    second = head[1]
    third = head[2] if len(head) > 2 else None
    layer = (second >> 1) & 0b11
    if second & 0xF0 == 0xF0 and layer == 0b00:
        # Sampling-frequency indexes 13 to 15 are reserved.
        if third is not None and (third >> 2) & 0xF >= 13:
            return None
        return "audio/aac"
    # Eleven sync bits, a version other than the reserved `01`, layer III.
    version = (second >> 3) & 0b11
    if second & 0xE0 == 0xE0 and version != 0b01 and layer == 0b01:
        # Bitrate index 15 is "bad" and sample-rate index 3 is reserved.
        if third is not None and ((third >> 4) == 0xF or (third >> 2) & 0b11 == 0b11):
            return None
        return "audio/mpeg"
    return None


def sniff_audio(head: object) -> str | None:
    """The media type of audio the transcription pipeline decodes, or None.

    `head` is the first bytes of the file, `SNIFF_BYTES` where the file is that
    long. Every signature is matched at a fixed offset; a miss is None. Takes
    `object` rather than `bytes` so a caller's `None` is a miss and not an
    `AttributeError`.
    """
    data = _as_head_bytes(head)
    if data is None:
        return None
    if data.startswith(_OGG_SIGNATURE):
        return "audio/ogg"
    if data.startswith(_ID3_SIGNATURE):
        return "audio/mpeg"
    if data.startswith(_FLAC_SIGNATURE):
        return "audio/flac"
    if data.startswith(_EBML_SIGNATURE):
        return "audio/webm"
    # RIFF holds WebP and AVI as well, so the form type is what says audio.
    if data.startswith(_RIFF_SIGNATURE) and data[8:12] == _WAVE_FORM_TYPE:
        return "audio/wav"
    if data[4:8] == _FTYP_BOX_TYPE and data[8:12] in _MP4_AUDIO_MAJOR_BRANDS:
        return "audio/mp4"
    return _mpeg_type(data)
