"""The commented `istota_config` reference in the management role's defaults.

Keys and defaults come from the `Config` dataclass tree, so the reference names
every setting the loader reads and the value it takes when the key is left out.
The prose comes from `config/config.example.toml`, matched by section and key.
The example stays the authoritative prose; this is a rendering of it, held to
it by tests/test_config_reference.py.

The example is read line by line rather than parsed, because most of it is
commented out. The header and assignment patterns are the field coverage
guard's, which already handle the prose that looks like an assignment.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import re
import tomllib
from pathlib import Path

from istota import config as config_module
from istota import setup_wizard
from istota.config import _NOT_CONFIGURATION, Config
from istota.config_mapper import _nested_dataclass
from tests.support.drift import source_of
from tests.test_config_field_coverage import (
    _AUDIT_ASSIGN,
    _DYNAMIC_HEADER,
    _HEADER,
    _UNNAMEABLE,
    _collection_of_dataclasses,
)

BEGIN = "# --- istota_config reference: generated, do not edit by hand ---"
END = "# --- end istota_config reference ---"

PREAMBLE = [
    "Every key istota_config takes, with its default and what it does. Generated",
    "from the config dataclasses and config/config.example.toml; regenerate with",
    "uv run env ISTOTA_UPDATE_CONFIG_REFERENCE=1 pytest tests/test_config_reference.py -n0",
    "",
    "Copy what you need into host_vars, uncommented, at the same nesting. A key",
    "left out takes the default of the release the VM runs, so set only what",
    "differs. A TOML table is a mapping here and [[x]] a list of mappings.",
]

#: One entry of `load_config`'s `_env_secret_overrides`: the credentials that
#: reach the config from the environment, which is how a stack secrets file
#: reaches it too.
_OVERRIDE = re.compile(r'\(\s*"(ISTOTA_[A-Z0-9_]+)",\s*"([a-z_.]+)",\s*"([a-z_]+)",?\s*\)')


def credentials() -> dict[str, str]:
    """Each credential key's dotted path, to its stack secrets file or ""."""
    found: dict[str, str] = {}
    table = source_of(config_module.load_config).split("_env_secret_overrides = [", 1)[1].split("\n    ]", 1)[0]
    for env, section, key in _OVERRIDE.findall(table):
        name = env.lower()
        found[f"{section}.{key}"] = name if name in setup_wizard.SECRET_NAMES else ""
    for name in setup_wizard.SECRET_NAMES:
        # brain.native.api_key is read two levels deep, outside the table.
        if name == "istota_brain_native_api_key":
            found["brain.native.api_key"] = name
    return found


def setup_paths() -> dict[str, str]:
    """The container layout `istota setup` writes: the top-level absolute paths."""
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


def _value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, enum.Enum):
        return _value(value.value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (str, Path)):
        return json.dumps(str(value), ensure_ascii=False)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return "[" + ", ".join(_value(item) for item in items) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"no rendering for {type(value).__name__}")


def _normal(value):
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, set, frozenset)):
        return [_normal(v) for v in (sorted(value) if isinstance(value, (set, frozenset)) else value)]
    if isinstance(value, list):
        return [_normal(v) for v in value]
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in value.items()}
    return value


def _comments(lines: list[str], indent: str) -> list[str]:
    return [f"{indent}# {line}".rstrip() for line in lines]


def _walk(instance, path: str, depth: int, docs: Docs, secrets: dict[str, str], paths: dict[str, str]) -> list[str]:
    out: list[str] = []
    indent = "  " * depth
    for field in dataclasses.fields(instance):
        name = field.name
        if name.startswith("_") or (not path and name in _NOT_CONFIGURATION):
            continue
        full = f"{path}.{name}" if path else name
        value = getattr(instance, name)
        nested = _nested_dataclass(instance, field)
        if nested is not None:
            out.append("")
            out += _comments(docs.sections.get(full, []), indent)
            out.append(f"{indent}{name}:")
            out += _walk(nested, full, depth + 1, docs, secrets, paths)
            continue
        if _collection_of_dataclasses(type(instance), field) or isinstance(value, dict):
            out.append("")
            prose = docs.sections.get(full) or docs.keys.get((path, name), [])
            out += _comments(prose, indent)
            if _collection_of_dataclasses(type(instance), field):
                shape = "[[" + full + "]]" if isinstance(value, list) else "[" + full + ".<name>]"
                out += _comments([f"Written {shape} in config.example.toml, which shows the fields."], indent)
            else:
                # The example's entries for a table of free keys ([models.aliases]).
                entries = [(key, text) for (section, key), text in docs.examples.items() if section == full]
                if entries:
                    out += _comments(["Example entries:"] + [f"  {key}: {text}" for key, text in entries], indent)
            out.append(f"{indent}{name}: {_value(_normal(value)) if value else ('[]' if isinstance(value, list) else '{}')}")
            continue
        prose = list(docs.keys.get((path, name), []))
        example = docs.examples.get((path, name))
        if example is not None:
            try:
                shown = tomllib.loads(f"v = {example}")["v"]
            except tomllib.TOMLDecodeError:
                shown = None
            if shown is not None and shown != _normal(value):
                prose.append(f"example: {example}")
        if value is None:
            prose.append("unset by default; give it a value or leave it out")
        if full in secrets:
            prose.append(
                f"A credential: set it as istota_secrets.{secrets[full]}, not here." if secrets[full]
                else "A credential with no stack secrets file: set it here, and keep the value in the vault."
            )
        if full in paths:
            prose.append(f"The container layout: `istota setup` writes {_value(paths[full])} here (code default {_value(_normal(value))}).")
            value = paths[full]
        # A gap before each explained key, except the first in its section.
        if prose and out and out[-1] != "":
            out.append("")
        out += _comments(prose, indent)
        rendered = _value(_normal(value))
        out.append(f"{indent}{name}:" + (f" {rendered}" if rendered else ""))
    return out


def render(example_text: str) -> str:
    """The block, markers included, every line a YAML comment."""
    body = ["istota_config:"] + _walk(Config(), "", 1, read_example(example_text), credentials(), setup_paths())
    lines = [BEGIN] + _comments(PREAMBLE, "") + ["#"] + [f"# {line}".rstrip() if line else "#" for line in body] + [END]
    return "\n".join(lines) + "\n"


def splice(defaults_text: str, block: str) -> str:
    """`defaults_text` with the block between the markers replaced."""
    start = defaults_text.index(BEGIN)
    end = defaults_text.index(END, start) + len(END) + 1
    return defaults_text[:start] + block + defaults_text[end:]
