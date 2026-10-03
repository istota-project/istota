"""Which bytes count as audio the transcription pipeline decodes.

The heads come from real files in `tests/fixtures/audio/`, each a tenth of a
second of a sine tone encoded by ffmpeg, so a signature here is one an encoder
actually writes rather than one transcribed from a specification. The misses
are the cases that matter for the caller: an image that a WhatsApp message
claimed was audio, AMR (deliberately not admitted), and a short or empty file.
"""

from pathlib import Path

import pytest

import istota.skills as skills_pkg
from istota.lib.audio_sniff import (
    AUDIO_EXTENSIONS,
    EXTENSION_BY_MEDIA_TYPE,
    SNIFF_BYTES,
    sniff_audio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "audio"


def _head(name: str) -> bytes:
    with open(FIXTURES / name, "rb") as f:
        return f.read(SNIFF_BYTES)


FIXTURE_HITS = [
    ("voice.ogg", "audio/ogg"),
    ("tone.mp3", "audio/mpeg"),
    ("tone.aac", "audio/aac"),
    ("tone.m4a", "audio/mp4"),
    ("tone.wav", "audio/wav"),
    ("tone.flac", "audio/flac"),
    ("tone.webm", "audio/webm"),
]


@pytest.mark.parametrize("name,expected", FIXTURE_HITS, ids=[c[0] for c in FIXTURE_HITS])
def test_each_fixture_sniffs_as_its_type(name, expected):
    assert sniff_audio(_head(name)) == expected


@pytest.mark.parametrize("name,expected", FIXTURE_HITS, ids=[c[0] for c in FIXTURE_HITS])
def test_each_fixture_maps_to_its_own_suffix(name, expected):
    assert EXTENSION_BY_MEDIA_TYPE[expected] == Path(name).suffix.lstrip(".")


def test_fixtures_stay_small():
    for path in FIXTURES.iterdir():
        assert path.stat().st_size < 4096, path.name


def test_an_id3_tag_is_mp3():
    assert sniff_audio(b"ID3\x04\x00\x00\x00\x00\x00\x00" + _head("tone.mp3")) == "audio/mpeg"


@pytest.mark.parametrize(
    "second_byte,expected",
    [
        (0xFB, "audio/mpeg"),  # MPEG-1 layer III
        (0xF3, "audio/mpeg"),  # MPEG-2 layer III
        (0xE3, "audio/mpeg"),  # MPEG-2.5 layer III, eleven sync bits
        (0xF1, "audio/aac"),  # ADTS, MPEG-4, no CRC
        (0xF9, "audio/aac"),  # ADTS, MPEG-2, no CRC
        (0xF0, "audio/aac"),  # ADTS with CRC
        (0xFD, None),  # MPEG-1 layer I
        (0xFC, None),  # MPEG-1 layer II
        (0xEB, None),  # reserved version
        (0xE1, None),  # eleven sync bits with ADTS's layer bits
        (0x00, None),
    ],
)
def test_mp3_and_adts_are_told_apart_by_the_layer_bits(second_byte, expected):
    assert sniff_audio(bytes([0xFF, second_byte, 0x90, 0x00])) == expected


@pytest.mark.parametrize(
    "head",
    [
        bytes([0xFF, 0xFB, 0xF0, 0x00]),  # MP3, bitrate index 15
        bytes([0xFF, 0xFB, 0x0C, 0x00]),  # MP3, reserved sample-rate index
        bytes([0xFF, 0xF1, 0x34, 0x00]),  # ADTS, sampling-frequency index 13
        bytes([0xFF, 0xF1, 0x3C, 0x00]),  # ADTS, sampling-frequency index 15
    ],
)
def test_a_frame_header_with_reserved_fields_misses(head):
    assert sniff_audio(head) is None


MISSES = [
    ("empty", b""),
    ("one byte", b"\xff"),
    ("amr", b"#!AMR\n\x3c\x48\xf5\x5f"),
    ("amr wideband", b"#!AMR-WB\n"),
    ("png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
    ("jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"),
    ("webp sticker", b"RIFF\x24\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00"),
    ("riff truncated before the form type", b"RIFF\x24\x00\x00\x00WAV"),
    ("heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"),
    ("quicktime brand", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"),
    ("ogg preceded by a byte", b"\x00OggS\x00\x02"),
    ("plain text", b"hello, this is not audio"),
]


@pytest.mark.parametrize("name,head", MISSES, ids=[c[0] for c in MISSES])
def test_everything_else_misses(name, head):
    assert sniff_audio(head) is None


@pytest.mark.parametrize("bad", [None, "OggS", 42, ["O", "g"], object()])
def test_never_raises_on_a_non_bytes_head(bad):
    assert sniff_audio(bad) is None


def test_a_bytearray_and_a_memoryview_are_accepted():
    head = _head("voice.ogg")
    assert sniff_audio(bytearray(head)) == "audio/ogg"
    assert sniff_audio(memoryview(head)) == "audio/ogg"


def test_a_memoryview_that_is_not_a_byte_view_is_refused():
    import array

    words = array.array("I", [int.from_bytes(b"OggS", "little")] * 4)
    assert sniff_audio(memoryview(words)) is None


def test_extensions_equal_the_whisper_skill_file_types():
    """The executor pre-transcribes what whisper says it takes, and no more."""
    from istota.skills._loader import _load_skill_meta

    meta = _load_skill_meta(Path(skills_pkg.__file__).parent / "whisper")
    assert AUDIO_EXTENSIONS == set(meta.file_types)


def test_every_named_suffix_is_one_the_executor_transcribes():
    """A copy named outside the set is skipped by pre-transcription in silence."""
    assert set(EXTENSION_BY_MEDIA_TYPE.values()) <= AUDIO_EXTENSIONS


def test_the_executor_re_exports_the_same_set():
    from istota.executor import _AUDIO_EXTENSIONS

    assert _AUDIO_EXTENSIONS is AUDIO_EXTENSIONS
