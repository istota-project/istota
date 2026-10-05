"""Frames out of one GIF-as-MP4, tiled into one still image: the child process.

WhatsApp sends a GIF as an MP4 `videoMessage` with `gifPlayback` (ISSUE-647),
and the image pipeline decodes only stills. This turns one such file into one
JPEG holding up to `MAX_FRAMES` frames spread evenly over the clip, tiled left
to right and top to bottom, so the turn still carries exactly one image and
everything downstream (the inbox copy, `prepare_image_attachments`, OCR) is
the photo path unchanged.

**A parser on untrusted input, so it runs here and nowhere else.** The daemon
spawns this with `python -P -m istota.lib.gif_frames` and a deadline
(`transport.whatsapp.gif_frames`), never under the batch's write lock, the way
OCR and whisper run. On Linux the child caps its own address space before the
decoder is imported. The stream's declared size is refused above
`MAX_FRAME_PIXELS` before the clip is decoded, and the clip above
`MAX_PACKETS` packets or `MAX_SECONDS`. Opening the file can decode a few
frames to probe the stream, and a frame's real size can differ from the
declared one, so each decoded frame is held to the same cap; the probe itself
is bounded only by the address-space cap, and only on Linux.

**Two passes, so memory holds `MAX_FRAMES` thumbnails and no more.** The
first demuxes packets without decoding to count the frames; the second decodes
in order and keeps only the chosen ones, each shrunk to `CELL_EDGE` at once.

Imports the standard library, PyAV and Pillow, and nothing from `istota`, for
`ocr_leaf`'s reason: what a spawned process imports is paid on every spawn.
PyAV ships with the `whisper` extra; without it the child answers
`decoder_missing` and `doctor` says so. Prints one JSON object; never raises
past `main`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

__all__ = [
    "CELL_EDGE",
    "MAX_FRAMES",
    "MAX_FRAME_PIXELS",
    "MAX_PACKETS",
    "MAX_SECONDS",
    "chosen_indexes",
    "extract",
    "main",
]

#: Frames tiled into the one output image.
MAX_FRAMES = 4
#: The long edge each frame is shrunk to before it is kept.
CELL_EDGE = 512
#: The declared frame size refused before decoding (a 4K frame is 8.3 MP).
MAX_FRAME_PIXELS = 8_294_400
#: Packets counted before the clip is refused; a minute at 30 fps is 1,800.
MAX_PACKETS = 3_000
#: The declared duration refused, where the container states one.
MAX_SECONDS = 120
#: The address-space cap the child sets on itself, where the platform has one.
DEFAULT_MEMORY_MB = 1024


def chosen_indexes(count: int, wanted: int = MAX_FRAMES) -> list[int]:
    """Up to *wanted* frame indexes spread evenly over *count*, first and last
    included, in order and without repeats."""
    if count <= 0 or wanted <= 0:
        return []
    if count <= wanted:
        return list(range(count))
    if wanted == 1:
        return [count // 2]
    step = (count - 1) / (wanted - 1)
    return sorted({round(i * step) for i in range(wanted)})


def _cap_memory(megabytes: int) -> None:
    try:
        import resource
    except ImportError:
        return
    if not sys.platform.startswith("linux") or megabytes <= 0:
        return
    limit = megabytes * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass


def _error(code: str) -> dict:
    return {"status": "error", "error": code}


def _write_exclusive(dest: str, image) -> bool:
    try:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            image.save(handle, format="JPEG", quality=85)
    except Exception:  # noqa: BLE001 — any failure is the one answer below
        return False
    return True


def extract(src: str, dest: str) -> dict:
    """Write the tiled frames of *src* to *dest*, a new file. Never raises."""
    try:
        import av
        from PIL import Image
    except ImportError:
        return _error("decoder_missing")
    try:
        with av.open(src, mode="r") as container:
            streams = container.streams.video
            if not streams:
                return _error("no_video")
            stream = streams[0]
            width = int(stream.codec_context.width or 0)
            height = int(stream.codec_context.height or 0)
            if width <= 0 or height <= 0:
                return _error("unreadable")
            if width * height > MAX_FRAME_PIXELS:
                return _error("too_large")
            duration = container.duration
            if duration is not None and duration / 1_000_000 > MAX_SECONDS:
                return _error("too_long")
            count = 0
            for packet in container.demux(stream):
                if packet.size:
                    count += 1
                    if count > MAX_PACKETS:
                        return _error("too_long")
        wanted = set(chosen_indexes(count))
        if not wanted:
            return _error("no_frames")
        kept = []
        with av.open(src, mode="r") as container:
            stream = container.streams.video[0]
            index = 0
            last = max(wanted)
            for frame in container.decode(stream):
                if frame.width * frame.height > MAX_FRAME_PIXELS:
                    return _error("too_large")
                if index in wanted:
                    still = frame.to_image()
                    still.thumbnail((CELL_EDGE, CELL_EDGE))
                    kept.append(still.convert("RGB"))
                if index >= last:
                    break
                index += 1
        if not kept:
            return _error("no_frames")
    except Exception:  # noqa: BLE001 — a decoder failure is the file's, reported as one code
        return _error("unreadable")

    columns = 1 if len(kept) == 1 else 2
    rows = math.ceil(len(kept) / columns)
    cell_w = max(still.width for still in kept)
    cell_h = max(still.height for still in kept)
    sheet = Image.new("RGB", (cell_w * columns, cell_h * rows), (0, 0, 0))
    for position, still in enumerate(kept):
        row, column = divmod(position, columns)
        sheet.paste(still, (column * cell_w, row * cell_h))
    if not _write_exclusive(dest, sheet):
        return _error("write_failed")
    return {"status": "ok", "frames": len(kept), "source_frames": count}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.lib.gif_frames",
        description="Tile frames of one GIF-as-MP4 into one JPEG",
    )
    parser.add_argument("--memory-mb", type=int, default=DEFAULT_MEMORY_MB)
    parser.add_argument("src")
    parser.add_argument("dest")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    _cap_memory(args.memory_mb)
    try:
        result = extract(args.src, args.dest)
    except Exception:  # noqa: BLE001 — the contract is one JSON object
        result = _error("unreadable")
    print(json.dumps(result))
    sys.exit(0 if result.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
