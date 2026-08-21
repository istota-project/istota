"""Tests for the out-of-process whisper runner (ISSUE-273).

The point of the module under test is that transcription memory belongs to a
process that exits. These tests never spawn a real model — they pin the
contract with the CLI: what argv is built, what comes back, and what happens
when the child misbehaves.
"""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

from istota.skills.whisper.out_of_process import (
    _parse_cli_json,
    transcribe_audio_out_of_process,
)


_POPEN = "istota.skills.whisper.out_of_process.subprocess.Popen"


def _fake_proc(stdout="", stderr="", returncode=0, timeout_first=False):
    """A stand-in for the child process.

    `timeout_first` makes the first `communicate()` raise `TimeoutExpired` and
    the second one return, which is exactly the shape of a kill-then-reap.
    """
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    if timeout_first:
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="whisper", timeout=1),
            (stdout, stderr),
        ]
    else:
        proc.communicate.return_value = (stdout, stderr)
    return proc


def _ok_payload(text="hello there"):
    return json.dumps({"status": "ok", "text": text, "model": "small"}, indent=2)


class TestArgv:
    def test_it_runs_the_cli_with_this_interpreter(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        argv = popen.call_args[0][0]
        assert argv[0] == sys.executable
        assert argv[1:4] == ["-P", "-m", "istota.skills.whisper"]
        assert argv[4] == "transcribe"
        assert "/tmp/voice.mp3" in argv
        assert "--output" in argv and argv[argv.index("--output") + 1] == "json"

    def test_the_child_gets_its_own_session(self):
        """So a decoder that shells out dies with the child rather than
        outliving it holding the pipes open."""
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert popen.call_args.kwargs["start_new_session"] is True

    def test_model_and_language_are_passed_through(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3", model="tiny", language="en")

        argv = popen.call_args[0][0]
        assert argv[argv.index("--model") + 1] == "tiny"
        assert argv[argv.index("--language") + 1] == "en"

    def test_language_is_omitted_when_not_given(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert "--language" not in popen.call_args[0][0]

    def test_the_path_goes_last_behind_a_double_dash(self):
        """An attachment name is sender-supplied. Without the guard a file
        called `-x.mp3` is read by argparse as an option and the child exits 2
        on a usage error, which reaches the caller as "no usable result"."""
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("-x.mp3")

        argv = popen.call_args[0][0]
        assert argv[-2:] == ["--", "-x.mp3"]

    def test_a_leading_dash_path_survives_the_real_parser(self):
        """The guard is only worth anything if the CLI's own parser honours it,
        so drive the real one rather than trusting the convention."""
        from istota.skills.whisper.cli import build_parser

        args = build_parser().parse_args(
            ["transcribe", "--model", "auto", "--output", "json", "--", "-x.mp3"]
        )
        assert args.audio_path == "-x.mp3"

    def test_segments_are_not_requested(self):
        """One dict per word, pretty-printed, is megabytes on a long recording
        — buffered and decoded in the daemon for a caller that reads `text`."""
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert "--no-segments" in popen.call_args[0][0]

    def test_the_child_does_not_inherit_the_daemon_stdin(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_the_working_directory_is_kept_off_the_child_import_path(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert popen.call_args[0][0][1] == "-P"

    def test_no_interpreter_to_spawn_is_an_error_not_a_crash(self):
        with patch("istota.skills.whisper.out_of_process.sys.executable", ""):
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert result["status"] == "error"
        assert "interpreter" in result["error"]

    def test_utf8_is_pinned_on_both_halves_of_the_pipe(self):
        """A transcript is in whatever language was spoken. Inheriting the
        daemon's locale means a host under LANG=C drops a non-English voice
        message and keeps an English one."""
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        kwargs = popen.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_the_rest_of_the_environment_is_inherited(self):
        """`WHISPER_MAX_MODEL`, `RAM_HEADROOM_MB` and the HF cache location all
        reach the child through the environment, so replacing it wholesale
        would silently change which model gets picked."""
        with patch.dict(os.environ, {"WHISPER_MAX_MODEL": "tiny"}), patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert popen.call_args.kwargs["env"]["WHISPER_MAX_MODEL"] == "tiny"


class TestResult:
    def test_a_successful_transcript_comes_back_intact(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload("remind me to buy milk"))
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert result["status"] == "ok"
        assert result["text"] == "remind me to buy milk"

    def test_an_error_result_is_returned_as_the_cli_wrote_it(self):
        """The CLI exits 1 on an error result but still prints it. A non-zero
        exit with a parseable body is a result, not a broken child."""
        payload = json.dumps({"status": "error", "error": "Audio file not found: /tmp/x.mp3"})
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=payload, returncode=1)
            result = transcribe_audio_out_of_process("/tmp/x.mp3")

        assert result["status"] == "error"
        assert "Audio file not found" in result["error"]

    def test_library_chatter_on_stdout_does_not_break_parsing(self):
        """ctranslate2 and huggingface both warn from code that writes to the
        process's stdout directly rather than through Python's sys.stderr."""
        noisy = (
            "Warning: You are sending unauthenticated requests to the HF Hub\n"
            "BertModel LOAD REPORT from: {not json}\n" + _ok_payload("still parsed")
        )
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=noisy)
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert result["status"] == "ok"
        assert result["text"] == "still parsed"

    def test_unparseable_output_becomes_an_error_dict(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(
                stdout="", stderr="Traceback: ImportError: no faster_whisper", returncode=1
            )
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert result["status"] == "error"
        assert "no faster_whisper" in result["error"]

    def test_a_spawn_failure_becomes_an_error_dict(self):
        with patch(_POPEN, side_effect=OSError("no such interpreter")):
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert result["status"] == "error"
        assert "no such interpreter" in result["error"]

    def test_it_never_raises_on_a_child_that_dies_mid_stream(self):
        with patch(_POPEN) as popen:
            proc = _fake_proc()
            proc.communicate.side_effect = ValueError("pipe went away")
            popen.return_value = proc
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        assert result["status"] == "error"


class TestTimeout:
    def test_a_hung_child_is_killed_by_group_and_reported(self):
        with patch(_POPEN) as popen, patch(
            "istota.skills.whisper.out_of_process.kill_process_group"
        ) as kill:
            popen.return_value = _fake_proc(timeout_first=True)
            result = transcribe_audio_out_of_process("/tmp/voice.mp3", timeout=5)

        kill.assert_called_once_with(4242)
        assert result["status"] == "error"
        assert "timed out" in result["error"]

    def test_a_transcript_the_reap_recovers_is_used_not_discarded(self):
        """The likely hang is *after* the answer: the CLI prints its JSON and
        then wedges tearing down ctranslate2's threads. `communicate()` after a
        timeout returns what was already buffered, so reporting a flat failure
        there loses a transcription that in fact completed."""
        with patch(_POPEN) as popen, patch(
            "istota.skills.whisper.out_of_process.kill_process_group"
        ):
            popen.return_value = _fake_proc(
                stdout=_ok_payload("it did finish"), timeout_first=True
            )
            result = transcribe_audio_out_of_process("/tmp/voice.mp3", timeout=5)

        assert result["status"] == "ok"
        assert result["text"] == "it did finish"

    def test_a_reap_that_also_fails_still_returns_the_timeout_error(self):
        with patch(_POPEN) as popen, patch(
            "istota.skills.whisper.out_of_process.kill_process_group"
        ):
            proc = _fake_proc()
            proc.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd="whisper", timeout=1),
                subprocess.TimeoutExpired(cmd="whisper", timeout=1),
            ]
            popen.return_value = proc
            result = transcribe_audio_out_of_process("/tmp/voice.mp3", timeout=5)

        assert result["status"] == "error"
        assert "timed out" in result["error"]
        # The pipes are closed by hand when the reap could not do it.
        proc.stdout.close.assert_called_once()
        proc.stderr.close.assert_called_once()

    def test_a_child_that_fails_mid_stream_is_also_killed(self):
        """It is still running and still holding the memory this module exists
        to bound, and `start_new_session` means nothing else will signal it."""
        with patch(_POPEN) as popen, patch(
            "istota.skills.whisper.out_of_process.kill_process_group"
        ) as kill:
            proc = _fake_proc()
            proc.communicate.side_effect = [ValueError("pipe went away"), ("", "")]
            popen.return_value = proc
            result = transcribe_audio_out_of_process("/tmp/voice.mp3")

        kill.assert_called_once_with(4242)
        assert result["status"] == "error"

    def test_the_timeout_is_handed_to_communicate(self):
        with patch(_POPEN) as popen:
            popen.return_value = _fake_proc(stdout=_ok_payload())
            transcribe_audio_out_of_process("/tmp/voice.mp3", timeout=123)

        assert popen.return_value.communicate.call_args.kwargs["timeout"] == 123


class TestTheHeavyModulesNeverEnterTheDaemon:
    """A guard on the import edge this change introduced, not a reproduction.

    Being explicit about what this does and does not prove. The *runtime* bug —
    calling `transcribe_audio` pulls faster_whisper into the daemon and strands
    its memory there — is what the mocked tests above cover, and they fail
    against the pre-fix code. This one would have passed before the fix too,
    because the old import sat inside the function body.

    What it guards is the new edge: `executor` now imports
    `whisper.out_of_process` at module level, so that module has to stay
    stdlib-only forever. Reinstating a top-level `from .transcribe import
    transcribe_audio` there — or in the executor — restores the import floor
    with every mechanism test still green, since none of them looks at what is
    actually loaded.

    It has to run in a child interpreter. Inside the suite another test has
    already imported faster_whisper and torch, so the assertion would be
    meaningless in-process.
    """

    def test_importing_the_executor_pulls_in_neither_whisper_nor_torch(self):
        probe = (
            "import sys; import istota.executor; "
            "print(repr(sorted(m for m in ('faster_whisper', 'torch', "
            "'sentence_transformers', 'ctranslate2') if m in sys.modules)))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=180,
        )

        assert proc.returncode == 0, f"probe failed: {proc.stderr[-2000:]}"
        assert proc.stdout.strip() == "[]", (
            "importing istota.executor dragged a heavy module into the daemon: "
            f"{proc.stdout.strip()}"
        )


class TestAgainstTheRealCli:
    """One pass through the actual seam, with nothing mocked.

    Everything above pins the contract against a fake child, which proves the
    runner reads what the CLI is *believed* to write. This spawns the real
    interpreter and the real CLI, so a rename, a changed exit code or an argv
    the parser rejects fails here rather than in production. A path that does
    not exist is the one error the CLI reports before it would load a model, so
    the round trip stays under a couple of seconds.
    """

    def test_a_real_spawn_round_trips_an_error_result(self):
        result = transcribe_audio_out_of_process("/nonexistent/definitely-not-here.mp3", timeout=120)

        assert result["status"] == "error"
        assert "not found" in result["error"].lower()
        # The CLI's own message, not the runner's fallback for output it could
        # not parse — the distinction is the whole point of this test.
        assert "without a usable result" not in result["error"]


class TestParseCliJson:
    def test_it_finds_the_outer_object_not_a_nested_one(self):
        """`segments` and `words` are objects too; only the result carries
        `status`, which is what keeps the scan from returning a fragment."""
        payload = json.dumps(
            {
                "status": "ok",
                "text": "hi",
                "segments": [{"start": 0.0, "end": 1.0, "words": [{"word": "hi"}]}],
            },
            indent=2,
        )
        parsed = _parse_cli_json(payload)
        assert parsed["status"] == "ok"
        assert parsed["text"] == "hi"

    def test_empty_output_parses_to_nothing(self):
        assert _parse_cli_json("") is None
        assert _parse_cli_json("no json here at all") is None
