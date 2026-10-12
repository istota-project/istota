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

`[config]` is validated through `load_config` and `config_mapper`, deciding on
the typed problems `config_mapper.report_problem` records rather than on log
wording: a `discarded` problem (a value the loader would ignore, default or
replace) or a loader exception refuses the plan, and an unknown key, a retired
one or any other warning is printed and applied (a key a newer release reads
has to load on an older one). `--check` runs that validation alone, reading no
current file and writing nothing, and `--json` prints its problems as one
object for the web's config editor. The file is rendered by
`lib/toml_write.py`, the tree's one TOML writer, and compared with the current
one as data, so a hand-formatted file with the same values is left alone, its
comments included. The diff printed is of the two documents with every
credential value replaced (`config_editor.edits.diff_documents`), rendered the
same way, so it shows values that change and not formatting.

`[meta] managed` and `source` say who owns the config, recorded in
`managed.json` beside it for the config editor, which leaves a managed
resource read-only. A change to that record alone is not a config change: it
is written and the exit code stays 0, so nothing restarts for it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from istota.config_editor.edits import diff_documents
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

#: The ownership record beside `config.toml`, and the resources it names.
#: `host/istota-stack`'s `PUT_FILES_PY` writes the `secrets` and `admins`
#: entries with its own copy of this logic, since it runs on the host with no
#: istota to import; the format here is the authoritative one.
MANAGED_FILE = "managed.json"
MANAGED_RESOURCES = ("config", "secrets", "admins")
MAX_SOURCE_LENGTH = 64

_BACKUP_STAMP = "%Y%m%dT%H%M%S.%fZ"
_BACKUP_SUFFIX = re.compile(r"\.bak\.\d{8}T\d{6}\.\d{6}Z")


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


@dataclass
class CheckResult:
    """What loading a `[config]` document reported.

    `problems` are `{"kind", "path", "message"}` entries from
    `config_mapper.collect_problems`, `path` a list of strings. `error` is a
    refusal that stopped the load, or `None`.
    """

    problems: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def discarded(self) -> list[dict]:
        return [problem for problem in self.problems if problem["kind"] == "discarded"]

    @property
    def refused(self) -> bool:
        return self.error is not None or bool(self.discarded)

    def as_json(self) -> dict:
        return {"problems": self.problems, "error": self.error}


@dataclass(frozen=True)
class Meta:
    """The plan's `[meta]`, validated: who the plan says owns the config."""

    managed: bool = False
    source: str = "plan"


def load_plan(path: Path | str, *, stdin=None) -> dict:
    """The plan document: JSON when the file ends in `.json`, TOML otherwise.

    `-` reads the plan from `stdin` (default `sys.stdin`), which has no name to
    go by: a document opening with `{` is JSON, anything else TOML.
    """
    raw: bytes | str
    if str(path) == "-":
        where = "on stdin"
        try:
            raw = (stdin if stdin is not None else sys.stdin).read()
        except (OSError, UnicodeDecodeError) as exc:
            raise PlanError(f"cannot read the plan {where}: {exc}") from None
        is_json = raw.lstrip().startswith("{")
    else:
        path = Path(path)
        where = str(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise PlanError(f"cannot read the plan {where}: {exc.strerror or exc}") from None
        is_json = path.suffix.lower() == ".json"
    try:
        if is_json:
            document = json.loads(raw)
        else:
            document = tomllib.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanError(f"the plan {where} does not parse: {exc}") from None
    if not isinstance(document, dict):
        raise PlanError(f"the plan {where} is not a table of sections")
    return document


def _check_meta(meta: object) -> Meta:
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
    managed = meta.get("managed", False)
    if not isinstance(managed, bool):
        raise PlanError("[meta] managed must be true or false")
    source = meta.get("source", "plan")
    if not isinstance(source, str) or len(source) > MAX_SOURCE_LENGTH:
        raise PlanError(f"[meta] source must be a string of at most {MAX_SOURCE_LENGTH} characters")
    # The config editor's request id, which the host records; apply only
    # checks its type.
    request = meta.get("request")
    if request is not None and (isinstance(request, bool) or not isinstance(request, int)):
        raise PlanError("[meta] request must be an integer")
    return Meta(managed=managed, source=source)


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


def collect(document: object) -> CheckResult:
    """Load `document` as `config.toml` and return what the loader reported.

    The load runs against a scratch copy whose `db_path` names nothing, so the
    database overlays `load_config` applies (and the clean-up it does on the
    way) touch no real database while a plan is only being considered.
    """
    from istota.config import load_config
    from istota.config_mapper import collect_problems

    if not isinstance(document, dict):
        return CheckResult(error="[config] must be a table holding config.toml")
    null = _find_null(document, "config")
    if null:
        return CheckResult(error=f"{null} is null; TOML has no null. Leave the key out for its default.")
    with tempfile.TemporaryDirectory(prefix="istota-apply-") as scratch:
        probe = dict(document)
        if isinstance(probe.get("db_path"), str):
            probe["db_path"] = str(Path(scratch) / "absent" / "istota.db")
        path = Path(scratch) / "config.toml"
        try:
            path.write_text(toml_write.dumps(probe), encoding="utf-8")
        except toml_write.TomlWriteError as exc:
            return CheckResult(error=f"[config]: {exc}")
        error = None
        with collect_problems() as problems:
            try:
                load_config(path)
            except Exception as exc:  # noqa: BLE001 - every loader refusal is a plan refusal
                error = f"[config] does not load: {exc}"
    scratch_name = str(path)
    reported = [
        {
            "kind": problem["kind"],
            "path": list(problem["path"]),
            "message": problem["message"].replace(scratch_name, "the plan's [config]"),
        }
        for problem in problems
    ]
    if error is not None:
        error = error.replace(scratch_name, "the plan's [config]")
    return CheckResult(problems=reported, error=error)


def validate_config(document: dict) -> list[str]:
    """Load `document` as `config.toml` and return its warnings.

    Raises `PlanError` for a value the loader would discard (a `discarded`
    problem) or refuses outright.
    """
    result = collect(document)
    if result.error is not None:
        raise PlanError(result.error)
    if result.discarded:
        raise PlanError(
            "[config] has values the loader would discard: "
            + "; ".join(problem["message"] for problem in result.discarded)
        )
    return [problem["message"] for problem in result.problems]


def render_config(document: dict) -> str:
    return CONFIG_HEADER + toml_write.dumps(document)


def _parsed_or_empty(text: str) -> dict:
    """The current file as a document; one that does not parse diffs as empty."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _same_document(current_text: str, document: dict) -> bool:
    try:
        return tomllib.loads(current_text) == document
    except tomllib.TOMLDecodeError:
        return False


def write_private(path: Path, text: str | bytes) -> None:
    """Replace `path` with `text`, 0600, through a temporary file and a rename.

    A config can hold credentials, and a reader must never see half a file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        if isinstance(text, bytes):
            stream = os.fdopen(handle, "wb")
        else:
            stream = os.fdopen(handle, "w", encoding="utf-8")
        with stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_managed(config_dir: Path) -> dict:
    """The ownership record in `config_dir`; a missing or unreadable one is empty."""
    try:
        document = json.loads((config_dir / MANAGED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _entry_is(entry: object, managed: bool, source: str) -> bool:
    return isinstance(entry, dict) and entry.get("managed") is managed and entry.get("source") == source


def managed_differs(config_dir: Path, resource: str, managed: bool, source: str) -> bool:
    return not _entry_is(read_managed(config_dir).get(resource), managed, source)


def write_managed(config_dir: Path, entries: dict[str, tuple[bool, str]]) -> bool:
    """Record `entries` (resource to `(managed, source)`) in the ownership record.

    Entries for other resources are kept, and one that already says the same
    keeps its timestamp. Returns whether the file was written.
    """
    current = read_managed(config_dir)
    updated = dict(current)
    stamp = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    for resource, (managed, source) in entries.items():
        if resource not in MANAGED_RESOURCES:
            raise ValueError(f"{resource!r} is not a resource {MANAGED_FILE} records")
        if _entry_is(current.get(resource), managed, source):
            continue
        updated[resource] = {"managed": managed, "source": source, "at": stamp}
    if updated == current:
        return False
    write_private(config_dir / MANAGED_FILE, json.dumps(updated, indent=2, sort_keys=True) + "\n")
    return True


def _keep_newest_backups(config_path: Path, keep: int) -> None:
    prefix = config_path.name
    backups = sorted(
        candidate for candidate in config_path.parent.iterdir()
        if candidate.name.startswith(prefix)
        and _BACKUP_SUFFIX.fullmatch(candidate.name[len(prefix):])
    )
    for stale in backups[:-keep]:
        stale.unlink(missing_ok=True)


def _write_config(config_path: Path, new_text: str, backup: int | None) -> None:
    if backup and config_path.exists():
        # Bytes, so the backup is the file as it was, line endings included.
        previous = config_path.read_bytes()
        stamp = _utc_now().strftime(_BACKUP_STAMP)
        write_private(config_path.with_name(f"{config_path.name}.bak.{stamp}"), previous)
    write_private(config_path, new_text)
    if backup:
        _keep_newest_backups(config_path, backup)


def plan_config(document: object, config_path: Path, *, backup: int | None = None) -> SectionPlan:
    if not isinstance(document, dict):
        raise PlanError("[config] must be a table holding config.toml")
    warnings = validate_config(document)
    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if _same_document(current, document):
        return SectionPlan("config", changed=False, warnings=warnings)
    new_text = render_config(document)
    diff = diff_documents(
        _parsed_or_empty(current), document,
        fromfile=f"a/{config_path.name}", tofile=f"b/{config_path.name}",
    )
    return SectionPlan(
        "config", changed=True, diff=diff, warnings=warnings,
        write=lambda: _write_config(config_path, new_text, backup),
    )


def _sections(plan: dict) -> tuple[Meta, object]:
    """The plan's validated `[meta]`, and its `[config]` (`None` when absent)."""
    for name in plan:
        if name not in ("meta", "config"):
            raise PlanError(
                f"[{name}] is not a section this istota applies; it reconciles "
                "[config] only. Remove it, or apply the plan with the release it "
                "was written for."
            )
    return _check_meta(plan.get("meta", {})), plan.get("config")


def check(plan_path: Path | str, *, as_json: bool, out, err, stdin=None) -> int:
    """Validate a plan without reading or writing any config. 0 valid, 1 refused.

    With `as_json`, prints `{"problems": [...], "error": str | null}` and
    nothing else, whatever the outcome; a plan that does not parse is an
    `error` there.
    """
    try:
        _, document = _sections(load_plan(plan_path, stdin=stdin))
    except PlanError as exc:
        result = CheckResult(error=str(exc))
    else:
        result = collect(document) if document is not None else CheckResult()
    if as_json:
        out(json.dumps(result.as_json()))
        return EXIT_ERROR if result.refused else EXIT_UNCHANGED
    for problem in result.problems:
        err(f"istota apply: {problem['kind']}: {problem['message']}")
    if result.error is not None:
        err(f"istota apply: {result.error}")
    if result.refused:
        err("istota apply: the plan would be refused.")
        return EXIT_ERROR
    out("The plan is valid.")
    return EXIT_UNCHANGED


def run(
    plan_path: Path | str, config_path: Path, *, dry_run: bool, out, err,
    backup: int | None = None, stdin=None,
) -> int:
    """Plan every section, print, and write unless `dry_run`. Returns the exit code."""
    try:
        meta, document = _sections(load_plan(plan_path, stdin=stdin))
        planned: list[SectionPlan] = []
        if document is not None:
            planned.append(plan_config(document, config_path, backup=backup))
    except PlanError as exc:
        err(f"istota apply: {exc}")
        err("istota apply: nothing was written.")
        return EXIT_ERROR

    # Only a plan with a config to own claims it.
    marker = document is not None and managed_differs(
        config_path.parent, "config", meta.managed, meta.source,
    )

    for section in planned:
        for warning in section.warnings:
            err(f"istota apply: warning: {warning}")
        for line in section.diff:
            out(line)
    changed = [section for section in planned if section.changed]
    if dry_run:
        if changed:
            out(f"Would change: {', '.join(section.name for section in changed)} (--dry-run, nothing written).")
            return EXIT_CHANGED
        out("Would update: management marker (--dry-run, nothing written)." if marker else "No changes.")
        return EXIT_UNCHANGED
    for section in changed:
        try:
            section.write()
        except OSError as exc:
            err(f"istota apply: writing [{section.name}] failed: {exc}")
            return EXIT_ERROR
    if marker:
        try:
            write_managed(config_path.parent, {"config": (meta.managed, meta.source)})
        except OSError as exc:
            # After a config write the change stands and must still restart.
            err(f"istota apply: writing {MANAGED_FILE} failed: {exc}")
            if not changed:
                return EXIT_ERROR
            marker = False
    if changed:
        out(f"Changed: {', '.join(section.name for section in changed)}.")
    if marker:
        out("Updated: management marker." if changed else "Updated: management marker; config.toml is unchanged.")
    if not changed:
        if not marker:
            out("No changes.")
        return EXIT_UNCHANGED
    return EXIT_CHANGED
