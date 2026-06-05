"""Per-user native-brain API key resolution.

The native brain key can be set per user in the encrypted secrets table
(service ``native_brain``, key ``api_key``). When present it overlays the
instance-wide key (``[brain.native] api_key`` / ``ISTOTA_BRAIN_NATIVE_API_KEY``);
when absent the instance key is used.
"""

from unittest.mock import patch

from istota.config import Config, NativeBrainConfig
from istota.executor import _native_with_user_key


def _cfg(tmp_path):
    c = Config()
    c.db_path = tmp_path / "istota.db"
    return c


def test_user_secret_overrides_instance_key(tmp_path):
    native = NativeBrainConfig(model="m", api_key="instance-key")
    with patch("istota.secrets_store.get_secret", return_value="user-key"):
        out = _native_with_user_key(native, _cfg(tmp_path), "alice")
    assert out.api_key == "user-key"
    assert out.model == "m"  # everything else preserved
    # original is not mutated
    assert native.api_key == "instance-key"


def test_falls_back_to_instance_key(tmp_path):
    native = NativeBrainConfig(model="m", api_key="instance-key")
    with patch("istota.secrets_store.get_secret", return_value=None):
        out = _native_with_user_key(native, _cfg(tmp_path), "alice")
    assert out.api_key == "instance-key"


def test_secret_lookup_error_falls_back(tmp_path):
    native = NativeBrainConfig(model="m", api_key="instance-key")
    with patch("istota.secrets_store.get_secret", side_effect=RuntimeError("no key")):
        out = _native_with_user_key(native, _cfg(tmp_path), "alice")
    assert out.api_key == "instance-key"
