"""A ``ToolSchema`` rendered as JSON Schema.

One renderer for every consumer that shows a model a tool's arguments: the
OpenAI-compatible provider's ``parameters`` and the MCP codec's
``inputSchema``. A leaf that imports only ``llm.types``, so the tool server can
render its own tool list without pulling in the provider's HTTP client.
``tests/test_tool_schema_parity.py`` holds the two renderings equal.
"""

from __future__ import annotations

from .types import ToolParameter, ToolSchema


def tool_parameters(tool: ToolSchema) -> dict:
    """The tool's arguments as one JSON-Schema object."""
    properties: dict = {}
    required: list[str] = []
    for p in tool.parameters:
        properties[p.name] = param_to_schema(p)
        if p.required:
            required.append(p.name)
    return {"type": "object", "properties": properties, "required": required}


def param_to_schema(p: ToolParameter) -> dict:
    """Render a ``ToolParameter`` as a JSON-Schema property.

    Recurses into ``properties`` (objects) and ``items`` (arrays). Strict
    providers — Google Gemini via OpenRouter in particular — reject an
    array that omits ``items`` (``...items: missing field``), and an
    object whose nested ``properties`` are absent, so both are emitted
    whenever they are declared.
    """
    schema: dict = {"type": p.type}
    if p.description:
        schema["description"] = p.description
    if p.enum:
        schema["enum"] = p.enum
    if p.properties:
        schema["properties"] = {
            sub.name: param_to_schema(sub) for sub in p.properties.values()
        }
    if p.items is not None:
        schema["items"] = param_to_schema(p.items)
    return schema
