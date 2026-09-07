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

from istota.skill_host_paths import path_under_roots, resolve_in_roots, write_resolved
from istota.skills._cli import error_envelope, parse_and_resolve, run_skill_cli
from istota.skills._hostpath import READ, host_path, write_roots
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


#: The suffix `--save` swaps in, per `--output` format. A table rather than a
#: literal at each branch, because the destination is now resolved once up
#: front and the branches only have the text to write.
_SUFFIXES = {"text": ".txt", "srt": ".srt", "vtt": ".vtt", "json": ".json"}


def _save_destination(audio_path: str, output_format: str) -> tuple[Path | None, str | None]:
    """Where `--save` may write, or the refusal saying why it may not.

    The destination is derived rather than named: `audio_path` is stamped
    `READ`, so it arrives resolved and inside a root, and swapping its suffix
    cannot leave its parent. Layer 3 of ISSUE-447 reads that as needing no
    second resolution, and **that is right about containment and silent about
    writability** (ISSUE-453). A `READ` argument is scoped against the task's
    whole working context, which includes `{mount}/Talk` — where a Talk voice
    message lands, and reading one is the point of this verb. The derived
    destination was then inside `{mount}/Talk` too: a directory the sandbox
    binds read-only, written host-side by the daemon user, where the task's
    own file tools could not have written at all.

    So the derived path goes back through the resolver against the *write*
    roots. `_hostpath.write_roots()` rather than `env_host_roots(writable=
    True)` spelled here: the mode-to-roots mapping lives in `_hostpath`, and a
    derived destination has no stamp to carry the mode, so asking that module
    is what keeps this from drifting the next time `WRITE` narrows. The only
    root it drops is `{mount}/Talk`; the channel directory and the task's
    deferred directory both stay, the first because the sandbox binds it
    read-write and a save there is one the task could have made itself.

    **A read root is not a write root, and a derivation inherits the root it
    came from rather than the one it needs.** That is the general rule, stated
    in `.claude/rules/sandbox.md` beside Layer 3 rather than left as this
    verb's own special case.

    Resolved before the transcription runs, not after. The answer depends on
    the arguments alone, so a refusal that waited would charge the task
    minutes of CPU and then discard the transcript it had just produced.
    """
    roots = write_roots()
    out_path = Path(audio_path).with_suffix(_SUFFIXES[output_format])
    resolved, error = resolve_in_roots(
        out_path, roots, writable=True, operation="whisper transcribe --save",
    )
    if error is None:
        return resolved, None
    # Only the containment refusal earns the copy-it-across remedy, and the
    # write roots are what identify it: `resolve_in_roots` also refuses a
    # symlink standing at the derived name, and refuses everything when there
    # are no roots at all — a daemon-side spawn that did not pass the task
    # identity. Telling either of those to copy the audio into the workspace
    # sends the model round the same loop, since the first destination already
    # is in the workspace and the second has no workspace to copy into. Those
    # keep the resolver's own message.
    if roots and not path_under_roots(out_path.parent, roots):
        return None, (
            f"{error}. --save writes beside the audio file, and this one is in "
            "a directory this task may read but not write — Talk is shared and "
            "bound read-only. Copy the audio into your workspace and transcribe "
            "that, or take the transcript from this command's output and write "
            "it where you want it."
        )
    return None, error


def _save_beside(dest: Path, text: str) -> str:
    """Write `text` to the destination `_save_destination` returned.

    `write_resolved` rather than `Path.write_text`, and the resolution above
    does not make it redundant: that check refuses a symlink standing at the
    derived name as of the moment it looks, and the workspace is bound
    read-write into the sandbox, so such a link is model-plantable in the
    window after it. `O_NOFOLLOW` makes the open fail instead of landing the
    transcript wherever the link points, written by the daemon user, with
    containment already reported as passed.

    **That window is now the length of the transcription**, because the
    destination is resolved before the model runs rather than at the write,
    and a long recording is minutes. Accepted rather than closed by a second
    resolution here: the roots are per-user, so the only actor who can swap a
    component of the path is another task of the same user, and what it can
    reach that way is its own owner's directory. The leaf is still covered by
    `O_NOFOLLOW` on every pass. Before this verb resolved the destination at
    all the window was the whole run and unbounded, so this is narrower than
    what it replaces rather than a cost the change introduced.

    `exclusive=False`, matching the overwrite this replaced: the name is
    derived from the caller's own argument rather than minted to be unique, so
    two runs over one file are a re-run rather than a collision.

    UTF-8 explicitly, where `write_text` took the locale's encoding — a
    transcript is model output in whatever language was spoken, and a daemon
    running under a C locale would otherwise raise on the first accent.
    """
    write_resolved(dest, text.encode("utf-8"))
    return str(dest)


def cmd_transcribe(args) -> dict:
    """Transcribe an audio file."""
    save_to = None
    if args.save:
        save_to, refusal = _save_destination(args.audio_path, args.output)
        if refusal is not None:
            return error_envelope(refusal, reason="host_path_refused")

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
        if save_to is not None:
            result["saved_to"] = _save_beside(save_to, text)
        if args.no_segments:
            result.pop("segments", None)
        return result

    if output_format == "srt":
        formatted = format_srt(result["segments"])
        if save_to is not None:
            result["saved_to"] = _save_beside(save_to, formatted)
        result["formatted_output"] = formatted
        del result["segments"]
        return result

    if output_format == "vtt":
        formatted = format_vtt(result["segments"])
        if save_to is not None:
            result["saved_to"] = _save_beside(save_to, formatted)
        result["formatted_output"] = formatted
        del result["segments"]
        return result

    # json (default) — return full result with segments
    if save_to is not None:
        result["saved_to"] = _save_beside(
            save_to, json.dumps(result, indent=2, ensure_ascii=False),
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
