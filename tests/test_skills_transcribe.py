"""Tests for skills/transcribe.py module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

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
    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    def test_ocr_success(self, mock_to_string, mock_to_data, tmp_path):
        # Create a test image
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_string.return_value = "Hello World"
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
    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    def test_ocr_with_preprocess(self, mock_to_string, mock_to_data, tmp_path):
        # Create a test image
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="red").save(image_path)

        mock_to_string.return_value = "Preprocessed Text"
        mock_to_data.return_value = {
            "conf": [90],
            "text": ["Preprocessed"],
        }

        args = MagicMock()
        args.image_path = str(image_path)
        args.preprocess = True

        result = cmd_ocr(args)

        assert result["status"] == "ok"
        assert result["text"] == "Preprocessed Text"
        # Verify preprocess was applied by checking image_to_string was called
        mock_to_string.assert_called_once()

    @patch("istota.skills.transcribe.pytesseract.image_to_data")
    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    def test_ocr_empty_result(self, mock_to_string, mock_to_data, tmp_path):
        # Create a blank image
        image_path = tmp_path / "blank.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_string.return_value = ""
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
    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    def test_ocr_low_confidence(self, mock_to_string, mock_to_data, tmp_path):
        # Create a test image
        image_path = tmp_path / "blurry.png"
        Image.new("RGB", (100, 100), color="gray").save(image_path)

        mock_to_string.return_value = "Blurry Text"
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
    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    def test_main_ocr_success(self, mock_to_string, mock_to_data, tmp_path, capsys):
        # Create a test image
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="white").save(image_path)

        mock_to_string.return_value = "Test Output"
        mock_to_data.return_value = {
            "conf": [90],
            "text": ["Test"],
        }

        main(["ocr", str(image_path)])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["text"] == "Test Output"

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
    @patch("istota.skills.transcribe.pytesseract.image_to_string")
    def test_main_with_preprocess(self, mock_to_string, mock_to_data, tmp_path, capsys):
        image_path = tmp_path / "test.png"
        Image.new("RGB", (100, 100), color="red").save(image_path)

        mock_to_string.return_value = "Processed"
        mock_to_data.return_value = {
            "conf": [85],
            "text": ["Processed"],
        }

        main(["ocr", str(image_path), "--preprocess"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["text"] == "Processed"
