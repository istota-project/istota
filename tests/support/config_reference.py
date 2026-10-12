"""The commented `istota_config` reference in the management role's defaults.

Keys and defaults come from the `Config` dataclass tree, so the reference names
every setting the loader reads and the value it takes when the key is left out.
The prose comes from `config/config.example.toml`, matched by section and key
by `istota.config_editor.reference.read_example`, which the config editor reads
its per-field docs through too. The example stays the authoritative prose; this
is a rendering of it, held to it by tests/test_config_reference.py.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import tomllib
from pathlib import Path

from istota.config import _NOT_CONFIGURATION, Config
from istota.config_editor.reference import (
    Docs,
    _collection_of_dataclasses,
    credential_files,
    read_example,
    setup_paths,
)
from istota.config_mapper import _nested_dataclass

__all__ = ["Docs", "credential_files", "read_example", "render", "setup_paths", "splice"]

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
    body = ["istota_config:"] + _walk(Config(), "", 1, read_example(example_text), credential_files(), setup_paths())
    lines = [BEGIN] + _comments(PREAMBLE, "") + ["#"] + [f"# {line}".rstrip() if line else "#" for line in body] + [END]
    return "\n".join(lines) + "\n"


def splice(defaults_text: str, block: str) -> str:
    """`defaults_text` with the block between the markers replaced."""
    start = defaults_text.index(BEGIN)
    end = defaults_text.index(END, start) + len(END) + 1
    return defaults_text[:start] + block + defaults_text[end:]
