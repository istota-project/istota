"""The playable-extension table exists in two languages; hold them equal.

``PLAYABLE_MEDIA_TYPES`` (``src/istota/feeds/models.py``) decides what the
poller stores as a media attachment. ``PLAYABLE_EXTENSIONS``
(``web/src/lib/feeds/embed.ts``) decides what the reader is willing to put in a
``<video>`` or ``<audio>``. They answer the same question on two sides of the
wire, and a key added to one only is invisible: the Python side writes a
``media_type``, the TypeScript side reads that type and never consults its own
table, so the drift only surfaces when a feed ships an attachment with no MIME
type at all — which is exactly the case the tables exist for.

Same shape and the same reason as ``tests/test_cli_render_cost.py`` +
``usageFormat.parity.test.ts``, which pin the cost render rule across the same
seam. Parsed with a regex rather than executed, so this needs no node.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from istota.feeds.models import PLAYABLE_MEDIA_TYPES

EMBED_TS = (
    Path(__file__).resolve().parents[1]
    / "web" / "src" / "lib" / "feeds" / "embed.ts"
)


def _typescript_table() -> dict[str, str]:
    """Extract ``PLAYABLE_EXTENSIONS`` as ``{ext: 'video'|'audio'}``."""
    source = EMBED_TS.read_text(encoding="utf-8")
    match = re.search(
        r"const PLAYABLE_EXTENSIONS:[^=]*=\s*\{(.*?)\n\};",
        source,
        re.DOTALL,
    )
    assert match, "PLAYABLE_EXTENSIONS not found in embed.ts — did it get renamed?"
    body = match.group(1)
    table = dict(re.findall(r"(\w+)\s*:\s*'(video|audio)'", body))
    assert table, f"parsed no entries out of PLAYABLE_EXTENSIONS body: {body!r}"
    return table


def _python_kinds() -> dict[str, str]:
    return {
        ext: mime.split("/", 1)[0]
        for ext, mime in PLAYABLE_MEDIA_TYPES.items()
    }


class TestExtensionTableParity:
    def test_the_two_tables_cover_the_same_extensions(self):
        assert set(_typescript_table()) == set(PLAYABLE_MEDIA_TYPES)

    def test_every_extension_agrees_on_video_versus_audio(self):
        # The one thing the TypeScript side decides on its own: which element
        # to render. Disagreeing here puts a video in an <audio> and loses the
        # picture with nothing to show for it.
        assert _typescript_table() == _python_kinds()

    def test_ogg_is_absent_from_both(self):
        # A container, not a format — `video/ogg` is registered, so guessing
        # audio would silently drop a Theora video's picture. `.oga` / `.ogv`
        # are unambiguous and stay. Named rather than left to the set
        # comparison above, which would happily agree on a wrong answer.
        assert "ogg" not in PLAYABLE_MEDIA_TYPES
        assert "ogg" not in _typescript_table()

    @pytest.mark.parametrize("ext,kind", sorted(_python_kinds().items()))
    def test_every_python_entry_declares_a_playable_kind(self, ext, kind):
        assert kind in ("video", "audio"), f"{ext} maps to a non-playable type"
