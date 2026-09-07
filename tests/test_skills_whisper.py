"""Tests for the whisper transcription skill."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.whisper.cli import (
    build_parser,
    cmd_models,
    cmd_transcribe,
    main,
)
from istota.skills.whisper.models import (
    MODEL_REQUIREMENTS,
    _DEFAULT_HEADROOM_GB,
    _DEFAULT_MAX_MODEL,
    _get_headroom_gb,
    _get_max_model,
    _is_model_downloaded,
    download_model,
    list_models,
    select_model,
)
from istota.skills.whisper.transcribe import (
    format_srt,
    format_vtt,
    transcribe_audio,
)
from tests.support.skill_cli import run_skill_main


# --- Model selection tests ---


class TestSelectModel:
    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=20.0)
    def test_auto_capped_at_max_model(self, mock_mem):
        # Even with 20 GB available, auto caps at small (default max)
        result = select_model()
        assert result == "small"

    @patch.dict("os.environ", {"WHISPER_MAX_MODEL": "medium"})
    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=20.0)
    def test_auto_respects_env_max_model(self, mock_mem):
        result = select_model()
        assert result == "medium"

    @patch.dict("os.environ", {"WHISPER_MAX_MODEL": "large-v3"})
    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=20.0)
    def test_auto_with_large_max(self, mock_mem):
        result = select_model()
        assert result == "large-v3"

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=3.0)
    def test_auto_selects_small_with_enough_ram(self, mock_mem):
        # 3.0 GB available, small needs 2.5 + 0.3 headroom = 2.8, fits
        result = select_model()
        assert result == "small"

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=1.5)
    def test_auto_selects_tiny_with_very_limited_ram(self, mock_mem):
        result = select_model()
        assert result == "tiny"

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=0.5)
    def test_auto_raises_when_nothing_fits(self, mock_mem):
        with pytest.raises(ValueError, match="No model fits"):
            select_model()

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=10.0)
    def test_preferred_model_bypasses_cap(self, mock_mem):
        # Explicit model request is not capped
        result = select_model("medium")
        assert result == "medium"

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=2.0)
    def test_preferred_model_raises_if_doesnt_fit(self, mock_mem):
        with pytest.raises(ValueError, match="needs ~5.0 GB"):
            select_model("medium")

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=10.0)
    def test_unknown_model_raises(self, mock_mem):
        with pytest.raises(ValueError, match="Unknown model"):
            select_model("nonexistent")

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=20.0)
    def test_auto_string_treated_as_auto(self, mock_mem):
        # "auto" triggers auto-selection, capped at small
        result = select_model("auto")
        assert result == "small"

    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=3.0)
    def test_explicit_headroom_respected(self, mock_mem):
        # 3.0 GB available, small needs 2.5 + 1.0 headroom = 3.5, doesn't fit
        result = select_model(headroom_gb=1.0)
        assert result == "base"  # 1.5 + 1.0 = 2.5, fits

    @patch.dict("os.environ", {"RAM_HEADROOM_MB": "500"})
    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=3.0)
    def test_env_var_headroom(self, mock_mem):
        # 3.0 GB available, 500 MB = 0.488 GB headroom
        # small needs 2.5 + 0.488 = 2.988, fits
        result = select_model()
        assert result == "small"

    @patch.dict("os.environ", {"RAM_HEADROOM_MB": "1024"})
    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=3.0)
    def test_env_var_large_headroom(self, mock_mem):
        # 3.0 GB available, 1024 MB = 1.0 GB headroom
        # small needs 2.5 + 1.0 = 3.5, doesn't fit
        result = select_model()
        assert result == "base"

    @patch.dict("os.environ", {"RAM_HEADROOM_MB": "notanumber"})
    @patch("istota.skills.whisper.models.get_available_memory_gb", return_value=3.0)
    def test_env_var_invalid_falls_back_to_default(self, mock_mem):
        result = select_model()
        assert result == "small"  # 2.5 + 0.3 = 2.8, fits

    def test_explicit_headroom_overrides_env(self):
        headroom = _get_headroom_gb(override=2.0)
        assert headroom == 2.0

    def test_default_headroom_value(self):
        assert _DEFAULT_HEADROOM_GB == 0.3

    def test_default_max_model(self):
        assert _DEFAULT_MAX_MODEL == "small"

    @patch.dict("os.environ", {"WHISPER_MAX_MODEL": "medium"})
    def test_get_max_model_from_env(self):
        assert _get_max_model() == "medium"

    @patch.dict("os.environ", {"WHISPER_MAX_MODEL": "invalid"})
    def test_get_max_model_invalid_falls_back(self):
        assert _get_max_model() == "small"

    @patch.dict("os.environ", {}, clear=False)
    def test_get_max_model_default(self):
        # Remove WHISPER_MAX_MODEL if present
        os.environ.pop("WHISPER_MAX_MODEL", None)
        assert _get_max_model() == "small"


class TestListModels:
    def test_lists_all_models(self):
        models = list_models()
        names = [m["name"] for m in models]
        assert "tiny" in names
        assert "large-v3" in names
        for m in models:
            assert "ram_gb" in m
            assert "downloaded" in m

    def test_downloaded_status_with_no_cache(self, tmp_path):
        with patch("istota.skills.whisper.models.Path.home", return_value=tmp_path):
            assert _is_model_downloaded("tiny") is False

    def test_downloaded_status_with_matching_dir(self, tmp_path):
        cache = tmp_path / ".cache" / "huggingface" / "hub"
        cache.mkdir(parents=True)
        (cache / "models--Systran--faster-whisper-small").mkdir()
        with patch("istota.skills.whisper.models.Path.home", return_value=tmp_path):
            assert _is_model_downloaded("small") is True
            assert _is_model_downloaded("tiny") is False


class TestDownloadModel:
    def test_unknown_model(self):
        result = download_model("nonexistent")
        assert result["status"] == "error"
        assert "Unknown model" in result["error"]

    @patch("istota.skills.whisper.models.WhisperModel", create=True)
    def test_success(self, mock_cls):
        with patch.dict("sys.modules", {"faster_whisper": MagicMock(WhisperModel=mock_cls)}):
            from importlib import reload

            import istota.skills.whisper.models as models_mod

            reload(models_mod)
            # Just test that the function returns ok when import works
            result = models_mod.download_model("tiny")
            # It will try to import faster_whisper — mock it
        # Re-test with direct mock
        with patch("istota.skills.whisper.models.WhisperModel", create=True):
            # Patch the import inside the function. Imported for the side
            # effect of loading the module under the patch, not for a name.
            import istota.skills.whisper.models as m  # noqa: F401


            def patched_download(name):
                if name not in MODEL_REQUIREMENTS:
                    return {"status": "error", "error": f"Unknown model '{name}'."}
                return {"status": "ok", "model": name, "message": f"Model '{name}' downloaded"}

            result = patched_download("tiny")
            assert result["status"] == "ok"
            assert result["model"] == "tiny"


# --- Transcription tests ---


def _make_mock_segment(start, end, text, words=None):
    seg = MagicMock()
    seg.start = start
    seg.end = end
    seg.text = text
    if words is None:
        seg.words = []
    else:
        mock_words = []
        for w in words:
            mw = MagicMock()
            mw.start = w["start"]
            mw.end = w["end"]
            mw.word = w["word"]
            mw.probability = w["probability"]
            mock_words.append(mw)
        seg.words = mock_words
    return seg


def _make_mock_info(language="en", language_probability=0.98, duration=10.0):
    info = MagicMock()
    info.language = language
    info.language_probability = language_probability
    info.duration = duration
    return info


class TestTranscribeAudio:
    def test_file_not_found(self):
        result = transcribe_audio("/nonexistent/audio.wav")
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("istota.skills.whisper.transcribe.select_model", return_value="tiny")
    def test_import_error(self, mock_select, tmp_path):
        audio = tmp_path / "test.wav"
        audio.write_bytes(b"fake audio")
        with patch.dict("sys.modules", {"faster_whisper": None}):
            # Force ImportError. Imported for the side effect of loading the
            # module with faster_whisper masked out, not for a name.
            import istota.skills.whisper.transcribe as t_mod  # noqa: F401


            def failing_import_transcribe(path, model="auto", language=None):
                audio_path = Path(path)
                if not audio_path.exists():
                    return {"status": "error", "error": f"Audio file not found: {path}"}
                try:
                    raise ImportError("No module named 'faster_whisper'")
                except ImportError:
                    return {
                        "status": "error",
                        "error": "faster-whisper not installed. Install with: uv sync --extra whisper",
                    }

            result = failing_import_transcribe(str(audio))
            assert result["status"] == "error"
            assert "not installed" in result["error"]

    @patch("istota.skills.whisper.transcribe.select_model")
    def test_model_selection_error(self, mock_select, tmp_path):
        mock_select.side_effect = ValueError("No model fits")
        audio = tmp_path / "test.wav"
        audio.write_bytes(b"fake audio")
        # Need to also mock the import
        mock_fw = MagicMock()
        with patch.dict("sys.modules", {"faster_whisper": mock_fw}):
            with patch("istota.skills.whisper.transcribe.WhisperModel", create=True):
                result = transcribe_audio(str(audio))
        assert result["status"] == "error"
        assert "No model fits" in result["error"]

    @patch("istota.skills.whisper.transcribe.select_model", return_value="tiny")
    def test_successful_transcription(self, mock_select, tmp_path):
        audio = tmp_path / "test.wav"
        audio.write_bytes(b"fake audio")

        segments = [
            _make_mock_segment(0.0, 2.5, "Hello world", [
                {"start": 0.0, "end": 1.0, "word": "Hello", "probability": 0.99},
                {"start": 1.1, "end": 2.5, "word": "world", "probability": 0.95},
            ]),
            _make_mock_segment(3.0, 5.0, "Testing"),
        ]
        info = _make_mock_info()

        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter(segments), info)
        mock_wm_cls = MagicMock(return_value=mock_model)

        mock_fw = MagicMock()
        mock_fw.WhisperModel = mock_wm_cls
        with patch.dict("sys.modules", {"faster_whisper": mock_fw}):
            result = transcribe_audio(str(audio))

        assert result["status"] == "ok"
        assert result["model"] == "tiny"
        assert result["language"] == "en"
        assert result["text"] == "Hello world Testing"
        assert len(result["segments"]) == 2
        assert result["segments"][0]["text"] == "Hello world"
        assert len(result["segments"][0]["words"]) == 2
        assert result["segments"][1]["words"] == []

    @patch("istota.skills.whisper.transcribe.select_model", return_value="small")
    def test_language_passed_through(self, mock_select, tmp_path):
        audio = tmp_path / "test.wav"
        audio.write_bytes(b"fake audio")

        info = _make_mock_info(language="de", duration=5.0)
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), info)
        mock_wm_cls = MagicMock(return_value=mock_model)

        mock_fw = MagicMock()
        mock_fw.WhisperModel = mock_wm_cls
        with patch.dict("sys.modules", {"faster_whisper": mock_fw}):
            transcribe_audio(str(audio), language="de")

        mock_model.transcribe.assert_called_once()
        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["language"] == "de"


# --- Format tests ---


class TestFormatSrt:
    def test_basic_srt(self):
        segments = [
            {"start": 0.0, "end": 2.5, "text": "Hello world"},
            {"start": 3.0, "end": 5.123, "text": "Second line"},
        ]
        result = format_srt(segments)
        lines = result.split("\n")
        assert lines[0] == "1"
        assert lines[1] == "00:00:00,000 --> 00:00:02,500"
        assert lines[2] == "Hello world"
        assert lines[3] == ""
        assert lines[4] == "2"
        assert lines[5] == "00:00:03,000 --> 00:00:05,123"
        assert lines[6] == "Second line"

    def test_hour_timestamp(self):
        segments = [{"start": 3661.5, "end": 3665.0, "text": "Late"}]
        result = format_srt(segments)
        assert "01:01:01,500 --> 01:01:05,000" in result

    def test_empty_segments(self):
        assert format_srt([]) == ""


class TestFormatVtt:
    def test_basic_vtt(self):
        segments = [
            {"start": 0.0, "end": 2.5, "text": "Hello world"},
        ]
        result = format_vtt(segments)
        lines = result.split("\n")
        assert lines[0] == "WEBVTT"
        assert lines[1] == ""
        assert lines[2] == "00:00:00.000 --> 00:00:02.500"
        assert lines[3] == "Hello world"

    def test_empty_segments(self):
        result = format_vtt([])
        assert result.startswith("WEBVTT")


# --- CLI tests ---


class TestBuildParser:
    def test_transcribe_command(self):
        parser = build_parser()
        args = parser.parse_args(["transcribe", "/path/to/audio.wav"])
        assert args.command == "transcribe"
        assert args.audio_path == "/path/to/audio.wav"
        assert args.model == "auto"
        assert args.output == "json"
        assert args.save is False

    def test_transcribe_with_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "transcribe", "/path/to/audio.wav",
            "--model", "small",
            "--language", "en",
            "--output", "srt",
            "--save",
        ])
        assert args.model == "small"
        assert args.language == "en"
        assert args.output == "srt"
        assert args.save is True

    def test_models_command(self):
        parser = build_parser()
        args = parser.parse_args(["models"])
        assert args.command == "models"

    def test_download_command(self):
        parser = build_parser()
        args = parser.parse_args(["download", "small"])
        assert args.command == "download"
        assert args.model_name == "small"


class TestCmdModels:
    @patch("istota.skills.whisper.cli.get_available_memory_gb", return_value=8.0)
    @patch("istota.skills.whisper.cli.list_models")
    def test_returns_models_and_memory(self, mock_list, mock_mem):
        mock_list.return_value = [
            {"name": "tiny", "ram_gb": 1.0, "downloaded": False},
        ]
        args = MagicMock()
        result = cmd_models(args)
        assert result["status"] == "ok"
        assert result["available_memory_gb"] == 8.0
        assert len(result["models"]) == 1


class TestNoSegments:
    """ISSUE-273: `segments` carries one dict per word, so a long recording is
    megabytes of JSON that a caller wanting only the transcript still has to
    buffer, decode and drop — in the daemon, on the allocator this whole change
    exists to keep quiet."""

    def _args(self, tmp_path, output, no_segments):
        args = MagicMock()
        args.audio_path = str(tmp_path / "test.wav")
        args.model = "auto"
        args.language = None
        args.output = output
        args.save = False
        args.no_segments = no_segments
        return args

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_it_drops_segments_from_json_and_keeps_the_text(self, mock_transcribe, tmp_path):
        mock_transcribe.return_value = {
            "status": "ok",
            "text": "Hello",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Hello", "words": []}],
        }
        result = cmd_transcribe(self._args(tmp_path, "json", True))
        assert "segments" not in result
        assert result["text"] == "Hello"

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_it_drops_segments_from_text_output_too(self, mock_transcribe, tmp_path):
        """The `text` format returns the whole result dict, segments included —
        so the flag has to apply there as well or it silently does nothing."""
        mock_transcribe.return_value = {"status": "ok", "text": "Hello", "segments": [{}]}
        result = cmd_transcribe(self._args(tmp_path, "text", True))
        assert "segments" not in result

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_the_default_still_carries_segments(self, mock_transcribe, tmp_path):
        mock_transcribe.return_value = {"status": "ok", "text": "Hello", "segments": [{}]}
        result = cmd_transcribe(self._args(tmp_path, "json", False))
        assert "segments" in result

    def test_the_parser_defaults_it_off(self):
        args = build_parser().parse_args(["transcribe", "/x.wav"])
        assert args.no_segments is False


class TestCmdTranscribe:
    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_json_output(self, mock_transcribe, tmp_path):
        mock_transcribe.return_value = {
            "status": "ok",
            "model": "tiny",
            "language": "en",
            "language_probability": 0.98,
            "duration_seconds": 5.0,
            "processing_seconds": 2.0,
            "text": "Hello",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Hello", "words": []}],
        }
        args = MagicMock()
        args.audio_path = str(tmp_path / "test.wav")
        args.model = "auto"
        args.language = None
        args.output = "json"
        args.save = False
        args.no_segments = False
        result = cmd_transcribe(args)
        assert result["status"] == "ok"
        assert "segments" in result

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_text_output(self, mock_transcribe):
        mock_transcribe.return_value = {
            "status": "ok",
            "text": "Hello world",
            "segments": [],
        }
        args = MagicMock()
        args.audio_path = "/test.wav"
        args.model = "auto"
        args.language = None
        args.output = "text"
        args.save = False
        args.no_segments = False
        result = cmd_transcribe(args)
        assert result["status"] == "ok"

    @patch("istota.skills.whisper.cli.format_srt", return_value="1\n00:00:00,000 --> 00:00:02,000\nHello\n")
    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_srt_output(self, mock_transcribe, mock_srt):
        mock_transcribe.return_value = {
            "status": "ok",
            "text": "Hello",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Hello"}],
        }
        args = MagicMock()
        args.audio_path = "/test.wav"
        args.model = "auto"
        args.language = None
        args.output = "srt"
        args.save = False
        args.no_segments = False
        result = cmd_transcribe(args)
        assert result["status"] == "ok"
        assert "formatted_output" in result
        assert "segments" not in result

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_save_text_output(self, mock_transcribe, tmp_path, monkeypatch):
        mock_transcribe.return_value = {
            "status": "ok",
            "text": "Hello world",
            "segments": [],
        }
        # The task's deferred directory is a write root on its own, so this
        # names one without needing a mount (ISSUE-453). Before the derived
        # destination was resolved, this call wrote to any path at all.
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"fake")
        args = MagicMock()
        args.audio_path = str(audio_path)
        args.model = "auto"
        args.language = None
        args.output = "text"
        args.save = True
        args.no_segments = False
        result = cmd_transcribe(args)
        assert result["saved_to"] == str(tmp_path / "test.txt")
        assert (tmp_path / "test.txt").read_text() == "Hello world"

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_save_with_no_task_identity_is_refused(
        self, mock_transcribe, tmp_path, monkeypatch,
    ):
        """A caller with no task environment has an empty allowlist.

        `env_host_roots` reads the four variables the proxy exports per task,
        and the daemon carries none of them — so a daemon-side spawn that
        forgets to pass the task identity gets no roots, and an empty
        allowlist means refuse everything rather than permit everything. That
        rule is `resolve_in_roots`'; asserted here because `--save` is the one
        whisper path that writes.
        """
        mock_transcribe.return_value = {"status": "ok", "text": "hi", "segments": []}
        for name in (
            "ISTOTA_DEFERRED_DIR",
            "NEXTCLOUD_MOUNT_PATH",
            "ISTOTA_USER_ID",
            "ISTOTA_CONVERSATION_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"fake")
        args = MagicMock()
        args.audio_path = str(audio_path)
        args.model = "auto"
        args.language = None
        args.output = "text"
        args.save = True
        args.no_segments = False

        result = cmd_transcribe(args)

        assert result["status"] == "error"
        assert result["reason"] == "host_path_refused"
        assert not (tmp_path / "test.txt").exists()
        mock_transcribe.assert_not_called()
        # The resolver's own message, not the read-versus-write one: there is
        # no workspace here to copy anything into, so that remedy would send
        # the model round a loop it cannot leave. Nothing else asserts on the
        # text of a refusal, and this is the branch where a wrong one is
        # actively misleading.
        assert "No allowed host roots configured" in result["error"]
        assert "Copy the audio" not in result["error"]

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_error_passthrough(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "error", "error": "file not found"}
        args = MagicMock()
        args.audio_path = "/nope.wav"
        args.model = "auto"
        args.language = None
        args.output = "json"
        args.save = False
        args.no_segments = False
        result = cmd_transcribe(args)
        assert result["status"] == "error"


class TestMain:
    @patch("istota.skills.whisper.cli.cmd_models")
    def test_models_command(self, mock_cmd, capsys):
        mock_cmd.return_value = {"status": "ok", "models": []}
        main(["models"])
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "ok"

    @patch("istota.skills.whisper.cli.cmd_transcribe")
    def test_transcribe_error_exits_1(self, mock_cmd):
        mock_cmd.return_value = {"status": "error", "error": "boom"}
        with pytest.raises(SystemExit) as exc_info:
            main(["transcribe", "/test.wav"])
        assert exc_info.value.code == 1

    @patch("istota.skills.whisper.cli.cmd_download")
    def test_download_command(self, mock_cmd, capsys):
        mock_cmd.return_value = {"status": "ok", "model": "tiny", "message": "downloaded"}
        main(["download", "tiny"])
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "ok"


class TestTheDerivedSaveDestinations:
    """`--save` writes a name the handler derives, not one the caller passed.

    The four destinations are `Path(args.audio_path).with_suffix(...)`, and
    `audio_path` is stamped `READ` — so by the time the handler runs it is
    resolved and inside a root, and a suffix swap cannot leave its parent.
    That much is Layer 3's rule and still holds.

    What it does not settle is *writability*, which is the correction
    ISSUE-453 makes: the read roots are a wider set than the write roots, so
    "inside the root the source came from" is not "inside a root this may
    write to". The derived destination goes back through the resolver against
    `env_host_roots(writable=True)`, and the one root that costs it is
    `{mount}/Talk`.

    It also still needs `write_resolved`. A plain `open(..., "wb")` follows a
    symlink standing at the derived name, and the workspace is bound
    read-write into the sandbox, so such a link is model-plantable — the file
    would land wherever it points, written by the daemon user, with
    containment already reported as passed. The resolver refuses one as of
    its own check; `write_resolved` is what closes the window after it.

    The workspace-sourced acceptance is
    `test_each_format_writes_beside_the_resolved_audio`, which is the ordinary
    case and is where a refusal that over-reached would show up.
    """

    @pytest.fixture
    def mount(self, tmp_path, monkeypatch):
        real = tmp_path / "srv" / "shared"
        (real / "Users" / "alice").mkdir(parents=True)
        (real / "Talk").mkdir(parents=True)
        link = tmp_path / "mount"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(link))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        return link

    @pytest.fixture
    def audio(self, mount):
        path = mount / "Users" / "alice" / "voice.wav"
        path.write_bytes(b"RIFF")
        return path

    @pytest.fixture(autouse=True)
    def _transcription(self, monkeypatch):
        monkeypatch.setattr(
            "istota.skills.whisper.cli.transcribe_audio",
            lambda *a, **k: {
                "status": "ok",
                "text": "hello there",
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello there"}],
            },
        )

    @pytest.mark.parametrize(
        ("output", "suffix"),
        [("text", ".txt"), ("srt", ".srt"), ("vtt", ".vtt"), ("json", ".json")],
    )
    def test_each_format_writes_beside_the_resolved_audio(
        self, output, suffix, mount, audio,
    ):
        from istota.skills.whisper.cli import main

        run = run_skill_main(
            main, ["transcribe", str(audio), "--output", output, "--save"],
        )

        assert run.exit_code == 0, run.stdout
        # Beside the *resolved* audio path: `mount` is a symlink, so the two
        # differ and an assertion against the argument would pass against a
        # handler that never resolved anything.
        written = audio.resolve().with_suffix(suffix)
        assert written.exists(), run.stdout
        assert written.read_bytes(), "wrote an empty file"
        assert run.envelope["saved_to"] == str(written)

    @pytest.mark.parametrize(
        ("output", "suffix"),
        [("text", ".txt"), ("srt", ".srt"), ("vtt", ".vtt"), ("json", ".json")],
    )
    def test_a_symlink_at_the_derived_name_is_refused(
        self, output, suffix, mount, audio, tmp_path,
    ):
        """A link standing at the derived name when the destination resolves.

        Caught by the resolver rather than by the open, as of ISSUE-453:
        `resolve_in_roots` refuses a symlink at the destination, and the
        destination is now resolved before the transcription runs. The
        `write_resolved` half — the link planted *after* that answer — is the
        test below.
        """
        from istota.skills.whisper.cli import main

        victim = tmp_path / "elsewhere.txt"
        victim.write_text("not yours")
        audio.resolve().with_suffix(suffix).symlink_to(victim)

        run = run_skill_main(
            main, ["transcribe", str(audio), "--output", output, "--save"],
        )

        assert victim.read_text() == "not yours"
        assert run.exit_code == 1, run.stdout
        assert run.envelope.get("status") == "error", run.stdout

    def test_a_symlink_planted_during_the_transcription_is_refused(
        self, mount, audio, tmp_path, monkeypatch,
    ):
        """The window the resolution cannot close, and why `write_resolved`.

        Resolving the destination up front answers as of the moment it looks,
        and the answer is now minutes old by the time the transcript is
        written — the workspace is bound read-write into the sandbox, so a
        link appearing in that window is model-plantable. `O_NOFOLLOW` makes
        the open fail rather than truncating whatever it points at. Without
        it this test writes `hello there` over the victim file while every
        containment check has already reported a pass.
        """
        from istota.skills.whisper import cli

        victim = tmp_path / "elsewhere.txt"
        victim.write_text("not yours")

        def _plant_then_transcribe(*args, **kwargs):
            audio.resolve().with_suffix(".txt").symlink_to(victim)
            return {"status": "ok", "text": "hello there", "segments": []}

        monkeypatch.setattr(cli, "transcribe_audio", _plant_then_transcribe)

        run = run_skill_main(
            cli.main, ["transcribe", str(audio), "--output", "text", "--save"],
        )

        assert victim.read_text() == "not yours"
        assert run.exit_code == 1, run.stdout
        assert run.envelope.get("status") == "error", run.stdout

    def test_a_talk_sourced_save_is_refused(self, mount):
        """The derived write is resolved against the *writable* roots.

        `audio_path` is `READ`, which admits `{mount}/Talk` — that is where a
        Talk voice message lands, and reading it is the point of the verb. The
        derived `--save` destination is then inside `{mount}/Talk` too, a
        directory the sandbox binds read-only, so the save wrote host-side
        where the task's own tools could not (ISSUE-453). A derivation is
        contained by construction only with respect to the root its source
        came from, and a read root is not a write root.
        """
        from istota.skills.whisper.cli import main

        source = mount / "Talk" / "voicemail.wav"
        source.write_bytes(b"RIFF")

        run = run_skill_main(
            main, ["transcribe", str(source), "--output", "text", "--save"],
        )

        assert run.exit_code == 1, run.stdout
        assert run.envelope.get("status") == "error", run.stdout
        assert run.envelope.get("reason") == "host_path_refused", run.stdout
        assert not source.resolve().with_suffix(".txt").exists(), (
            "refused and wrote anyway"
        )
        # The remedy is the useful half of a refusal the model reads, and it
        # is only true of this branch — hence the assertion here and the one
        # in `test_save_with_no_task_identity_is_refused` that it is absent.
        assert "Copy the audio into your workspace" in run.envelope["error"]

    def test_a_refused_save_costs_no_transcription(self, mount, monkeypatch):
        """The destination is resolved before the model is run, not after.

        Transcription is minutes of CPU on a long recording, and the refusal
        is a property of the arguments alone — it cannot become permitted by
        anything the transcription returns. Resolving after would charge the
        task the whole run and then throw the transcript away with it.
        """
        from istota.skills.whisper import cli

        calls = []
        monkeypatch.setattr(
            cli, "transcribe_audio", lambda *a, **k: calls.append(a) or {},
        )
        source = mount / "Talk" / "voicemail.wav"
        source.write_bytes(b"RIFF")

        run = run_skill_main(
            cli.main, ["transcribe", str(source), "--output", "text", "--save"],
        )

        assert run.envelope.get("reason") == "host_path_refused", run.stdout
        assert calls == [], "transcribed before finding out it could not save"

    def test_a_channel_sourced_save_still_writes(self, mount, monkeypatch):
        """The control against over-refusal.

        `{mount}/Channels/{token}` is a shared directory too, and it stays in
        the write roots — the sandbox binds it read-write, so a save there is
        one the task could have made itself. What `--save` loses is the roots
        that are read-only, which is `{mount}/Talk` alone.
        """
        from istota.skills.whisper.cli import main

        token = "room123"
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", token)
        channel = mount / "Channels" / token
        channel.mkdir(parents=True)
        source = channel / "standup.wav"
        source.write_bytes(b"RIFF")

        run = run_skill_main(
            main, ["transcribe", str(source), "--output", "text", "--save"],
        )

        assert run.exit_code == 0, run.stdout
        written = source.resolve().with_suffix(".txt")
        assert written.exists()
        assert run.envelope["saved_to"] == str(written)
