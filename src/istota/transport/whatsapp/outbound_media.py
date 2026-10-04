"""An image the model embedded in a reply, sent as a WhatsApp image (ISSUE-639).

**The declaration is the one web chat already reads**: a markdown image of a
`/chat/files` URL in the answer body (`config/guidelines/web.md`). A phone room
is stored in our `messages` table and rendered in web, so the stored answer
draws the picture there, and this module lifts the same link out of the text at
send time. One spelling, read through `webui.chat_files.chat_file_images`, so
the two surfaces cannot disagree about what counts as an embed.

**The bytes that leave are a copy, never the workspace file.** `stage_image`
resolves the path with `/chat/files`' own rule against the owner's workspace,
opens it with no link followed (`sandbox.attachment_source`), sniffs it, and
re-encodes it with Pillow into the media staging directory: EXIF orientation
applied and then dropped with the rest of the metadata, so a phone photo's GPS
fix and camera serial stay home, and the long edge capped. The sidecar is
handed the staged file's *name*, which it joins under its own staging root.

The caller owns the decision whether a send may carry media at all
(`deliver_whatsapp(attach_media=)`), because only it knows whose turn the text
answers. Everything here is per call and never raises.
"""

from __future__ import annotations

import io
import logging
import os
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...lib import image_sniff
from ...webui.chat_files import ChatFileError, chat_file_images, resolve_chat_file
from . import media as media_rules
from ._types import WhatsAppOutboundMedia

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

#: The long edge a sent image is scaled to. WhatsApp recompresses anything
#: larger on the recipient's side, so a bigger file costs upload time against
#: the bridge's send timeout and buys nothing on the phone.
OUTBOUND_MAX_EDGE = 2048

#: What a re-encoded copy may weigh. Well under the sidecar's `MAX_MEDIA_BYTES`
#: (a larger file is refused there) and small enough to upload inside the
#: bridge's send timeout. A PNG over it, a photo of noise or a dense scan, is
#: tried again as JPEG; one with transparency is not sent.
OUTBOUND_MAX_BYTES = 8 * 1024 * 1024

#: What `staged_name`'s random half looks like for an outbound copy. The
#: prefix keeps it apart from an inbound staged name in a log line; the sweep
#: treats both alike.
_OUTBOUND_PREFIX = "out-"

_SOURCE_FORMATS = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@dataclass(frozen=True)
class ImageSplit:
    """The text with its first embedded image taken out, two ways.

    `with_alt` has every embed replaced by its alt text, which is what goes
    out when no image can: a relative `/chat/files` URL is not a link on a
    phone. `without` drops the first embed and keeps the rest as alt text,
    which is the caption when the image does go.
    """
    path: str | None
    with_alt: str
    without: str


def split_image(text: str) -> ImageSplit:
    """Lift the first `/chat/files` image out of `text`. Never raises."""
    images = chat_file_images(text or "")
    if not images:
        return ImageSplit(None, text, text)
    with_alt: list[str] = []
    without: list[str] = []
    position = 0
    for index, (start, end, label, _path) in enumerate(images):
        with_alt += [text[position:start], label]
        without += [text[position:start], "" if index == 0 else label]
        position = end
    with_alt.append(text[position:])
    without.append(text[position:])
    return ImageSplit(
        images[0][3], "".join(with_alt).strip(), "".join(without).strip(),
    )


def stage_image(
    config: "Config", owner: str, path: str,
) -> WhatsAppOutboundMedia | None:
    """A metadata-free copy of `owner`'s workspace image, staged for the sidecar.

    `None` for anything that should go out as alt text instead: a path
    `/chat/files` would refuse, a link, a file that is not one of the four
    inline raster formats, one over the media cap or the pixel ceiling, a
    full staging directory. Each refusal is a warning naming the reason and
    never the path, which is the user's.
    """
    from ...sandbox.attachment_source import open_attachment  # noqa: PLC0415

    try:
        real = resolve_chat_file(config, owner, path)
        root = config.workspace_root(owner)
        opened = open_attachment(real, roots=[root]) if root is not None else None
    except ChatFileError as exc:
        logger.warning("whatsapp.outbound.media_refused reason=%s", exc.status)
        return None
    except Exception:
        logger.warning("whatsapp.outbound.media_refused reason=resolve", exc_info=True)
        return None
    if opened is None:
        logger.warning("whatsapp.outbound.media_refused reason=not_a_plain_file")
        return None
    fd, _source = opened
    try:
        with os.fdopen(fd, "rb") as handle:
            encoded = _reencode(handle)
    except Exception:
        logger.warning("whatsapp.outbound.media_refused reason=encode", exc_info=True)
        return None
    if encoded is None:
        return None
    data, mimetype, extension = encoded
    try:
        media_dir = media_rules.ensure_media_dir(media_rules.default_media_dir(config))
        if not media_rules.has_staging_room(media_dir, incoming_bytes=len(data)):
            return None
        name = f"{_OUTBOUND_PREFIX}{secrets.token_hex(8)}.{extension}"
        out = media_rules.open_staged_write(media_dir, name)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(out, view):]
        finally:
            os.close(out)
    except Exception:
        logger.warning("whatsapp.outbound.media_refused reason=stage", exc_info=True)
        return None
    return WhatsAppOutboundMedia(name=name, mimetype=mimetype, kind="image")


def _reencode(handle) -> tuple[bytes, str, str] | None:
    """`(bytes, mimetype, extension)` for one open source, or None.

    The gates run before the decode, as `image_attachments` runs them: the
    file size against the media cap, the signature against the inline raster
    set, and the header's declared pixel count against the same ceiling the
    inbound pipeline uses. A PNG stays a PNG, since a screenshot or a chart
    takes JPEG ringing on every edge; anything else becomes a JPEG unless it
    carries transparency.
    """
    from PIL import Image, ImageOps  # noqa: PLC0415

    from ...image_attachments import JPEG_QUALITY, MAX_SOURCE_PIXELS  # noqa: PLC0415

    size = os.fstat(handle.fileno()).st_size
    if size > media_rules.MAX_MEDIA_BYTES:
        logger.warning("whatsapp.outbound.media_refused reason=over_cap bytes=%d", size)
        return None
    head = handle.read(image_sniff.SNIFF_BYTES)
    source_type = image_sniff.sniff_raster(head)
    if source_type not in _SOURCE_FORMATS:
        logger.warning("whatsapp.outbound.media_refused reason=not_an_image")
        return None
    handle.seek(0)
    try:
        with Image.open(handle) as opened:
            width, height = opened.size
            if width * height > MAX_SOURCE_PIXELS:
                logger.warning("whatsapp.outbound.media_refused reason=too_many_pixels")
                return None
            # Scaled before anything else touches the pixels: `thumbnail`
            # decodes a JPEG at a reduced scale, so a 50 MP photo is never
            # held whole. The transpose after it still reads the orientation,
            # which the resize leaves in `info`.
            opened.thumbnail(
                (OUTBOUND_MAX_EDGE, OUTBOUND_MAX_EDGE), Image.Resampling.LANCZOS,
            )
            picture = ImageOps.exif_transpose(opened)
    except (Image.DecompressionBombError, MemoryError):
        logger.warning("whatsapp.outbound.media_refused reason=too_many_pixels")
        return None
    alpha = picture.mode in ("RGBA", "LA", "PA") or (
        picture.mode == "P" and "transparency" in picture.info
    )
    if alpha:
        picture = picture.convert("RGBA")
    else:
        picture = picture.convert("RGB")
    # Cleared rather than trusted to stay behind: some Pillow writers fall back
    # to `info` (PNG takes its EXIF and ICC from there), and nothing from the
    # source's metadata is meant to reach the copy.
    picture.info = {}
    if source_type == "image/png" or alpha:
        data = _save(picture, "PNG")
        if len(data) <= OUTBOUND_MAX_BYTES:
            return data, "image/png", "png"
        if alpha:
            logger.warning(
                "whatsapp.outbound.media_refused reason=over_cap bytes=%d", len(data),
            )
            return None
    data = _save(picture, "JPEG", quality=JPEG_QUALITY)
    if len(data) > OUTBOUND_MAX_BYTES:
        logger.warning("whatsapp.outbound.media_refused reason=over_cap bytes=%d", len(data))
        return None
    return data, "image/jpeg", "jpg"


def _save(picture, output: str, **options) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, output, optimize=True, **options)
    return buffer.getvalue()


def discard(config: "Config", staged: WhatsAppOutboundMedia | None) -> None:
    """Remove a staged copy once its send has settled. Never raises.

    The sidecar has read the file by the time an answer arrives, and a send
    that never got one leaves it to `media.prune_media_dir`'s orphan window.
    """
    if staged is None or not media_rules.is_staged_name(staged.name):
        return
    try:
        media_rules.discard_staged(
            media_rules.default_media_dir(config) / staged.name
        )
    except Exception:
        logger.warning("whatsapp.outbound.media_discard_failed", exc_info=True)


__all__ = ["ImageSplit", "OUTBOUND_MAX_EDGE", "discard", "split_image", "stage_image"]
