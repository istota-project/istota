"""`istota secret`'s vault half: the generated passphrase, and the two verbs.

Three separate rules meet on this command and each has a test class.

**The passphrase must be generated, not chosen**, and §5 says plainly that this
is the only boundary in the design standing against an adversary rather than a
mistake: the vault file sits in a tree bound read-write into the user's own
sandbox, so a prompt-injected task can carry its ciphertext out, and Argon2id
makes that useless against 256 random bits and not against a memorable phrase.
So a *supplied* value below the floor is **refused**, not warned about — a
warning in `vault-status` is read by whoever goes looking, which is not the
person who just typed a weak passphrase.

`TestTheFloorAppliesToTheSuppliedPathOnly` is the control for that, and it is the
one test here that would not be written without thinking about it: the floor
exists to make `--generate` the path everybody takes, so a floor that could
refuse `--generate` itself would break the remedy it was built to serve.

**A vault-owned service refuses `istota secret ensure`** (open question 3,
settled). The premise the earlier "just warn" answer rested on is false: a CLI
write does not touch the vault file, so §7's cycle short-circuits before parsing
and the value stands indefinitely — a permanent silent divergence from the file
the user believes is authoritative. `--force` is for the operator deliberately
testing a value before putting it in the file, and it says what will happen to it.

**Neither verb prints a value.** `vault-sync` prints counts and names of keys;
`vault-status` prints group names, key names and counts. The sweep at the bottom
is what holds that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import db
from istota.credentials import store as secrets_store

PASSPHRASE = "cli-fixture-passphrase-not-a-real-one"
API_KEY_VALUE = "ak-cli-fixture-alpha"
BASE_URL_VALUE = "https://karakeep.example.com"


class _Args:
    """The argparse namespace `cmd_secret` reads, with every flag defaulted."""

    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "action": None,
            "user": None,
            "service": None,
            "key": None,
            "value": None,
            "generate": False,
            "force": False,
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A config file with a workspace and one user, plus a real secret key."""
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
        f'workspace_path = "{mount}"\n'
        "\n"
        "[users.alice]\n"
        'display_name = "Alice"\n'
    )
    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    return cfg, db_path, mount


def _with_vault(env, *, vault_path="config/vault.kdbx"):
    """Rewrite the config file so alice has a vault, and return the paths."""
    cfg, db_path, mount = env
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_dir(cfg)}"\n'
        f'workspace_path = "{mount}"\n'
        "\n"
        "[users.alice]\n"
        'display_name = "Alice"\n'
        f'vault_path = "{vault_path}"\n'
    )
    return cfg, db_path, mount


def tmp_dir(cfg: Path) -> Path:
    return cfg.parent / "tmp"


def _write_unscoped_vault(path: Path, *, password=PASSPHRASE):
    """A KDBX with no top-level `istota` group, so §1 reads the whole file."""
    from tests.support.kdbx import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    group = kp.add_group(kp.root_group, "karakeep")
    kp.add_entry(group, "base_url", "", BASE_URL_VALUE)
    kp.add_entry(group, "api_key", "", API_KEY_VALUE)
    kp.save()


def _write_colliding_vault(path: Path, *, password=PASSPHRASE):
    """Two entries that slug to one name, so the read records a skip."""
    from tests.support.kdbx import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    kp.add_entry(kp.add_group(root, "aws"), "key", "", API_KEY_VALUE)
    kp.add_entry(root, "AWS Key", "", BASE_URL_VALUE)
    kp.save()


def _write_vault(path: Path, *, password=PASSPHRASE, ntfy=False, group_name="karakeep"):
    from tests.support.kdbx import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    group = kp.add_group(root, group_name)
    kp.add_entry(group, "base_url", "", BASE_URL_VALUE)
    kp.add_entry(group, "api_key", "", API_KEY_VALUE)
    if ntfy:
        kp.add_entry(kp.add_group(root, "ntfy"), "topic", "", "ntfy-cli-topic")
    kp.save()


# ---------------------------------------------------------------------------
# --generate and the floor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Open question 3: the refusal on a vault-owned service
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# vault-sync and vault-status
# ---------------------------------------------------------------------------


def test_vault_new_refuses_a_name_a_local_credential_holds(env, monkeypatch, capsys):
    """A generated name never shares a row with a stored credential, and the
    refusal comes before the file is touched."""
    import sys

    from tests.support.kdbx import create_database

    from istota.cli import main
    from istota.credentials.broker.bindings import parse_binding
    from istota.credentials.names import VAULT_ENTRY_SERVICE

    cfg, db_path, mount = _with_vault(env)
    path = mount / "Users" / "alice" / "config" / "vault.kdbx"
    path.parent.mkdir(parents=True, exist_ok=True)
    create_database(str(path), password=PASSPHRASE)
    before = path.read_bytes()
    secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)
    secrets_store.set_secret(
        db_path, "alice", VAULT_ENTRY_SERVICE, "generated_example_username", "typed",
        binding={**parse_binding("", {}, [], source="local"), "credential": "generated_example"},
    )
    monkeypatch.setattr(sys, "argv", [
        "istota", "-c", str(cfg), "secret", "vault-new", "--user", "alice",
        "--slug", "example", "--no-symbols",
    ])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already exists" in err and "generated_example_username" in err
    assert path.read_bytes() == before


def test_retire_needs_yes_and_removes_only_a_generated_credential(env, monkeypatch, capsys):
    import sys

    from istota import db
    from istota.cli import main
    from istota.credentials import generated
    from istota.credentials.broker.bindings import parse_binding
    from istota.credentials.names import VAULT_ENTRY_SERVICE

    cfg, db_path, _ = _with_vault(env)
    with db.get_db(db_path) as conn:
        generated.create(conn, "alice", name="generated_example", username="alice",
                         password="fixture-password", url="")
    secrets_store.set_secret(db_path, "alice", VAULT_ENTRY_SERVICE, "github", "typed",
                             binding=parse_binding("github.com", {}, [], source="local"))

    def run(*extra):
        monkeypatch.setattr(sys, "argv", ["istota", "-c", str(cfg), "secret", "retire",
                                          "--user", "alice", *extra])
        with pytest.raises(SystemExit) as exc:
            main()
        return exc.value.code

    assert run("--name", "generated_example") == 1
    assert "--yes" in capsys.readouterr().err
    assert secrets_store.get_secret(db_path, "alice", VAULT_ENTRY_SERVICE, "generated_example")
    assert run("--name", "github", "--yes") == 1
    assert secrets_store.get_secret(db_path, "alice", VAULT_ENTRY_SERVICE, "github") == "typed"

    monkeypatch.setattr(sys, "argv", ["istota", "-c", str(cfg), "secret", "retire", "--user", "alice",
                                      "--name", "generated_example", "--yes"])
    main()
    assert "Retired credential: generated_example" in capsys.readouterr().out
    assert secrets_store.get_secret(db_path, "alice", VAULT_ENTRY_SERVICE, "generated_example") is None


def test_export_and_import_use_a_terminal_and_shared_modules(tmp_path, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    from istota.cli import _cmd_secret_file
    from istota.config import Config, UserConfig
    from istota.credentials import kdbx_export
    from tests.test_kdbx_import import seed
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    path = tmp_path / "test.db"
    db.init_db(path)
    config = Config(db_path=path, users={"alice": UserConfig()})
    seed(path, "example", "first-value")
    monkeypatch.setattr(kdbx_export, "INTERACTIVE", kdbx_export.ExportOptions(1024, 1, 1))
    args = SimpleNamespace(action="export", user="alice", out=str(tmp_path / "export.kdbx"))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        _cmd_secret_file(config, args)
    assert not Path(args.out).exists()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    _cmd_secret_file(config, args)
    password = capsys.readouterr().out.split("save it now): ")[1].strip()
    assert Path(args.out).stat().st_mode & 0o777 == 0o600
    seed(path, "example", "current-value")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _: password)
    args = SimpleNamespace(action="import", user="alice", path=args.out, keyfile=None, include_changed=False)
    _cmd_secret_file(config, args)
    assert secrets_store.get_secret(path, "alice", "vault_entries", "example") == "current-value"
    args.include_changed = True
    _cmd_secret_file(config, args)
    assert secrets_store.get_secret(path, "alice", "vault_entries", "example") == "first-value"
    assert password not in capsys.readouterr().out
