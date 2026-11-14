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

One more, the private push signing key (#947), read from the effective
`[push] signing_key_path`. Only the private half is kept; the `.pub` is
derived when it is written. A restore hands it to the host applier, which
writes it with `istota push import-key`, and only to the default path under
the config directory (`push_key_refusal`): it makes no host path writable,
so `destination_refusal` does not name it.

`build_installation_kdbx` writes a bundle as the encrypted backup and
`open_installation_backup` reads one back through the disposable decrypt
worker (`credentials/kdbx_worker.py`), behind the installation-wide decrypt
slot; `parse_installation_backup` is the strict reading of what the worker
returns.

A restore reads an upload with `read_plain_backup` or `open_installation_backup`,
shows each credential through `credential_statuses` and ties the reviewed
preview to its submit with `restore_input_digest` and `preview_digest`.

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
from istota.config_editor.edits import PUSH_KEY_SECRET
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


#: The push signing key's destination id, which is also its name among a
#: request's sealed values.
PUSH_KEY_ID = PUSH_KEY_SECRET
PUSH_KEY_LABEL = "push.signing_key_path"
#: The most a push key file is read to; a PEM Ed25519 key is about 120 bytes.
PUSH_KEY_MAX_BYTES = 64 * 1024


@dataclasses.dataclass(frozen=True)
class PushSigningKey:
    """The private push signing key, as unencrypted PKCS8 PEM text."""

    value: str = dataclasses.field(repr=False)

    @property
    def destination(self) -> str:
        return PUSH_KEY_ID


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
    #: A parsed backup's manifest; None for a gather.
    manifest: "Manifest | None" = None
    push_key: PushSigningKey | None = None

    @property
    def credentials(self) -> list["Credential"]:
        found: list[Credential] = [*self.file_credentials, *self.inline_credentials]
        if self.push_key is not None:
            found.append(self.push_key)
        return found

    def summary(self) -> dict:
        return {
            "file_credentials": len(self.file_credentials),
            "inline_credentials": len(self.inline_credentials),
            "push_signing_key": self.push_key is not None,
            "omissions": [omission.as_json() for omission in self.omissions],
            "secrets_dir_available": self.secrets_dir_available,
        }


Credential = FileCredential | InlineCredential | PushSigningKey


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


# --- the push signing key -----------------------------------------------------

PUSH_KEY_ENV = "ISTOTA_PUSH_SIGNING_KEY_PATH"
_PUSH_KEY_ON_THE_HOST = (
    "Write it on the host with `istota-stack push-key import`, the key on stdin."
)
REENROLL_NOTE = (
    "Replaces the push signing key this installation has now: phones enrolled under that key "
    "must enroll again before they show private previews. Phones enrolled under the restored "
    "key keep working."
)


def push_key_path(document: Any, environ: Mapping[str, str]) -> str:
    """The effective `[push] signing_key_path` of `document`, as `load_config`
    reads it: `ISTOTA_PUSH_SIGNING_KEY_PATH` wins when it is not blank."""
    override = environ.get(PUSH_KEY_ENV, "").strip()
    if override:
        return override
    push = document.get("push") if isinstance(document, dict) else None
    value = push.get("signing_key_path") if isinstance(push, dict) else None
    return value.strip() if isinstance(value, str) else ""


def default_push_key_path(config_dir: Path) -> Path:
    """Where a restore may write the push key: `push/signing.pem` in the
    config directory, the path the management role and the runbook use."""
    return Path(config_dir) / "push" / "signing.pem"


def push_key_refusal(document: Any, environ: Mapping[str, str], config_dir: Path | None) -> str | None:
    """Why a restore may not write the push key under `document`, or None.

    Only to the default path, so a backup never makes another host path
    writable; any other is the operator's, on the host.
    """
    path = push_key_path(document, environ)
    if not path:
        return (
            "[push] signing_key_path is not set in the configuration this restore leaves, "
            "so the key has nowhere to go. " + _PUSH_KEY_ON_THE_HOST
        )
    if config_dir is None or Path(path) != default_push_key_path(config_dir):
        return (
            "A restore writes the push signing key only to push/signing.pem in the config "
            "directory, and [push] signing_key_path names another file. " + _PUSH_KEY_ON_THE_HOST
        )
    return None


def _push_key_pem(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


def push_key_id(pem: str) -> str:
    """The key ID (`envelope.key_id`) of a PEM the bundle holds."""
    from istota.push import envelope

    key = envelope.parse_signing_key(pem.encode("ascii"))
    return envelope.key_id(key.public_key().public_bytes_raw())


def _read_push_key_file(path: Path) -> bytes | None:
    """The key file's bytes, or None when there is none. A link at the leaf,
    a non-regular file, an oversized one or a failed read refuses."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    what = "The push signing key at [push] signing_key_path"
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        reason = "is a symlink" if exc.errno == errno.ELOOP else "could not be read"
        raise BackupError("unreadable_secret", f"{what} {reason}.") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BackupError("unreadable_secret", f"{what} is not a regular file.")
        chunks: list[bytes] = []
        size = 0
        while size <= PUSH_KEY_MAX_BYTES:
            chunk = os.read(fd, PUSH_KEY_MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        raise BackupError("unreadable_secret", f"{what} could not be read.") from None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > PUSH_KEY_MAX_BYTES:
        raise _too_large("push signing key", PUSH_KEY_MAX_BYTES)
    return data


def _push_key_text(data: bytes) -> str:
    from istota.push import envelope

    try:
        key = envelope.parse_signing_key(data)
    except envelope.KeyFileError:
        raise BackupError(
            "invalid_saved_credential",
            "The push signing key at [push] signing_key_path is not an unencrypted Ed25519 key.",
        ) from None
    return _push_key_pem(key)


def current_push_key(path: Path) -> str | None:
    """The key at `path` now, as a gather reads it, or None when there is none."""
    data = _read_push_key_file(Path(path))
    return None if data is None else _push_key_text(data)


def _valid_push_key(value: Any) -> bool:
    from istota.push import envelope

    if not isinstance(value, str):
        return False
    try:
        return _push_key_pem(envelope.parse_signing_key(value.encode("ascii"))) == value
    except (envelope.KeyFileError, UnicodeEncodeError):
        return False


# --- limits -------------------------------------------------------------------


def _value_bytes(credential: Credential) -> int:
    if isinstance(credential, (FileCredential, PushSigningKey)):
        return len(credential.value.encode("utf-8"))
    return len(json.dumps(credential.value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _record(credential: Credential) -> dict:
    if isinstance(credential, PushSigningKey):
        return {"destination": credential.destination, "kind": "push_key", "value": credential.value}
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


def canonical_credentials(credentials: Collection[Credential]) -> bytes:
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
        if isinstance(credential, PushSigningKey):
            refusal = None if _valid_push_key(credential.value) else "not an Ed25519 key"
        elif isinstance(credential, FileCredential):
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


def _read_state(
    config_path: Path, secrets_dir: Path, environ: Mapping[str, str],
) -> tuple[bytes, dict[str, bytes | None], bool, bytes | None]:
    try:
        config_bytes = Path(config_path).read_bytes()
    except OSError:
        raise BackupError("config_unreadable", "config.toml could not be read.") from None
    files = {name: _read_secret_file(secrets_dir, name) for name in _gathered_names()}
    try:
        document = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        document = {}  # `_build` refuses it
    key_path = push_key_path(document, environ)
    push_key = _read_push_key_file(Path(key_path)) if key_path else None
    return config_bytes, files, Path(secrets_dir).is_dir(), push_key


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
    push_key_data: bytes | None, environ: Mapping[str, str],
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
            from istota.config_editor.handoff import HandoffError, secret_file_text

            try:
                value = secret_file_text(secrets.label(path), name, inline)
            except HandoffError:
                raise BackupError(
                    "invalid_saved_credential",
                    f"The saved credential at {secrets.label(path)} is not a value its "
                    f"secret file {name} can hold.",
                ) from None
            file_credentials.append(FileCredential(target=name, source="inline", value=value))
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

    # A file that is no Ed25519 key (a stale path under `content = "legacy"`)
    # is left out and named; one that cannot be read refuses, as a secret file does.
    push_key = None
    if push_key_data is not None:
        try:
            push_key = PushSigningKey(_push_key_text(push_key_data))
        except BackupError:
            omissions.append(Omission(
                "push_signing_key", PUSH_KEY_LABEL, "the file there is not an unencrypted Ed25519 key",
            ))
    elif push_key_path(document, environ):
        omissions.append(Omission("push_signing_key", PUSH_KEY_LABEL, "no key file at that path"))

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
        push_key=push_key,
    )
    validate_bundle(bundle)
    return bundle


def deployment_secrets_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The stack's secret directory, by `istota-secrets`' own rule:
    `ISTOTA_SECRETS_DIR` when set and nonempty, else `/run/secrets`."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("ISTOTA_SECRETS_DIR") or "/run/secrets")


def encrypted_backup_available() -> bool:
    """Whether the vault extra, which writes and reads the KDBX, is installed."""
    import importlib.util

    return importlib.util.find_spec("pykeepass") is not None


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
        first = _read_state(config_path, secrets_dir, environ)
        second = _read_state(config_path, secrets_dir, environ)
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


MOVED_ELEMENT_REASON = (
    "The list entry this credential belongs to is not at the same place in the current "
    "configuration. Restore the configuration with it, or enter the credential again."
)


def _backup_document(bundle: Bundle) -> dict:
    try:
        return tomllib.loads(bundle.config_toml)
    except tomllib.TOMLDecodeError:
        raise BackupError("invalid_backup", "The backup's configuration does not parse.") from None


def _element_moved(credential: FileCredential | InlineCredential, backup_document: dict, current: dict) -> bool:
    """Whether an inline credential inside an array would land in a different
    element of `current` than the one it was backed up from. Recorded by
    index, so a credentials-only restore must not write it there."""
    if not isinstance(credential, InlineCredential):
        return False
    return not secrets.same_elements(backup_document, current, credential.path)


def _warning(path: tuple[str, ...] | None, message: str) -> dict:
    return {
        "kind": "warning", "path": list(path or ()), "message": message,
        "blocking": False, "pre_existing": False,
    }


def restore_change_set(
    bundle: Bundle, current_text: str, *, restore_config: bool, selected: Collection[str],
    base_digest: str, current_files: Mapping[str, str] | None = None,
    config_dir: Path | None = None, current_push_key: str | None = None,
    environ: Mapping[str, str] | None = None,
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

    A selected push signing key goes in as `push_key`, `{value, replaces}`,
    `replaces` the key ID of `current_push_key` (the live key at the default
    path) or None. It is left out when it is the live key, and refused where
    `push_key_refusal` says it cannot be written.
    """
    from istota.config_editor import edits, schema
    from istota.config_editor.edits import UNREADABLE_CONFIG, ChangeError

    environ = os.environ if environ is None else environ
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
    push = next((item for item in chosen if isinstance(item, PushSigningKey)), None)
    validate_bundle(Bundle(config_toml=bundle.config_toml, file_credentials=[
        item for item in chosen if isinstance(item, FileCredential)
    ], inline_credentials=[item for item in chosen if isinstance(item, InlineCredential)], push_key=push))

    backup_document = _backup_document(bundle)
    if push is not None and push_key_refusal(
        backup_document if restore_config else current, environ, config_dir,
    ) is not None:
        raise ChangeError("the selection names a push signing key this restore cannot write")
    if restore_config:
        document = backup_document
    else:
        if any(_element_moved(credential, backup_document, current) for credential in chosen):
            raise ChangeError("the selection names a credential whose list entry is not in this configuration")
        document = secrets.redact_document(current)
    for credential in chosen:
        if isinstance(credential, InlineCredential):
            _put_at(document, credential.path, credential.value)

    restored_files = {
        credential.target: credential for credential in chosen if isinstance(credential, FileCredential)
    }
    document, dropped = secrets.drop_missing_sentinels(document, current)
    # Dropping every header of an inline `extra_headers` table leaves `{}` at a
    # mapped path, which `edits.check_file_credentials` reads as a new value.
    for mapped in _mapped_paths():
        found, value = secrets.lookup(document, mapped)
        if found and value in ({}, []) and secrets.lookup(current, mapped) != (True, value):
            edits._unset_at(document, mapped)
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
    change_set = {"base_digest": base_digest, "ops": [], "raw": raw, "secrets": file_secrets}
    if push is not None and push.value != current_push_key:
        replaces = push_key_id(current_push_key) if current_push_key else None
        change_set["push_key"] = {"value": push.value, "replaces": replaces}
        if replaces is not None:
            warnings.append(_warning(tuple(PUSH_KEY_LABEL.split(".")), REENROLL_NOTE))
    return change_set, warnings


# --- a restore's inputs and what it shows -------------------------------------

#: The first eight bytes of a KDBX file (KeePass 2.x signature).
KDBX_SIGNATURE = b"\x03\xd9\xa2\x9a\x67\xfb\x4b\xb5"


def read_plain_backup(data: bytes) -> Bundle:
    """An uploaded plain TOML backup, as a bundle with no credentials.

    Refuses an empty or whitespace-only file rather than read it as a reset,
    text that is not UTF-8 TOML, and a file holding a real credential value at
    any secret path (`secrets.extract_inline_secrets`, the one predicate): a
    plain upload must not become a change set carrying a pasted secret.
    """
    from istota.config_editor import edits

    if data.startswith(KDBX_SIGNATURE):
        raise _invalid("This is an encrypted backup; restore it as one.")
    if len(data) > CONFIG_MAX_BYTES:
        raise _too_large("configuration", CONFIG_MAX_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("The file is not UTF-8 text.") from None
    if not text.strip():
        raise _invalid("The file is empty.")
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise _invalid("The file is not a TOML configuration.") from None
    _, held = secrets.extract_inline_secrets(document)
    if any(edits._holds_value(value) for value in held.values()):
        raise BackupError(
            "plaintext_secrets",
            "This file holds credential values in plain text. Restore an encrypted backup, "
            "or enter the credentials in the editor.",
        )
    bundle = Bundle(config_toml=text)
    validate_bundle(bundle)
    return bundle


def current_secret_files(secrets_dir: Path) -> dict[str, str]:
    """What each secret file a backup may restore holds now, nonempty only,
    read as a gather reads it. Feeds `credential_statuses` and
    `restore_change_set(current_files=...)`."""
    found: dict[str, str] = {}
    for name in _gathered_names():
        value = _file_value(_read_secret_file(secrets_dir, name), name)
        if value:
            found[name] = value
    return found


def _same(now: Any, value: Any) -> bool:
    return type(now) is type(value) and now == value


def _current_value(
    credential: FileCredential | InlineCredential, current: dict, current_files: Mapping[str, str],
) -> Any:
    """The value this installation uses now at the credential's destination,
    or None. A secret file credential falls back to its mapped inline value,
    as the launcher and a gather do."""
    from istota.config_editor import edits, schema
    from istota.config_editor.handoff import HandoffError, secret_file_text

    if isinstance(credential, InlineCredential):
        found, value = secrets.lookup(current, credential.path)
        return value if found and edits._holds_value(value) else None
    if credential.target in current_files:
        return current_files[credential.target]
    paths = {name: path for path, name in schema.file_credentials().items()}
    path = paths.get(credential.target)
    if path is None:
        return None
    found, inline = secrets.lookup(edits.normalized(current), path)
    if not found or not edits._holds_value(inline):
        return None
    try:
        return secret_file_text(secrets.label(path), credential.target, inline)
    except HandoffError:
        return None


def _push_key_row(
    credential: PushSigningKey, document: dict, *, config_dir: Path | None,
    current_push_key: str | None, current_push_key_error: str | None, environ: Mapping[str, str],
) -> dict:
    reason = push_key_refusal(document, environ, config_dir)
    if reason is None and current_push_key_error is not None:
        reason = (
            f"{current_push_key_error} Replace it on the host with "
            "`istota-stack push-key import --force`, the key on stdin."
        )
    if reason is not None:
        status = "host_only"
    elif current_push_key is None:
        status = "new"
    elif current_push_key == credential.value:
        status = "unchanged"
    else:
        status, reason = "changed", REENROLL_NOTE
    return {
        "id": credential.destination,
        "kind": "push_key",
        "name": f"Private push signing key ({PUSH_KEY_LABEL})",
        "source": "file",
        "status": status,
        "reason": reason,
        "selectable": status in ("new", "changed"),
    }


def credential_statuses(
    bundle: Bundle, current_text: str, current_files: Mapping[str, str], *, restore_config: bool,
    config_dir: Path | None = None, current_push_key: str | None = None,
    current_push_key_error: str | None = None, environ: Mapping[str, str] | None = None,
) -> list[dict]:
    """Each credential the backup holds, as a restore preview shows it: its
    destination id, name, kind, source and status (`new`, `changed`,
    `unchanged` or `host_only`). Never a value. Only `new` and `changed`
    entries are selectable.

    An inline credential in an array element that `current` does not hold at
    the same index is `host_only` for a credentials-only restore, which would
    write it into whichever element is there now, and `new` when the backup's
    document is restored with it, since the current value at that index is
    another element's.

    The push signing key is compared with `current_push_key`, the key at the
    default path now, and is `host_only` wherever `push_key_refusal` says the
    configuration the restore leaves puts it elsewhere, or where that key
    could not be read (`current_push_key_error`, a fixed message)."""
    from istota.config_editor.edits import UNREADABLE_CONFIG, ChangeError

    environ = os.environ if environ is None else environ
    try:
        current = tomllib.loads(current_text)
    except tomllib.TOMLDecodeError:
        raise ChangeError(UNREADABLE_CONFIG) from None
    backup_document = _backup_document(bundle)
    rows = []
    for credential in sorted(bundle.credentials, key=lambda item: item.destination):
        if isinstance(credential, PushSigningKey):
            rows.append(_push_key_row(
                credential, backup_document if restore_config else current, config_dir=config_dir,
                current_push_key=current_push_key, current_push_key_error=current_push_key_error,
                environ=environ,
            ))
            continue
        reason = host_only_reason(credential)
        moved = _element_moved(credential, backup_document, current)
        if reason is not None:
            status = "host_only"
        elif moved and not restore_config:
            status, reason = "host_only", MOVED_ELEMENT_REASON
        elif moved:
            status = "new"
        else:
            now = _current_value(credential, current, current_files)
            if now is None:
                status = "new"
            elif _same(now, credential.value):
                status = "unchanged"
            else:
                status = "changed"
        is_file = isinstance(credential, FileCredential)
        rows.append({
            "id": credential.destination,
            "kind": "file" if is_file else "inline",
            "name": _file_title(credential) if is_file else secrets.label(credential.path),
            "source": credential.source if is_file else "inline",
            "status": status,
            "reason": reason,
            "selectable": status in ("new", "changed"),
        })
    return rows


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def restore_input_digest(
    data: bytes, *, fmt: str, restore_config: bool, selected: Collection[str],
) -> str:
    """What a restore preview was made from: the uploaded bytes, the format,
    whether the configuration is restored and the selected destinations. Never
    the password or key file. A consistency check, not an authorization."""
    return hashlib.sha256(_canonical({
        "file": hashlib.sha256(data).hexdigest(),
        "format": fmt,
        "restore_config": bool(restore_config),
        "selected": sorted(set(selected)),
    })).hexdigest()


def preview_digest(public: Any) -> str:
    """The sha256 of a restore preview's public fields, canonically encoded,
    so a submit can tell the preview it rebuilt from the one reviewed."""
    return hashlib.sha256(_canonical(public)).hexdigest()


# --- the encrypted backup -----------------------------------------------------
#
# One reserved root group holding a manifest entry, a Configuration entry with
# the redacted `config.toml` attached, and one subgroup per credential kind.
# Routing is by the `istota.*` properties, never by titles, which are for a
# person reading the file in KeePassXC.

FORMAT_VERSION = "1"
#: A backup holding the push signing key, which a release before #947 cannot
#: read: it then says the format is too new rather than that the file is not
#: a backup. Without a key the file stays version 1.
PUSH_KEY_FORMAT_VERSION = "2"
_READABLE_VERSIONS = (FORMAT_VERSION, PUSH_KEY_FORMAT_VERSION)
MANIFEST_TITLE = "Backup manifest"
CONFIGURATION_TITLE = "Configuration"
CONFIG_ATTACHMENT = "config.toml"
DEPLOYMENT_GROUP = "Deployment credentials"
INLINE_GROUP = "Inline credentials"
PUSH_GROUP = "Push signing key"
RECOVERY_SCOPE = "configuration-and-installation-credentials"
#: What a rebuild needs beyond this file. Prerequisites, not something the
#: exporter checked.
RECOVERY_REQUIRES: tuple[str, ...] = (
    "framework and module database snapshots",
    "workspace files and the config/admins copies",
    "the original credential-store master key",
    "the deployment bootstrap inputs (stack.env, host.env)",
    "a way to regain admin access",
)

_P = "istota."
_ENTRY_ROLE = _P + "entry"
_KIND = _P + "kind"
_TARGET = _P + "target"
_SOURCE = _P + "source"
_PATH = _P + "path"
_VALUE = _P + "value"

#: The worker's bounds on a decrypted file, beside the bundle's own limits.
#: Strings: the credential aggregate twice (an inline value is in its Password
#: field and its typed property) plus a fixed allowance for titles, property
#: names and the manifest. Attachments: the configuration's cap. Depth: the
#: credential subgroups under the reserved group, nothing deeper.
WORKER_TEXT_ALLOWANCE_BYTES = 128 * 1024


def _worker_limits():
    from istota.credentials.kdbx_import import VAULT_MAX_ENTRIES
    from istota.credentials.kdbx_worker import WorkerLimits

    return WorkerLimits(
        entries=VAULT_MAX_ENTRIES,
        depth=1,
        text_bytes=2 * CREDENTIALS_MAX_BYTES + WORKER_TEXT_ALLOWANCE_BYTES,
        attachment_bytes=CONFIG_MAX_BYTES,
    )


@dataclasses.dataclass(frozen=True)
class Manifest:
    """The manifest's fields, none of them a credential."""

    version: str
    created: str
    app_version: str
    config_sha256: str
    recovery_scope: str
    recovery_requires: tuple[str, ...]


def decrypt_lock_path(db_path: Path) -> Path:
    """The installation-wide decrypt admission anchor, beside the database:
    every web worker shares that directory, and no sandbox binds it."""
    return Path(db_path).parent / ".config-backup-decrypt.lock"


def _readable(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, allow_nan=False)


def _file_title(credential: FileCredential) -> str:
    from istota.config_editor import schema

    paths = {name: path for path, name in schema.file_credentials().items()}
    path = paths.get(credential.target)
    return f"{secrets.label(path)} ({credential.target})" if path else credential.target


def build_installation_kdbx(
    bundle: Bundle, *, password: str, keyfile: bytes | None,
    options=None, created: str | None = None,
) -> bytes:
    """`bundle` as a fresh encrypted KDBX file, built in memory.

    The bundle is validated before the key derivation and the finished file
    is checked against `VAULT_READ_CAP_BYTES`, the import's own cap, so an
    export the restore would refuse is never returned.
    """
    from datetime import datetime, timezone

    from istota import __version__
    from istota.credentials import kdbx_export
    from istota.credentials.kdbx_import import (
        INSTALLATION_BACKUP_GROUP, INSTALLATION_FORMAT, INSTALLATION_FORMAT_PROPERTY,
        VAULT_READ_CAP_BYTES,
    )

    if keyfile is not None and kdbx_export.keyfile_refusal(keyfile):
        raise BackupError("keyfile_invalid", "The key file is not a KeePass XML key file.")
    validate_bundle(bundle)
    config_bytes = bundle.config_toml.encode("utf-8")
    kp = kdbx_export.new_database(
        password=password, keyfile=keyfile, options=options or kdbx_export.INTERACTIVE,
    )
    root = kp.add_group(kp.root_group, INSTALLATION_BACKUP_GROUP)

    manifest = kp.add_entry(root, MANIFEST_TITLE, "", "")
    properties = {
        INSTALLATION_FORMAT_PROPERTY: INSTALLATION_FORMAT,
        _P + "version": PUSH_KEY_FORMAT_VERSION if bundle.push_key is not None else FORMAT_VERSION,
        _ENTRY_ROLE: "manifest",
        _P + "created": created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        _P + "app_version": __version__,
        _P + "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        _P + "omissions": json.dumps([omission.as_json() for omission in bundle.omissions]),
        _P + "secrets_dir_available": "true" if bundle.secrets_dir_available else "false",
        _P + "recovery_scope": RECOVERY_SCOPE,
        _P + "recovery_requires": json.dumps(list(RECOVERY_REQUIRES)),
    }
    for key, value in properties.items():
        manifest.set_custom_property(key, value)

    configuration = kp.add_entry(root, CONFIGURATION_TITLE, "", "")
    configuration.set_custom_property(_ENTRY_ROLE, "configuration")
    configuration.add_attachment(kp.add_binary(config_bytes), CONFIG_ATTACHMENT)

    if bundle.file_credentials:
        group = kp.add_group(root, DEPLOYMENT_GROUP)
        for credential in bundle.file_credentials:
            entry = kp.add_entry(group, _file_title(credential), "", credential.value)
            entry.set_custom_property(_KIND, "file")
            entry.set_custom_property(_TARGET, credential.target)
            entry.set_custom_property(_SOURCE, credential.source)
    if bundle.inline_credentials:
        group = kp.add_group(root, INLINE_GROUP)
        for credential in bundle.inline_credentials:
            entry = kp.add_entry(group, secrets.label(credential.path), "", _readable(credential.value))
            entry.set_custom_property(_KIND, "inline")
            entry.set_custom_property(_PATH, json.dumps(list(credential.path), ensure_ascii=False))
            entry.set_custom_property(
                _VALUE, json.dumps(credential.value, ensure_ascii=False, allow_nan=False), protect=True,
            )
    if bundle.push_key is not None:
        # The PEM in the password field, so a person can copy it out of
        # KeePassXC for `istota-stack push-key import`.
        group = kp.add_group(root, PUSH_GROUP)
        entry = kp.add_entry(group, f"Private push signing key ({PUSH_KEY_LABEL})", "", bundle.push_key.value)
        entry.set_custom_property(_KIND, "push_key")
    data = kdbx_export.serialize(kp)
    if len(data) > VAULT_READ_CAP_BYTES:
        raise _too_large("encrypted file", VAULT_READ_CAP_BYTES)
    return data


# --- reading one back ---------------------------------------------------------


def _invalid(reason: str = "The file is not an Istota installation backup this version can restore.") -> BackupError:
    return BackupError("invalid_backup", reason)


def _strict_json(text: str) -> Any:
    def refuse(_constant):
        raise ValueError("not finite")

    return json.loads(text, parse_constant=refuse)


def _str_field(record: dict, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise _invalid()
    return value


def _entries_of(group: dict) -> list[dict]:
    entries = group.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise _invalid()
    for entry in entries:
        if not isinstance(entry.get("properties"), dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in entry["properties"].items()
        ):
            raise _invalid()
        if not isinstance(entry.get("attachments"), list):
            raise _invalid()
    return entries


def _groups_of(group: dict) -> list[dict]:
    groups = group.get("groups")
    if not isinstance(groups, list) or not all(isinstance(item, dict) for item in groups):
        raise _invalid()
    return groups


def _manifest(entry: dict) -> tuple[Manifest, list[Omission], bool]:
    from istota.credentials.kdbx_import import INSTALLATION_FORMAT, INSTALLATION_FORMAT_PROPERTY

    properties = entry["properties"]
    if properties.get(INSTALLATION_FORMAT_PROPERTY) != INSTALLATION_FORMAT:
        raise _invalid()
    version = properties.get(_P + "version")
    if version not in _READABLE_VERSIONS:
        raise _invalid("The backup's format version is not one this version of Istota can restore.")
    try:
        omissions_raw = _strict_json(properties.get(_P + "omissions", "[]"))
        requires_raw = _strict_json(properties.get(_P + "recovery_requires", "[]"))
    except (ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(omissions_raw, list) or not isinstance(requires_raw, list):
        raise _invalid()
    omissions = []
    for item in omissions_raw:
        if not (isinstance(item, dict) and set(item) == {"category", "name", "reason"}
                and all(isinstance(value, str) for value in item.values())):
            raise _invalid()
        omissions.append(Omission(item["category"], item["name"], item["reason"]))
    if not all(isinstance(item, str) for item in requires_raw):
        raise _invalid()
    available = properties.get(_P + "secrets_dir_available", "true")
    if available not in ("true", "false"):
        raise _invalid()
    digest = properties.get(_P + "config_sha256", "")
    if not (isinstance(digest, str) and len(digest) == 64):
        raise _invalid()
    manifest = Manifest(
        version=version,
        created=properties.get(_P + "created", ""),
        app_version=properties.get(_P + "app_version", ""),
        config_sha256=digest,
        recovery_scope=properties.get(_P + "recovery_scope", ""),
        recovery_requires=tuple(requires_raw),
    )
    return manifest, omissions, available == "true"


def _configuration(entry: dict, manifest: Manifest) -> str:
    import base64
    import binascii

    attachments = entry["attachments"]
    if len(attachments) != 1 or not isinstance(attachments[0], dict):
        raise _invalid()
    attachment = attachments[0]
    if attachment.get("filename") != CONFIG_ATTACHMENT or not isinstance(attachment.get("data"), str):
        raise _invalid()
    try:
        data = base64.b64decode(attachment["data"], validate=True)
    except (binascii.Error, ValueError):
        raise _invalid() from None
    if len(data) > CONFIG_MAX_BYTES:
        raise _too_large("configuration", CONFIG_MAX_BYTES)
    if hashlib.sha256(data).hexdigest() != manifest.config_sha256:
        raise _invalid("The backup's configuration does not match its manifest.")
    try:
        text = data.decode("utf-8")
        tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise _invalid("The backup's configuration does not parse.") from None
    return text


def _no_attachments(entry: dict) -> None:
    if entry["attachments"]:
        raise _invalid("The backup holds an attachment it does not restore.")


def _file_credential(entry: dict) -> FileCredential:
    properties = entry["properties"]
    if properties.get(_KIND) != "file":
        raise _invalid()
    value = _str_field(entry, "password")
    if not value:
        raise _invalid("The backup holds an empty credential.")
    return FileCredential(
        target=properties.get(_TARGET, ""), source=properties.get(_SOURCE, ""), value=value,
    )


def _inline_credential(entry: dict) -> InlineCredential:
    properties = entry["properties"]
    if properties.get(_KIND) != "inline" or _PATH not in properties or _VALUE not in properties:
        raise _invalid()
    try:
        path = _strict_json(properties[_PATH])
        value = _strict_json(properties[_VALUE])
    except (ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(path, list) or not all(isinstance(segment, str) for segment in path):
        raise _invalid()
    if not _typed_scalar(value) or value == "":
        raise _invalid("The backup holds an empty or untyped credential.")
    return InlineCredential(path=tuple(path), value=value)


def _push_key_entry(entry: dict) -> PushSigningKey:
    if entry["properties"].get(_KIND) != "push_key":
        raise _invalid()
    value = _str_field(entry, "password")
    try:
        return PushSigningKey(_push_key_text(value.encode("utf-8")))
    except BackupError:
        raise _invalid("The backup's push signing key is not an unencrypted Ed25519 key.") from None


def parse_installation_backup(opened: dict) -> Bundle:
    """The strict reading of the decrypt worker's answer, as a bundle.

    Refuses as `invalid_backup` anything but exactly one reserved group
    holding one manifest of a known version, one Configuration entry whose
    one `config.toml` attachment matches the manifest's digest, and credential
    entries in their kind's subgroup; any other entry, subgroup or attachment
    in the reserved group refuses the whole file. Entry history is ignored.
    Limits, forbidden and repeated destinations are `validate_bundle`'s, the
    same check a gather passes.
    """
    if not isinstance(opened, dict):
        raise _invalid()
    matches, group = opened.get("matches"), opened.get("group")
    if matches == 0 or group is None:
        raise _invalid()
    if matches != 1 or not isinstance(group, dict):
        raise _invalid("The file holds more than one installation backup.")

    manifests, configurations = [], []
    for entry in _entries_of(group):
        role = entry["properties"].get(_ENTRY_ROLE)
        if role == "manifest":
            manifests.append(entry)
        elif role == "configuration":
            configurations.append(entry)
        else:
            raise _invalid("The backup holds an entry it does not restore.")
    if len(manifests) != 1 or len(configurations) != 1:
        raise _invalid()
    _no_attachments(manifests[0])
    manifest, omissions, dir_available = _manifest(manifests[0])
    config_toml = _configuration(configurations[0], manifest)

    file_credentials: list[FileCredential] = []
    inline_credentials: list[InlineCredential] = []
    push_keys: list[PushSigningKey] = []
    seen_groups: set[str] = set()
    for subgroup in _groups_of(group):
        name = subgroup.get("name")
        if name not in (DEPLOYMENT_GROUP, INLINE_GROUP, PUSH_GROUP) or name in seen_groups:
            raise _invalid("The backup holds a group it does not restore.")
        seen_groups.add(name)
        if _groups_of(subgroup):
            raise _invalid("The backup holds a group it does not restore.")
        for entry in _entries_of(subgroup):
            _no_attachments(entry)
            if name == DEPLOYMENT_GROUP:
                file_credentials.append(_file_credential(entry))
            elif name == INLINE_GROUP:
                inline_credentials.append(_inline_credential(entry))
            else:
                push_keys.append(_push_key_entry(entry))
    if len(push_keys) > 1:
        raise _invalid("The backup holds more than one push signing key.")

    bundle = Bundle(
        config_toml=config_toml,
        file_credentials=file_credentials,
        inline_credentials=inline_credentials,
        omissions=omissions,
        secrets_dir_available=dir_available,
        manifest=manifest,
        push_key=push_keys[0] if push_keys else None,
    )
    validate_bundle(bundle)
    return bundle


async def open_installation_backup(
    data: bytes, *, password: str | None, keyfile: bytes | None, db_path: Path,
    timeout: float | None = None, command: list[str] | None = None,
) -> Bundle:
    """An uploaded encrypted backup, decrypted in the worker behind the
    installation's one decrypt slot, then parsed. Every failure is a
    `BackupError` with a fixed code and message; nothing uploaded is echoed."""
    from istota.credentials import kdbx_import, kdbx_worker
    from istota.credentials.kdbx_import import VAULT_READ_CAP_BYTES

    if not isinstance(data, bytes) or not data:
        raise _invalid()
    if len(data) > VAULT_READ_CAP_BYTES:
        raise _too_large("encrypted file", VAULT_READ_CAP_BYTES)
    if keyfile is not None and (not isinstance(keyfile, bytes) or not keyfile or len(keyfile) > 4096):
        raise BackupError("keyfile_invalid", "The key file is not one Istota can use.")
    try:
        opened = await kdbx_worker.open_group_async(
            data, password, keyfile,
            group=kdbx_import.INSTALLATION_BACKUP_GROUP, limits=_worker_limits(),
            lock_path=decrypt_lock_path(db_path),
            timeout=timeout or kdbx_worker.TIMEOUT_SECONDS, command=command,
        )
    except kdbx_worker.WorkerBusy:
        raise BackupError("backup_busy", "Another backup is being opened; try again shortly.") from None
    except kdbx_worker.WorkerUnavailable:
        raise BackupError("backup_unavailable", "Backups cannot be opened on this installation now.") from None
    except kdbx_worker.WorkerTimeout:
        raise BackupError("backup_timeout", "Opening the backup took too long.") from None
    except kdbx_worker.WorkerTooLarge as exc:
        if exc.limit == "depth":
            raise _invalid("The backup holds a group it does not restore.") from None
        raise BackupError("too_large", "The backup holds more than this version restores.") from None
    except kdbx_worker.WorkerFailed:
        raise _invalid() from None
    except kdbx_import.VaultLocked:
        raise BackupError("backup_locked", "Wrong password or key file.") from None
    except kdbx_import.VaultLibraryMissing:
        raise BackupError("vault_unavailable", "Install the vault extra to open encrypted backups.") from None
    except kdbx_import.VaultCorrupt:
        raise _invalid() from None
    return parse_installation_backup(opened)
