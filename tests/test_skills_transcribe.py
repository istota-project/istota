"""Tests for skills/transcribe.py module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pytesseract", reason="pytesseract not installed (install with: uv sync --extra transcribe)")
pytest.importorskip("PIL", reason="Pillow not installed (install with: uv sync --extra transcribe)")
from PIL import Image

import istota.skills.transcribe as transcribe_pkg
from istota.skills.transcribe import (
    build_parser,
    cmd_ocr,
    main,
    preprocess_image,
)


class TestPreprocessImage:
    def test_converts_to_grayscale(self):
        # Create a color image
        image = Image.new("RGB", (100, 100), color=(255, 0, 0))

        result = preprocess_image(image)

        assert result.mode == "L"

    def test_preserves_grayscale(self):
        # Already grayscale
        image = Image.new("L", (100, 100), color=128)

        result = preprocess_image(image)

        assert result.mode == "L"

    def test_enhances_contrast(self):
        # Create low-contrast image
        image = Image.new("L", (100, 100), color=128)

        result = preprocess_image(image)

        # Result should still be a valid image
        assert result.size == (100, 100)


class TestCmdOcr:
    def test_file_not_found(self, tmp_path):
        args = MagicMock()
        args.image_path = str(tmp_path / "nonexistent.png")
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_ocr_success(self, mock_to_data, tmp_path):
        # Create a test image
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_data.return_value = {
            "conf": [95, 92, -1],  # -1 values should be excluded
            "text": ["Hello", "World", ""],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["status"] == "ok"
        assert result["text"] == "Hello World"
        assert result["confidence"] == 0.94  # (95 + 92) / 2 / 100
        assert result["word_count"] == 2

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_ocr_with_preprocess(self, mock_to_data, tmp_path):
        # Create a test image
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="red").save(image_path)

        mock_to_data.return_value = {
            "conf": [90],
            "text": ["Preprocessed"],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = True

        result = cmd_ocr(args)

        assert result["status"] == "ok"
        assert result["text"] == "Preprocessed"
        # The single pass ran against the preprocessed image.
        mock_to_data.assert_called_once()

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_ocr_empty_result(self, mock_to_data, tmp_path):
        # Create a blank image
        image_path = tmp_path / "blank.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_data.return_value = {
            "conf": [-1, -1],  # No confident text
            "text": ["", ""],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["status"] == "ok"
        assert result["text"] == ""
        assert result["confidence"] == 0
        assert result["word_count"] == 0

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_ocr_low_confidence(self, mock_to_data, tmp_path):
        # Create a test image
        image_path = tmp_path / "blurry.png"
        Image.new("RGB", (100, 100), color="gray").save(image_path)

        mock_to_data.return_value = {
            "conf": [45, 38, 52],
            "text": ["Blurry", "Text", "Maybe"],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["status"] == "ok"
        assert result["confidence"] == 0.45  # (45 + 38 + 52) / 3 / 100
        assert result["word_count"] == 3

    def test_ocr_invalid_image(self, tmp_path):
        # Create an invalid "image" file
        image_path = tmp_path / "invalid.png"
        image_path.write_text("not an image")

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["status"] == "error"
        assert "error" in result

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_ocr_tesseract_error(self, mock_to_data, tmp_path):
        # Create a valid image but simulate tesseract failure
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_data.side_effect = Exception("Tesseract not found")

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["status"] == "error"
        assert "Tesseract not found" in result["error"]


class TestBuildParser:
    def test_parser_has_ocr_command(self):
        parser = build_parser()
        args = parser.parse_args(["ocr", "/path/to/image.png"])

        assert args.command == "ocr"
        assert args.image_path == "/path/to/image.png"
        assert args.preprocess is False

    def test_parser_preprocess_flag(self):
        parser = build_parser()
        args = parser.parse_args(["ocr", "/path/to/image.png", "--preprocess"])

        assert args.command == "ocr"
        assert args.image_path == "/path/to/image.png"
        assert args.preprocess is True

    def test_parser_requires_command(self):
        parser = build_parser()

        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_parser_requires_image_path(self):
        parser = build_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["ocr"])


class TestMain:
    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_main_ocr_success(self, mock_to_data, tmp_path, capsys):
        # Create a test image
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_data.return_value = {
            "conf": [90],
            "text": ["Test"],
        }

        main(["ocr", str(image_path)])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["text"] == "Test"

    def test_main_ocr_file_not_found(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["ocr", str(tmp_path / "nonexistent.png")])

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert "not found" in output["error"]

    def test_main_missing_command(self):
        with pytest.raises(SystemExit):
            main([])

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_main_with_preprocess(self, mock_to_data, tmp_path, capsys):
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="red").save(image_path)

        mock_to_data.return_value = {
            "conf": [85],
            "text": ["Processed"],
        }

        main(["ocr", str(image_path), "--preprocess"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["text"] == "Processed"


class TestOneTesseractPassPerCall:
    """`cmd_ocr` used to shell out twice per image, with no shared page state.

    Automatic OCR runs it once per attachment and up to twenty attachments per
    task, so the second pass would be forty full passes over the same pixels
    for one full send. Text and confidence now both come out of the single
    `image_to_data` result; the JSON schema is unchanged.
    """

    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_tesseract_runs_exactly_once(self, mock_to_data, mock_to_string, tmp_path):
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)
        mock_to_data.return_value = {
            "conf": [95, 92],
            "text": ["Hello", "World"],
            "page_num": [1, 1],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 1],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert mock_to_data.call_count == 1
        assert mock_to_string.call_count == 0
        assert set(result) == {"status", "text", "confidence", "word_count"}
        assert result["text"] == "Hello World"

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_line_structure_becomes_line_breaks(self, mock_to_data, tmp_path):
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)
        mock_to_data.return_value = {
            "conf": [90, 90, 88, 88],
            "text": ["Account", "12345", "Balance", "42"],
            "page_num": [1, 1, 1, 1],
            "block_num": [1, 1, 1, 1],
            "par_num": [1, 1, 1, 1],
            "line_num": [1, 1, 2, 2],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["text"] == "Account 12345\nBalance 42"

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    def test_blank_word_entries_do_not_produce_empty_lines(self, mock_to_data, tmp_path):
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)
        mock_to_data.return_value = {
            "conf": [-1, 90, -1, 88],
            "text": ["", "One", "  ", "Two"],
            "page_num": [1, 1, 1, 1],
            "block_num": [1, 1, 1, 1],
            "par_num": [1, 1, 1, 1],
            "line_num": [1, 1, 2, 2],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = False

        result = cmd_ocr(args)

        assert result["text"] == "One\nTwo"
        assert result["word_count"] == 2


class TestSkillMetadata:
    """The eager file-type list has to cover what the executor prepares.

    A `.heif` attachment is normalized and OCR'd by `image_attachments`, so if
    it does not also select `transcribe` it loses the reconciliation guidance
    and the `untrusted_input` companion — which is the stated reason the skill
    stays eager for images.
    """

    def _meta(self):
        from istota.skills._loader import _load_skill_meta

        return _load_skill_meta(Path(transcribe_pkg.__file__).parent)

    def test_every_prepared_image_extension_selects_the_skill(self):
        from istota.image_attachments import IMAGE_EXTENSIONS

        assert IMAGE_EXTENSIONS <= set(self._meta().file_types)

    def test_untrusted_input_is_still_a_companion(self):
        assert "untrusted_input" in self._meta().companion_skills

    def test_an_unexplained_image_is_no_longer_a_transcription_request(self):
        body = (Path(transcribe_pkg.__file__).parent / "skill.md").read_text()

        assert "treat it as a transcription request" not in body

    def test_the_save_procedure_is_scoped_to_an_explicit_request(self):
        body = (Path(transcribe_pkg.__file__).parent / "skill.md").read_text()

        assert "asks you to transcribe" in body
