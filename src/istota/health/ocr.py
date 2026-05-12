"""OCR + LLM extraction pipeline for uploaded lab reports.

Two stages:

1. **Text extraction** — input-format dispatch:
   - PDF: try ``pdftotext`` first (most modern lab PDFs are text-native;
     OCR loses precision). Fall back to rasterising via ``pdftoppm`` +
     Tesseract per page if the text extraction yields too little content.
   - Image: hand to Tesseract directly.

2. **LLM extraction** — pass the extracted text + the canonical
   ``biomarker_refs`` list (names + aliases) to the configured brain and
   ask for structured JSON. The brain returns a list of
   ``{name, value, unit, ref_range_low?, ref_range_high?, flag?}``
   objects which we sanity-check against the canonical ranges.

Best-effort: every stage falls back to ``[]`` on a missing optional
dependency (``pdftotext``, ``pytesseract``, the brain CLI). The web UI's
review step expects the user to fill in or correct the table either way,
so a partial / empty extraction is still useful.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from istota.health import db as health_db
from istota.health.models import HealthContext, Panel


logger = logging.getLogger(__name__)


# Below this many extracted chars from text-native PDF parsing we assume
# the PDF is a scan and fall back to OCR.
_TEXT_NATIVE_MIN_CHARS = 200


def _resolve_source_file(ctx: HealthContext, panel: Panel) -> Path | None:
    if not panel.source_file:
        return None
    candidate = (ctx.uploads_dir / panel.source_file).resolve()
    try:
        candidate.relative_to(ctx.uploads_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _pdftotext(path: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def _pypdf_extract(path: Path) -> str | None:
    try:
        import pypdf  # noqa: PLC0415
    except ImportError:
        return None
    try:
        reader = pypdf.PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        return "\n\f\n".join(pages)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_ocr_pypdf_failed path=%s error=%s", path, e)
        return None


def _ocr_image(path: Path) -> str | None:
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return pytesseract.image_to_string(img)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_ocr_image_failed path=%s error=%s", path, e)
        return None


def _ocr_pdf_via_pdftoppm(path: Path) -> str | None:
    if not shutil.which("pdftoppm"):
        return None
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", str(path), str(prefix), "-png"],
                check=True, capture_output=True, timeout=60,
            )
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
            logger.warning("health_ocr_pdftoppm_failed path=%s error=%s", path, e)
            return None
        pages: list[str] = []
        for image_path in sorted(Path(tmp).glob("page-*.png")):
            try:
                with Image.open(image_path) as img:
                    pages.append(pytesseract.image_to_string(img))
            except Exception:  # noqa: BLE001
                continue
        return "\n\f\n".join(pages) if pages else None


def extract_text(ctx: HealthContext, panel: Panel) -> str:
    """Return the best-effort extracted text for ``panel.source_file``.

    Empty string when nothing usable could be extracted. Caller should
    decide whether to surface a warning to the user.
    """
    source = _resolve_source_file(ctx, panel)
    if source is None:
        return ""
    mime = (panel.source_mime or "").lower()

    if mime.startswith("image/"):
        return _ocr_image(source) or ""

    if mime == "application/pdf" or source.suffix.lower() == ".pdf":
        text = _pdftotext(source) or _pypdf_extract(source) or ""
        if len(text.strip()) >= _TEXT_NATIVE_MIN_CHARS:
            return text
        fallback = _ocr_pdf_via_pdftoppm(source)
        return fallback or text

    return ""


def _build_extraction_prompt(text: str, refs: list[dict]) -> str:
    """Construct the LLM prompt for structured extraction."""
    # Compact reference list — name, unit, aliases. We don't need to ship
    # the full range table.
    refs_block: list[str] = []
    for r in refs:
        aliases = ", ".join(r["aliases"]) if r["aliases"] else ""
        refs_block.append(
            f"- {r['name']} ({r['default_unit']}){' aliases: ' + aliases if aliases else ''}"
        )
    refs_str = "\n".join(refs_block)

    return f"""You are extracting structured biomarker data from a lab report.

The text below was extracted from an image or PDF of a lab report. Parse it and
return a JSON object with a single key, ``biomarkers``, whose value is a JSON
array. Each element must have these keys:

- ``name`` (string, REQUIRED) — match the canonical name from the reference
  list when possible; if none matches, use the name as printed on the report.
- ``display_name`` (string, OPTIONAL) — exactly as printed on the report.
- ``value`` (number, REQUIRED).
- ``unit`` (string, REQUIRED) — exactly as printed on the report.
- ``ref_range_low`` (number or null, OPTIONAL) — the lab's printed lower bound.
- ``ref_range_high`` (number or null, OPTIONAL) — the lab's printed upper bound.
- ``flag`` (string or null, OPTIONAL) — ``H``, ``L``, or ``C`` exactly as the
  lab reported it. Leave ``null`` if not flagged.

Rules:
- Output ONLY the JSON object, no commentary, no markdown fences.
- Skip rows that aren't quantitative biomarker results (header text, footnotes,
  patient demographics, clinic addresses, comments).
- For ratios or percentages, use the numeric value and the unit as printed.
- If you see a value with a unit that doesn't make sense (e.g. extremely far
  from the canonical range below), include it anyway — the user will review.

Canonical biomarker names + units (use these names when the report matches):

{refs_str}

Lab report text (between <text> markers):

<text>
{text}
</text>
"""


def _strip_code_fences(raw: str) -> str:
    """LLM sometimes wraps JSON in ```json … ``` despite the prompt."""
    raw = raw.strip()
    if raw.startswith("```"):
        # remove opening fence (optionally with lang tag)
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        # remove closing fence
        if raw.endswith("```"):
            raw = raw[: -3]
    return raw.strip()


def _parse_llm_json(raw: str) -> list[dict]:
    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to recover by finding the first { ... } block.
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict) and "biomarkers" in parsed:
        items = parsed["biomarkers"]
    elif isinstance(parsed, list):
        items = parsed
    else:
        return []
    out: list[dict] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                out.append(item)
    return out


def _call_brain(prompt: str, config) -> str | None:
    """Run the extraction prompt through the active brain. Returns the
    raw response text or ``None`` on failure.
    """
    try:
        from istota.brain import BrainRequest, make_brain  # noqa: PLC0415
    except ImportError as e:
        logger.warning("health_ocr_brain_import_failed error=%s", e)
        return None
    if config is None:
        return None
    try:
        brain = make_brain(config.brain)
        model = brain.resolve_model_name("general")
    except Exception as e:  # noqa: BLE001
        logger.warning("health_ocr_brain_init_failed error=%s", e)
        return None
    req = BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=Path(getattr(config, "temp_dir", None) or "/tmp"),
        env=dict(os.environ),
        timeout_seconds=180,
        model=model,
        streaming=False,
    )
    try:
        result = brain.execute(req)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_ocr_brain_failed error=%s", e)
        return None
    if not result.success:
        logger.warning(
            "health_ocr_brain_unsuccessful stop_reason=%s",
            getattr(result, "stop_reason", "?"),
        )
        return None
    return result.result_text or ""


def _sanity_check(biomarkers: list[dict], refs_by_name: dict[str, dict]) -> list[str]:
    """Flag obvious extraction errors against canonical ranges.

    Returns a list of human-readable warning strings; we don't mutate the
    biomarker list — the user decides whether to keep or correct each row
    in the review UI.
    """
    warnings: list[str] = []
    for b in biomarkers:
        name = str(b.get("name") or "").strip()
        try:
            val = float(b.get("value"))
        except (TypeError, ValueError):
            warnings.append(f"{name or '<unnamed>'}: non-numeric value")
            continue
        ref = refs_by_name.get(name)
        if ref is None:
            continue
        # Use the widest canonical range (unisex if present, else sex-specific
        # min/max). This is a sanity check, not the final flag computation.
        lows = [
            v for v in (
                ref.get("ref_range_low"),
                ref.get("ref_range_low_m"),
                ref.get("ref_range_low_f"),
            ) if v is not None
        ]
        highs = [
            v for v in (
                ref.get("ref_range_high"),
                ref.get("ref_range_high_m"),
                ref.get("ref_range_high_f"),
            ) if v is not None
        ]
        widest_low = min(lows) if lows else None
        widest_high = max(highs) if highs else None
        # 10x outside the widest canonical bound is almost certainly an
        # OCR/parsing error (decimal place lost, unit confused).
        if widest_high is not None and val > widest_high * 10:
            warnings.append(
                f"{name}: value {val} {b.get('unit', '')} is far above the "
                f"expected canonical range ({widest_high}). Possible OCR error.",
            )
        elif widest_low is not None and widest_low > 0 and val < widest_low / 10:
            warnings.append(
                f"{name}: value {val} {b.get('unit', '')} is far below the "
                f"expected canonical range ({widest_low}). Possible OCR error.",
            )
    return warnings


def extract_from_panel(ctx: HealthContext, panel: Panel, *, config=None) -> dict:
    """Run the full OCR + LLM pipeline for a panel.

    Returns a dict ready to send back to the web client:
    ``{biomarkers, warnings, raw_text}``. The web UI shows the extracted
    rows in an editable table next to the source preview; on confirm it
    POSTs the (possibly edited) list to ``/panels/{id}/biomarkers``.

    The ``config`` arg is only used for the brain call. When omitted, we
    pick it up off the panel's environment via the same mechanism the
    routes use; falling back to "no extraction" if unavailable.
    """
    text = extract_text(ctx, panel)
    raw_chars = len(text)
    if not text.strip():
        # No usable text — persist what we have so the review UI can show
        # the source document alongside an empty table.
        with health_db.connect(ctx.db_path) as conn:
            conn.execute(
                "UPDATE panels SET ocr_text = '' WHERE id = ?", (panel.id,),
            )
            conn.commit()
        return {
            "biomarkers": [],
            "warnings": [
                "Could not extract text from the source file. "
                "Add biomarkers manually below.",
            ],
            "raw_text": "",
        }

    # Persist the raw OCR text first — useful for diagnostics and as a
    # fallback if the LLM call fails.
    with health_db.connect(ctx.db_path) as conn:
        conn.execute(
            "UPDATE panels SET ocr_text = ? WHERE id = ?",
            (text, panel.id),
        )
        conn.commit()
        ref_rows = health_db.list_biomarker_refs(conn)
    refs = [
        {
            "name": r.name,
            "display_name": r.display_name,
            "default_unit": r.default_unit,
            "aliases": r.aliases,
            "ref_range_low": r.ref_range_low,
            "ref_range_high": r.ref_range_high,
            "ref_range_low_m": r.ref_range_low_m,
            "ref_range_high_m": r.ref_range_high_m,
            "ref_range_low_f": r.ref_range_low_f,
            "ref_range_high_f": r.ref_range_high_f,
        }
        for r in ref_rows
    ]

    prompt = _build_extraction_prompt(text, refs)
    response = _call_brain(prompt, config)
    if not response:
        return {
            "biomarkers": [],
            "warnings": [
                f"Extracted {raw_chars} characters of text but the LLM extraction step "
                "is unavailable on this instance. Add biomarkers manually below.",
            ],
            "raw_text": text,
        }

    biomarkers = _parse_llm_json(response)
    refs_by_name = {r["name"]: r for r in refs}
    warnings = _sanity_check(biomarkers, refs_by_name)

    if not biomarkers:
        warnings.insert(
            0,
            "The LLM returned a response we couldn't parse as biomarker JSON. "
            "Add rows manually or try again.",
        )

    return {
        "biomarkers": biomarkers,
        "warnings": warnings,
        "raw_text": text,
    }
