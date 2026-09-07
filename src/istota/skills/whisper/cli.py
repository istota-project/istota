"""Whisper transcription skill CLI.

CLI:
    python -m istota.skills.whisper transcribe /path/to/audio.wav [--model auto] [--language en]
    python -m istota.skills.whisper transcribe /path/to/audio.wav --output srt --save
    python -m istota.skills.whisper models
    python -m istota.skills.whisper download <model-name>
"""

import argparse
import json
from pathlib import Path

from istota.skill_host_paths import write_resolved
from istota.skills._cli import parse_and_resolve, run_skill_cli
from istota.skills._hostpath import READ, host_path
from istota.skills.whisper.models import (
    download_model,
    get_available_memory_gb,
    list_models,
)
from istota.skills.whisper.transcribe import (
    format_srt,
    format_vtt,
    transcribe_audio,
)


def _save_beside(audio_path: str, suffix: str, text: str) -> str:
    """Write `text` next to the transcribed audio, under a code-owned name.

    The destination is derived rather than named: `audio_path` is stamped
    `READ`, so it arrives resolved and inside a root, and swapping its suffix
    cannot leave its parent. That is Layer 3 of ISSUE-447 — a derived path
    whose only added component is code-owned is already contained and needs no
    second resolution.

    It still needs `write_resolved`. `Path.write_text` follows a symlink
    standing at the derived name, and the workspace is bound read-write into
    the sandbox, so such a link is model-plantable: the transcript would land
    wherever it points, written by the daemon user, with containment already
    reported as passed. `O_NOFOLLOW` makes the open fail instead.

    `exclusive=False`, matching the overwrite this replaced: the name is
    derived from the caller's own argument rather than minted to be unique, so
    two runs over one file are a re-run rather than a collision.

    UTF-8 explicitly, where `write_text` took the locale's encoding — a
    transcript is model output in whatever language was spoken, and a daemon
    running under a C locale would otherwise raise on the first accent.
    """
    out_path = Path(audio_path).with_suffix(suffix)
    write_resolved(out_path, text.encode("utf-8"))
    return str(out_path)


def cmd_transcribe(args) -> dict:
    """Transcribe an audio file."""
    result = transcribe_audio(
        args.audio_path,
        model=args.model,
        language=args.language,
    )

    if result.get("status") == "error":
        return result

    output_format = args.output

    if output_format == "text":
        text = result["text"]
        if args.save:
            result["saved_to"] = _save_beside(args.audio_path, ".txt", text)
        if args.no_segments:
            result.pop("segments", None)
        return result

    if output_format == "srt":
        formatted = format_srt(result["segments"])
        if args.save:
            result["saved_to"] = _save_beside(args.audio_path, ".srt", formatted)
        result["formatted_output"] = formatted
        del result["segments"]
        return result

    if output_format == "vtt":
        formatted = format_vtt(result["segments"])
        if args.save:
            result["saved_to"] = _save_beside(args.audio_path, ".vtt", formatted)
        result["formatted_output"] = formatted
        del result["segments"]
        return result

    # json (default) — return full result with segments
    if args.save:
        result["saved_to"] = _save_beside(
            args.audio_path, ".json", json.dumps(result, indent=2, ensure_ascii=False),
        )

    # After the save, so `--save` still writes the whole thing. `segments` is
    # one entry per *word*, which is megabytes on a long recording; a caller
    # that only wants the transcript should not have to receive it, parse it
    # and drop it (ISSUE-273).
    if args.no_segments:
        result.pop("segments", None)

    return result


def cmd_models(args) -> dict:
    """List available models."""
    models = list_models()
    available_gb = get_available_memory_gb()
    return {
        "status": "ok",
        "available_memory_gb": round(available_gb, 1),
        "models": models,
    }


def cmd_download(args) -> dict:
    """Download a model."""
    return download_model(args.model_name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.whisper",
        description="Audio transcription using faster-whisper (CPU, int8)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # transcribe command
    tr = sub.add_parser("transcribe", help="Transcribe an audio file")
    # The transcript comes back to whoever called, inside the task, so this is
    # the task's whole working context — including `{mount}/Talk`, which is
    # where a Talk voice message lands. `out_of_process.py` is the daemon-side
    # caller and has to pass the task identity for any of it to resolve.
    host_path(tr, "audio_path", mode=READ, help="Path to audio file")
    tr.add_argument(
        "--model",
        default="auto",
        help="Model name or 'auto' to select based on available RAM (default: auto)",
    )
    tr.add_argument("--language", help="Language code (e.g., 'en'). Auto-detected if omitted.")
    tr.add_argument(
        "--output",
        choices=["json", "text", "srt", "vtt"],
        default="json",
        help="Output format (default: json)",
    )
    tr.add_argument(
        "--save",
        action="store_true",
        help="Save output to file alongside the audio file",
    )
    tr.add_argument(
        "--no-segments",
        action="store_true",
        help="Omit per-word segments from json/text output (transcript text only)",
    )

    # models command
    sub.add_parser("models", help="List available models and RAM requirements")

    # download command
    dl = sub.add_parser("download", help="Pre-download a model")
    dl.add_argument("model_name", help="Model to download (e.g., 'small', 'medium')")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)

    commands = {
        "transcribe": cmd_transcribe,
        "models": cmd_models,
        "download": cmd_download,
    }

    run_skill_cli(commands, args)
