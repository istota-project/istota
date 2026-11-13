"""The installation backup: what it holds, gathered from saved state, and what
restoring it asks of the editor.

A backup is the saved `config.toml`, redacted, plus the installation
credentials it and the stack's secret files hold. `gather_backup` reads them;
`restore_change_set` turns one back into an ordinary editor change set for
`edits.preview`. The KDBX container is the credential exporter's; nothing here
encrypts.

Two kinds of credential entry:

- **file**: one of `setup_wizard.SECRET_NAMES`, from its secret file or, when
  that file is absent or empty, from the saved inline value at its mapped path
  (`schema.file_credentials`). Either way it is restored to the secret file.
- **inline**: any other secret leaf (`secrets.is_secret_path`) held in the
  file, restored at its path with its TOML type.

`destination_refusal` is the one exclusion and destination policy, applied on
gather and on import; `validate_bundle` is the one set of size limits, applied
to a gathered bundle and to a parsed upload. Credential values are never in a
record's repr, an error message or an omission.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import math
import os
import stat
import tomllib
from pathlib import Path
from typing import Any, Collection, Mapping

from istota.config_editor import secrets
from istota.lib import toml_write

#: Inclusive maxima, in UTF-8 bytes.
CONFIG_MAX_BYTES = 256 * 1024
CREDENTIALS_MAX_BYTES = 256 * 1024

#: The comment that heads the configuration in a plain download and in the
#: encrypted backup's attachment, counted in `CONFIG_MAX_BYTES`.
CONFIG_PREAMBLE = (
    "# Istota configuration backup.\n"
    "# Credentials are replaced by \"<unchanged>\"; database-backed settings and\n"
    "# environment overrides are not in this file.\n\n"
)

#: Deployment credentials that are runtime keys, not service credentials:
#: never gathered, never restored.
RUNTIME_KEY_FILES: frozenset[str] = frozenset({"istota_web_session_secret_key"})
#: Runtime keys held only in the environment, recorded as excluded.
RUNTIME_KEY_VARIABLES: tuple[str, ...] = ("ISTOTA_SECRET_KEY", "ISTOTA_WEB_TOKEN_KEY")

_SOURCES = ("file", "inline")


class BackupError(Exception):
    """A backup that cannot be gathered or used. `code` is the fixed reason;
    the message never carries a credential value or uploaded text."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class FileCredential:
    target: str
    source: str
    value: str = dataclasses.field(repr=False)

    @property
    def destination(self) -> str:
        return f"file:{self.target}"


@dataclasses.dataclass(frozen=True)
class InlineCredential:
    path: tuple[str, ...]
    value: Any = dataclasses.field(repr=False)

    @property
    def destination(self) -> str:
        return f"inline:{secrets.label(self.path)}"


@dataclasses.dataclass(frozen=True)
class Omission:
    """A credential the backup does not hold, and why. Never a value."""

    category: str
    name: str
    reason: str

    def as_json(self) -> dict:
        return {"category": self.category, "name": self.name, "reason": self.reason}


@dataclasses.dataclass
class Bundle:
    """A backup's contents, gathered or parsed.

    `config_toml` is the redacted document with `CONFIG_PREAMBLE`, exactly the
    bytes a download or the attachment holds. `config_digest` is the sha256 of
    the saved `config.toml` it was made from, or None for a parsed upload.
    """

    config_toml: str
    file_credentials: list[FileCredential] = dataclasses.field(default_factory=list)
    inline_credentials: list[InlineCredential] = dataclasses.field(default_factory=list)
    omissions: list[Omission] = dataclasses.field(default_factory=list)
    config_digest: str | None = None
    secrets_dir_available: bool = True

    @property
    def credentials(self) -> list[FileCredential | InlineCredential]:
        return [*self.file_credentials, *self.inline_credentials]

    def summary(self) -> dict:
        return {
            "file_credentials": len(self.file_credentials),
            "inline_credentials": len(self.inline_credentials),
            "omissions": [omission.as_json() for omission in self.omissions],
            "secrets_dir_available": self.secrets_dir_available,
        }


# --- the destination policy -------------------------------------------------


def _mapped_paths() -> dict[tuple[str, ...], str]:
    """Each path, current or legacy spelling, that a secret file credential
    is read from, to the file's name."""
    from istota.config import legacy_spellings
    from istota.config_editor import schema

    found: dict[tuple[str, ...], str] = {}
    for path, name in schema.file_credentials().items():
        found[path] = name
        for older in legacy_spellings(path):
            found[tuple(older)] = name
    return found


def _mapped_file(path: tuple[str, ...]) -> str | None:
    """The secret file whose mapped path is `path` or contains it."""
    for mapped, name in _mapped_paths().items():
        if path[:len(mapped)] == mapped:
            return name
    return None


def destination_refusal(kind: str, destination: Any) -> str | None:
    """Why a backup may not hold or restore this destination, or None.

    `destination` is a secret file's name for `kind == "file"` and a path for
    `kind == "inline"`. Holds for gather and import alike, so a backup never
    grants a right to write a host path or rotate a runtime key.
    """
    from istota.config_editor import edits
    from istota.setup_wizard import GENERATED_SECRETS, SECRET_NAMES

    if kind == "file":
        if not isinstance(destination, str) or destination not in SECRET_NAMES:
            return "not a deployment credential"
        if destination in RUNTIME_KEY_FILES:
            return "a runtime key, not a service credential"
        if destination in GENERATED_SECRETS:
            return "generated by the stack"
        return None
    if kind == "inline":
        if (
            not isinstance(destination, tuple) or not destination
            or len(destination) > edits._MAX_SEGMENTS
            or not all(
                isinstance(segment, str) and segment and len(segment) <= edits._MAX_SEGMENT_CHARS
                for segment in destination
            )
        ):
            return "not a config path"
        if not secrets.is_secret_path(destination):
            return "not a credential path"
        name = _mapped_file(destination)
        if name in RUNTIME_KEY_FILES:
            return "a runtime key, not a service credential"
        if name is not None:
            return "a deployment credential, restored through its secret file"
        return None
    return "not a credential kind"


def host_only_reason(credential: FileCredential | InlineCredential) -> str | None:
    """The editor's reason a restore cannot write this from the web, or None."""
    from istota.config_editor import schema

    if isinstance(credential, FileCredential):
        paths = {name: path for path, name in schema.file_credentials().items()}
        path = paths.get(credential.target)
        return schema.read_only_reason(path) if path else None
    return schema.read_only_reason(credential.path)


# --- limits -------------------------------------------------------------------


def _value_bytes(credential: FileCredential | InlineCredential) -> int:
    if isinstance(credential, FileCredential):
        return len(credential.value.encode("utf-8"))
    return len(json.dumps(credential.value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _record(credential: FileCredential | InlineCredential) -> dict:
    if isinstance(credential, FileCredential):
        return {
            "destination": credential.destination, "kind": "file",
            "target": credential.target, "source": credential.source,
            "value": credential.value,
        }
    return {
        "destination": credential.destination, "kind": "inline",
        "path": list(credential.path), "value": credential.value,
    }


def canonical_credentials(credentials: Collection[FileCredential | InlineCredential]) -> bytes:
    """Every destination/value record as the one JSON encoding the aggregate
    limit is measured on: sorted by destination, compact, UTF-8."""
    records = sorted((_record(item) for item in credentials), key=lambda record: record["destination"])
    return json.dumps(
        records, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _too_large(category: str, limit: int, unit: str = "bytes") -> BackupError:
    return BackupError("too_large", f"The backup's {category} is over its limit of {limit} {unit}.")


def validate_bundle(bundle: Bundle) -> None:
    """Refuse a bundle over a limit, or holding a forbidden or repeated
    destination. The same check for a gathered bundle and a parsed upload."""
    from istota.credentials.kdbx_import import VAULT_MAX_ENTRIES
    from istota.credentials.names import VAULT_MAX_VALUE_BYTES

    if len(bundle.config_toml.encode("utf-8")) > CONFIG_MAX_BYTES:
        raise _too_large("configuration", CONFIG_MAX_BYTES)
    seen: set[str] = set()
    for credential in bundle.credentials:
        if isinstance(credential, FileCredential):
            refusal = destination_refusal("file", credential.target)
            if refusal is None and (credential.source not in _SOURCES or not isinstance(credential.value, str)):
                refusal = "not a valid credential record"
        else:
            refusal = destination_refusal("inline", credential.path)
            if refusal is None and not _typed_scalar(credential.value):
                refusal = "not a valid credential value"
        if refusal is not None:
            raise BackupError("invalid_backup", "The backup holds a credential it may not restore.")
        if credential.destination in seen:
            raise BackupError("invalid_backup", "The backup holds one credential destination twice.")
        seen.add(credential.destination)
        if _value_bytes(credential) > VAULT_MAX_VALUE_BYTES:
            raise _too_large("credential value", VAULT_MAX_VALUE_BYTES)
    if len(canonical_credentials(bundle.credentials)) > CREDENTIALS_MAX_BYTES:
        raise _too_large("credentials", CREDENTIALS_MAX_BYTES)
    # The manifest and the Configuration entry count too.
    if len(bundle.credentials) + 2 > VAULT_MAX_ENTRIES:
        raise _too_large("entry count", VAULT_MAX_ENTRIES, "entries")


def _typed_scalar(value: Any) -> bool:
    if isinstance(value, bool) or isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str)


# --- gathering ----------------------------------------------------------------


def config_document_text(redacted: dict) -> str:
    """The configuration as a download or attachment holds it."""
    return CONFIG_PREAMBLE + toml_write.dumps(redacted)


def _read_secret_file(secrets_dir: Path, name: str) -> bytes | None:
    """The secret file's bytes, or None when it is absent. A symlink, a
    non-regular file, an oversized one or a failed read refuses the gather:
    only a missing file falls back to the inline value."""
    from istota.credentials.names import VAULT_MAX_VALUE_BYTES

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(secrets_dir / name, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENOTDIR:
            raise BackupError("unreadable_secret", "The secret directory is not a directory.") from None
        reason = "is a symlink" if exc.errno == errno.ELOOP else "could not be read"
        raise BackupError("unreadable_secret", f"The secret file {name} {reason}.") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BackupError("unreadable_secret", f"The secret file {name} is not a regular file.")
        chunks: list[bytes] = []
        size = 0
        # The launcher strips trailing newlines, so allow a few past the cap.
        cap = VAULT_MAX_VALUE_BYTES + 64
        while size <= cap:
            chunk = os.read(fd, cap + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
    except OSError:
        raise BackupError("unreadable_secret", f"The secret file {name} could not be read.") from None
    finally:
        os.close(fd)
    if len(data) > cap:
        raise _too_large(f"secret file {name}", VAULT_MAX_VALUE_BYTES)
    return data


def _gathered_names() -> list[str]:
    from istota.setup_wizard import SECRET_NAMES

    return [name for name in SECRET_NAMES if destination_refusal("file", name) is None]


def _read_state(config_path: Path, secrets_dir: Path) -> tuple[bytes, dict[str, bytes | None], bool]:
    try:
        config_bytes = Path(config_path).read_bytes()
    except OSError:
        raise BackupError("config_unreadable", "config.toml could not be read.") from None
    files = {name: _read_secret_file(secrets_dir, name) for name in _gathered_names()}
    return config_bytes, files, Path(secrets_dir).is_dir()


def _file_value(data: bytes | None, name: str) -> str:
    """What `istota-secrets` would export from the file: `$(cat)` drops
    trailing newlines, and an empty result is no credential."""
    if data is None:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BackupError("unreadable_secret", f"The secret file {name} is not UTF-8 text.") from None
    return text.rstrip("\n")


def _inline_credentials(document: dict) -> list[InlineCredential]:
    """Every secret leaf held in the file that no secret file is read from."""
    found: list[InlineCredential] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + (key,))
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (str(index),))
            return
        if not path or not secrets.is_secret_path(path):
            return
        if _mapped_file(path) is not None:
            return
        if value == "" or value == secrets.SENTINEL:
            return
        if not _typed_scalar(value):
            raise BackupError(
                "invalid_saved_credential",
                f"The saved credential at {secrets.label(path)} is not a string, number or boolean.",
            )
        if destination_refusal("inline", path) is not None:
            return
        found.append(InlineCredential(path=path, value=value))

    walk(document, ())
    return found


def _build(
    config_bytes: bytes, files: dict[str, bytes | None], dir_available: bool,
    environ: Mapping[str, str],
) -> Bundle:
    from istota.config import SECRET_ENV_OVERRIDES
    from istota.config_editor import edits, schema
    from istota.setup_wizard import DAEMON_ONLY_SECRETS, GENERATED_SECRETS

    try:
        document = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise BackupError("config_unreadable", edits.UNREADABLE_CONFIG) from None
    read = edits.normalized(document)
    paths_by_name = {name: path for path, name in schema.file_credentials().items()}

    omissions = [
        Omission("runtime_key", name, "a runtime key, not a service credential")
        for name in (*sorted(RUNTIME_KEY_FILES), *RUNTIME_KEY_VARIABLES)
    ]
    omissions += [
        Omission("generated", name, "generated by the stack") for name in sorted(GENERATED_SECRETS)
    ]
    file_credentials: list[FileCredential] = []
    for name in _gathered_names():
        file_value = _file_value(files.get(name), name)
        path = paths_by_name.get(name)
        present, inline = secrets.lookup(read, path) if path else (False, None)
        holds_inline = present and edits._holds_value(inline)
        if file_value:
            file_credentials.append(FileCredential(target=name, source="file", value=file_value))
            if holds_inline:
                omissions.append(Omission(
                    "shadowed_inline", secrets.label(path),
                    f"the secret file {name} is used, so the inactive value in config.toml was not kept",
                ))
            continue
        if holds_inline:
            if not isinstance(inline, str):
                raise BackupError(
                    "invalid_saved_credential",
                    f"The saved credential at {secrets.label(path)} is not a string.",
                )
            file_credentials.append(FileCredential(target=name, source="inline", value=inline))
            continue
        if environ.get(name.upper(), "").strip():
            omissions.append(Omission("environment", name, "supplied outside the saved configuration"))
        elif name in DAEMON_ONLY_SECRETS:
            omissions.append(Omission("daemon_only", name, "not readable by the web process"))

    inline_credentials = _inline_credentials(document)
    held = {credential.path for credential in inline_credentials}
    for variable, section, key in SECRET_ENV_OVERRIDES:
        path = tuple(section.split(".")) + (key,)
        if path in held or _mapped_file(path) is not None:
            continue
        if environ.get(variable, "").strip():
            omissions.append(Omission(
                "environment", secrets.label(path), "supplied outside the saved configuration",
            ))

    try:
        config_toml = config_document_text(secrets.redact_document(document))
    except toml_write.TomlWriteError:
        raise BackupError("config_unreadable", "config.toml cannot be written back as TOML.") from None
    bundle = Bundle(
        config_toml=config_toml,
        file_credentials=file_credentials,
        inline_credentials=inline_credentials,
        omissions=omissions,
        config_digest=hashlib.sha256(config_bytes).hexdigest(),
        secrets_dir_available=dir_available,
    )
    validate_bundle(bundle)
    return bundle


def _open_request(db_path: Path | None) -> bool:
    from istota import db
    from istota.config_editor import requests

    if not db.database_present(db_path):
        return False
    with db.get_db(db_path) as conn:
        return requests._open_id(conn) is not None


def gather_backup(
    config_path: Path, secrets_dir: Path, *,
    db_path: Path | None = None, environ: Mapping[str, str] | None = None,
) -> Bundle:
    """The saved configuration and its installation credentials, as a bundle.

    `secrets_dir` is the trusted deployment directory (`ISTOTA_SECRETS_DIR`,
    else `/run/secrets`); only `SECRET_NAMES` are opened in it. The config and
    the secret files are read twice and must match, with one full retry, as
    `settings_plan.snapshot.gather` does; that is two matching reads, not a
    transaction. With `db_path`, an open config change request refuses.
    `environ` only says which credentials came from outside the saved files.
    """
    environ = os.environ if environ is None else environ
    if _open_request(db_path):
        raise BackupError(
            "request_open", "A configuration change is waiting to be applied; back up after it finishes.",
        )
    for _ in range(2):
        first = _read_state(config_path, secrets_dir)
        second = _read_state(config_path, secrets_dir)
        if first == second:
            return _build(*first, environ)
    raise BackupError("state_changed", "The configuration changed while it was being read; try again.")


# --- restoring ----------------------------------------------------------------


def _put_at(document: dict, path: tuple[str, ...], value: Any) -> None:
    """Set `path` in `document`, creating tables; an array index must exist."""
    from istota.config_editor.edits import ChangeError

    node: Any = document
    for depth, segment in enumerate(path):
        last = depth == len(path) - 1
        if isinstance(node, dict):
            if last:
                node[segment] = value
                return
            if segment not in node:
                node[segment] = {}
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            if last:
                node[int(segment)] = value
                return
            node = node[int(segment)]
        else:
            raise ChangeError(f"{secrets.label(path)} has no place in this configuration")


def _warning(path: tuple[str, ...] | None, message: str) -> dict:
    return {
        "kind": "warning", "path": list(path or ()), "message": message,
        "blocking": False, "pre_existing": False,
    }


def restore_change_set(
    bundle: Bundle, current_text: str, *, restore_config: bool, selected: Collection[str],
    base_digest: str, current_files: Mapping[str, str] | None = None,
) -> tuple[dict, list[dict]]:
    """An editor change set restoring `bundle` onto `current_text`, and the
    non-blocking warnings a review shows.

    `restore_config` replaces the document with the backup's; otherwise the
    current one is kept. `selected` names the credentials to restore by
    destination. A selected inline value is put into the document before any
    sentinel is resolved, so it lands even where the current file has no
    value there; a selected file credential goes through `secrets`, never its
    inline path. A sentinel with nothing to keep is dropped and named. A file
    credential whose value equals `current_files` is left out, since
    `edits.preview` counts every supplied file credential as a change.
    """
    from istota.config_editor import schema
    from istota.config_editor.edits import UNREADABLE_CONFIG, ChangeError

    try:
        current = tomllib.loads(current_text)
    except tomllib.TOMLDecodeError:
        raise ChangeError(UNREADABLE_CONFIG) from None
    by_destination = {credential.destination: credential for credential in bundle.credentials}
    chosen = []
    for destination in sorted(set(selected)):
        if destination not in by_destination:
            raise ChangeError("the selection names a credential that is not in the backup")
        chosen.append(by_destination[destination])
    validate_bundle(Bundle(config_toml=bundle.config_toml, file_credentials=[
        item for item in chosen if isinstance(item, FileCredential)
    ], inline_credentials=[item for item in chosen if isinstance(item, InlineCredential)]))

    if restore_config:
        try:
            document = tomllib.loads(bundle.config_toml)
        except tomllib.TOMLDecodeError:
            raise BackupError("invalid_backup", "The backup's configuration does not parse.") from None
    else:
        document = secrets.redact_document(current)
    for credential in chosen:
        if isinstance(credential, InlineCredential):
            _put_at(document, credential.path, credential.value)

    restored_files = {
        credential.target: credential for credential in chosen if isinstance(credential, FileCredential)
    }
    document, dropped = secrets.drop_missing_sentinels(document, current)
    warnings: list[dict] = []
    for name in dropped:
        path = secrets.parse_label(name)
        if path is not None and _mapped_file(path) in restored_files:
            continue
        warnings.append(_warning(path, f"{name} has no value on this installation; it stays unset"))
    paths_by_name = {name: path for path, name in schema.file_credentials().items()}
    file_secrets: dict[str, str] = {}
    for name, credential in sorted(restored_files.items()):
        if current_files is not None and current_files.get(name) == credential.value:
            continue
        file_secrets[name] = credential.value
        if credential.source == "inline":
            path = paths_by_name.get(name)
            shown = secrets.label(path) if path else name
            warnings.append(_warning(path, (
                f"{shown} was held in config.toml when the backup was made; "
                f"restoring writes it to the secret file {name}"
            )))
    try:
        raw = toml_write.dumps(document)
    except toml_write.TomlWriteError as exc:
        raise ChangeError(f"the restored document cannot be written: {exc}") from None
    return {"base_digest": base_digest, "ops": [], "raw": raw, "secrets": file_secrets}, warnings
