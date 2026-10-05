"""Routed turns on the scripted model endpoint.

A positional script answers requests in arrival order, so a poller's task can
take a scenario's turn and a flow with two tasks cannot say which one gets
which answer. A route (`{"when": marker, "turns": [...]}`) answers from the
request itself: the route whose marker sits furthest right in the first user
message, at the index given by the assistant messages already in the request.
These drive the endpoint over a real socket, with no Docker.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from testbed.services.model_endpoint import (
    UNSCRIPTED_TURN,
    _turn_frames,
    assistant_turns,
    choose_route,
    first_user_text,
    serve_script,
)


def _chat(endpoint, messages: list[dict], *, model: str = "m") -> bytes:
    request = urllib.request.Request(
        endpoint.url + "/chat/completions",
        data=json.dumps({"model": model, "stream": True, "messages": messages}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def _text(raw: bytes) -> str:
    """The content deltas of an OpenAI SSE reply, joined."""
    out = []
    for frame in raw.decode().split("\n\n"):
        if not frame.startswith("data: ") or frame == "data: [DONE]":
            continue
        payload = json.loads(frame[len("data: "):])
        for choice in payload.get("choices") or []:
            out.append(choice.get("delta", {}).get("content") or "")
    return "".join(out)


def _error(raw: bytes) -> str:
    for frame in raw.decode().split("\n\n"):
        if frame.startswith("data: {"):
            payload = json.loads(frame[len("data: "):])
            if "error" in payload:
                return payload["error"]["message"]
    return ""


def _ask(text: str, *, assistants: int = 0) -> list[dict]:
    """A request whose first user message is `text`, after `assistants` turns."""
    messages = [{"role": "system", "content": "the system half [e2e:sys]"},
                {"role": "user", "content": text}]
    for index in range(assistants):
        messages.append({"role": "assistant", "content": f"turn {index}"})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "ok"})
    return messages


class TestARouteIsChosenFromTheRequest:
    def test_the_first_user_message_picks_the_route(self):
        script = [
            {"when": "[e2e:a]", "turns": [{"text": "answer a"}]},
            {"when": "[e2e:b]", "turns": [{"text": "answer b"}]},
        ]
        with serve_script(script) as endpoint:
            second = _text(_chat(endpoint, _ask("mail b [e2e:b]")))
            first = _text(_chat(endpoint, _ask("mail a [e2e:a]")))

        assert (first, second) == ("answer a", "answer b")

    def test_the_system_half_is_not_read(self):
        """`_ask` puts `[e2e:sys]` in the system message; a route on it must
        not match, since the marker travels in the user half."""
        script = [{"when": "[e2e:sys]", "turns": [{"text": "wrong"}]}]
        with serve_script(script) as endpoint:
            got = _text(_chat(endpoint, _ask("no marker here")))

        assert got == UNSCRIPTED_TURN["text"]

    def test_the_rightmost_marker_wins_over_one_in_history(self):
        """A thread's second mail carries the first mail's marker in its
        history, ahead of its own request."""
        script = [
            {"when": "[e2e:first]", "turns": [{"text": "first mail"}]},
            {"when": "[e2e:second]", "turns": [{"text": "second mail"}]},
        ]
        body = "history: [e2e:first] ... the request: [e2e:second]"
        with serve_script(script) as endpoint:
            got = _text(_chat(endpoint, _ask(body)))

        assert got == "second mail"

    def test_a_marker_counts_at_its_last_occurrence(self):
        """A mail quoting an earlier one repeats that mail's marker after the
        other's: the last occurrence is the one compared."""
        script = [
            {"when": "[e2e:a]", "turns": [{"text": "a"}]},
            {"when": "[e2e:b]", "turns": [{"text": "b"}]},
        ]
        with serve_script(script) as endpoint:
            got = _text(_chat(endpoint, _ask("[e2e:a] then [e2e:b] then [e2e:a]")))

        assert got == "a"

    def test_the_turn_index_is_the_assistant_count(self):
        script = [{"when": "[e2e:t]", "turns": [{"text": "zero"}, {"text": "one"}]}]
        with serve_script(script) as endpoint:
            # Out of order on purpose: the position comes from the request, not
            # from a counter.
            one = _text(_chat(endpoint, _ask("[e2e:t]", assistants=1)))
            zero = _text(_chat(endpoint, _ask("[e2e:t]")))

        assert (zero, one) == ("zero", "one")

    def test_a_route_run_past_its_end_is_the_exhausted_frame_and_recorded(self):
        script = [{"when": "[e2e:t]", "turns": [{"text": "only"}]}]
        with serve_script(script) as endpoint:
            raw = _chat(endpoint, _ask("x [e2e:t]", assistants=1))
            unmatched = endpoint.marked_unmatched()

        assert "scripted endpoint exhausted" in _error(raw)
        assert [(u["reason"], u["when"], u["markers"]) for u in unmatched] == [
            ("exhausted", "[e2e:t]", ["[e2e:t]"]),
        ]


class TestARequestMatchingNoRoute:
    def test_takes_the_next_positional_item_first(self):
        script = [
            {"when": "[e2e:r]", "turns": [{"text": "routed"}]},
            {"text": "positional"},
        ]
        with serve_script(script) as endpoint:
            got = _text(_chat(endpoint, _ask("a poller task")))
            routed = _text(_chat(endpoint, _ask("[e2e:r]")))

            assert endpoint.unmatched == []
        assert (got, routed) == ("positional", "routed")

    def test_then_gets_the_default_turn_and_is_recorded(self):
        script = [{"when": "[e2e:r]", "turns": [{"text": "routed"}]}]
        with serve_script(script) as endpoint:
            unmarked = _text(_chat(endpoint, _ask("a poller task " + "x" * 300)))
            marked = _text(_chat(endpoint, _ask("mine [e2e:other]")))
            recorded = list(endpoint.unmatched)
            marked_only = endpoint.marked_unmatched()

        assert unmarked == marked == UNSCRIPTED_TURN["text"]
        assert [u["reason"] for u in recorded] == ["no_route", "no_route"]
        assert len(recorded[0]["excerpt"]) == 200
        assert recorded[0]["markers"] == []
        assert [u["markers"] for u in marked_only] == [["[e2e:other]"]]

    def test_unmatched_is_cleared_by_a_rescript(self):
        with serve_script([{"when": "[e2e:r]", "turns": []}]) as endpoint:
            _chat(endpoint, _ask("[e2e:none]"))
            endpoint.rescript([])
            assert endpoint.unmatched == []
            assert "0 unmatched" in endpoint.describe()


class TestAScriptWithNoRouteIsUnchanged:
    def test_frames_are_byte_identical_to_the_positional_answer(self):
        script = [
            {"text": "first answer"},
            {"tool_calls": [{"id": "c1", "name": "Bash", "arguments": {"command": "ls"}}]},
        ]
        with serve_script(script) as endpoint:
            first = _chat(endpoint, _ask("[e2e:ignored]"), model="mm")
            second = _chat(endpoint, _ask("anything"), model="mm")

        assert first == b"".join(_turn_frames(script[0], "mm"))
        assert second == b"".join(_turn_frames(script[1], "mm"))

    def test_running_off_the_end_names_the_served_index(self):
        with serve_script([{"text": "only"}]) as endpoint:
            _chat(endpoint, _ask("[e2e:x]"))
            raw = _chat(endpoint, _ask("[e2e:x]"))
            unmatched = list(endpoint.unmatched)

        assert "request 2 arrived but only 1 turn(s) were scripted" in _error(raw)
        assert unmatched == []


class TestTheTitleRequestIsUntouched:
    def test_a_title_request_takes_no_route_and_no_turn(self):
        title = {"model": "m", "messages": [{"role": "user", "content": "[e2e:r]"}],
                 "output_config": {"format": {
                     "type": "json_schema",
                     "schema": {"type": "object", "required": ["title"]},
                 }}}
        script = [{"when": "[e2e:r]", "turns": [{"text": "routed"}]}]
        with serve_script(script) as endpoint:
            request = urllib.request.Request(
                endpoint.url.removesuffix("/v1") + "/v1/messages",
                data=json.dumps(title).encode(),
                headers={"content-type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode()

            assert endpoint.titles_served == 1
            assert endpoint.served == 0
            assert endpoint.unmatched == []
        assert "Scripted session" in _anthropic_text(body)


def _anthropic_text(body: str) -> str:
    return "".join(
        json.loads(line[len("data: "):])["delta"]["text"]
        for line in body.splitlines()
        if line.startswith("data: ") and '"text_delta"' in line
    )


class TestTheAnthropicHalfRoutesToo:
    def test_a_routed_turn_and_an_exhausted_route(self):
        script = [{"when": "[e2e:a]", "turns": [{"text": "anthropic a"}]}]
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi [e2e:a]"}]}]
        with serve_script(script) as endpoint:
            request = urllib.request.Request(
                endpoint.url.removesuffix("/v1") + "/v1/messages",
                data=json.dumps({"model": "m", "messages": messages}).encode(),
                headers={"content-type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode()
            past = messages + [{"role": "assistant", "content": "x"}]
            request = urllib.request.Request(
                endpoint.url.removesuffix("/v1") + "/v1/messages",
                data=json.dumps({"model": "m", "messages": past}).encode(),
                headers={"content-type": "application/json"}, method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=10)

        assert _anthropic_text(body) == "anthropic a"
        assert caught.value.code == 400


class TestTheHelpers:
    def test_first_user_text_reads_text_blocks_and_skips_the_system_role(self):
        body = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "a"},
                                         {"type": "image"}, {"type": "text", "text": "b"}]},
            {"role": "user", "content": "later"},
        ]}
        assert first_user_text(body) == "a\nb"
        assert first_user_text({"messages": "nonsense"}) == ""

    def test_assistant_turns_counts_only_assistant_messages(self):
        body = {"messages": _ask("x", assistants=2)}
        assert assistant_turns(body) == 2
        assert assistant_turns({}) == 0

    def test_choose_route_breaks_a_tie_toward_the_longer_marker(self):
        short = {"when": "b]"}
        long = {"when": "[e2e:ab]"}
        assert choose_route([short, long], "text [e2e:ab]") is long
        assert choose_route([short, long], "nothing") is None
