"""Argument coercion at the dispatch layer.

LLMs routinely return ``"42"`` when a schema wants an integer, ``"true"`` for a
boolean, or a JSON string where an object is expected. Coercing once at the
dispatch layer (rather than in every tool) keeps the tools clean.

Prior art: Hermes-agent's handle_function_call() coercion (model_tools.py).
"""

from __future__ import annotations

import json

from istota.llm.types import ToolSchema

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
