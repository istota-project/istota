"""Private push settings, `istota push keygen` and the `push.signing_key` check."""

import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from istota import cli, doctor
from istota.config import Config, PushConfig, load_config
from istota.push import envelope


def write_config(tmp_path, push_body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'db_path = "{tmp_path / "istota.db"}"\n[push]\n{push_body}')
    return path


# --- Settings ---------------------------------------------------------------


def test_defaults_are_legacy_with_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_PUSH_SIGNING_KEY_PATH", raising=False)
    push = load_config(write_config(tmp_path, "enabled = true\n"), apply_db_overlays=False).push
    assert push.content == "legacy"
    assert push.signing_key_path == ""
    assert PushConfig().content == "legacy"


def test_private_with_a_key_path_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_PUSH_SIGNING_KEY_PATH", raising=False)
    path = write_config(tmp_path, 'content = "private"\nsigning_key_path = "/example/push/signing.pem"\n')
    push = load_config(path, apply_db_overlays=False).push
    assert (push.content, push.signing_key_path) == ("private", "/example/push/signing.pem")


def test_unknown_content_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_PUSH_SIGNING_KEY_PATH", raising=False)
    path = write_config(tmp_path, 'content = "encrypted"\nsigning_key_path = "/example/k.pem"\n')
    with pytest.raises(ValueError, match=r"\[push\] content"):
        load_config(path, apply_db_overlays=False)


def test_private_without_a_key_path_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_PUSH_SIGNING_KEY_PATH", raising=False)
    with pytest.raises(ValueError, match="signing_key_path"):
        load_config(write_config(tmp_path, 'content = "private"\n'), apply_db_overlays=False)


def test_environment_override_supplies_and_replaces_the_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_PUSH_SIGNING_KEY_PATH", "/example/from-env.pem")
    push = load_config(write_config(tmp_path, 'content = "private"\n'), apply_db_overlays=False).push
    assert push.signing_key_path == "/example/from-env.pem"
    path = write_config(tmp_path, 'content = "private"\nsigning_key_path = "/example/from-file.pem"\n')
    assert load_config(path, apply_db_overlays=False).push.signing_key_path == "/example/from-env.pem"
    monkeypatch.setenv("ISTOTA_PUSH_SIGNING_KEY_PATH", "  ")
    assert load_config(path, apply_db_overlays=False).push.signing_key_path == "/example/from-file.pem"


# --- Keygen -----------------------------------------------------------------


def run_cli(monkeypatch, config_path, *argv):
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config_path), "push", "keygen", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


@pytest.fixture
def key_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_PUSH_SIGNING_KEY_PATH", raising=False)
    key = tmp_path / "config" / "push" / "signing.pem"
    return write_config(tmp_path, f'signing_key_path = "{key}"\n'), key


def test_keygen_writes_a_matching_pair_with_modes(key_config, monkeypatch, capsys):
    config_path, key = key_config
    assert run_cli(monkeypatch, config_path) == 0
    pub = Path(f"{key}.pub")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(pub.stat().st_mode) == 0o644
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    signing = envelope.load_signing_key(key)
    raw = envelope.load_public_key(pub)
    assert raw == signing.public_key().public_bytes_raw()
    out = capsys.readouterr()
    assert envelope.key_id(raw) in out.out
    assert "PRIVATE" not in out.out + out.err
    assert envelope.b64url(signing.private_bytes_raw()) not in out.out + out.err


def test_keygen_refuses_to_overwrite_without_force(key_config, monkeypatch, capsys):
    config_path, key = key_config
    assert run_cli(monkeypatch, config_path) == 0
    before = key.read_bytes(), Path(f"{key}.pub").read_bytes()
    capsys.readouterr()
    assert run_cli(monkeypatch, config_path) == 1
    assert "--force" in capsys.readouterr().err
    assert (key.read_bytes(), Path(f"{key}.pub").read_bytes()) == before


def test_keygen_refuses_when_only_the_public_half_exists(key_config, monkeypatch):
    config_path, key = key_config
    key.parent.mkdir(parents=True)
    Path(f"{key}.pub").write_text("left-over\n")
    assert run_cli(monkeypatch, config_path) == 1
    assert not key.exists()


def test_keygen_force_rotates_and_warns_about_re_enrollment(key_config, monkeypatch, capsys):
    config_path, key = key_config
    assert run_cli(monkeypatch, config_path) == 0
    old = envelope.load_public_key(f"{key}.pub")
    capsys.readouterr()
    assert run_cli(monkeypatch, config_path, "--force") == 0
    new = envelope.load_public_key(f"{key}.pub")
    assert new != old
    assert new == envelope.load_signing_key(key).public_key().public_bytes_raw()
    assert "re-enroll" in capsys.readouterr().out


def test_keygen_without_a_configured_path_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ISTOTA_PUSH_SIGNING_KEY_PATH", raising=False)
    assert run_cli(monkeypatch, write_config(tmp_path, "enabled = true\n")) == 1
    assert "signing_key_path" in capsys.readouterr().err


def test_a_failed_write_leaves_the_existing_key_and_no_staging_file(key_config, monkeypatch):
    config_path, key = key_config
    assert run_cli(monkeypatch, config_path) == 0
    before = key.read_bytes()

    def refuse(src, dst, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        envelope.write_signing_key(key, force=True)
    assert key.read_bytes() == before
    assert sorted(p.name for p in key.parent.iterdir()) == ["signing.pem", "signing.pem.pub"]


# --- Doctor -----------------------------------------------------------------


@pytest.fixture
def private_config(tmp_path):
    key = tmp_path / "push" / "signing.pem"
    envelope.write_signing_key(key)
    return Config(push=PushConfig(enabled=True, content="private", signing_key_path=str(key))), key


def test_doctor_passes_a_good_pair_and_names_the_key_id(private_config):
    config, key = private_config
    results = doctor.run_checks(config, only=("push.signing_key",), probe=False)
    assert len(results) == 1 and results[0].status == doctor.OK
    assert envelope.key_id(envelope.load_public_key(f"{key}.pub")) in results[0].detail
    assert doctor.CHECK_SCOPES["push.signing_key"] == doctor.DEPLOYMENT


def test_doctor_skips_legacy_and_disabled(private_config):
    config, _ = private_config
    config.push.content = "legacy"
    assert doctor.check_push_signing_key(config, False).status == doctor.SKIP
    config.push.content = "private"
    config.push.enabled = False
    assert doctor.check_push_signing_key(config, False).status == doctor.SKIP


def test_doctor_fails_missing_key_and_loose_mode(private_config):
    config, key = private_config
    key.chmod(0o644)
    assert doctor.check_push_signing_key(config, False).status == doctor.FAIL
    key.chmod(0o600)
    config.push.signing_key_path += ".missing"
    assert doctor.check_push_signing_key(config, False).status == doctor.FAIL


def test_doctor_fails_an_unparsable_key(private_config):
    config, key = private_config
    key.write_text("not a key\n")
    key.chmod(0o600)
    result = doctor.check_push_signing_key(config, False)
    assert result.status == doctor.FAIL
    assert "not a key" not in result.detail + result.remedy


def test_doctor_fails_a_public_half_that_does_not_match(private_config):
    config, key = private_config
    other = Ed25519PrivateKey.generate()
    Path(f"{key}.pub").write_text(envelope.public_key_line(other))
    assert doctor.check_push_signing_key(config, False).status == doctor.FAIL
    Path(f"{key}.pub").unlink()
    assert doctor.check_push_signing_key(config, False).status == doctor.FAIL


def test_doctor_fails_a_key_of_another_type(private_config):
    from cryptography.hazmat.primitives.asymmetric import ec

    config, key = private_config
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    key.write_bytes(pem)
    assert doctor.check_push_signing_key(config, False).status == doctor.FAIL


def test_keygen_on_a_read_only_config_names_the_deployment_route(key_config, monkeypatch, capsys):
    import errno

    config_path, _ = key_config

    def read_only(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(envelope, "write_signing_key", read_only)
    assert run_cli(monkeypatch, config_path) == 1
    err = capsys.readouterr().err
    assert "read-only" in err
    assert "istota_push" in err


def test_keygen_on_a_read_only_config_names_the_stack_verb(key_config, monkeypatch, capsys):
    """#948: a stack the role does not manage has `istota-stack push-key`."""
    import errno

    config_path, _ = key_config

    def read_only(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(envelope, "write_signing_key", read_only)
    assert run_cli(monkeypatch, config_path) == 1
    err = capsys.readouterr().err
    assert "istota-stack push-key create" in err
    assert "--force" not in err
    assert run_cli(monkeypatch, config_path, "--force") == 1
    assert "istota-stack push-key create --force" in capsys.readouterr().err


def test_doctor_names_the_stack_verb_for_a_missing_key(private_config):
    config, key = private_config
    config.push.signing_key_path += ".missing"
    result = doctor.check_push_signing_key(config, False)
    assert result.status == doctor.FAIL
    assert "istota-stack push-key create" in result.remedy
    assert "istota_push" in result.remedy


# --- import-key -------------------------------------------------------------


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class _Stdin:
    """A piped stdin carrying `data`, as `istota-stack push-key import` hands it over."""

    def __init__(self, data: bytes, tty: bool = False):
        import io

        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def run_import(monkeypatch, config_path, data: bytes, *argv, tty: bool = False):
    monkeypatch.setattr("sys.stdin", _Stdin(data, tty))
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config_path), "push", "import-key", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


def test_write_signing_key_writes_a_given_key(tmp_path):
    given = Ed25519PrivateKey.generate()
    key = tmp_path / "push" / "signing.pem"
    written = envelope.write_signing_key(key, given=given)
    assert written is given
    assert envelope.load_signing_key(key).private_bytes_raw() == given.private_bytes_raw()
    assert envelope.load_public_key(f"{key}.pub") == given.public_key().public_bytes_raw()


def test_import_writes_the_given_key_and_its_public_half(key_config, monkeypatch, capsys):
    config_path, key = key_config
    given = Ed25519PrivateKey.generate()
    assert run_import(monkeypatch, config_path, _pem(given)) == 0
    pub = Path(f"{key}.pub")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(pub.stat().st_mode) == 0o644
    assert envelope.load_signing_key(key).private_bytes_raw() == given.private_bytes_raw()
    raw = envelope.load_public_key(pub)
    assert raw == given.public_key().public_bytes_raw()
    out = capsys.readouterr()
    assert envelope.key_id(raw) in out.out
    assert "PRIVATE" not in out.out + out.err
    assert envelope.b64url(given.private_bytes_raw()) not in out.out + out.err
    assert "re-enroll" not in out.out


def test_import_refuses_an_existing_key_without_force(key_config, monkeypatch, capsys):
    config_path, key = key_config
    assert run_cli(monkeypatch, config_path) == 0
    before = key.read_bytes(), Path(f"{key}.pub").read_bytes()
    capsys.readouterr()
    assert run_import(monkeypatch, config_path, _pem(Ed25519PrivateKey.generate())) == 1
    assert "--force" in capsys.readouterr().err
    assert (key.read_bytes(), Path(f"{key}.pub").read_bytes()) == before


def test_import_force_replaces_and_warns_about_re_enrollment(key_config, monkeypatch, capsys):
    config_path, key = key_config
    assert run_cli(monkeypatch, config_path) == 0
    capsys.readouterr()
    given = Ed25519PrivateKey.generate()
    assert run_import(monkeypatch, config_path, _pem(given), "--force") == 0
    assert envelope.load_public_key(f"{key}.pub") == given.public_key().public_bytes_raw()
    assert "re-enroll" in capsys.readouterr().out


@pytest.mark.parametrize("make", ["ec", "garbage", "empty", "encrypted"])
def test_import_refuses_anything_but_an_unencrypted_ed25519_key(key_config, monkeypatch, capsys, make):
    from cryptography.hazmat.primitives.asymmetric import ec

    config_path, key = key_config
    if make == "ec":
        data = _pem(ec.generate_private_key(ec.SECP256R1()))
    elif make == "garbage":
        data = b"not-a-key-secret-material\n"
    elif make == "empty":
        data = b""
    else:
        data = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        )
    assert run_import(monkeypatch, config_path, data) == 1
    out = capsys.readouterr()
    assert "not-a-key-secret-material" not in out.out + out.err
    assert "PRIVATE" not in out.out + out.err
    assert not key.exists() and not Path(f"{key}.pub").exists()


def test_import_refuses_a_terminal_on_stdin(key_config, monkeypatch, capsys):
    config_path, key = key_config
    assert run_import(monkeypatch, config_path, b"", tty=True) == 1
    assert "stdin" in capsys.readouterr().err
    assert not key.exists()


def test_import_on_a_read_only_config_names_the_stack_verb(key_config, monkeypatch, capsys):
    import errno

    config_path, _ = key_config

    def read_only(*args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(envelope, "write_signing_key", read_only)
    assert run_import(monkeypatch, config_path, _pem(Ed25519PrivateKey.generate())) == 1
    assert "istota-stack push-key import" in capsys.readouterr().err
