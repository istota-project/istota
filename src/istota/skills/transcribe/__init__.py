"""OCR transcription using Tesseract.

Provides a CLI for extracting text from images:
    python -m istota.skills.transcribe ocr /path/to/image.png
    python -m istota.skills.transcribe ocr /path/to/image.png --preprocess
"""

import argparse
import json
import sys
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance


def preprocess_image(image: Image.Image) -> Image.Image:
    """Apply preprocessing for better OCR results.

    Converts to grayscale and enhances contrast.
    """
    gray = image.convert("L")
    enhanced = ImageEnhance.Contrast(gray).enhance(1.5)
    return enhanced


def cmd_ocr(args) -> dict:
    """Run Tesseract OCR on an image file."""
    path = Path(args.image_path)
    if not path.exists():
        return {"status": "error", "error": f"Image not found: {path}"}

    try:
        image = Image.open(path)
        if args.preprocess:
            image = preprocess_image(image)

        # Get OCR text and confidence data
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        text = pytesseract.image_to_string(image).strip()

        # Calculate average confidence (exclude -1 values which indicate no text)
        confidences = [c for c in data["conf"] if c > 0]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0

        # Count actual words (non-empty text entries)
        word_count = len([w for w in data["text"] if w.strip()])

        return {
            "status": "ok",
            "text": text,
            "confidence": round(avg_confidence / 100, 2),  # Normalize to 0-1
            "word_count": word_count,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.transcribe",
        description="OCR transcription skill",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ocr command
    ocr_parser = sub.add_parser("ocr", help="Extract text from image using OCR")
    ocr_parser.add_argument("image_path", help="Path to image file")
    ocr_parser.add_argument(
        "--preprocess",
        action="store_true",
        help="Apply preprocessing (grayscale + contrast) for better results",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "ocr": cmd_ocr,
    }

    try:
        result = commands[args.command](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "error":
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
