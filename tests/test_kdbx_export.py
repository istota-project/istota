"""Encrypted downloads round trip through the production KeePass reader."""
from istota.credentials import kdbx_import as credential_read
import hashlib
import io
import re
from dataclasses import asdict

import pytest
from pykeepass import PyKeePass
from pykeepass.exceptions import CredentialsError

from istota import db
from istota.credentials import generated, store
from istota.credentials.broker import bindings, grants
from tests.test_kdbx_import import database, seed, OTP  # noqa: F401

PASSWORD = "SENTINEL-export-password-7f3a"


def keyfile():
    data = bytes(range(32))
    digest = hashlib.sha256(data).hexdigest()[:8].upper()
    return f'<KeyFile><Meta><Version>2.0</Version></Meta><Key><Data Hash="{digest}">{data.hex()}</Data></Key></KeyFile>'.encode()


@pytest.fixture
def cheap(monkeypatch):
    from istota.credentials import kdbx_export
    from tests.support.kdbx import create_database, _template_bytes
    _template_bytes()
    monkeypatch.setattr("pykeepass.create_database", create_database)
    monkeypatch.setattr(kdbx_export, "INTERACTIVE", kdbx_export.ExportOptions(64, 1, 1))
    return kdbx_export


def test_production_cost_and_password():
    from istota.credentials import kdbx_export as export
    assert asdict(export.INTERACTIVE) == dict(argon2_memory_kib=262144, argon2_iterations=8, argon2_parallelism=2)
    first, second = export.generate_export_password(), export.generate_export_password()
    assert re.fullmatch(r"[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){5}", first)
    assert first != second


def test_round_trip_header_and_exclusions(database, cheap, tmp_path, monkeypatch, caplog):  # noqa: F811
    seed(database, "portal", "local-value", url="https://portal.example/login")
    seed(database, "portal_username", "alice", owner="portal", url="https://portal.example/login")
    seed(database, "portal_url", "https://portal.example/login", owner="portal", url="https://portal.example/login")
    seed(database, "portal_totp", OTP, owner="portal", url="https://portal.example/login")
    seed(database, "operator", "config-only", source="config")
    store.set_secret(database, "alice", "wallet", "number", "wallet-only")
    seed(database, "other", "another-value")
    store.set_secret(database, "bob", "vault_entries", "bob_only", "another-user")
    with db.get_db(database) as conn:
        generated.create(conn, "alice", name="generated_account", username="alice", password="generated-value", url="https://account.example")
        generated.set_otp(conn, "alice", "generated_account", OTP)
        generated.set_recovery(conn, "alice", "generated_account", "(used) first-code\nsecond-code")
        db.kv_set(conn, "alice", grants.NAMESPACE, "auto:portal", grants.AUTO_GRANT_DECLINED)
    old_file = tmp_path / "vault.kdbx"
    old_file.write_bytes(b"original vault")
    import builtins
    import os
    original_open, original_os_open = builtins.open, os.open
    def guard(fn):
        def check(path, *args, **kwargs):
            assert str(path) != str(old_file)
            return fn(path, *args, **kwargs)
        return check
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", guard(original_open))
        patch.setattr(os, "open", guard(original_os_open))
        data, summary = cheap.build_kdbx(database, "alice", password=PASSWORD, keyfile=None, options=cheap.INTERACTIVE)
    assert old_file.read_bytes() == b"original vault"
    kp = PyKeePass(io.BytesIO(data), password=PASSWORD)
    assert kp.version == (4, 0)
    params = kp.kdbx.header.value.dynamic_header.kdf_parameters.data.dict
    assert params["$UUID"].value.hex() == "9e298b1956db4773b23dfc3ec6f0a1e6"
    assert (params["M"].value, params["I"].value, params["P"].value) == (65536, 1, 1)
    read = credential_read.parse_vault(data, PASSWORD)
    assert read.services["portal"] == "local-value"
    assert read.services["portal_url"] == "https://portal.example/login"
    assert read.services["portal_username"] == "alice"
    from istota.lib import totp
    assert totp.parse_otpauth(read.services["portal_totp"]) == totp.parse_otpauth(OTP)
    assert "portal" in read.no_auto_grant
    assert read.generated["generated_account"]["recovery"] == "(used) first-code\nsecond-code"
    assert read.generated["generated_account"]["password"] == "generated-value"
    assert "operator" not in read.services
    assert {entry.title for entry in kp.entries} == {"portal", "account", "other"}
    assert asdict(summary) == {"credentials": 3, "generated": 1, "otp": 2, "recovery": 1}
    with db.get_db(database) as conn:
        assert PASSWORD not in "\n".join(conn.iterdump())
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("url,attributes,tags", [
    ("", {}, []), ("portal.example.com", {}, []), ("https://Portal.Example.com:443/login", {}, []),
    ("https://portal.example:8443", {"istota_hosts": "api.example, [2001:db8::1]", "istota_headers": "X-API-Key"}, ["istota:reveal"]),
    ("http://portal.example:8080", {"istota_hosts": "api.example"}, []),
    ("invalid://host", {}, []), ("", {"istota_headers": "Proxy-Authorization"}, []),
])
def test_binding_inverse(url, attributes, tags):
    binding = bindings.parse_binding(url, attributes, tags)
    assert bindings.parse_binding(*bindings.binding_entry_fields(binding)) == binding


def test_keyfile_required(database, cheap):  # noqa: F811
    seed(database, "portal", "fixture")
    data, _ = cheap.build_kdbx(database, "alice", password=PASSWORD, keyfile=keyfile(), options=cheap.INTERACTIVE)
    assert PyKeePass(io.BytesIO(data), password=PASSWORD, keyfile=io.BytesIO(keyfile())).entries
    with pytest.raises(CredentialsError):
        PyKeePass(io.BytesIO(data), password=PASSWORD)


@pytest.mark.parametrize("value", [b"x" * 4097, b"not xml", b'<!DOCTYPE KeyFile [<!ENTITY x SYSTEM "file:///etc/passwd">]><KeyFile>&x;</KeyFile>', keyfile().replace(b"2.0", b"1.0"), keyfile().replace(b'Hash="630DCD29"', b'Hash="00000000"'), keyfile().replace(b"000102", b"0001")])
def test_keyfile_refusals(cheap, value):
    assert cheap.keyfile_refusal(value)
    assert cheap.keyfile_refusal(keyfile()) is None


def test_export_import_preserves_generated_binding_and_custom_fields(database, cheap, tmp_path):  # noqa: F811
    from istota.credentials import kdbx_import
    seed(database, "portal", "local-value")
    seed(database, "portal_extra", "custom-value", owner="portal")
    with db.get_db(database) as conn:
        generated.create(conn, "alice", name="generated_account", username="alice", password="fixture", url="https://account.example")
        binding = bindings.parse_binding("https://account.example", {"istota_hosts": "api.example", "istota_headers": "x-api-key"}, ["istota:reveal"], source="generated")
        for name in bindings.credential_groups(conn, "alice")["generated_account"]:
            bindings.put_binding(conn, "alice", name, {**binding, "credential": "generated_account"})
    data, _ = cheap.build_kdbx(database, "alice", password=PASSWORD, keyfile=None, options=cheap.INTERACTIVE)
    target = tmp_path / "restored.db"
    db.init_db(target)
    preview = kdbx_import.preview(target, "alice", data, PASSWORD)
    kdbx_import.apply(target, "alice", data, PASSWORD, selected=[i.name for i in preview.items], expected_digest=preview.digest, actor="import")
    with db.get_db(target) as conn:
        assert bindings.get_binding(conn, "alice", "generated_account") == {**binding, "kind": "value"}
    assert store.get_secret(target, "alice", "vault_entries", "portal_extra") == "custom-value"
