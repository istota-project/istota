"""Phase 2 — argument coercion at the dispatch layer."""

from istota.agent.coercion import coerce_arguments
from istota.llm.types import ToolParameter, ToolSchema


def _schema(*params: ToolParameter) -> ToolSchema:
    return ToolSchema(name="t", description="", parameters=list(params))


class TestCoerceArguments:
    def test_string_to_integer(self):
        schema = _schema(ToolParameter(name="n", type="integer"))
        assert coerce_arguments({"n": "42"}, schema) == {"n": 42}

    def test_string_to_number(self):
        schema = _schema(ToolParameter(name="x", type="number"))
        assert coerce_arguments({"x": "3.14"}, schema) == {"x": 3.14}

    def test_string_to_boolean_true_variants(self):
        schema = _schema(ToolParameter(name="b", type="boolean"))
        for v in ("true", "True", "1", "yes", "YES"):
            assert coerce_arguments({"b": v}, schema) == {"b": True}

    def test_string_to_boolean_false_variants(self):
        schema = _schema(ToolParameter(name="b", type="boolean"))
        for v in ("false", "False", "0", "no"):
            assert coerce_arguments({"b": v}, schema) == {"b": False}

    def test_json_string_to_object(self):
        schema = _schema(ToolParameter(name="o", type="object"))
        assert coerce_arguments({"o": '{"k": 1}'}, schema) == {"o": {"k": 1}}

    def test_json_string_to_array(self):
        schema = _schema(ToolParameter(name="a", type="array"))
        assert coerce_arguments({"a": "[1, 2, 3]"}, schema) == {"a": [1, 2, 3]}

    def test_unknown_key_passes_through(self):
        schema = _schema(ToolParameter(name="n", type="integer"))
        assert coerce_arguments({"extra": "v"}, schema) == {"extra": "v"}

    def test_already_correct_type_unchanged(self):
        schema = _schema(ToolParameter(name="n", type="integer"))
        assert coerce_arguments({"n": 7}, schema) == {"n": 7}

    def test_uncoercible_integer_passes_through(self):
        schema = _schema(ToolParameter(name="n", type="integer"))
        assert coerce_arguments({"n": "not a number"}, schema) == {"n": "not a number"}

    def test_string_type_left_alone(self):
        schema = _schema(ToolParameter(name="s", type="string"))
        assert coerce_arguments({"s": "42"}, schema) == {"s": "42"}

    def test_malformed_json_object_passes_through(self):
        schema = _schema(ToolParameter(name="o", type="object"))
        assert coerce_arguments({"o": "{not json}"}, schema) == {"o": "{not json}"}
