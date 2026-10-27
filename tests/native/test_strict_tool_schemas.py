"""Strict tool schemas (pi 1.x round, stage 6).

`strictify_schema` over every native tool's real schema, the `strict` flag on
the tool wire under `off`/`auto`/`on`, the refusal predicate, and the coercion
that drops the `null` a strict model sends for an optional key.
"""

import copy
import logging

import pytest

from istota.agent.coercion import drop_null_optionals
from istota.llm import catalog, make_provider
from istota.llm.openai_compat import (
    OpenAICompatibleProvider,
    carries_strict_tools,
    is_strict_schema_rejection,
    strict_tools_enabled,
    strictify_schema,
)
from istota.llm.types import STRICT_TOOL_SCHEMA_MODES, ToolParameter, ToolSchema
from istota.session.tools import ToolEnv, WebFetchPolicy, build_default_tools
from istota.session.tools.files import EDIT_SCHEMA, prepare_edit_arguments


def _all_native_tools(tmp_path):
    env = ToolEnv(cwd=tmp_path, web_fetch=WebFetchPolicy(enabled=True))
    return build_default_tools(env)


def _wire_parameters(schema: ToolSchema) -> dict:
    return OpenAICompatibleProvider._tool_to_wire(schema)["function"]["parameters"]


def _walk_objects(node):
    if not isinstance(node, dict):
        return
    t = node.get("type")
    types = t if isinstance(t, list) else [t]
    if "object" in types:
        yield node
        for sub in node["properties"].values():
            yield from _walk_objects(sub)
    if "array" in types:
        yield from _walk_objects(node["items"])


class TestStrictifyEveryNativeTool:
    def test_the_seven_tools_are_all_here(self, tmp_path):
        names = {t.schema.name for t in _all_native_tools(tmp_path)}
        assert names == {"Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebFetch"}

    def test_every_tool_strictifies_or_degrades_to_none(self, tmp_path):
        # A future tool with a construct strictify does not handle must come
        # back None (sent non-strict), never raise and never half-convert.
        for tool in _all_native_tools(tmp_path):
            params = _wire_parameters(tool.schema)
            before = copy.deepcopy(params)
            strict = strictify_schema(params)
            assert params == before, f"{tool.schema.name}: input mutated"
            if strict is None:
                continue
            for obj in _walk_objects(strict):
                assert obj["additionalProperties"] is False, tool.schema.name
                assert sorted(obj["required"]) == sorted(obj["properties"]), tool.schema.name

    def test_every_shipped_tool_is_handled(self, tmp_path):
        # Today's seven use only constructs strictify knows.
        unhandled = [
            t.schema.name
            for t in _all_native_tools(tmp_path)
            if strictify_schema(_wire_parameters(t.schema)) is None
        ]
        assert unhandled == []

    def test_optional_properties_become_nullable_and_required_ones_do_not(self, tmp_path):
        for tool in _all_native_tools(tmp_path):
            params = _wire_parameters(tool.schema)
            strict = strictify_schema(params)
            was_required = set(params.get("required", []))
            for name, prop in strict["properties"].items():
                original_type = params["properties"][name]["type"]
                if name in was_required:
                    assert prop["type"] == original_type, (tool.schema.name, name)
                else:
                    assert prop["type"] == [original_type, "null"], (tool.schema.name, name)

    def test_edit_nested_item_fields_are_nullable(self):
        strict = strictify_schema(_wire_parameters(EDIT_SCHEMA))
        item = strict["properties"]["edits"]["items"]
        assert item["type"] == "object"
        assert item["additionalProperties"] is False
        assert item["required"] == ["old_string", "new_string"]
        assert item["properties"]["old_string"]["type"] == ["string", "null"]

    def test_a_nullable_enum_admits_null(self, tmp_path):
        grep = next(t for t in _all_native_tools(tmp_path) if t.schema.name == "Grep")
        mode = strictify_schema(_wire_parameters(grep.schema))["properties"]["output_mode"]
        assert mode["type"] == ["string", "null"]
        assert mode["enum"][-1] is None
        assert "content" in mode["enum"]


class TestStrictifyUnhandled:
    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"x": {"type": "string", "default": "a"}}},
            {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}]}}},
            {"type": "object", "properties": {"x": {"type": "array"}}},
            {"type": "object", "properties": {"x": {"type": "object"}}},
            {"type": "object", "properties": {"x": {"type": "null"}}},
            {"type": "object", "properties": {}, "required": ["ghost"]},
            {"type": "string"},
            "not a schema",
        ],
    )
    def test_returns_none(self, schema):
        assert strictify_schema(schema) is None


def _info(**kw):
    return catalog.ModelInfo(id="m", context_window=100_000, max_output_tokens=1000, **kw)


class TestModes:
    def test_the_vocabulary(self):
        assert STRICT_TOOL_SCHEMA_MODES == ("off", "auto", "on")

    def test_off_and_on(self):
        assert strict_tools_enabled("off", "m") is False
        assert strict_tools_enabled("on", "m") is True
        assert strict_tools_enabled("nonsense", "m") is False

    def test_auto_follows_the_model_capability(self, monkeypatch):
        catalog.set_model_overrides({"yes/m": {"supports_structured_outputs": True}})
        assert strict_tools_enabled("auto", "yes/m") is True
        assert strict_tools_enabled("auto", "no/m") is False

    def test_model_overrides_can_set_the_flag(self, monkeypatch):
        catalog.set_model_overrides({})
        catalog.set_model_overrides({"x/y": {"supports_structured_outputs": True}})
        try:
            assert catalog.get_model_info("x/y").supports_structured_outputs is True
        finally:
            catalog.set_model_overrides(None)
        assert catalog.get_model_info("x/y").supports_structured_outputs is False


def _tools():
    return [
        ToolSchema(
            name="Read",
            description="read",
            parameters=[
                ToolParameter(name="file_path", type="string"),
                ToolParameter(name="limit", type="integer", required=False),
            ],
        )
    ]


def _body(provider, **kw):
    return provider._build_chat_completion_request("sys", [], _tools(), "m", 100, **kw)


class TestToolWire:
    def test_off_is_todays_wire(self):
        body = _body(OpenAICompatibleProvider(api_key="k"))
        fn = body["tools"][0]["function"]
        assert "strict" not in fn
        assert fn["parameters"] == {
            "type": "object",
            "properties": {"file_path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["file_path"],
        }

    def test_on_sends_strict_with_the_transformed_schema(self):
        body = _body(OpenAICompatibleProvider(api_key="k", strict_tool_schemas="on"))
        fn = body["tools"][0]["function"]
        assert fn["strict"] is True
        assert fn["parameters"] == {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "limit": {"type": ["integer", "null"]},
            },
            "required": ["file_path", "limit"],
            "additionalProperties": False,
        }

    def test_the_per_call_override_turns_it_off(self):
        provider = OpenAICompatibleProvider(api_key="k", strict_tool_schemas="on")
        fn = _body(provider, strict_tool_schemas="off")["tools"][0]["function"]
        assert "strict" not in fn

    def test_auto_reads_the_request_model(self, monkeypatch):
        catalog.set_model_overrides({"m": {"supports_structured_outputs": True}})
        provider = OpenAICompatibleProvider(api_key="k", strict_tool_schemas="auto")
        assert _body(provider)["tools"][0]["function"]["strict"] is True
        catalog.set_model_overrides({})
        assert "strict" not in _body(provider)["tools"][0]["function"]

    def test_the_default_config_sends_nothing_strict_without_the_catalog_flag(self, monkeypatch):
        from istota.config import NativeBrainConfig

        catalog.set_model_overrides({})
        catalog.set_fetched_catalog(None)
        from istota.brain import native_provider_spec
        from istota.config import BrainConfig

        config = NativeBrainConfig(provider="anthropic", api_key="k")
        provider = make_provider(native_provider_spec(BrainConfig(native=config)))
        assert provider.strict_tool_schemas == "auto"
        body = provider._build_chat_completion_request("sys", [], _tools(), config.model, 100)
        assert "strict" not in body["tools"][0]["function"]

    def test_an_unhandled_tool_goes_non_strict_and_the_others_stay_strict(self):
        odd = ToolSchema(
            name="Odd",
            description="",
            parameters=[ToolParameter(name="blob", type="object", required=False)],
        )
        provider = OpenAICompatibleProvider(api_key="k", strict_tool_schemas="on")
        body = provider._build_chat_completion_request("", [], _tools() + [odd], "m", 100)
        read_fn, odd_fn = (t["function"] for t in body["tools"])
        assert read_fn["strict"] is True
        assert "strict" not in odd_fn
        assert odd_fn["parameters"]["properties"] == {"blob": {"type": "object"}}

    def test_an_unknown_mode_warns_and_runs_off(self, caplog):
        caplog.set_level(logging.WARNING, logger="istota.llm.openai_compat")
        provider = OpenAICompatibleProvider(api_key="k", strict_tool_schemas="always")
        assert provider.strict_tool_schemas == "off"
        assert any("strict_tool_schemas" in r.getMessage() for r in caplog.records)

    def test_make_provider_passes_the_mode(self):
        from istota.llm.providers import InferenceKey, ProviderSpec, resolve_compat

        def spec(**settings):
            return ProviderSpec(
                id="p", kind="openai_compat", base_url="https://x/v1",
                inference_key=InferenceKey("k"),
                compat=resolve_compat("openai_compat", "https://x/v1", settings),
            )

        assert make_provider(spec(strict_tool_schemas="on")).strict_tool_schemas == "on"
        assert make_provider(spec()).strict_tool_schemas == "auto"

    def test_carries_strict_tools(self):
        assert carries_strict_tools(_tools(), "m", "on") is True
        assert carries_strict_tools(_tools(), "m", "off") is False
        assert carries_strict_tools([], "m", "on") is False


class TestRejectionPredicate:
    @pytest.mark.parametrize(
        "message",
        [
            'HTTP 400: {"error":{"message":"strict mode is not supported"}}',
            "HTTP 400: Invalid schema for function 'Read'",
            "HTTP 400: response_format is not supported by this provider",
        ],
    )
    def test_names_strict_schema_or_response_format(self, message):
        assert is_strict_schema_rejection(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "HTTP 500: invalid schema",
            "HTTP 400: max_tokens too large",
            "Connection error: schema",
            "",
        ],
    )
    def test_other_errors_are_not(self, message):
        assert is_strict_schema_rejection(message) is False


class TestDropNullOptionals:
    def test_drops_null_for_an_optional_key_only(self):
        schema = _tools()[0]
        assert drop_null_optionals({"file_path": "/a", "limit": None}, schema) == {
            "file_path": "/a"
        }
        # A null for a required key is left for validation to see.
        assert drop_null_optionals({"file_path": None}, schema) == {"file_path": None}

    def test_unknown_keys_and_real_values_pass_through(self):
        schema = _tools()[0]
        args = {"file_path": "/a", "limit": 0, "extra": None}
        assert drop_null_optionals(args, schema) == args

    def test_nested_item_nulls_are_dropped(self):
        args = {
            "file_path": "/a",
            "old_string": None,
            "new_string": None,
            "replace_all": None,
            "edits": [{"old_string": "x", "new_string": "y"}],
        }
        assert drop_null_optionals(args, EDIT_SCHEMA) == {
            "file_path": "/a",
            "edits": [{"old_string": "x", "new_string": "y"}],
        }

    def test_a_strict_legacy_edit_still_reaches_the_shim_shape(self):
        # The strict model sends every key; `edits: null` must not stop the
        # shim building the one-element batch.
        args = {
            "file_path": "/a",
            "old_string": "x",
            "new_string": "y",
            "edits": None,
            "replace_all": None,
        }
        out = prepare_edit_arguments(drop_null_optionals(args, EDIT_SCHEMA))
        assert out == {
            "file_path": "/a",
            "old_string": "x",
            "new_string": "y",
            "edits": [{"old_string": "x", "new_string": "y"}],
        }
