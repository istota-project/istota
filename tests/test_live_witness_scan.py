"""The `live` witness's transcript scanning, tested where it is free.

`tests/live/test_claude_code_read_image.py` needs a real model call and a real
credential, so it runs by hand and costs money. Its assertions are two things
stacked: a claim about what Claude Code does, which only that run can settle,
and some JSON scanning, which can be wrong on its own and would then report the
product as broken (or, worse, green) for a reason that has nothing to do with
the product. This file holds the second half.

The transcripts here are written by hand from the frame shapes the CLI's
stream-json output uses, not captured from a run. That is a real limitation and
it points one way: if the live run finds a different shape, the fix belongs
here as well as there, and these cases become the record of it.
"""

import json

from tests.live.stream_json import (
    carries_image,
    iter_frames,
    read_calls,
    tool_result_content,
    transcript_summary,
)

_IMAGE_PATH = "/tmp/istota/attachments/task_7/0_shot.png"


def _assistant_read(call_id: str, path: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Opening the attachment."},
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Read",
                    "input": {"file_path": path},
                },
            ],
        },
    }


def _image_result(call_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        }
                    ],
                }
            ],
        },
    }


def _text_result(call_id: str, text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": text}
            ],
        },
    }


def _transcript(*frames: dict) -> str:
    lines = [json.dumps({"type": "system", "subtype": "init", "tools": ["Read"]})]
    lines += [json.dumps(frame) for frame in frames]
    lines.append(json.dumps({"type": "result", "subtype": "success", "result": "ok"}))
    return "\n".join(lines) + "\n"


class TestFraming:
    def test_non_json_lines_are_not_frames(self):
        raw = "warning: something on stdout\n" + _transcript()
        assert len(iter_frames(raw)) == 2

    def test_a_json_array_line_is_not_a_frame(self):
        """Only objects. A bare array would index like a frame and answer `.get`
        nowhere, which is a crash rather than a miss."""
        assert iter_frames('[1, 2, 3]\n{"type": "result"}\n') == [{"type": "result"}]


class TestReadCalls:
    def test_a_read_call_is_found_with_its_id_and_path(self):
        frames = iter_frames(_transcript(_assistant_read("toolu_1", _IMAGE_PATH)))
        assert read_calls(frames) == [("toolu_1", _IMAGE_PATH)]

    def test_another_tool_is_not_a_read(self):
        frame = _assistant_read("toolu_1", _IMAGE_PATH)
        frame["message"]["content"][1]["name"] = "Bash"
        assert read_calls(iter_frames(_transcript(frame))) == []

    def test_an_answer_that_names_the_path_in_prose_is_not_a_read(self):
        """The failure this witness exists to catch. A model that ignores the
        directive and writes about the file it was handed produces text, and
        text is not a recorded tool call."""
        frame = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"I read {_IMAGE_PATH}: it is red."}
                ],
            },
        }
        assert read_calls(iter_frames(_transcript(frame))) == []


class TestToolResults:
    def test_an_image_result_is_image_bearing(self):
        frames = iter_frames(
            _transcript(
                _assistant_read("toolu_1", _IMAGE_PATH), _image_result("toolu_1")
            )
        )
        assert carries_image(tool_result_content(frames, "toolu_1"))

    def test_a_text_result_is_not(self):
        """The negative control. A `Read` that came back as text — the CLI
        refusing a binary file, or an error — must not read as sight."""
        frames = iter_frames(
            _transcript(
                _assistant_read("toolu_1", _IMAGE_PATH),
                _text_result("toolu_1", "This file is binary and cannot be read"),
            )
        )
        assert tool_result_content(frames, "toolu_1") is not None
        assert not carries_image(tool_result_content(frames, "toolu_1"))

    def test_another_calls_image_result_does_not_answer_for_this_one(self):
        """Scoped by `tool_use_id`, not by "an image appears somewhere"."""
        frames = iter_frames(
            _transcript(
                _assistant_read("toolu_1", _IMAGE_PATH),
                _text_result("toolu_1", "could not read"),
                _assistant_read("toolu_2", "/tmp/istota/attachments/task_7/1_b.png"),
                _image_result("toolu_2"),
            )
        )
        assert not carries_image(tool_result_content(frames, "toolu_1"))
        assert carries_image(tool_result_content(frames, "toolu_2"))

    def test_a_missing_result_reads_as_none_rather_than_raising(self):
        frames = iter_frames(_transcript(_assistant_read("toolu_1", _IMAGE_PATH)))
        assert tool_result_content(frames, "toolu_1") is None


class TestSummary:
    def test_the_summary_names_the_frames_and_the_tools(self):
        frames = iter_frames(
            _transcript(
                _assistant_read("toolu_1", _IMAGE_PATH), _image_result("toolu_1")
            )
        )
        summary = transcript_summary(frames)
        assert "system/init" in summary
        assert "Read" in summary

    def test_no_tools_says_so_rather_than_rendering_empty(self):
        assert "tools called: none" in transcript_summary(iter_frames(_transcript()))
