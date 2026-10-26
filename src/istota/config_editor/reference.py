"""What the config editor knows about a key besides its value.

- `read_example` reads `config/config.example.toml`'s comment blocks onto the
  section and key they document. The example is read line by line rather than
  parsed, because most of it is commented out. `field_docs` is the same prose
  keyed by path, for the editor's per-field docs.
- `INSTALLATION_CREDENTIALS` maps each installation credential's config key to
  its stack secrets file, and `NOT_CREDENTIALS` names the secret-looking keys
  that are not credentials. `credential_files` is the first, plus "" for a
  credential that has no file and lives in `config.toml`.
- `setup_paths` is the container layout `istota setup` writes.

The header and assignment patterns are the field coverage guard's
(`tests/test_config_field_coverage.py`), which imports them from here.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
import typing
from pathlib import Path

from istota import config as config_module

#: A section header, live or commented, alone on its line but for a trailing
#: comment. The example names `[models.aliases]` mid-prose several times, and a
#: pattern with no end anchor reads each of those as a header, which would
#: misfile every assignment after it.
_HEADER = re.compile(r"^[ \t]*(?:#[ \t]*)?\[{1,2}([a-z_][a-z0-9_.]*)\]{1,2}[ \t]*(?:#.*)?$")

#: A header whose name is built by Jinja (`[models.aliases."{{ name }}"]` in a
#: template). It is a real section boundary that `_HEADER` cannot name, so
#: without this the walk keeps the previous header current and files every
#: assignment after it into the wrong section.
_DYNAMIC_HEADER = re.compile(r"^[ \t]*(?:#[ \t]*)?\[{1,2}[^\]\n]*\{[{%]")

#: A section no field path can equal, so a dynamic header ends the block above
#: it rather than extending it.
_UNNAMEABLE = "\x00dynamic"

#: An assignment, live or commented out, with the value terminated: end of
#: line, or a trailing comment. The terminator is what keeps prose such as
#: `# db_path = "/data/db/istota.db", so the directory` from reading as a key.
_AUDIT_ASSIGN = re.compile(
    r"""^[ \t]*(?:\#[ \t]*)?([a-z_][a-z0-9_]*)[ \t]*=[ \t]*
        (?:"[^"]*"|'[^']*'|\[[^\]]*\]|\[|\{[^}]*\}|-?[\d.]+|true|false|\{\{.*?\}\})
        [ \t]*(?:\#.*)?$""",
    re.VERBOSE,
)


def _collection_of_dataclasses(owner: type, field: dataclasses.Field) -> bool:
    """Whether the field is declared as a dict or list of dataclasses.

    Read off the declared type rather than the value, because the default is
    an empty container that says nothing about what goes in it.
    """
    declared = typing.get_type_hints(owner).get(field.name)
    if typing.get_origin(declared) not in (dict, list):
        return False
    return any(dataclasses.is_dataclass(arg) for arg in typing.get_args(declared))


#: Credentials whose variable is outside `config.SECRET_ENV_OVERRIDES`,
#: because they sit deeper than its section/key rows: each built-in provider's
#: key and headers under the `providers` dict field, the native brain's two
#: legacy ones (read for one release through `config.LEGACY_PROVIDER_SOURCES`),
#: and each arr app's key under the `servarr.apps` dict field. An operator's
#: own provider has no secret file, so its key stays in `config.toml`.
_DEEP_CREDENTIAL_VARIABLES: tuple[tuple[str, str], ...] = tuple(
    (f"providers.{provider_id}.{field_name}", variable(provider_id))
    for provider_id in config_module.BUILTIN_PROVIDERS
    for field_name, variable in (
        ("api_key", config_module.provider_key_variable),
        ("extra_headers", config_module.provider_headers_variable),
    )
) + (
    ("brain.native.api_key", "ISTOTA_BRAIN_NATIVE_API_KEY"),
    ("brain.native.extra_headers", "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS"),
) + tuple(
    (f"servarr.apps.{app}.api_key", config_module.servarr_key_variable(app))
    for app in config_module.SERVARR_APPS
)


def _credential_variables() -> list[tuple[str, str]]:
    """Every config path a credential variable can set, with that variable."""
    found = [
        (f"{section}.{key}", env)
        for env, section, key in config_module.SECRET_ENV_OVERRIDES
    ]
    found.extend(_DEEP_CREDENTIAL_VARIABLES)
    return found


def _installation_credentials() -> dict[str, str]:
    from istota import setup_wizard

    return {
        dotted: env.lower()
        for dotted, env in _credential_variables()
        if env.lower() in setup_wizard.SECRET_NAMES
    }


#: Every installation credential `Config` reads, by dotted path, to its stack
#: secrets file (`setup_wizard.SECRET_NAMES`). The file name is the variable
#: `load_config` reads it from, lowercased. A path whose variable has no file
#: is not here. `tests/test_installation_credentials.py` requires every path
#: `is_secret_path` flags to be here or in `NOT_CREDENTIALS`.
INSTALLATION_CREDENTIALS: dict[str, str] = _installation_credentials()

#: Paths `is_secret_path` flags that hold no credential, each with the reason.
#: Redaction is broad on purpose; this list is the exact answer the guard needs,
#: and an entry here never un-redacts anything.
NOT_CREDENTIALS: dict[str, str] = {
    "email.authserv_id": "the id our MTA stamps its Authentication-Results headers with",
    "scheduler.credential_backup_interval": "seconds between encrypted credential backups",
    "developer.author_credit": "a line appended to commit messages",
    "security.credential_history_days": "retention for credential history, in days",
    "security.credential_audit_days": "retention for the credential audit log, in days",
    "security.credential_exports_per_day": "a rate limit on credential exports",
    "security.credential_backup_retention": "how many credential backups are kept",
    "security.wallet_authorization_minutes": "how long a wallet authorization lasts, in minutes",
    "web.auth_step_up_ttl_minutes": "how long a step-up lasts, in minutes",
    "web.oauth2_provider": "the OAuth2 provider's URL",
    "web.oauth2_client_id": "the OAuth2 client's public identifier",
    "web.oauth2_userinfo_endpoint": "a URL",
    "web.oauth2_redirect_uri": "a URL",
    "web.map.api_key": "public by construction: MapLibre sends it to every browser in the tile URL",
}


def credential_files() -> dict[str, str]:
    """Each credential key's dotted path, to its stack secrets file or "".

    `INSTALLATION_CREDENTIALS`, plus every other path a credential variable can
    set, mapped to "": a credential with no file lives in `config.toml`.
    """
    found = {dotted: "" for dotted, _env in _credential_variables()}
    found.update(INSTALLATION_CREDENTIALS)
    return found


def setup_paths() -> dict[str, str]:
    """The container layout `istota setup` writes: the top-level absolute paths."""
    from istota import setup_wizard

    document = setup_wizard.container_config_document(setup_wizard.ContainerAnswers(), inline_credentials=False)
    return {k: v for k, v in document.items() if isinstance(v, str) and v.startswith("/")}


_BANNER = re.compile(r"^[=\-#\s]*$")
_COMMENT = re.compile(r"^[ \t]*#[ \t]?(.*)$")
_CONTINUATION = re.compile(r"^[ \t]*#?[ \t]+#[ \t]?(.*)$")


@dataclasses.dataclass
class Docs:
    keys: dict[tuple[str, str], list[str]] = dataclasses.field(default_factory=dict)
    examples: dict[tuple[str, str], str] = dataclasses.field(default_factory=dict)
    sections: dict[str, list[str]] = dataclasses.field(default_factory=dict)


def _trim(lines: list[str]) -> list[str]:
    while lines and lines[0] == "":
        lines = lines[1:]
    while lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


def _split_value(rest: str) -> tuple[str, str]:
    """`value  # comment` into the value's text and the comment."""
    for match in re.finditer("#", rest):
        left = rest[: match.start()].strip()
        try:
            tomllib.loads(f"v = {left}")
        except tomllib.TOMLDecodeError:
            continue
        return left, rest[match.end():].strip()
    return rest.strip(), ""


def read_example(text: str) -> Docs:
    docs = Docs()
    section = ""
    pending: list[str] = []
    last_key: tuple[str, str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            pending, last_key = [], None
            continue
        header = _HEADER.match(line)
        if header:
            docs.sections.setdefault(header.group(1), _trim(pending))
            section, pending, last_key = header.group(1), [], None
            continue
        if _DYNAMIC_HEADER.match(line):
            section, pending, last_key = _UNNAMEABLE, [], None
            continue
        assign = _AUDIT_ASSIGN.match(line)
        if assign:
            key = (section, assign.group(1))
            body = re.sub(r"^[ \t]*#?[ \t]*", "", line)
            value, trailing = _split_value(body.split("=", 1)[1])
            if key not in docs.keys:
                docs.keys[key] = _trim(pending) + ([trailing] if trailing else [])
                docs.examples[key] = value
            pending, last_key = [], key
            continue
        continuation = _CONTINUATION.match(line)
        if continuation and last_key is not None:
            docs.keys[last_key].append(continuation.group(1).strip())
            continue
        comment = _COMMENT.match(line)
        if comment:
            last_key = None
            text_ = comment.group(1).rstrip()
            if text_ and _BANNER.match(text_):
                continue
            if text_ or pending:
                pending.append(text_)
        # Anything else is the body of a multi-line value; it keeps last_key.
    return docs


def _prose(lines: list[str]) -> str:
    """Comment lines as paragraphs: wrapped lines joined, paragraphs blank-line separated."""
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    return "\n\n".join(" ".join(paragraph) for paragraph in paragraphs if paragraph)


def _path_of(section: str) -> tuple[str, ...]:
    return tuple(section.split(".")) if section else ()


def docs_from_text(text: str) -> dict[tuple[str, ...], str]:
    """The example's prose keyed by path: each key's, then each section's."""
    docs = read_example(text)
    found: dict[tuple[str, ...], str] = {}
    for (section, name), lines in docs.keys.items():
        if section == _UNNAMEABLE:
            continue
        prose = _prose(lines)
        if prose:
            found[_path_of(section) + (name,)] = prose
    for section, lines in docs.sections.items():
        prose = _prose(lines)
        if prose and _path_of(section) not in found:
            found[_path_of(section)] = prose
    return found


EXAMPLE_NAME = "config.example.toml"

_docs_cache: dict[Path, tuple[int, dict[tuple[str, ...], str]]] = {}


def field_docs(config_dir: Path) -> dict[tuple[str, ...], str]:
    """`docs_from_text` over `config_dir`'s example, memoised on path and mtime.

    `config_dir` is the directory `executor` reads `emissaries.md` from
    (`config.skills_dir.parent`, `/app/config` in the image). No example there
    is no docs, not an error.
    """
    path = Path(config_dir) / EXAMPLE_NAME
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _docs_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        docs = docs_from_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}
    _docs_cache[path] = (mtime, docs)
    return docs
