"""A user's own provider key in their native tasks.

A user may store a key for a provider (service ``provider_keys``, key the
provider id). In their tasks a request to native's provider carries it instead
of the deployment's key; with none, or on a failed lookup, the deployment's key
is used. Another provider's key is never touched.
"""

from unittest.mock import patch

from istota.config import config_from_document
from istota.executor import _with_user_provider_key
from istota.llm.providers import InferenceKey


def _cfg(tmp_path, provider="openrouter", providers=()):
    config = config_from_document({
        "brain": {"kind": "native", "native": {
            "provider": provider, "providers": list(providers), "model": "m",
        }},
        "providers": {
            "openrouter": {"api_key": "instance-key"},
            "anthropic": {"api_key": "anthropic-instance-key"},
        },
    })
    config.db_path = tmp_path / "istota.db"
    return config


def test_user_secret_overrides_the_providers_key(tmp_path):
    config = _cfg(tmp_path)
    with patch("istota.credentials.store.get_secret", return_value="user-key") as lookup:
        out = _with_user_provider_key(config.brain, config, "alice")
    assert lookup.call_args.args[1:] == ("alice", "provider_keys", "openrouter")
    assert out.provider_specs["openrouter"].inference_key == InferenceKey("user-key")
    assert out.native.model == "m"  # everything else preserved
    # the deployment's record is not mutated
    assert config.brain.provider_specs["openrouter"].inference_key == InferenceKey("instance-key")


def test_another_providers_key_is_left_alone(tmp_path):
    config = _cfg(tmp_path)
    with patch("istota.credentials.store.get_secret", return_value="user-key"):
        out = _with_user_provider_key(config.brain, config, "alice")
    assert out.provider_specs["anthropic"].inference_key == InferenceKey("anthropic-instance-key")


def test_falls_back_to_the_deployment_key(tmp_path):
    config = _cfg(tmp_path)
    with patch("istota.credentials.store.get_secret", return_value=None):
        out = _with_user_provider_key(config.brain, config, "alice")
    assert out.provider_specs["openrouter"].inference_key == InferenceKey("instance-key")


def test_secret_lookup_error_falls_back(tmp_path):
    config = _cfg(tmp_path)
    with patch("istota.credentials.store.get_secret", side_effect=RuntimeError("no key")):
        out = _with_user_provider_key(config.brain, config, "alice")
    assert out.provider_specs["openrouter"].inference_key == InferenceKey("instance-key")


def test_an_unconfigured_native_brain_is_returned_as_is(tmp_path):
    config = _cfg(tmp_path, provider="")
    with patch("istota.credentials.store.get_secret", return_value="user-key") as lookup:
        out = _with_user_provider_key(config.brain, config, "alice")
    assert out is config.brain
    lookup.assert_not_called()


def test_the_provider_a_task_s_ref_routes_to_gets_the_user_s_key_too(tmp_path):
    # Native routes `anthropic/...` to the anthropic provider when
    # `[brain.native] providers` lists it (D4), so the user's own key for that
    # provider rides along; no other provider's does.
    config = _cfg(tmp_path, providers=["anthropic"])
    with patch("istota.credentials.store.get_secret", return_value="user-key") as lookup:
        out = _with_user_provider_key(config.brain, config, "alice", "anthropic/claude-x")
    asked = sorted(call.args[3] for call in lookup.call_args_list)
    assert asked == ["anthropic", "openrouter"]
    assert out.provider_specs["anthropic"].inference_key == InferenceKey("user-key")
    assert out.provider_specs["openrouter"].inference_key == InferenceKey("user-key")


def test_an_unlisted_provider_s_key_is_not_asked_for(tmp_path):
    # Not on the list, the ref is not routed, so its provider's key is not used.
    config = _cfg(tmp_path)
    with patch("istota.credentials.store.get_secret", return_value="user-key") as lookup:
        out = _with_user_provider_key(config.brain, config, "alice", "anthropic/claude-x")
    assert [call.args[3] for call in lookup.call_args_list] == ["openrouter"]
    assert out.provider_specs["anthropic"].inference_key == InferenceKey("anthropic-instance-key")


def test_a_model_naming_no_provider_asks_for_native_s_key_only(tmp_path):
    config = _cfg(tmp_path, providers=["anthropic"])
    with patch("istota.credentials.store.get_secret", return_value=None) as lookup:
        _with_user_provider_key(config.brain, config, "alice", "moonshotai/kimi")
    assert [call.args[3] for call in lookup.call_args_list] == ["openrouter"]
