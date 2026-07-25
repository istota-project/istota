"""istota.llm model-metadata resolution: override > fetched > default.

The bundled ``model_catalog.json`` is gone; resolution is config-first with a
live-fetched (OpenRouter) enrichment layer installed at runtime.
"""

import sys

from istota.llm.catalog import (
    ModelInfo,
    get_model_info,
    set_fetched_catalog,
    set_model_overrides,
)


class TestGetModelInfo:
    def teardown_method(self):
        set_fetched_catalog(None)
        set_model_overrides({})

    def test_unknown_model_falls_back_to_default(self):
        info = get_model_info("totally-made-up-model-xyz")
        # conservative default window, zero price (cost surfaces as unknown)
        assert isinstance(info, ModelInfo)
        assert info.context_window == 200_000
        assert info.input_price_per_mtok == 0.0
        # id is always the queried id, never the "unknown" sentinel
        assert info.id == "totally-made-up-model-xyz"

    def test_any_model_gets_default_when_no_layers(self):
        # With no fetched catalog and no overrides, every id resolves to the
        # conservative default (200k window) carrying its own id.
        info = get_model_info("anthropic/claude-opus-4.8")
        assert info.context_window == 200_000
        assert info.id == "anthropic/claude-opus-4.8"

    def test_default_model_info_fields(self):
        info = ModelInfo(id="x", context_window=1000, max_output_tokens=100)
        assert info.supports_tools is True
        assert info.supports_vision is False
        assert info.cache_read_price_per_mtok == 0.0

    def test_import_does_no_file_io(self):
        # The bundled JSON is gone; importing the catalog must not read a file.
        # (Reimport with a monkeypatched open would be brittle; instead assert
        # the module exposes no catalog path and the default is a pure constant.)
        import istota.llm.catalog as cat

        assert not hasattr(cat, "_CATALOG")
        assert not hasattr(cat, "_CATALOG_PATH")
        assert not hasattr(cat, "_load_catalog")
        # the module file lives next to no data file it loads at import
        assert "istota.llm.catalog" in sys.modules


class TestFetchedCatalog:
    def teardown_method(self):
        set_fetched_catalog(None)
        set_model_overrides({})

    def test_fetched_entry_wins_over_default(self):
        set_fetched_catalog(
            {
                "anthropic/claude-opus-4.8": ModelInfo(
                    id="anthropic/claude-opus-4.8",
                    context_window=500_000,
                    max_output_tokens=64000,
                    supports_thinking=True,
                    supports_vision=True,
                    input_price_per_mtok=5.0,
                )
            }
        )
        info = get_model_info("anthropic/claude-opus-4.8")
        assert info.context_window == 500_000
        assert info.supports_thinking is True
        assert info.input_price_per_mtok == 5.0

    def test_fetched_miss_falls_back_to_default(self):
        set_fetched_catalog({"a/b": ModelInfo(id="a/b", context_window=8000, max_output_tokens=1000)})
        info = get_model_info("c/d")
        assert info.context_window == 200_000
        assert info.id == "c/d"

    def test_clearing_fetched_restores_default(self):
        set_fetched_catalog({"a/b": ModelInfo(id="a/b", context_window=8000, max_output_tokens=1000)})
        set_fetched_catalog(None)
        assert get_model_info("a/b").context_window == 200_000
        set_fetched_catalog({"a/b": ModelInfo(id="a/b", context_window=8000, max_output_tokens=1000)})
        set_fetched_catalog({})
        assert get_model_info("a/b").context_window == 200_000

    def test_non_modelinfo_values_dropped(self):
        set_fetched_catalog({"a/b": "not a modelinfo"})  # type: ignore[dict-item]
        assert get_model_info("a/b").context_window == 200_000


class TestModelOverrides:
    """NB-4: operators declare per-model capabilities/window for endpoints no
    live catalog knows (a local qwen-thinking, a small-window model), and can
    correct a single wrong field on a fetched model."""

    def teardown_method(self):
        set_model_overrides({})
        set_fetched_catalog(None)

    def test_override_unknown_model_capabilities(self):
        set_model_overrides(
            {"qwen/qwen3-thinking": {"supports_thinking": True, "context_window": 32000}}
        )
        info = get_model_info("qwen/qwen3-thinking")
        assert info.supports_thinking is True
        assert info.context_window == 32000
        assert info.id == "qwen/qwen3-thinking"

    def test_override_merges_onto_fetched_model(self):
        # Override only the window; other fetched fields survive (partial merge).
        set_fetched_catalog(
            {
                "anthropic/claude-sonnet": ModelInfo(
                    id="anthropic/claude-sonnet",
                    context_window=200_000,
                    max_output_tokens=64000,
                    supports_vision=True,
                )
            }
        )
        set_model_overrides({"anthropic/claude-sonnet": {"context_window": 500000}})
        info = get_model_info("anthropic/claude-sonnet")
        assert info.context_window == 500000
        assert info.supports_vision is True  # unchanged fetched value

    def test_override_over_default_no_fetched(self):
        # NB-4 regression: override merges over the conservative default too.
        set_model_overrides({"m": {"context_window": 8000, "supports_thinking": True}})
        info = get_model_info("m")
        assert info.context_window == 8000
        assert info.supports_thinking is True

    def test_unknown_keys_ignored(self):
        set_model_overrides({"m": {"context_window": 8000, "bogus_field": 1}})
        info = get_model_info("m")
        assert info.context_window == 8000

    def test_clearing_overrides_restores_default(self):
        set_model_overrides({"z": {"context_window": 8000}})
        set_model_overrides({})
        assert get_model_info("z").context_window == 200_000
