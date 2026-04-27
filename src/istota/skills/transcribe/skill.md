---
name: transcribe
triggers: [transcribe, ocr, screenshot, text in image, handwriting, scan, extract text, image]
description: OCR transcription for images with text
cli: true
file_types: [png, jpg, jpeg, gif, webp, bmp, tiff, tif, heic]
companion_skills: [notes, untrusted_input]
dependencies: [pytesseract]
---
# Image Transcription with OCR

When you receive images containing text (screenshots, documents, handwritten notes), use the OCR skill to get a text extraction, then compare with what you see.

## Usage

```bash
istota-skill transcribe ocr /path/to/image.png
istota-skill transcribe ocr /path/to/image.png --preprocess
```

Use `--preprocess` for low-contrast or noisy images. This applies grayscale conversion and contrast enhancement.

## Output

```json
{
  "status": "ok",
  "text": "Extracted text here...",
  "confidence": 0.85,
  "word_count": 42
}
```

- **text**: The extracted text content
- **confidence**: Average OCR confidence (0-1 scale). Below 0.7 suggests poor image quality or unusual fonts.
- **word_count**: Number of words detected

## Reconciliation Guidelines

When transcribing images:

1. Run the OCR skill to get machine-extracted text
2. Compare with what you see in the image
3. Reconcile differences:
   - **Trust OCR for**: exact spelling, numbers, codes, unusual words
   - **Trust your vision for**: layout, formatting, context, semantic meaning
4. Flag uncertainties with [?] if OCR and vision disagree significantly

## When to Use

- Screenshots with text content
- Scanned documents
- Handwritten notes (use `--preprocess`)
- Images where exact text matters (codes, IDs, addresses)
- Low-quality or small text that's hard to read visually

## Examples

Extract text from a screenshot:
```bash
istota-skill transcribe ocr /srv/mount/nextcloud/content/Users/alice/inbox/screenshot.png
```

Process a handwritten note with preprocessing:
```bash
istota-skill transcribe ocr /tmp/handwritten_note.jpg --preprocess
```

## Transcription workflow

When you receive an image without an explicit request, treat it as a transcription request.

### Saving

- Always save transcriptions automatically — don't ask "should I save this?"
- For where to save, follow the notes skill save location rules

### Title

- Choose a memorable or representative sentence (or fragment) from the transcribed content
- Only capitalize the first letter, not every word
- Never include a title header in the note body — the filename is the title

### Frontmatter

Every transcription note must include YAML frontmatter:
- `created`: date in YYYY-MM-DD format
- `tags`: 1-5 tags. If the user has a canonical tag list (in resources or memory), choose only from it. Otherwise, generate a few descriptive lowercase tags based on the content. Tags go in frontmatter only, never as inline hashtags in the body

Example:
```
---
created: 2026-01-29
tags: [screenshot, philosophy]
---

The transcribed content goes here...

---

*Commentary in italics*
```

### Commentary

After the transcription body, append any relevant commentary or context about the topic in italics, separated by a horizontal rule (`---`). Skip commentary if you have nothing meaningful to add.
