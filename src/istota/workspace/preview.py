"""What the file viewer shows for a file's first bytes, whichever store holds it.

`/chat/files/preview` reads a workspace file and the Nextcloud preview reads
the caller's own account; both hand the same head to `classify`, so the viewer
gets one payload shape and one rule for what renders as what.
"""

import codecs
from pathlib import PurePosixPath

from istota.lib.audio_sniff import AUDIO_EXTENSIONS, sniff_audio
from istota.lib.image_sniff import sniff_raster
from istota.lib.video_sniff import sniff_video

PREVIEW_MAX_BYTES = 1_048_576


def classify(name: str, size: int, modified: str, head: bytes, *, truncated: bool) -> dict:
    """Classify at most `PREVIEW_MAX_BYTES` of a file's head by its bytes."""
    result = {
        "name": name,
        "size": size,
        "modified": modified,
        "kind": "binary",
        "truncated": False,
    }
    media_type = sniff_raster(head)
    kind = "image"
    if media_type is None and head.startswith(b"%PDF-"):
        kind, media_type = "pdf", "application/pdf"
    if media_type is None:
        audio_type = sniff_audio(head)
        video_type = sniff_video(head)
        extension = PurePosixPath(name).suffix.lower().lstrip(".")
        # A container signature does not prove its track types. The name
        # chooses a player only after the existing sniffers confirm media.
        if audio_type == "audio/mp4" or video_type == "video/mp4":
            kind = "audio" if extension in {"m4a", "m4b"} else "video"
            media_type = f"{kind}/mp4"
        elif audio_type == "audio/webm":
            audio_names = (AUDIO_EXTENSIONS - {"webm", "mp4"}) | {"weba", "m4b"}
            kind = "audio" if extension in audio_names else "video"
            media_type = f"{kind}/webm"
        elif audio_type == "audio/ogg" and extension == "ogv":
            kind, media_type = "video", "video/ogg"
        elif audio_type is not None:
            kind, media_type = "audio", audio_type
    if media_type is not None:
        result.update(kind=kind, media_type=media_type)
        return result
    if b"\x00" in head:
        return result
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        text = decoder.decode(head.removeprefix(codecs.BOM_UTF8), final=not truncated)
    except UnicodeDecodeError:
        return result
    result.update(kind="text", text=text, truncated=truncated)
    return result
