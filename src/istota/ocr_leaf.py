"""One Tesseract pass over one image, in a module that imports almost nothing.

This is the OCR child's entry point, and it is a top-level leaf for the reason
`git_hardening.py` and `forge_bin.py` are: what a spawned process imports is
paid on every spawn, and automatic attachment OCR spawns one per image.

The child used to be `python -m istota.skills.transcribe`, which runs
`istota/skills/__init__.py`. That star-imports every skill in the package, so a
process whose whole job is to read one image with Tesseract first imported
`istota.skills.calendar`, and through it `caldav` and `niquests` — an HTTP
client. Measured in this checkout: 0.41s to import `istota.skills.transcribe`
against 0.19s for `PIL` and `pytesseract` alone, on a pass that costs about a
second per image. A five-image send paid the difference five times, in the
window between the transport receiving the message and the model seeing its
first byte.

So the rule for this file is narrow and worth keeping: **it imports the
standard library, Pillow and pytesseract, and nothing from `istota`.** Adding
an import from the package would quietly put the star-import back, and nothing
about the resulting slowdown would look like a bug — the OCR would still be
correct, just slower, on a path with no timing assertion in front of a user.
`tests/test_transcribe_out_of_process.py::TestTheChildImportSurface` spawns a
real subprocess and fails if any `istota.*` module other than this one is
resident after importing it.

What that guard does **not** cover is a third-party import, and one is left in
deliberately: `pytesseract` does `try: import pandas` at its own module scope,
for an output type this never asks for, which is about 0.12s of the remaining
0.23s. It could be suppressed by seeding `sys.modules["pandas"] = None` before
the import, since pytesseract catches the ImportError and carries on. That is
not done — it is a hack on another project's internals that would turn a future
`Output.DATAFRAME` call into a confusing failure, and running the children
concurrently amortizes the cost across a send rather than paying it per image.

`istota.skills.transcribe` re-exports these functions rather than keeping a
second copy, so the skill CLI a user invokes by hand and the child the daemon
spawns run the same code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import pytesseract
    from PIL import Image, ImageEnhance
except ImportError:  # pragma: no cover - exercised by the caller's error path
    pytesseract = None
    Image = None
    ImageEnhance = None

__all__ = [
    "preprocess_image",
    "text_from_data",
    "ocr_image",
    "build_parser",
    "main",
]


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


def ocr_image(image_path, preprocess: bool = False) -> dict:
    """Run Tesseract OCR on an image file and return the result dict.

    One Tesseract pass, not two. This used to call `image_to_data` and then
    `image_to_string`, which is two full passes over the same pixels with no
    shared page-segmentation state between them — and automatic attachment OCR
    runs this once per image for up to twenty images in a send.
    """
    if pytesseract is None:
        raise ImportError("pytesseract not installed. Install with: uv sync --extra transcribe")
    path = Path(image_path)
    if not path.exists():
        return {"status": "error", "error": f"Image not found: {path}"}

    try:
        image = Image.open(path)
        if preprocess:
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
    """The child's own parser, matching the skill CLI's `ocr` subcommand.

    A subcommand rather than a bare positional, so the argv the daemon spawns
    reads the same as the one a user types at the skill CLI.
    """
    parser = argparse.ArgumentParser(
        prog="python -m istota.ocr_leaf",
        description="One Tesseract pass over one image",
    )
    sub = parser.add_subparsers(dest="command", required=True)
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
    try:
        result = ocr_image(args.image_path, preprocess=args.preprocess)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "error":
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
