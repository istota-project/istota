"""Push-only deployment through the real settings writer and generated credentials."""
import importlib.util
import os
import subprocess
from pathlib import Path
import stat
import tomllib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from istota.lib import toml_write

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path):
    spec = importlib.util.spec_from_file_location("configure_push", REPO / "ansible/files/configure_push.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "config.toml"
    document = {"bot_name": "Existing bot", "db_path": str(tmp_path / "state/app.db"),
                "scheduler": {"poll_interval": 17}, "push": {"chat_delay_seconds": 28}}
    config.write_text(toml_write.dumps(document))
    marker = tmp_path / "managed.json"
    marker.write_text('{"config": {"managed": true, "source": "ansible"}}')
    key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    settings = {"enabled": True, "apns_key_id": "ABCDEFGHIJ", "apns_team_id": "KLMNOPQRST",
                "apns_topic": "com.example.app"}
    return module, config, document, marker, key, settings


def test_configures_only_push_and_repeats_without_writes(deployment):
    module, config, before, marker, key, settings = deployment
    original_marker = marker.read_bytes()
    target = config.parent / "apns/AuthKey.p8"
    assert module.configure(config, settings, key) == 2
    after = tomllib.loads(config.read_text())
    assert after.pop("push") == {**before.pop("push"), **settings, "apns_key_path": str(target)}
    assert after == before
    assert marker.read_bytes() == original_marker
    assert target.read_bytes() == key
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_uid == os.geteuid()
    stamps = (config.stat().st_mtime_ns, target.stat().st_mtime_ns)
    assert module.configure(config, settings, key) == 0
    assert stamps == (config.stat().st_mtime_ns, target.stat().st_mtime_ns)
    assert not (config.parent / "state/app.db").exists()


@pytest.mark.parametrize("invalid", ["key", "settings"])
def test_invalid_input_writes_nothing(deployment, invalid):
    module, config, _, marker, key, settings = deployment
    original = config.read_bytes(), marker.read_bytes()
    if invalid == "key":
        key = b"not a signing key"
    else:
        settings["scheduler"] = {"poll_interval": 1}
    with pytest.raises(ValueError):
        module.configure(config, settings, key)
    assert (config.read_bytes(), marker.read_bytes()) == original
    assert not (config.parent / "apns").exists()


def test_key_rotation_and_partial_failure_request_restart(deployment, monkeypatch):
    module, config, _, _, key, settings = deployment
    assert module.configure(config, settings, key) == 2
    replacement = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    assert module.configure(config, settings, replacement) == 2
    def fail(*args):
        raise OSError("private failure detail")
    monkeypatch.setattr(module.engine, "execute_plan", fail)
    assert module.configure(config, settings, key) == 3


@pytest.mark.parametrize("code,owed,restart,exit_code", [
    (0, False, False, 0), (2, False, True, 2),
    (3, False, False, 1), (0, True, True, 2), (1, False, False, 1),
])
def test_stack_lock_and_restart_protocol(tmp_path, code, owed, restart, exit_code):
    stack = tmp_path / "stack"
    stack.write_text("""set -euo pipefail
RESTART_OWED="$PUSH_TEST_DIR/restart-owed"
take_stack_lock() { echo locked > "$PUSH_TEST_DIR/lock"; }
main_service() { echo daemon; }
oneshot() {
    test -f "$PUSH_TEST_DIR/lock" || return 99
    test "$ISTOTA_CONFIG_READ_ONLY" = false || return 99
    return "$PUSH_TEST_CODE"
}
restart_for_config() {
    test -f "$PUSH_TEST_DIR/lock" || return 99
    touch "$PUSH_TEST_DIR/restarted"
    rm -f "$RESTART_OWED"
}
""")
    marker = tmp_path / "restart-owed"
    if owed:
        marker.touch()
    result = subprocess.run(["/bin/bash", str(REPO / "ansible/files/configure_push.sh"), str(stack), str(tmp_path)],
                            env={**os.environ, "PUSH_TEST_DIR": str(tmp_path), "PUSH_TEST_CODE": str(code)},
                            capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    assert (tmp_path / "restarted").exists() == restart
    assert marker.exists() == (code == 3)


def test_refuses_a_conflicting_environment_path(deployment, monkeypatch):
    module, config, _, _, key, settings = deployment
    monkeypatch.setenv("ISTOTA_APNS_KEY_PATH", "/other/key.p8")
    with pytest.raises(ValueError):
        module.configure(config, settings, key)
    assert not (config.parent / "apns").exists()


def test_private_content_gets_a_signing_key_once(deployment):
    module, config, _, _, key, settings = deployment
    settings["content"] = "private"
    target = config.parent / "push/signing.pem"
    assert module.configure(config, settings, key) == 2
    assert tomllib.loads(config.read_text())["push"]["signing_key_path"] == str(target)
    signing_key = serialization.load_pem_private_key(target.read_bytes(), password=None)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert (config.parent / "push/signing.pem.pub").read_text().strip()
    original = target.read_bytes()
    assert module.configure(config, settings, key) == 0
    assert target.read_bytes() == original
    assert signing_key is not None


def test_an_existing_signing_key_is_never_replaced(deployment):
    module, config, _, _, key, settings = deployment
    target = config.parent / "keys/signing.pem"
    settings.update(content="private", signing_key_path=str(target))
    assert module.configure(config, settings, key) == 2
    original = target.read_bytes(), (config.parent / "keys/signing.pem.pub").read_bytes()
    settings["chat_delay_seconds"] = 40
    assert module.configure(config, settings, key) == 2
    assert (target.read_bytes(), (config.parent / "keys/signing.pem.pub").read_bytes()) == original


def test_legacy_content_writes_no_signing_key(deployment):
    module, config, _, _, key, settings = deployment
    assert module.configure(config, settings, key) == 2
    assert "signing_key_path" not in tomllib.loads(config.read_text())["push"]
    assert not (config.parent / "push").exists()


def test_a_broken_signing_key_pair_is_refused_not_replaced(deployment):
    module, config, _, _, key, settings = deployment
    settings["content"] = "private"
    target = config.parent / "push/signing.pem"
    target.parent.mkdir(mode=0o700)
    target.write_bytes(b"half written")
    original = config.read_bytes()
    with pytest.raises(ValueError):
        module.configure(config, settings, key)
    assert target.read_bytes() == b"half written"
    assert config.read_bytes() == original


@pytest.mark.parametrize("variable,value", [
    ("ISTOTA_PUSH_SIGNING_KEY_PATH", "/other/signing.pem"),
])
def test_refuses_a_conflicting_signing_key_environment_path(deployment, monkeypatch, variable, value):
    module, config, _, _, key, settings = deployment
    settings["content"] = "private"
    monkeypatch.setenv(variable, value)
    original = config.read_bytes()
    with pytest.raises(ValueError):
        module.configure(config, settings, key)
    assert config.read_bytes() == original
    assert not (config.parent / "push").exists()


def test_a_symlinked_signing_key_destination_is_refused(deployment, tmp_path):
    module, config, _, _, key, settings = deployment
    settings["content"] = "private"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (config.parent / "push").symlink_to(elsewhere)
    with pytest.raises(ValueError):
        module.configure(config, settings, key)
    assert list(elsewhere.iterdir()) == []
