"""Image delivery on the two Claude Code brains (image-attachment-vision, Stage 3).

Neither CLI brain can be handed image bytes: `claude -p` takes text on stdin and
the tmux brain submits a bracketed paste. Their provider-supported image path is
Claude Code's own `Read` tool, which returns visual content rather than raw
bytes — so the delivery mechanism here is a prompt directive that *requires* one
`Read` per prepared image, plus the executor's post-run audit of the trace
(`tests/test_executor_images.py`) which is what stops the claim resting on the
model's compliance.

The other half is the degradation branch: a provider that rejects the image
payload must cost the task its images, not its answer.
"""

import typing
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.brain import BrainRequest, ClaudeCodeBrain
from istota.brain._types import ImageInput
from istota.brain.claude_code import (
    IMAGE_DIRECTIVE_HEADER,
    IMAGE_OMITTED_HEADER,
    IMAGE_WITHDRAWN_HEADER,
    build_image_prompt,
    is_image_payload_rejection,
)


def _image(tmp_path: Path, name: str = "shot.png") -> ImageInput:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return ImageInput(
        path=path.resolve(), media_type="image/png", display_name=name,
    )


def _req(tmp_path, *, images=(), allowed_tools=("Read", "Bash"), prompt="What is in this?"):
    return BrainRequest(
        prompt=prompt,
        allowed_tools=list(allowed_tools),
        cwd=tmp_path,
        env={},
        timeout_seconds=60,
        streaming=False,
        images=list(images),
    )


class _Run:
    """A `subprocess.run` stub recording the stdin text of every invocation."""

    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def __call__(self, cmd, **kwargs):
        self.prompts.append(kwargs.get("input") or "")
        out = self.outputs[min(len(self.prompts) - 1, len(self.outputs) - 1)]
        return typing.cast(
            typing.Any,
            type("R", (), {"stdout": out, "stderr": "", "returncode": 0})(),
        )


def _execute(req, runner):
    with patch("istota.brain.claude_code.subprocess.run", side_effect=runner):
        return ClaudeCodeBrain().execute(req)


# --------------------------------------------------------------------------
# the mandatory Read directive
# --------------------------------------------------------------------------


class TestInspectionDirective:
    def test_a_tool_bearing_image_request_carries_the_read_directive(self, tmp_path):
        img = _image(tmp_path)
        runner = _Run("done")

        _execute(_req(tmp_path, images=[img]), runner)

        sent = runner.prompts[0]
        assert IMAGE_DIRECTIVE_HEADER in sent
        assert "Read" in sent
        assert str(img.path) in sent

    def test_the_directive_precedes_the_users_own_request(self, tmp_path):
        img = _image(tmp_path)
        runner = _Run("done")

        _execute(_req(tmp_path, images=[img], prompt="MARKER-REQUEST"), runner)

        sent = runner.prompts[0]
        assert sent.index(IMAGE_DIRECTIVE_HEADER) < sent.index("MARKER-REQUEST")
        # The typed request survives verbatim; the directive is a prefix, not a
        # replacement.
        assert "MARKER-REQUEST" in sent

    def test_every_named_path_is_absolute_and_resolved(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        target = real / "a.png"
        target.write_bytes(b"x")
        img = ImageInput(
            path=(link / "a.png").resolve(),
            media_type="image/png",
            display_name="a.png",
        )

        text = build_image_prompt(_req(tmp_path, images=[img]))

        assert str(target.resolve()) in text
        assert str(link / "a.png") not in text

    def test_every_image_is_named(self, tmp_path):
        images = [_image(tmp_path, "one.png"), _image(tmp_path, "two.png")]

        text = build_image_prompt(_req(tmp_path, images=images))

        for img in images:
            assert str(img.path) in text

    def test_the_directive_says_a_failed_read_must_be_reported(self, tmp_path):
        text = build_image_prompt(_req(tmp_path, images=[_image(tmp_path)]))
        assert "fail" in text.lower()

    def test_the_directive_says_the_path_text_is_not_visual_access(self, tmp_path):
        text = build_image_prompt(_req(tmp_path, images=[_image(tmp_path)]))
        assert "not visual access" in text.lower()

    def test_a_request_with_no_images_is_left_byte_identical(self, tmp_path):
        req = _req(tmp_path, images=[], prompt="plain request")
        assert build_image_prompt(req) == "plain request"

    def test_a_request_with_no_images_reaches_the_cli_unchanged(self, tmp_path):
        runner = _Run("done")

        _execute(_req(tmp_path, images=[], prompt="plain request"), runner)

        assert runner.prompts == ["plain request"]

    def test_the_streaming_path_carries_the_directive_too(self, tmp_path):
        """The prompt goes to stdin on both paths; neither may skip it."""
        img = _image(tmp_path)
        req = _req(tmp_path, images=[img])
        req.streaming = True
        written: list[str] = []

        class _Stdin:
            def write(self, text):
                written.append(text)

            def close(self):
                pass

        class _Proc:
            pid = 4242
            returncode = 0
            stdin = _Stdin()
            stdout = iter(())
            stderr = iter(())

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with patch("istota.brain.claude_code.subprocess.Popen", return_value=_Proc()):
            ClaudeCodeBrain().execute(req)

        assert written and IMAGE_DIRECTIVE_HEADER in written[0]


class TestTextOnlyRequests:
    """`allowed_tools=[]` is a policy decision, not a gap to fill."""

    def test_a_text_only_image_request_gets_a_named_omission(self, tmp_path):
        img = _image(tmp_path, "photo.png")
        runner = _Run("done")

        _execute(_req(tmp_path, images=[img], allowed_tools=[]), runner)

        sent = runner.prompts[0]
        assert IMAGE_OMITTED_HEADER in sent
        assert IMAGE_DIRECTIVE_HEADER not in sent
        assert "photo.png" in sent

    def test_the_omission_names_the_basename_not_the_directory(self, tmp_path):
        img = _image(tmp_path, "photo.png")

        text = build_image_prompt(_req(tmp_path, images=[img], allowed_tools=[]))

        assert str(img.path) not in text
        assert "photo.png" in text

    def test_no_tools_are_enabled_implicitly(self, tmp_path):
        img = _image(tmp_path)
        seen: list[list[str]] = []

        def runner(cmd, **kwargs):
            seen.append(list(cmd))
            return typing.cast(
                typing.Any,
                type("R", (), {"stdout": "done", "stderr": "", "returncode": 0})(),
            )

        _execute(_req(tmp_path, images=[img], allowed_tools=[]), runner)

        cmd = seen[0]
        assert "--dangerously-skip-permissions" not in cmd
        assert "--allowedTools" not in cmd


# --------------------------------------------------------------------------
# the image-payload rejection branch
# --------------------------------------------------------------------------


_TOO_LARGE_413 = (
    'API Error: 413 {"type":"error","error":{"type":"request_too_large",'
    '"message":"request exceeds the maximum allowed size"}}'
)
_IMAGE_400 = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
    '"message":"messages.0.content.1.image: image exceeds 5 MB maximum"}}'
)
_UNRELATED_400 = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
    '"message":"model: unknown model name"}}'
)


class TestRejectionPredicate:
    def test_a_413_on_an_image_request_is_an_image_payload_rejection(self):
        assert is_image_payload_rejection(_TOO_LARGE_413, has_images=True)

    def test_a_413_without_images_is_not(self):
        assert not is_image_payload_rejection(_TOO_LARGE_413, has_images=False)

    def test_a_400_naming_an_image_is(self):
        assert is_image_payload_rejection(_IMAGE_400, has_images=True)

    def test_a_400_naming_nothing_image_related_is_not(self):
        assert not is_image_payload_rejection(_UNRELATED_400, has_images=True)

    def test_ordinary_text_is_not(self):
        assert not is_image_payload_rejection("here is your answer", has_images=True)


class TestReissueWithoutImages:
    def test_a_413_reissues_exactly_once_without_the_images(self, tmp_path):
        img = _image(tmp_path, "big.png")
        runner = _Run(_TOO_LARGE_413, "answered from text")

        result = _execute(_req(tmp_path, images=[img]), runner)

        assert len(runner.prompts) == 2
        assert IMAGE_DIRECTIVE_HEADER in runner.prompts[0]
        assert IMAGE_DIRECTIVE_HEADER not in runner.prompts[1]
        assert result.success
        assert result.result_text == "answered from text"

    def test_the_reissue_names_every_withdrawn_image_and_the_reason(self, tmp_path):
        images = [_image(tmp_path, "one.png"), _image(tmp_path, "two.png")]
        runner = _Run(_TOO_LARGE_413, "ok")

        _execute(_req(tmp_path, images=images), runner)

        second = runner.prompts[1]
        assert IMAGE_WITHDRAWN_HEADER in second
        assert "one.png" in second
        assert "two.png" in second
        assert "413" in second

    def test_the_reissue_keeps_the_users_typed_request(self, tmp_path):
        runner = _Run(_TOO_LARGE_413, "ok")

        _execute(
            _req(tmp_path, images=[_image(tmp_path)], prompt="MARKER-REQUEST"), runner
        )

        assert "MARKER-REQUEST" in runner.prompts[1]

    def test_a_rejection_on_the_reissue_falls_through_to_classification(
        self, tmp_path
    ):
        runner = _Run(_TOO_LARGE_413, _TOO_LARGE_413)

        result = _execute(_req(tmp_path, images=[_image(tmp_path)]), runner)

        assert len(runner.prompts) == 2, "no second re-issue"
        assert not result.success
        assert result.stop_reason == "error"
        # The provider diagnostic survives to the surfaced result.
        assert "413" in result.result_text

    def test_an_unrelated_400_is_not_reissued(self, tmp_path):
        runner = _Run(_UNRELATED_400, "should never run")

        result = _execute(_req(tmp_path, images=[_image(tmp_path)]), runner)

        assert len(runner.prompts) == 1
        assert not result.success

    def test_a_413_on_a_request_with_no_images_is_not_reissued(self, tmp_path):
        runner = _Run(_TOO_LARGE_413, "should never run")

        _execute(_req(tmp_path, images=[]), runner)

        assert len(runner.prompts) == 1

    def test_an_answer_that_quotes_a_provider_error_is_not_a_rejection(
        self, tmp_path
    ):
        """Summarising an incident must not cost the user their answer.

        The CLI reports a real rejection as a *failed* result (`claude -p`
        returns rc 0 with the banner as the answer, which
        `_success_frame_stop_reason` reclassifies), so success is the
        discriminator — without it a model explaining a 413 to the user is
        re-run without its images and the explanation is replaced.
        """
        answer = (
            "Your log line means the provider refused the upload: "
            f"{_TOO_LARGE_413}"
        )
        runner = _Run(answer, "should never run")

        result = _execute(_req(tmp_path, images=[_image(tmp_path)]), runner)

        assert len(runner.prompts) == 1
        assert result.success or "should never run" not in result.result_text

    def test_a_successful_image_request_runs_once(self, tmp_path):
        runner = _Run("fine")

        _execute(_req(tmp_path, images=[_image(tmp_path)]), runner)

        assert len(runner.prompts) == 1


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------


class TestTmuxPromptFile:
    """The same directive, delivered through the prompt file the paste loads."""

    def test_the_prompt_file_carries_the_directive_before_the_request(self, tmp_path):
        from istota.brain import tmux_claude

        img = _image(tmp_path)
        req = _req(tmp_path, images=[img], prompt="MARKER-REQUEST")

        text = tmux_claude.prompt_file_text(req)

        assert IMAGE_DIRECTIVE_HEADER in text
        assert text.index(IMAGE_DIRECTIVE_HEADER) < text.index("MARKER-REQUEST")
        assert str(img.path) in text

    def test_a_request_with_no_images_writes_the_prompt_unchanged(self, tmp_path):
        from istota.brain import tmux_claude

        req = _req(tmp_path, images=[], prompt="plain request")

        assert tmux_claude.prompt_file_text(req) == "plain request"

    def test_the_brain_writes_that_text_to_the_prompt_file(self, tmp_path):
        """One file, one bracketed paste — the directive rides in, not beside."""
        import inspect

        from istota.brain import tmux_claude

        src = inspect.getsource(tmux_claude.TmuxClaudeBrain)
        assert "prompt_file.write_text(prompt_file_text(req)" in src
        # Still exactly one load-buffer submission per run.
        assert src.count('self._tmux("load-buffer"') <= 1


@pytest.mark.parametrize("header", [
    IMAGE_DIRECTIVE_HEADER, IMAGE_OMITTED_HEADER, IMAGE_WITHDRAWN_HEADER,
])
def test_the_three_headers_are_distinct(header):
    assert header.strip()
    assert len({IMAGE_DIRECTIVE_HEADER, IMAGE_OMITTED_HEADER, IMAGE_WITHDRAWN_HEADER}) == 3
