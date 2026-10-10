"""`istota apply -f plan`: bring the install in line with a declarative plan.

A plan is a TOML or JSON document of sections. This version reconciles one,
`[config]`, which holds the whole of `config.toml` as a nested table, so a
config manager (Ansible, say) can keep its variables as the source of truth
without a template per key: `{"config": istota_config} | to_json`.

The contract is the exit code, after `terraform plan -detailed-exitcode`:

    0  nothing to change
    2  changed, or would change under --dry-run
    1  an error, and nothing was written

Every section is planned before anything is written, and a refusal anywhere
writes nothing. The declarative-apply spec extends the same verb with
`[users.*]` (profiles, resources, briefings, jobs, heartbeats, secret slots),
`--prune` and `istota snapshot`; each arrives as another entry in `SECTIONS`,
and the codes above do not move. A section this version does not reconcile is
refused, never skipped, so a plan written for a newer istota fails loudly here
rather than being half applied.

`[config]` is validated through `load_config` and `config_mapper`: an unknown
key warns (a key a newer release reads has to load on an older one), a value
the loader would discard or raise on is refused. The file is rendered by
`lib/toml_write.py`, the tree's one TOML writer, and compared with the current
one as data, so a hand-formatted file with the same values is left alone, its
comments included. The diff printed is the text diff, with credential values
replaced.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from istota.lib import toml_write

EXIT_UNCHANGED = 0
EXIT_ERROR = 1
EXIT_CHANGED = 2

#: The plan format this version reads. `[meta] version` is optional; a higher
#: one is a plan written for a newer istota.
PLAN_VERSION = 1

CONFIG_HEADER = (
    "# Istota configuration, written by `istota apply` from a plan.\n"
    "# The plan is the source of truth: an edit here is replaced by the next\n"
    "# apply that changes anything. Reference: config/config.example.toml.\n\n"
)


class PlanError(Exception):
    """A plan this version refuses. Nothing has been written."""


@dataclass
class SectionPlan:
    """What applying one section would do."""

    name: str
    changed: bool
    diff: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    write: Callable[[], None] | None = None


def load_plan(path: Path) -> dict:
    """The plan document: JSON when the file ends in `.json`, TOML otherwise."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read the plan {path}: {exc.strerror or exc}") from None
    try:
        if path.suffix.lower() == ".json":
            document = json.loads(raw)
        else:
            document = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanError(f"the plan {path} does not parse: {exc}") from None
    if not isinstance(document, dict):
        raise PlanError(f"the plan {path} is not a table of sections")
    return document


def _check_meta(meta: object) -> None:
    if not isinstance(meta, dict):
        raise PlanError("[meta] must be a table")
    version = meta.get("version", PLAN_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanError("[meta] version must be an integer")
    if version > PLAN_VERSION:
        raise PlanError(
            f"the plan is format version {version}; this istota reads version "
            f"{PLAN_VERSION}. Apply it with the release it was written for."
        )


def _find_null(value: object, where: str) -> str | None:
    if value is None:
        return where
    if isinstance(value, dict):
        for key, item in value.items():
            found = _find_null(item, f"{where}.{key}")
            if found:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_null(item, f"{where}[{index}]")
            if found:
                return found
    return None


#: The config loader's own words for a value it discarded. Matched on the
#: message, since `config_mapper` reports through logging and never raises.
_DISCARDED = re.compile(r"ignoring the value|ignoring the section|must be a table")
_UNKNOWN = re.compile(r"unrecognised key")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def validate_config(document: dict) -> list[str]:
    """Load `document` as `config.toml` and return its warnings.

    Raises `PlanError` for a value the loader would discard or refuses outright.
    The load runs against a scratch copy whose `db_path` names nothing, so the
    database overlays `load_config` applies (and the clean-up it does on the
    way) touch no real database while a plan is only being considered.
    """
    from istota.config import load_config

    null = _find_null(document, "config")
    if null:
        raise PlanError(f"{null} is null; TOML has no null. Leave the key out for its default.")
    with tempfile.TemporaryDirectory(prefix="istota-apply-") as scratch:
        probe = dict(document)
        if isinstance(probe.get("db_path"), str):
            probe["db_path"] = str(Path(scratch) / "absent" / "istota.db")
        path = Path(scratch) / "config.toml"
        try:
            path.write_text(toml_write.dumps(probe), encoding="utf-8")
        except toml_write.TomlWriteError as exc:
            raise PlanError(f"[config]: {exc}") from None
        capture = _Capture()
        logger = logging.getLogger("istota.config")
        logger.addHandler(capture)
        try:
            load_config(path)
        except Exception as exc:  # noqa: BLE001 - every loader refusal is a plan refusal
            raise PlanError(f"[config] does not load: {exc}") from None
        finally:
            logger.removeHandler(capture)
    discarded = [message for message in capture.messages if _DISCARDED.search(message)]
    if discarded:
        raise PlanError("[config] has values the loader would discard: " + "; ".join(discarded))
    return [message.replace(str(path), "the plan's [config]") for message in capture.messages]


def render_config(document: dict) -> str:
    return CONFIG_HEADER + toml_write.dumps(document)


def _same_document(current_text: str, document: dict) -> bool:
    try:
        return tomllib.loads(current_text) == document
    except tomllib.TOMLDecodeError:
        return False


_ASSIGNMENT = re.compile(r"^(?P<lead>[-+ ]\s*)(?P<key>[A-Za-z0-9_\-\"]+)(?P<eq>\s*=\s*).*$")


def _redact(line: str) -> str:
    from istota.webui.admin_config_view import is_secret_name

    match = _ASSIGNMENT.match(line)
    if match and is_secret_name(match.group("key").strip('"')):
        return f"{match.group('lead')}{match.group('key')}{match.group('eq')}<redacted>"
    return line


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, 0600, through a temporary file and a rename.

    A config can hold credentials, and a reader must never see half a file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def plan_config(document: object, config_path: Path) -> SectionPlan:
    if not isinstance(document, dict):
        raise PlanError("[config] must be a table holding config.toml")
    warnings = validate_config(document)
    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if _same_document(current, document):
        return SectionPlan("config", changed=False, warnings=warnings)
    new_text = render_config(document)
    diff = [
        _redact(line.rstrip("\n"))
        for line in difflib.unified_diff(
            current.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{config_path.name}",
            tofile=f"b/{config_path.name}",
        )
    ]
    return SectionPlan(
        "config", changed=True, diff=diff, warnings=warnings,
        write=lambda: write_private(config_path, new_text),
    )


def run(plan_path: Path, config_path: Path, *, dry_run: bool, out, err) -> int:
    """Plan every section, print, and write unless `dry_run`. Returns the exit code."""
    try:
        plan = load_plan(plan_path)
        planned: list[SectionPlan] = []
        for name, value in plan.items():
            if name == "meta":
                _check_meta(value)
            elif name == "config":
                planned.append(plan_config(value, config_path))
            else:
                raise PlanError(
                    f"[{name}] is not a section this istota applies; it reconciles "
                    "[config] only. Remove it, or apply the plan with the release it "
                    "was written for."
                )
    except PlanError as exc:
        err(f"istota apply: {exc}")
        err("istota apply: nothing was written.")
        return EXIT_ERROR

    for section in planned:
        for warning in section.warnings:
            err(f"istota apply: warning: {warning}")
        for line in section.diff:
            out(line)
    changed = [section for section in planned if section.changed]
    if not changed:
        out("No changes.")
        return EXIT_UNCHANGED
    if dry_run:
        out(f"Would change: {', '.join(section.name for section in changed)} (--dry-run, nothing written).")
        return EXIT_CHANGED
    for section in changed:
        try:
            section.write()
        except OSError as exc:
            err(f"istota apply: writing [{section.name}] failed: {exc}")
            return EXIT_ERROR
    out(f"Changed: {', '.join(section.name for section in changed)}.")
    return EXIT_CHANGED
