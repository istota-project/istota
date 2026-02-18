# Image Transcription with OCR

When you receive images containing text (screenshots, documents, handwritten notes), use the OCR skill to get a text extraction, then compare with what you see.

## Usage

```bash
python -m istota.skills.transcribe ocr /path/to/image.png
python -m istota.skills.transcribe ocr /path/to/image.png --preprocess
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
python -m istota.skills.transcribe ocr /srv/mount/nextcloud/content/Users/alice/inbox/screenshot.png
```

Process a handwritten note with preprocessing:
```bash
python -m istota.skills.transcribe ocr /tmp/handwritten_note.jpg --preprocess
```
