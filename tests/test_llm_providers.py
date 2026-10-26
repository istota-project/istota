"""Providers and their keys (the model-catalogue spec, Stage 2).

A provider is one endpoint and the key for it, and a request to a provider
carries that provider's key whichever brain sends it. These tests hold the
records (`llm.providers`), the `[providers]` config with its two built-ins,
`[brain.native] provider` with no default, the per-provider variables, the
transition table for the legacy `[brain.native]` endpoint, the writers of the
config, and the one predicate for an unconfigured brain.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

from istota import config as config_module
from istota.brain import (
    brain_unconfigured_reason,
    native_provider_spec,
    reachable_brain_kinds,
    resolve_brain_kind,
    room_selectable_kinds,
)
from istota.brain._fallback import effective_fallback_kind
from istota.config import (
    LEGACY_PROVIDER_SOURCES,
    config_from_document,
    load_config,
)
from istota.config_mapper import collect_problems
from istota.llm import make_provider
from istota.llm.providers import (
    KIND_DEFAULTS,
    InferenceKey,
    ModelListingKey,
    ProviderSpec,
    implied_provider_id,
    provider_headers_variable,
    provider_key_variable,
    resolve_compat,
    reveal_inference_key,
)

REPO = Path(__file__).resolve().parents[1]


def _doc(**sections):
    return config_from_document(sections)


def _problems(document):
    with collect_problems() as problems:
        config = config_from_document(document)
    return config, problems


@pytest.fixture(autouse=True)
def _no_provider_variables(monkeypatch):
    for provider_id in ("anthropic", "openrouter", "local", "native"):
        monkeypatch.delenv(provider_key_variable(provider_id), raising=False)
        monkeypatch.delenv(provider_headers_variable(provider_id), raising=False)
    monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
    monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS", raising=False)


class TestTheKeyTypes:
    def test_neither_shows_its_value(self):
        for key in (InferenceKey("sk-sentinel"), ModelListingKey("sk-sentinel")):
            assert "sk-sentinel" not in repr(key)
            assert "sk-sentinel" not in str(key)
            assert "sk-sentinel" not in f"{key}"

    def test_no_common_base_and_no_conversion(self):
        assert not issubclass(ModelListingKey, InferenceKey)
        assert not issubclass(InferenceKey, ModelListingKey)
        assert not hasattr(ModelListingKey("x"), "reveal")
        assert not hasattr(InferenceKey("x"), "reveal_for_model_listing")

    def test_only_an_inference_key_reaches_a_provider(self):
        with pytest.raises(TypeError):
            reveal_inference_key(ModelListingKey("listing"))
        with pytest.raises(TypeError):
            reveal_inference_key("plain-string")
        assert reveal_inference_key(InferenceKey("k")) == "k"

    def test_they_are_immutable_and_unpicklable(self):
        import pickle

        key = InferenceKey("k")
        with pytest.raises(AttributeError):
            key._value = "other"
        with pytest.raises(TypeError):
            pickle.dumps(key)


class TestKindDefaults:
    def test_the_three_kinds(self):
        assert KIND_DEFAULTS["anthropic"].prompt_caching is True
        assert KIND_DEFAULTS["openrouter"].prompt_cache_key is True
        assert KIND_DEFAULTS["openrouter"].cost_accounting is True
        assert KIND_DEFAULTS["openrouter"].attribution_headers is True
        assert KIND_DEFAULTS["openai_compat"].prompt_caching is False
        assert KIND_DEFAULTS["openai_compat"].prompt_cache_key is False

    def test_openai_s_own_host_is_the_one_host_rule(self):
        compat = resolve_compat("openai_compat", "https://api.openai.com/v1", {})
        assert compat.prompt_cache_key is True
        assert compat.uses_max_completion_tokens("o3-mini")
        assert not compat.uses_max_completion_tokens("gpt-4o")
        local = resolve_compat("openai_compat", "http://localhost:11434/v1", {})
        assert not local.uses_max_completion_tokens("o3-mini")

    @pytest.mark.parametrize("name, value", [
        ("prompt_caching", False),
        ("prompt_cache_key", False),
        ("prompt_cache_ttl", "1h"),
        ("strict_tool_schemas", "on"),
        ("reasoning_replay", "off"),
    ])
    def test_an_explicit_wire_setting_wins_field_by_field(self, name, value):
        config = _doc(providers={"openrouter": {name: value}})
        compat = config.brain.provider_specs["openrouter"].compat
        assert getattr(compat, name) == value
        # The rest stay the kind's.
        assert compat.cost_accounting is True

    def test_the_compat_table(self):
        config = _doc(providers={"local": {
            "kind": "openai_compat", "base_url": "http://192.0.2.10:8080/v1",
            "compat": {"cost_accounting": True, "max_completion_tokens": True},
        }})
        compat = config.brain.provider_specs["local"].compat
        assert compat.cost_accounting is True
        assert compat.uses_max_completion_tokens("anything")


class TestTheBuiltIns:
    def test_they_exist_without_a_table(self):
        config = _doc()
        assert set(config.providers) == {"anthropic", "openrouter"}
        assert config.providers["anthropic"].kind == "anthropic"
        assert config.providers["anthropic"].base_url == "https://api.anthropic.com"
        assert config.providers["openrouter"].base_url == "https://openrouter.ai/api/v1"

    def test_a_table_sets_fields_on_one(self):
        config = _doc(providers={"anthropic": {"base_url": "https://gateway.example.test"}})
        assert config.providers["anthropic"].base_url == "https://gateway.example.test"
        assert config.providers["anthropic"].kind == "anthropic"

    def test_another_kind_for_a_built_in_is_refused(self):
        config, problems = _problems({"providers": {"openrouter": {"kind": "openai_compat"}}})
        assert config.providers["openrouter"].kind == "openrouter"
        assert ("providers", "openrouter", "kind") in [p["path"] for p in problems if p["kind"] == "discarded"]

    def test_a_provider_existing_makes_no_brain_use_it(self):
        assert _doc().brain.native.provider == ""


class TestAnOperatorsOwnProvider:
    def test_it_loads(self):
        config = _doc(providers={"local": {
            "kind": "openai_compat", "base_url": "http://192.0.2.10:8080/v1", "api_key": "k",
        }})
        spec = config.brain.provider_specs["local"]
        assert spec.kind == "openai_compat"
        assert spec.inference_key == InferenceKey("k")

    @pytest.mark.parametrize("table, field", [
        ({"base_url": "http://192.0.2.10/v1"}, "kind"),
        ({"kind": "openai_compat"}, "base_url"),
        ({"kind": "openai_compat", "base_url": "ftp://192.0.2.10"}, "base_url"),
        ({"kind": "quantum", "base_url": "http://192.0.2.10/v1"}, "kind"),
    ])
    def test_an_incomplete_table_is_refused_and_the_rest_loads(self, table, field):
        config, problems = _problems({"providers": {"local": table}, "bot_name": "Zed"})
        assert "local" not in config.providers
        assert config.bot_name == "Zed"
        assert ("providers", "local", field) in [p["path"] for p in problems if p["kind"] == "discarded"]

    @pytest.mark.parametrize("provider_id", ["my-p", "My", "1st", "a/b", "a:b"])
    def test_an_id_outside_the_shape_is_refused(self, provider_id):
        config, problems = _problems({"providers": {provider_id: {
            "kind": "openai_compat", "base_url": "http://192.0.2.10/v1",
        }}})
        assert provider_id not in config.providers
        assert [p["kind"] for p in problems if p["path"] == ("providers", provider_id)] == ["discarded"]

    def test_a_models_entry_without_an_id_refuses_the_provider(self):
        config, problems = _problems({"providers": {"local": {
            "kind": "openai_compat", "base_url": "http://192.0.2.10/v1", "models": [{"name": "x"}],
        }}})
        assert "local" not in config.providers


class TestNativesProvider:
    def test_it_has_no_default(self):
        config = _doc(brain={"kind": "native"})
        assert config.brain.native.provider == ""
        reason = brain_unconfigured_reason("native", config)
        assert reason and "[brain.native] provider" in reason
        assert native_provider_spec(config.brain) is None

    def test_a_named_provider_configures_it(self):
        config = _doc(brain={"kind": "native", "native": {"provider": "openrouter"}})
        assert brain_unconfigured_reason("native", config) is None
        assert native_provider_spec(config.brain).id == "openrouter"

    def test_a_provider_naming_nothing_is_the_reason(self):
        config = _doc(brain={"kind": "native", "native": {"provider": "nowhere"}})
        assert "names no configured provider" in brain_unconfigured_reason("native", config)

    def test_the_old_wire_protocol_value_reads_as_unset(self):
        config = _doc(brain={"kind": "native", "native": {"provider": "openai_compat"}})
        assert config.brain.native.provider == ""
        assert brain_unconfigured_reason("native", config) is not None

    def test_an_unconfigured_native_brain_is_still_built(self):
        from istota.brain import make_brain

        config = _doc(brain={"kind": "native"})
        result = make_brain(config.brain)
        assert type(result).__name__ == "NativeBrain"


class TestTheUnconfiguredReasonIsRead:
    """`brain_unconfigured_reason` is the one predicate, and each reader asks it."""

    def _config(self):
        return _doc(brain={
            "kind": "claude_code", "fallback": "native", "room_selectable": ["native", "claude_code"],
        })

    def test_room_selectable_drops_it(self):
        assert room_selectable_kinds(self._config().brain) == frozenset({"claude_code"})

    def test_it_is_skipped_as_a_fallback(self):
        assert effective_fallback_kind(self._config().brain) is None

    def test_a_pin_to_it_falls_through(self, caplog):
        config = self._config()
        with caplog.at_level(logging.WARNING, logger="istota.brain"):
            routed = resolve_brain_kind("talk", config.brain, override="native")
        assert routed.kind == "claude_code"

    def test_an_unconfigured_base_kind_is_still_resolved(self):
        config = _doc(brain={"kind": "native"})
        assert resolve_brain_kind("talk", config.brain).kind == "native"

    def test_doctor_names_it(self):
        from istota.doctor import run_checks

        config = _doc(brain={"kind": "native"})
        result = run_checks(config, only=("runtime.native_brain",))[0]
        assert result.status == "fail"
        assert "[brain.native] provider" in result.detail

    def test_configured_fallback_is_kept(self):
        config = _doc(brain={
            "kind": "claude_code", "fallback": "native", "native": {"provider": "openrouter"},
        })
        assert effective_fallback_kind(config.brain) == "native"
        assert "native" in reachable_brain_kinds(config.brain)


class TestTheKeyRule:
    def test_a_request_to_a_provider_carries_its_key(self):
        config = _doc(providers={
            "anthropic": {"api_key": "anthropic-key"},
            "openrouter": {"api_key": "openrouter-key"},
        })
        for provider_id in ("anthropic", "openrouter"):
            provider = make_provider(config.brain.provider_specs[provider_id])
            assert provider._api_key == f"{provider_id}-key"

    def test_the_variables_override_the_table(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_PROVIDER_OPENROUTER_API_KEY", "from-env")
        monkeypatch.setenv("ISTOTA_PROVIDER_OPENROUTER_EXTRA_HEADERS", '{"X-Tenant": "t"}')
        path = tmp_path / "config.toml"
        path.write_text('[providers.openrouter]\napi_key = "inline"\n')
        config = load_config(path)
        assert config.providers["openrouter"].api_key == "from-env"
        assert config.providers["openrouter"].extra_headers == {"X-Tenant": "t"}

    def test_an_own_providers_variable_is_read_too(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_PROVIDER_LOCAL_API_KEY", "local-env")
        path = tmp_path / "config.toml"
        path.write_text(
            '[providers.local]\nkind = "openai_compat"\nbase_url = "http://192.0.2.10/v1"\n'
        )
        assert load_config(path).providers["local"].api_key == "local-env"

    def test_the_path_rule(self):
        anthropic = ProviderSpec(id="a", kind="anthropic", base_url="https://api.anthropic.com/")
        assert anthropic.openai_base_url == "https://api.anthropic.com/v1"
        openrouter = ProviderSpec(id="o", kind="openrouter", base_url="https://openrouter.ai/api/v1")
        assert openrouter.openai_base_url == "https://openrouter.ai/api/v1"

    def test_no_anthropic_default_is_left_in_the_wire_layer(self):
        for module in ("llm/__init__.py", "llm/openai_compat.py"):
            text = (REPO / "src" / "istota" / module).read_text()
            assert "api.anthropic.com" not in text, module
            assert "openrouter.ai\" in" not in text, module


class TestTheTransitionTable:
    def test_every_document_row_is_a_native_field(self):
        native = {f for f in config_module.NativeBrainConfig.__dataclass_fields__}
        for row in LEGACY_PROVIDER_SOURCES:
            if row.source.startswith("env:"):
                continue
            assert row.source.rsplit(".", 1)[1] in native, row.source
            assert row.target in config_module.ProviderConfig.__dataclass_fields__, row.target

    @pytest.mark.parametrize("base_url, implied", [
        ("https://api.anthropic.com/v1", "anthropic"),
        ("https://openrouter.ai/api/v1", "openrouter"),
        ("http://192.0.2.10:8080/v1", "native"),
    ])
    def test_the_implied_id_comes_from_the_host(self, base_url, implied):
        assert implied_provider_id(base_url) == implied
        config = _doc(brain={"native": {"base_url": base_url, "api_key": "legacy"}})
        assert config.brain.native.provider == implied
        spec = config.brain.provider_specs[implied]
        assert spec.inference_key == InferenceKey("legacy")

    def test_anthropic_drops_the_v1(self):
        config = _doc(brain={"native": {"base_url": "https://api.anthropic.com/v1"}})
        assert config.providers["anthropic"].base_url == "https://api.anthropic.com"
        assert config.brain.provider_specs["anthropic"].openai_base_url == "https://api.anthropic.com/v1"

    def test_an_own_endpoint_becomes_an_openai_compat_provider(self):
        config = _doc(brain={"native": {
            "base_url": "http://192.0.2.10:8080/v1", "prompt_caching": True,
            "model_catalog_fetch": False,
        }})
        native = config.providers["native"]
        assert native.kind == "openai_compat"
        assert native.prompt_caching is True
        assert native.discover is False

    def test_a_current_table_wins_over_the_old_fields(self):
        config = _doc(
            brain={"native": {"base_url": "https://openrouter.ai/api/v1", "api_key": "legacy"}},
            providers={"openrouter": {"api_key": "current"}},
        )
        assert config.providers["openrouter"].api_key == "current"

    def test_a_current_variable_wins_over_the_legacy_one(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "legacy")
        monkeypatch.setenv("ISTOTA_PROVIDER_OPENROUTER_API_KEY", "current")
        path = tmp_path / "config.toml"
        path.write_text('[brain.native]\nbase_url = "https://openrouter.ai/api/v1"\n')
        assert load_config(path).providers["openrouter"].api_key == "current"

    def test_the_legacy_variable_fills_a_named_provider(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "legacy")
        path = tmp_path / "config.toml"
        path.write_text('[brain.native]\nprovider = "anthropic"\n')
        assert load_config(path).providers["anthropic"].api_key == "legacy"

    def test_no_base_url_and_no_provider_is_not_configured(self, monkeypatch, tmp_path):
        """What silently meant Anthropic before (D18)."""
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "legacy")
        path = tmp_path / "config.toml"
        path.write_text('[brain]\nkind = "native"\n')
        config = load_config(path)
        assert config.brain.native.provider == ""
        assert brain_unconfigured_reason("native", config) is not None

    def test_each_source_in_use_is_a_warning_and_nothing_is_logged(self, caplog, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "legacy-sentinel")
        path = tmp_path / "config.toml"
        path.write_text('[brain.native]\nbase_url = "https://openrouter.ai/api/v1"\n')
        with caplog.at_level(logging.DEBUG), collect_problems() as problems:
            load_config(path)
        notes = [p for p in problems if "is read for one release" in p["message"]]
        assert {p["kind"] for p in notes} == {"warning"}
        assert len(notes) == 2
        assert "is read for one release" not in caplog.text
        assert "legacy-sentinel" not in str(problems)


class TestModelsTheConfigDeclares:
    def test_an_own_providers_models_feed_the_catalogue(self, tmp_path):
        from istota.llm.catalog import get_model_info

        path = tmp_path / "config.toml"
        path.write_text(
            '[providers.local]\nkind = "openai_compat"\nbase_url = "http://192.0.2.10/v1"\n'
            'models = [ { id = "qwen-test", context_window = 32768 } ]\n'
        )
        load_config(path)
        assert get_model_info("local/qwen-test").context_window == 32768
        assert get_model_info("qwen-test").context_window == 32768
        assert get_model_info("local/qwen-test").price_source == "operator"

    def test_an_override_applies_to_a_model_a_layer_has(self, tmp_path):
        from istota.llm.catalog import get_model_info

        path = tmp_path / "config.toml"
        path.write_text(
            '[providers.local]\nkind = "openai_compat"\nbase_url = "http://192.0.2.10/v1"\n'
            'models = [ { id = "qwen-test", context_window = 32768 } ]\n'
            '[models.overrides."local/qwen-test"]\ncontext_window = 65536\n'
            '[models.overrides."local/nothing"]\ncontext_window = 7\n'
        )
        load_config(path)
        assert get_model_info("local/qwen-test").context_window == 65536
        # One naming a model no layer has is ignored.
        assert get_model_info("local/nothing").context_window == 200_000

    def test_the_legacy_overrides_keep_their_reach(self, tmp_path):
        from istota.llm.catalog import get_model_info

        path = tmp_path / "config.toml"
        path.write_text('[brain.native.model_overrides."a-local-model"]\ncontext_window = 8192\n')
        load_config(path)
        assert get_model_info("a-local-model").context_window == 8192


class TestTheMoveOfAUsersKey:
    def test_init_moves_native_brain_rows_to_natives_provider(self, monkeypatch, tmp_path):
        from istota import db
        from istota.credentials import store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        store.set_secret(db_path, "alice", "native_brain", "api_key", "alice-key")
        store.set_secret(db_path, "bob", "native_brain", "api_key", "bob-old")
        store.set_secret(db_path, "bob", "provider_keys", "openrouter", "bob-current")

        assert store.move_legacy_native_keys(db_path, "openrouter") == 1
        assert store.get_secret(db_path, "alice", "provider_keys", "openrouter") == "alice-key"
        assert store.get_secret(db_path, "bob", "provider_keys", "openrouter") == "bob-current"
        assert store.get_secret(db_path, "alice", "native_brain", "api_key") is None
        assert store.get_secret(db_path, "bob", "native_brain", "api_key") is None
        # Once moved, nothing is left to move.
        assert store.move_legacy_native_keys(db_path, "openrouter") == 0

    def test_the_cli_init_runs_it(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        from istota import cli, db
        from istota.credentials import store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        store.set_secret(db_path, "alice", "native_brain", "api_key", "alice-key")
        path = tmp_path / "config.toml"
        path.write_text(
            f'db_path = "{db_path}"\ntemp_dir = "{tmp_path / "tmp"}"\n'
            '[brain.native]\nprovider = "anthropic"\n'
        )
        cli.cmd_init(SimpleNamespace(config=str(path), check_schema=False, relocate_rooms=False))
        assert store.get_secret(db_path, "alice", "provider_keys", "anthropic") == "alice-key"


class TestTheWriters:
    def test_a_yes_run_needs_natives_provider(self):
        from istota import setup_wizard

        a = setup_wizard.ContainerAnswers(
            user_id="ada", brain_kind="native", native_model="m", native_api_key="k",
        )
        fields = [e.field for e in setup_wizard.validate_container_answers(a)]
        assert "native_provider" in fields

    def test_a_built_in_providers_key_goes_to_its_file(self):
        from istota import setup_wizard

        a = setup_wizard.ContainerAnswers(
            user_id="ada", brain_kind="native", native_provider="openrouter",
            native_model="m", native_api_key="k",
        )
        values = setup_wizard.container_secret_values(a)
        assert values["istota_provider_openrouter_api_key"] == "k"
        assert values["istota_brain_native_api_key"] == ""
        document = setup_wizard.container_config_document(a, inline_credentials=False)
        assert document["brain"]["native"] == {"provider": "openrouter", "model": "m"}
        assert "providers" not in document

    def test_a_rerun_carries_a_legacy_config_into_the_current_shape(self, tmp_path):
        from istota import setup_wizard

        data, vm = tmp_path / "data", tmp_path / "vm"
        (data / "config").mkdir(parents=True)
        (vm / "secrets").mkdir(parents=True)
        (data / "config" / "config.toml").write_text(
            '[brain]\nkind = "native"\n[brain.native]\n'
            'base_url = "https://openrouter.ai/api/v1"\nmodel = "m"\n'
        )
        (vm / "secrets" / "istota_brain_native_api_key").write_text("legacy-key")
        prior = setup_wizard.prior_answers(data, vm)
        assert prior.native_provider == "openrouter"
        assert prior.native_api_key == "legacy-key"
        setup_wizard.write_container_setup(prior, data_dir=data, vm_dir=vm, out=lambda *_: None)
        written = tomllib.loads((data / "config" / "config.toml").read_text())
        assert written["brain"]["native"] == {"provider": "openrouter", "model": "m"}
        assert (vm / "secrets" / "istota_provider_openrouter_api_key").read_text() == "legacy-key"
        # The legacy file is left as it is; the replaced config is kept for a rollback.
        assert (vm / "secrets" / "istota_brain_native_api_key").read_text() == "legacy-key"
        assert "base_url" in (data / "config" / "config.toml.pre-providers").read_text()

    def test_the_standalone_import_names_anthropic_for_a_config_with_no_base_url(self):
        from istota.maintenance import standalone_import as si

        old = {"brain": {"kind": "native", "native": {"model": "m", "api_key": "k"}}}
        answers = si._answers(old, "alice", "localhost:8181")
        assert answers.native_provider == "anthropic"
        document: dict = {}
        carried = {"brain": {"native": {}}}
        credentials = {"istota_brain_native_api_key": "k"}
        changes: list[str] = []
        si._native_to_provider(old, answers, document, carried, credentials, changes)
        assert credentials == {"istota_provider_anthropic_api_key": "k"}
        assert any('provider: "anthropic"' in change for change in changes)

    def test_the_standalone_import_moves_legacy_fields_onto_the_provider(self):
        from istota.maintenance import standalone_import as si

        old = {"brain": {"kind": "native", "native": {
            "base_url": "http://192.0.2.10:8080/v1", "model": "m", "prompt_caching": True,
        }}}
        answers = si._answers(old, "alice", "localhost:8181")
        document: dict = {}
        carried = {"brain": {"native": {"base_url": "http://192.0.2.10:8080/v1", "prompt_caching": True}}}
        si._native_to_provider(old, answers, document, carried, {}, [])
        assert document["providers"]["native"]["prompt_caching"] is True
        assert carried["brain"]["native"] == {}

    def test_the_testbed_names_its_scripted_provider(self):
        from testbed.services.model_endpoint import ScriptedEndpoint

        class _Placed(ScriptedEndpoint):
            container_url = "http://host.docker.internal:1/v1"

        fragment = _Placed.config(_Placed.__new__(_Placed))
        config = config_from_document(fragment)
        assert config.brain.native.provider == "scripted"
        assert native_provider_spec(config.brain).kind == "openai_compat"
