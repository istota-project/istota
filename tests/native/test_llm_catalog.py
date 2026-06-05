"""Phase 1 — istota.llm bundled model catalog."""

from istota.llm.catalog import ModelInfo, get_model_info


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
