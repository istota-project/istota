"""The files `istota config-request take` hands the host applier, and what
`finish` keeps of the host's output.

`take` writes three files, each 0400, into the staging directory the host
binds at `/out`, which is on the host's `/run` tmpfs:

- `plan.json`: the plan `istota apply` reads, its `[config]` the whole new
  `config.toml` with every credential value back in place, and `[meta]`
  marking the config as the web's and naming the request.
- `files.json`: what `istota-stack put-files` reads: the file credentials and
  the admins file the request changes, and who manages them now. Empty (`{}`)
  when the request changes neither, which the host skips. A credential the
  request moves out of `config.toml` is read here from the live file and
  written to its secret file; it never passed through the web.
- `claude-code`: `1` or `0`, whether the new config needs Claude Code in the
  image (`needs_claude_code`).

`finish` stores the host's output with every line holding one of those
credential values removed, a second pass behind the redaction the writers do
at source.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

from istota.config_editor import secrets

PLAN_FILE = "plan.json"
FILES_FILE = "files.json"
CLAUDE_CODE_FILE = "claude-code"

#: The most of the host's output a request row keeps, from the end.
MAX_OUTPUT_CHARS = 64 * 1024

REMOVED_LINE = "[a line holding a credential value was removed]"

#: The brain kinds that run Claude Code in the image.
_CLAUDE_CODE_KINDS = frozenset({"claude_code", "tmux_claude"})


class HandoffError(Exception):
    """The request cannot be turned into the host's files; nothing was written."""


def needs_claude_code(document: dict) -> bool:
    """Whether a config needs Claude Code in the image.

    The rule `host/istota-stack`'s `needs_claude_code` applies to the file on
    the host, which is the authoritative copy; this one reads the request's new
    document. `tests/test_cli_config_request.py` runs both on the same
    documents.
    """
    brain = document.get("brain", {})
    if not isinstance(brain, dict):
        return False
    kinds = [brain.get("kind", "claude_code"), brain.get("fallback", "")]
    overrides = brain.get("source_type_overrides") or {}
    if isinstance(overrides, dict):
        kinds += list(overrides.values())
    selectable = brain.get("room_selectable") or []
    if isinstance(selectable, list):
        kinds += selectable
    return any(isinstance(kind, str) and kind in _CLAUDE_CODE_KINDS for kind in kinds)


def _secret_file_text(label: str, name: str, value: Any) -> str:
    """A moved value as its secret file holds it: a table (the native brain's
    headers) as the JSON object its variable is read as."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return json.dumps(value)
    raise HandoffError(
        f"{label} holds a value its secret file {name} cannot carry; move it on the host"
    )


def _moved_secrets(
    moves: list[str], current: dict, file_secrets: dict[str, str], environ: Mapping[str, str],
) -> dict[str, str]:
    """Each credential `moves` names, by its secret file, read from `current`.

    A path that holds no value now (another change emptied it) refuses the
    request rather than writing an empty file over nothing. So does a secret
    file that holds a value now: the preview checked it was empty, but a value
    put there on the host since is newer than the inline one, and a secret
    file has no backup.
    """
    from istota.config_editor.edits import inline_file_credentials, normalized
    from istota.config_editor.reference import INSTALLATION_CREDENTIALS

    held = dict(inline_file_credentials(current))
    current = normalized(current)
    found: dict[str, str] = {}
    for label in moves:
        name = INSTALLATION_CREDENTIALS.get(label) if isinstance(label, str) else None
        if not name:
            raise HandoffError("the request moves something that is not a credential with a secret file")
        if name in file_secrets:
            raise HandoffError(f"the request both moves and replaces {label}")
        if environ.get(name.upper(), "").strip():
            raise HandoffError(
                f"{label} already has a value in its secret file {name}, set since the "
                "request was made; remove it from config.toml on the host"
            )
        path = tuple(label.split("."))
        if held.get(path) != name:
            raise HandoffError(
                f"{label} holds no value in config.toml any more, so there is nothing to move to {name}"
            )
        found[name] = _secret_file_text(label, name, secrets.lookup(current, path)[1])
    return found


def build(
    taken: Any, current: dict | None, environ: Mapping[str, str] | None = None,
) -> tuple[dict, dict, bool]:
    """The plan, the put-files request and the Claude Code flag for `taken`.

    `taken` is a `requests.TakenRequest`; `current` the live `config.toml` as
    a document, whose values fill every sentinel no new value replaces, or
    None when it does not parse, which refuses the request. A moved
    credential's value comes from `current` too, and is dropped from the plan.
    `environ` defaults to the process's own, which `take` runs with the secret
    files loaded, so it says what each one holds now.
    """
    from istota.config_editor.edits import UNREADABLE_CONFIG, _unset_everywhere
    from istota.setup_wizard import SECRET_NAMES

    if current is None:
        raise HandoffError(UNREADABLE_CONFIG)
    try:
        document = tomllib.loads(taken.document_toml)
    except tomllib.TOMLDecodeError as exc:
        raise HandoffError(f"the stored document does not parse: {exc}") from None
    file_secrets = {name: value for name, value in taken.secrets.items() if name in SECRET_NAMES}
    inline = {name: value for name, value in taken.secrets.items() if name not in SECRET_NAMES}
    try:
        document = secrets.restore_sentinels(document, current, values=inline)
    except secrets.SentinelError as exc:
        raise HandoffError(f"{exc}; the credential was removed since the request was made") from None
    moves = list(getattr(taken, "moves", None) or [])
    environ = os.environ if environ is None else environ
    moved = _moved_secrets(moves, current, file_secrets, environ)
    for label in moves:
        _unset_everywhere(document, tuple(label.split(".")))
    file_secrets = {**moved, **file_secrets}
    plan = {
        "meta": {"managed": False, "source": "web", "request": int(taken.id)},
        "config": document,
    }
    try:
        json.dumps(plan)
    except (TypeError, ValueError):
        raise HandoffError(
            "the new config.toml holds a date or time value, which the plan's JSON "
            "cannot carry; change it on the host with `istota-stack apply`"
        ) from None
    files: dict[str, Any] = {}
    if file_secrets:
        files["secrets"] = file_secrets
    if taken.admins is not None:
        files["admins"] = "".join(f"{user_id}\n" for user_id in taken.admins)
    resources = {name: False for name in ("secrets", "admins") if name in files}
    if resources:
        files["managed"] = {"source": "web", **resources}
    return plan, files, needs_claude_code(document)


def _write_private(path: Path, text: str) -> None:
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o400)
        stream.write(text)


def write(out_dir: Path, plan: dict, files: dict, claude_code: bool) -> None:
    """The three files, each 0400, in `out_dir`."""
    if not out_dir.is_dir():
        raise HandoffError(f"{out_dir} is not a directory")
    _write_private(out_dir / PLAN_FILE, json.dumps(plan, indent=2, sort_keys=True) + "\n")
    _write_private(out_dir / FILES_FILE, json.dumps(files, indent=2, sort_keys=True) + "\n")
    _write_private(out_dir / CLAUDE_CODE_FILE, "1\n" if claude_code else "0\n")


def _credential_values(staging: Path) -> set[str]:
    """Every credential value the staged files hold: the file credentials, and
    each secret leaf of the plan's config."""
    found: set[str] = set()
    try:
        files = json.loads((staging / FILES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        files = {}
    if isinstance(files, dict) and isinstance(files.get("secrets"), dict):
        found.update(value for value in files["secrets"].values() if isinstance(value, str))
    try:
        plan = json.loads((staging / PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    document = plan.get("config") if isinstance(plan, dict) else None
    if isinstance(document, dict):
        _, inline = secrets.extract_inline_secrets(document)
        found.update(str(value) for value in inline.values())
    return {value for value in found if value.strip()}


def redact_output(text: str, staging: Path) -> str:
    """`text` without any line holding a credential value from `staging`'s
    files, and no longer than `MAX_OUTPUT_CHARS` from its end."""
    values = _credential_values(staging)
    lines = [
        REMOVED_LINE if any(value in line for value in values) else line
        for line in text.splitlines()
    ]
    kept = "\n".join(lines)
    if len(kept) > MAX_OUTPUT_CHARS:
        kept = kept[-MAX_OUTPUT_CHARS:]
    return kept
