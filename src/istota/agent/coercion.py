"""Argument coercion at the dispatch layer.

LLMs routinely return ``"42"`` when a schema wants an integer, ``"true"`` for a
boolean, or a JSON string where an object is expected. Coercing once at the
dispatch layer (rather than in every tool) keeps the tools clean.

Prior art: Hermes-agent's handle_function_call() coercion (model_tools.py).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from istota.llm.types import ToolParameter, ToolSchema

if TYPE_CHECKING:
    from .tools import AgentTool

logger = logging.getLogger("istota.agent.coercion")

_TRUE_STRINGS = ("true", "1", "yes")
_FALSE_STRINGS = ("false", "0", "no")


def coerce_arguments(args: dict, schema: ToolSchema) -> dict:
    """Coerce tool arguments to match the JSON-schema parameter types.

    Only string values are coerced, and only when the target type differs.
    Unknown keys (not in the schema) and values that fail to coerce pass
    through unchanged — the tool or the model's next turn deals with them.
    """
    result: dict = {}
    param_map = {p.name: p for p in schema.parameters}

    for key, value in args.items():
        param = param_map.get(key)
        if param is None:
            result[key] = value
            continue

        if isinstance(value, str):
            if param.type == "integer":
                try:
                    result[key] = int(value)
                    continue
                except ValueError:
                    pass
            elif param.type == "number":
                try:
                    result[key] = float(value)
                    continue
                except ValueError:
                    pass
            elif param.type == "boolean":
                lowered = value.lower()
                if lowered in _TRUE_STRINGS:
                    result[key] = True
                    continue
                if lowered in _FALSE_STRINGS:
                    result[key] = False
                    continue
            elif param.type in ("object", "array"):
                try:
                    result[key] = json.loads(value)
                    continue
                except json.JSONDecodeError:
                    pass

        result[key] = value

    return result


def drop_null_optionals(args: dict, schema: ToolSchema) -> dict:
    """Drop the ``null`` a strict model sends for an optional key it left unset.

    Strict tool schemas make every optional property nullable
    (``llm.openai_compat.strictify_schema``), so a tool would otherwise see a
    ``None`` it never asked for. Only keys optional in the original schema go:
    a top-level parameter with ``required=False``, and any nested property,
    since nested objects go on the wire without a ``required`` list. A
    ``None`` for a required key is left for validation to see.
    """
    if not isinstance(args, dict):
        return args
    params = {p.name: p for p in schema.parameters}
    result: dict = {}
    for key, value in args.items():
        param = params.get(key)
        if param is None:
            result[key] = value
            continue
        if value is None and not param.required:
            continue
        result[key] = _drop_nested_nulls(value, param)
    return result


def _drop_nested_nulls(value, param: ToolParameter):
    if param.type == "object" and param.properties and isinstance(value, dict):
        nested: dict = {}
        for key, sub_value in value.items():
            sub_param = param.properties.get(key)
            if sub_param is None:
                nested[key] = sub_value
            elif sub_value is not None:
                nested[key] = _drop_nested_nulls(sub_value, sub_param)
        return nested
    if param.type == "array" and param.items is not None and isinstance(value, list):
        return [_drop_nested_nulls(item, param.items) for item in value]
    return value


def prepare_tool_arguments(tool: AgentTool, raw_args: dict) -> tuple[dict | None, str | None]:
    """The arguments ``tool`` runs with, or the error text the model sees instead.

    The steps every dispatcher applies before ``execute``, in order: drop the
    nulls a strict model sends for unset optionals, then the tool's own
    ``prepare_arguments`` hook or the generic coercion, then the required-key
    check. The agent loop and ``session/tools/dispatch.py`` both call this, so
    the native loop and the CLI harness hand a tool the same arguments. A hook
    that raises is contained here: it becomes an error, never an exception.
    """
    try:
        args = drop_null_optionals(raw_args, tool.schema)
        if tool.prepare_arguments is not None:
            args = tool.prepare_arguments(args)
        else:
            args = coerce_arguments(args, tool.schema)
    except Exception as exc:  # noqa: BLE001 — contain into an error result
        logger.warning("tool_prepare_error tool=%s: %s", tool.schema.name, exc)
        return None, f"Argument preparation failed: {exc}"

    missing = _missing_required(args, tool.schema)
    if missing:
        return None, f"Missing required argument(s): {', '.join(missing)}"
    return args, None


def _missing_required(args: dict, schema: ToolSchema) -> list[str]:
    """Required schema parameters absent from (or null in) ``args``."""
    missing = []
    for param in schema.parameters:
        if param.required and (param.name not in args or args[param.name] is None):
            missing.append(param.name)
    return missing
