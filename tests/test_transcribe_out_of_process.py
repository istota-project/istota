"""Tests for the out-of-process OCR runner.

Same shape as `tests/test_whisper_out_of_process.py`, and for the same reason:
these never run Tesseract. They pin the contract with the CLI — what argv is
built, what comes back, and what happens when the child misbehaves.

The runner exists for memory isolation and an enforceable timeout, not to keep
`pytesseract` out of the daemon; `health/ocr._ocr_image` already imports it
in-process. So nothing here asserts an import floor.
"""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.transcribe.out_of_process import (
    DEFAULT_TIMEOUT_SECONDS,
    _parse_cli_json,
    ocr_image_out_of_process,
)

_POPEN = "istota.skills.transcribe.out_of_process.subprocess.Popen"


def _fake_proc(stdout="", stderr="", returncode=0, timeout_first=False):
    proc = MagicMock()
    proc.pid = 5151
    proc.returncode = returncode
    if timeout_first:
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="ocr", timeout=1),
            (stdout, stderr),
        ]
    else:
        proc.communicate.return_value = (stdout, stderr)
    return proc


def _ok_payload(text="Hello World", confidence=0.94, word_count=2):
    return json.dumps(
        {"status": "ok", "text": text, "confidence": confidence, "word_count": word_count},
        indent=2,
        ensure_ascii=False,
    )


class TestArgv:
    def test_it_runs_the_transcribe_cli_with_this_interpreter(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        argv = popen.call_args[0][0]
        assert argv[0] == sys.executable
        assert argv[1:4] == ["-P", "-m", "istota.skills.transcribe"]
        assert argv[4] == "ocr"

    def test_the_path_goes_last_behind_a_double_dash(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        argv = popen.call_args[0][0]
        assert argv[-2:] == ["--", "/tmp/shot.png"]

    def test_a_leading_dash_path_survives_the_real_parser(self):
        """`--` is what keeps a sender-named `-x.png` a path rather than a flag.

        argparse strips the separator itself, so this needs no parser change —
        only this test to pin it.
        """
        from istota.skills.transcribe import build_parser

        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("-x.png")

        argv = popen.call_args[0][0]
        args = build_parser().parse_args(argv[4:])
        assert args.command == "ocr"
        assert args.image_path == "-x.png"

    def test_preprocess_is_not_requested(self):
        """Automatic OCR runs the normal mode once, never a second enhanced pass."""
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        assert "--preprocess" not in popen.call_args[0][0]

    def test_the_child_gets_its_own_session(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        assert popen.call_args.kwargs["start_new_session"] is True

    def test_the_child_does_not_inherit_the_daemon_stdin(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_the_working_directory_is_kept_off_the_child_import_path(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        assert "-P" in popen.call_args[0][0]

    def test_utf8_is_pinned_on_both_halves_of_the_pipe(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            ocr_image_out_of_process("/tmp/shot.png")

        kwargs = popen.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_the_timeout_is_handed_to_communicate(self):
        with patch(_POPEN) as popen:
            proc = _fake_proc(stdout=_ok_payload())
            popen.return_value = proc
            ocr_image_out_of_process("/tmp/shot.png", timeout=12.5)

        assert proc.communicate.call_args.kwargs["timeout"] == 12.5

    def test_no_interpreter_to_spawn_is_an_error_not_a_crash(self):
        with patch("istota.skills.transcribe.out_of_process.sys.executable", ""):
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["status"] == "error"


class TestResults:
    def test_a_successful_result_comes_back_intact(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload("Account 12345"))
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result == {
            "status": "ok",
            "text": "Account 12345",
            "confidence": 0.94,
            "word_count": 2,
        }

    def test_an_empty_ocr_result_is_a_success_not_a_failure(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload("", 0, 0))
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["status"] == "ok"
        assert result["text"] == ""

    def test_utf8_text_survives_the_round_trip(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload("Grüße — 日本語"))
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["text"] == "Grüße — 日本語"

    def test_a_structured_error_from_a_nonzero_child_is_passed_through(self):
        payload = json.dumps({"status": "error", "error": "Image not found: /tmp/gone.png"})
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=payload, returncode=1)
            result = ocr_image_out_of_process("/tmp/gone.png")

        assert result["status"] == "error"
        assert "not found" in result["error"]

    def test_noise_before_the_json_does_not_break_parsing(self):
        noisy = "Warning: some library said something\n" + _ok_payload("Fine")
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=noisy)
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["status"] == "ok"
        assert result["text"] == "Fine"

    def test_unparseable_output_becomes_an_error_dict(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout="", stderr="segfault", returncode=139)
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["status"] == "error"
        assert "139" in result["error"]


class TestFailureModes:
    def test_a_spawn_failure_becomes_an_error_dict(self):
        with patch(_POPEN, side_effect=OSError("no fork for you")):
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["status"] == "error"
        assert "no fork for you" in result["error"]

    def test_a_hung_child_is_killed_by_group_and_reaped(self):
        with patch(_POPEN) as popen, patch(
            "istota.skills.transcribe.out_of_process.kill_process_group"
        ) as kill:
            proc = _fake_proc(stdout="", timeout_first=True)
            popen.return_value = proc
            result = ocr_image_out_of_process("/tmp/shot.png", timeout=3)

        kill.assert_called_once_with(5151)
        assert proc.communicate.call_count == 2
        assert result["status"] == "error"
        assert "timed out" in result["error"]

    def test_a_result_the_reap_recovers_is_used_not_discarded(self):
        with patch(_POPEN) as popen, patch(
            "istota.skills.transcribe.out_of_process.kill_process_group"
        ):
            popen.return_value = _fake_proc(stdout=_ok_payload("Recovered"), timeout_first=True)
            result = ocr_image_out_of_process("/tmp/shot.png", timeout=3)

        assert result["status"] == "ok"
        assert result["text"] == "Recovered"

    def test_a_reap_that_also_fails_still_returns_the_timeout_error(self):
        with patch(_POPEN) as popen, patch(
            "istota.skills.transcribe.out_of_process.kill_process_group"
        ):
            proc = MagicMock()
            proc.pid = 5151
            proc.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd="ocr", timeout=1),
                OSError("no child"),
            ]
            popen.return_value = proc
            result = ocr_image_out_of_process("/tmp/shot.png", timeout=3)

        assert result["status"] == "error"
        assert "timed out" in result["error"]

    def test_a_child_that_dies_mid_stream_is_killed_and_reported(self):
        with patch(_POPEN) as popen, patch(
            "istota.skills.transcribe.out_of_process.kill_process_group"
        ) as kill:
            proc = MagicMock()
            proc.pid = 5151
            proc.communicate.side_effect = ValueError("broken pipe")
            popen.return_value = proc
            result = ocr_image_out_of_process("/tmp/shot.png")

        kill.assert_called_once_with(5151)
        assert result["status"] == "error"

    @pytest.mark.parametrize(
        "boom",
        [OSError("nope"), ValueError("nope"), RuntimeError("nope")],
    )
    def test_it_never_raises(self, boom):
        with patch(_POPEN, side_effect=boom):
            result = ocr_image_out_of_process("/tmp/shot.png")

        assert result["status"] == "error"


class TestParseCliJson:
    def test_it_finds_the_outer_object_not_a_nested_one(self):
        stdout = '{"nested": {"status": "decoy"}, "status": "ok", "text": "x"}'
        assert _parse_cli_json(stdout)["text"] == "x"

    def test_empty_output_parses_to_nothing(self):
        assert _parse_cli_json("") is None

    def test_an_object_without_status_is_not_the_result(self):
        assert _parse_cli_json('{"text": "x"}') is None


class TestAgainstTheRealCli:
    """One pass through the actual seam, with nothing mocked.

    A path that does not exist is the one error the CLI reports before it would
    reach Tesseract, so the round trip is a bare interpreter start.
    """

    def test_a_real_spawn_round_trips_an_error_result(self):
        result = ocr_image_out_of_process("/nonexistent/definitely-not-here.png", timeout=120)

        assert result["status"] == "error"
        assert "not found" in result["error"].lower()


def test_the_default_timeout_is_the_shared_ocr_deadline():
    assert DEFAULT_TIMEOUT_SECONDS == 60.0
