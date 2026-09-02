"""The tool-server wire format, with no I/O at all.

`tool_server_protocol` is stdlib-only and touches no socket, which is what
makes this file possible: every rule the two ends depend on is asserted against
bytes rather than against a running server.
"""

import json
import struct

import pytest

from istota import tool_server_protocol as proto


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


ALL_EIGHT = [
    {"type": "hello", "protocol": 1, "cwd": "/w", "subprocess_env": {"A": "b"},
     "read_roots": ["/w"], "write_roots": ["/w"], "write_denied_roots": [],
     "deferred_dir": "/w/tmp", "bash_timeout_seconds": 120,
     "max_output_bytes": 30000, "max_read_lines": 2000,
     "max_read_bytes": 25000000, "bash_spill_full_output": True},
    {"type": "call", "id": "c1", "tool": "Read", "args": {"file_path": "/w/x"}},
    {"type": "abort", "id": "c1"},
    {"type": "shutdown"},
    {"type": "ready", "protocol": 1, "tools": ["Bash", "Read"]},
    {"type": "update", "id": "c1", "text": "partial output\n"},
    {"type": "result", "id": "c1", "content": [{"type": "text", "text": "ok"}],
     "is_error": False, "terminate": False},
    {"type": "fatal", "message": "boom"},
]


class TestRoundTrip:
    @pytest.mark.parametrize("message", ALL_EIGHT, ids=[m["type"] for m in ALL_EIGHT])
    def test_every_message_survives_a_round_trip(self, message):
        decoder = proto.FrameDecoder()
        assert decoder.feed(proto.encode(message)) == [message]
        decoder.close()  # a whole frame leaves nothing buffered

    def test_the_eight_are_the_whole_vocabulary(self):
        """A message type added to one side and not the other is refused by
        both decoders, so this list is the contract rather than a sample."""
        assert {m["type"] for m in ALL_EIGHT} == set(proto.ALL_MESSAGES)
        assert proto.DAEMON_MESSAGES.isdisjoint(proto.SERVER_MESSAGES)

    def test_several_messages_in_one_read(self):
        """Neither end reads in frame-sized pieces, so a coalesced read must
        yield every whole message in order."""
        blob = b"".join(proto.encode(m) for m in ALL_EIGHT)
        assert proto.FrameDecoder().feed(blob) == ALL_EIGHT

    def test_a_message_split_across_reads(self):
        decoder = proto.FrameDecoder()
        blob = proto.encode(ALL_EIGHT[1])
        for i in range(len(blob) - 1):
            assert decoder.feed(blob[i : i + 1]) == []
        assert decoder.feed(blob[-1:]) == [ALL_EIGHT[1]]

    def test_non_ascii_survives(self):
        """`ensure_ascii=False` plus an explicit utf-8 encode: the length
        prefix counts *bytes*, and a frame whose length was computed over
        characters would truncate every multi-byte payload."""
        message = {"type": "update", "id": "c1", "text": "café — 日本語"}
        assert proto.FrameDecoder().feed(proto.encode(message)) == [message]


class TestRefusals:
    def test_truncated_at_eof_is_a_fault_not_a_quiet_end(self):
        decoder = proto.FrameDecoder()
        assert decoder.feed(proto.encode(ALL_EIGHT[3])[:-2]) == []
        with pytest.raises(proto.ProtocolError, match="mid-frame"):
            decoder.close()

    def test_a_header_alone_is_still_a_fault(self):
        decoder = proto.FrameDecoder()
        assert decoder.feed(struct.pack(">I", 99)) == []
        with pytest.raises(proto.ProtocolError):
            decoder.close()

    def test_over_cap_is_refused_before_the_body_is_waited_for(self):
        """The reason the check is on the *length* and not on the buffer: a
        garbled or hostile 4-byte length must never become an allocation."""
        decoder = proto.FrameDecoder()
        with pytest.raises(proto.ProtocolError, match="cap"):
            decoder.feed(struct.pack(">I", proto.MAX_FRAME_BYTES + 1))

    def test_encode_refuses_an_over_cap_frame_too(self):
        """Both ends, not just the reader. A cap only on the reader lets a
        writer build a frame it can never deliver and then block on it."""
        huge = {"type": "update", "id": "c", "text": "x" * (proto.MAX_FRAME_BYTES + 10)}
        with pytest.raises(proto.ProtocolError, match="cap"):
            proto.encode(huge)

    def test_non_json_is_refused(self):
        with pytest.raises(proto.ProtocolError, match="not valid JSON"):
            proto.FrameDecoder().feed(_frame(b"{not json"))

    def test_invalid_utf8_is_refused(self):
        with pytest.raises(proto.ProtocolError, match="UTF-8"):
            proto.FrameDecoder().feed(_frame(b"\xff\xfe\x00"))

    def test_a_json_value_that_is_not_an_object_is_refused(self):
        for payload in (b"[]", b'"hi"', b"3", b"null"):
            with pytest.raises(proto.ProtocolError, match="must be an object"):
                proto.FrameDecoder().feed(_frame(payload))

    def test_an_unknown_type_is_refused_on_both_ends(self):
        with pytest.raises(proto.ProtocolError, match="unknown message type"):
            proto.FrameDecoder().feed(_frame(json.dumps({"type": "steer"}).encode()))
        with pytest.raises(proto.ProtocolError, match="unknown message type"):
            proto.encode({"type": "steer"})

    def test_a_missing_type_is_refused(self):
        with pytest.raises(proto.ProtocolError):
            proto.FrameDecoder().feed(_frame(b'{"id": "c1"}'))
        with pytest.raises(proto.ProtocolError):
            proto.encode({"id": "c1"})

    def test_encode_refuses_something_json_cannot_represent(self):
        """Tool arguments come off the model's JSON, so they are always
        serializable — but a `content` block built from a live object would
        otherwise raise `TypeError` from inside the writer's lock."""
        with pytest.raises(proto.ProtocolError, match="not JSON-serializable"):
            proto.encode({"type": "result", "id": "c", "content": [object()]})

    def test_a_zero_length_frame_is_legal_but_still_needs_a_type(self):
        with pytest.raises(proto.ProtocolError):
            proto.FrameDecoder().feed(_frame(b""))


class TestPendingBytes:
    def test_pending_is_zero_at_a_frame_boundary(self):
        decoder = proto.FrameDecoder()
        decoder.feed(proto.encode(ALL_EIGHT[2]))
        assert decoder.pending_bytes == 0

    def test_pending_counts_the_partial_tail(self):
        decoder = proto.FrameDecoder()
        blob = proto.encode(ALL_EIGHT[2])
        decoder.feed(blob + blob[:3])
        assert decoder.pending_bytes == 3


class TestTheContentCodec:
    """`content_to_wire` / `content_from_wire` live in `session/tools/remote.py`
    so that `tool_server_protocol` stays stdlib-only, but they are part of this
    wire format and both ends import the same two functions."""

    def test_an_image_block_survives_the_round_trip(self):
        from istota.llm.types import ImageContent, TextContent
        from istota.session.tools import remote

        blocks = [
            TextContent(text="hi"),
            ImageContent(media_type="image/png", data="Zm9v", display_name="a.png"),
        ]
        assert remote.content_from_wire(remote.content_to_wire(blocks)) == blocks

    def test_an_unknown_block_is_dropped_rather_than_raised_on(self):
        """Both ends ship from one tree, so an unknown type can only be a
        future format; losing one block beats failing the attempt over it."""
        from istota.session.tools import remote

        assert remote.content_from_wire([{"type": "video"}, "junk", None]) == []

    def test_the_codec_uses_this_modules_names(self):
        from istota.llm.types import ImageContent, TextContent
        from istota.session.tools import remote

        wire = remote.content_to_wire([TextContent(text="x"), ImageContent()])
        assert [b["type"] for b in wire] == [proto.CONTENT_TEXT, proto.CONTENT_IMAGE]
