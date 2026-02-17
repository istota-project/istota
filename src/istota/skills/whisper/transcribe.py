"""Core transcription logic using faster-whisper."""

import time
from pathlib import Path

from istota.skills.whisper.models import select_model


def transcribe_audio(
    path: str,
    model: str = "auto",
    language: str | None = None,
) -> dict:
    """Transcribe an audio file using faster-whisper.

    Returns a result dict with status, model used, detected language,
    duration, processing time, text, and segments.
    """
    audio_path = Path(path)
    if not audio_path.exists():
        return {"status": "error", "error": f"Audio file not found: {path}"}

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {
            "status": "error",
            "error": "faster-whisper not installed. Install with: uv sync --extra whisper",
        }

    try:
        model_name = select_model(model)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    start = time.monotonic()
    whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments_iter, info = whisper_model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        word_timestamps=True,
    )

    segments = []
    full_text_parts = []
    for seg in segments_iter:
        words = []
        if seg.words:
            words = [
                {"start": w.start, "end": w.end, "word": w.word, "probability": round(w.probability, 4)}
                for w in seg.words
            ]
        segment_data = {
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "words": words,
        }
        segments.append(segment_data)
        full_text_parts.append(seg.text.strip())

    elapsed = time.monotonic() - start

    return {
        "status": "ok",
        "model": model_name,
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "duration_seconds": round(info.duration, 2),
        "processing_seconds": round(elapsed, 2),
        "text": " ".join(full_text_parts),
        "segments": segments,
    }


def format_srt(segments: list[dict]) -> str:
    """Format segments as SRT subtitle format."""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _format_timestamp_srt(seg["start"])
        end = _format_timestamp_srt(seg["end"])
        lines.append(f"{i}")
        lines.append(f"{start} --> {end}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def format_vtt(segments: list[dict]) -> str:
    """Format segments as WebVTT subtitle format."""
    lines = ["WEBVTT", ""]
    for seg in segments:
        start = _format_timestamp_vtt(seg["start"])
        end = _format_timestamp_vtt(seg["end"])
        lines.append(f"{start} --> {end}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def _format_timestamp_srt(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_timestamp_vtt(seconds: float) -> str:
    """Format seconds as WebVTT timestamp: HH:MM:SS.mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
