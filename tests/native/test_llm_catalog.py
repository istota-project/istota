"""Phase 1 — istota.llm bundled model catalog."""

from istota.llm.catalog import ModelInfo, get_model_info, set_model_overrides


class TestGetModelInfo:
    def test_known_model_has_real_window(self):
        info = get_model_info("claude-opus-4-8")
        assert isinstance(info, ModelInfo)
        assert info.context_window > 0
        assert info.id == "claude-opus-4-8"

    def test_known_sonnet_present(self):
        info = get_model_info("claude-sonnet-4-6")
        assert info.context_window > 0

    def test_unknown_model_falls_back_to_default(self):
        info = get_model_info("totally-made-up-model-xyz")
        # conservative default window, zero price (cost surfaces as unknown)
        assert info.context_window == 200_000
        assert info.input_price_per_mtok == 0.0
        assert info.id == "unknown"

    def test_default_model_info_fields(self):
        info = ModelInfo(id="x", context_window=1000, max_output_tokens=100)
        assert info.supports_tools is True
        assert info.supports_vision is False
        assert info.cache_read_price_per_mtok == 0.0

    def test_openai_vision_and_reasoning_entries_present(self):
        # NB-4: a few stable non-Anthropic ids ship so vision/reasoning aren't
        # silently disabled on the harness's headline model-agnostic use.
        assert get_model_info("gpt-4o").supports_vision is True
        assert get_model_info("o3").supports_thinking is True


class TestModelOverrides:
    """NB-4: operators declare per-model capabilities/window for endpoints the
    bundled catalog doesn't know, so a local qwen-thinking or a small-window
    model isn't crippled by the conservative default."""

    def teardown_method(self):
        set_model_overrides({})

    def test_override_unknown_model_capabilities(self):
        set_model_overrides(
            {"qwen/qwen3-thinking": {"supports_thinking": True, "context_window": 32000}}
        )
        info = get_model_info("qwen/qwen3-thinking")
        assert info.supports_thinking is True
        assert info.context_window == 32000
        assert info.id == "qwen/qwen3-thinking"

    def test_override_merges_onto_known_model(self):
        # Override only the window; other fields keep the bundled values.
        set_model_overrides({"claude-sonnet-4-6": {"context_window": 500000}})
        info = get_model_info("claude-sonnet-4-6")
        assert info.context_window == 500000
        assert info.supports_vision is True  # unchanged bundled value

    def test_unknown_keys_ignored(self):
        set_model_overrides({"m": {"context_window": 8000, "bogus_field": 1}})
        info = get_model_info("m")
        assert info.context_window == 8000

    def test_clearing_overrides_restores_default(self):
        set_model_overrides({"z": {"context_window": 8000}})
        set_model_overrides({})
        assert get_model_info("z").context_window == 200_000
