"""OCR transcription using Tesseract.

Provides a CLI for extracting text from images:
    python -m istota.skills.transcribe ocr /path/to/image.png
    python -m istota.skills.transcribe ocr /path/to/image.png --preprocess
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import pytesseract
    from PIL import Image, ImageEnhance
except ImportError:
    pytesseract = None
    Image = None
    ImageEnhance = None


def preprocess_image(image):
    """Apply preprocessing for better OCR results.

    Converts to grayscale and enhances contrast.
    """
    gray = image.convert("L")
    enhanced = ImageEnhance.Contrast(gray).enhance(1.5)
    return enhanced


def text_from_data(data: dict) -> str:
    """Reassemble Tesseract's word table into text, one line per OCR line.

    `image_to_data` already carries every word `image_to_string` would return,
    under `text`, alongside the page/block/paragraph/line numbers that say
    where each one sat. Grouping on those four reproduces the line breaks
    instead of paying for a second full pass over the same pixels.

    The line columns are read defensively rather than assumed: a caller that
    supplies only `text` and `conf` gets everything on one line, which is a
    worse transcript than the real table gives but not a crash.
    """
    words = data.get("text") or []
    keys = ("page_num", "block_num", "par_num", "line_num")
    grouped = all(len(data.get(key) or ()) == len(words) for key in keys)
    if not grouped and words:
        # pytesseract's `file_to_dict` drops a column for any short TSV row, so
        # one ragged row silently collapses the whole transcript onto a single
        # line. Say so rather than degrading in silence — the caller's word
        # count and confidence are still right, only the line structure is
        # gone.
        print(
            "warning: OCR line columns are incomplete; line breaks not reconstructed",
            file=sys.stderr,
        )

    lines: list[list[str]] = []
    previous: tuple | None = None
    for index, raw in enumerate(words):
        word = (raw or "").strip() if isinstance(raw, str) else str(raw).strip()
        if not word:
            continue
        current = tuple(data[key][index] for key in keys) if grouped else ()
        if not lines or (grouped and current != previous):
            # A blank line at a block change, so paragraph separation survives.
            # `image_to_string` returns "a\n\nb" across a block boundary and
            # "a\nb" within one, and losing that ran headings into body text.
            if lines and previous is not None and current[:2] != previous[:2]:
                lines.append([])
            lines.append([])
        previous = current
        lines[-1].append(word)

    return "\n".join(" ".join(line) for line in lines).strip()


def cmd_ocr(args) -> dict:
    """Run Tesseract OCR on an image file.

    One Tesseract pass, not two. This used to call `image_to_data` and then
    `image_to_string`, which is two full passes over the same pixels with no
    shared page-segmentation state between them — and automatic attachment OCR
    runs this once per image for up to twenty images in a send. The result
    schema is unchanged.
    """
    if pytesseract is None:
        raise ImportError("pytesseract not installed. Install with: uv sync --extra transcribe")
    path = Path(args.image_path)
    if not path.exists():
        return {"status": "error", "error": f"Image not found: {path}"}

    try:
        image = Image.open(path)
        if args.preprocess:
            image = preprocess_image(image)

        # One pass: text, confidence and word count all come out of this.
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        text = text_from_data(data)

        # Calculate average confidence (exclude -1 values which indicate no text)
        confidences = [c for c in data["conf"] if c > 0]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0

        # Count actual words (non-empty text entries). Coerced the same way
        # `text_from_data` coerces, so the two readings of this one column
        # cannot disagree about what counts as a word.
        word_count = len([w for w in data["text"] if str(w).strip()])

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
