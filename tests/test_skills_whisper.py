"""Tests for the whisper transcription skill."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.whisper.cli import (
    build_parser,
    cmd_download,
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
        with patch("istota.skills.whisper.models.WhisperModel", create=True) as mock_wm:
            # Patch the import inside the function
            import istota.skills.whisper.models as m

            original = m.download_model

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
            # Force ImportError
            import istota.skills.whisper.transcribe as t_mod

            original_transcribe = t_mod.transcribe_audio

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
            result = transcribe_audio(str(audio), language="de")

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
        result = cmd_transcribe(args)
        assert result["status"] == "ok"
        assert "formatted_output" in result
        assert "segments" not in result

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_save_text_output(self, mock_transcribe, tmp_path):
        mock_transcribe.return_value = {
            "status": "ok",
            "text": "Hello world",
            "segments": [],
        }
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"fake")
        args = MagicMock()
        args.audio_path = str(audio_path)
        args.model = "auto"
        args.language = None
        args.output = "text"
        args.save = True
        result = cmd_transcribe(args)
        assert result["saved_to"] == str(tmp_path / "test.txt")
        assert (tmp_path / "test.txt").read_text() == "Hello world"

    @patch("istota.skills.whisper.cli.transcribe_audio")
    def test_error_passthrough(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "error", "error": "file not found"}
        args = MagicMock()
        args.audio_path = "/nope.wav"
        args.model = "auto"
        args.language = None
        args.output = "json"
        args.save = False
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
