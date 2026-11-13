"""The decrypt worker's address-space cap against a backup made at the
production key derivation cost.

`kdbx_worker.main` sets `RLIMIT_AS` to `ADDRESS_SPACE_BYTES` on Linux only,
and every other test opens a file made at a cheap Argon2 cost, so this is the
one place a real backup (256 MiB, 8 iterations, 2 lanes) meets the real cap.
"""

import sys
from dataclasses import asdict

import pytest

from istota.config_editor import backup
from istota.credentials import kdbx_export

from .test_sandbox_real import _unavailable

pytestmark = pytest.mark.linux

PASSWORD = "fixture-backup-password-7e21"
FILE_VALUE = "fixture-imap-password-4c90"
INLINE_VALUE = "fixture-provider-key-1d3a"


@pytest.fixture(autouse=True)
def linux_only():
    if sys.platform != "linux":
        _unavailable("RLIMIT_AS is set on Linux only")
    pytest.importorskip("pykeepass")


async def test_a_production_cost_backup_opens_under_the_cap(tmp_path):
    assert asdict(kdbx_export.INTERACTIVE) == dict(
        argon2_memory_kib=262144, argon2_iterations=8, argon2_parallelism=2,
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[email]\nimap_host = "imap.example.org"\n\n'
        f'[brain.native.providers.own]\napi_key = "{INLINE_VALUE}"\n',
        encoding="utf-8",
    )
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "istota_email_imap_password").write_text(FILE_VALUE, encoding="utf-8")
    bundle = backup.gather_backup(config_path, secrets_dir, environ={})
    data = backup.build_installation_kdbx(bundle, password=PASSWORD, keyfile=None)

    opened = await backup.open_installation_backup(
        data, password=PASSWORD, keyfile=None, db_path=tmp_path / "istota.db",
    )

    assert opened.config_toml == bundle.config_toml
    assert {item.destination: item.value for item in opened.credentials} == {
        "file:istota_email_imap_password": FILE_VALUE,
        "inline:brain.native.providers.own.api_key": INLINE_VALUE,
    }
