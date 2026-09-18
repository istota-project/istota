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

import re
from pathlib import Path

import pytest

from istota import db, secrets_store

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


@pytest.fixture(autouse=True)
def _clean_sync_state():
    from istota import secrets_vault

    secrets_vault.reset_sync_state()
    yield
    secrets_vault.reset_sync_state()


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


def _with_vault(env, *, vault_path="config/vault.kdbx", services=("karakeep",)):
    """Rewrite the config file so alice has a vault, and return the paths."""
    cfg, db_path, mount = env
    services_toml = ", ".join(f'"{s}"' for s in services)
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_dir(cfg)}"\n'
        f'workspace_path = "{mount}"\n'
        "\n"
        "[users.alice]\n"
        'display_name = "Alice"\n'
        f'vault_path = "{vault_path}"\n'
        f"vault_services = [{services_toml}]\n"
    )
    return cfg, db_path, mount


def tmp_dir(cfg: Path) -> Path:
    return cfg.parent / "tmp"


def _write_vault(path: Path, *, password=PASSPHRASE, ntfy=False):
    from pykeepass import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    group = kp.add_group(root, "karakeep")
    kp.add_entry(group, "base_url", "", BASE_URL_VALUE)
    kp.add_entry(group, "api_key", "", API_KEY_VALUE)
    if ntfy:
        kp.add_entry(kp.add_group(root, "ntfy"), "topic", "", "ntfy-cli-topic")
    kp.save()


# ---------------------------------------------------------------------------
# --generate and the floor
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_generate_mints_stores_and_prints_the_value_once(self, env, capsys):
        from istota.cli import cmd_secret
        from istota.secrets_vault import VAULT_PASSPHRASE_MIN_CHARS

        cfg, db_path, _mount = env
        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="vault",
                key="passphrase",
                generate=True,
            )
        )
        out = capsys.readouterr().out
        stored = secrets_store.get_secret(db_path, "alice", "vault", "passphrase")

        assert stored is not None
        # 32 random bytes as urlsafe base64. The length is asserted as a literal
        # rather than recomputed from the constant, which would compare the
        # implementation with itself.
        assert len(stored) == 43
        assert len(stored) >= VAULT_PASSPHRASE_MIN_CHARS
        assert re.fullmatch(r"[A-Za-z0-9_-]+", stored)
        # Printed exactly once: the operator has to copy it into their own
        # password manager, and every extra copy is another line of scrollback.
        assert out.count(stored) == 1

    def test_two_generates_do_not_produce_the_same_value(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        args = dict(
            config=str(cfg),
            action="ensure",
            user="alice",
            service="vault",
            key="passphrase",
            generate=True,
        )
        cmd_secret(_Args(**args))
        first = secrets_store.get_secret(db_path, "alice", "vault", "passphrase")
        capsys.readouterr()
        cmd_secret(_Args(**args))
        second = secrets_store.get_secret(db_path, "alice", "vault", "passphrase")
        assert first != second

    def test_generate_with_a_value_is_refused(self, env, capsys):
        """Two answers to one question. Neither is safe to pick silently."""
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        with pytest.raises(SystemExit) as exc:
            cmd_secret(
                _Args(
                    config=str(cfg),
                    action="ensure",
                    user="alice",
                    service="vault",
                    key="passphrase",
                    generate=True,
                    value="x" * 64,
                )
            )
        assert exc.value.code == 1
        assert secrets_store.secret_exists(db_path, "alice", "vault", "passphrase") is False

    def test_generate_is_refused_for_anything_but_the_vault_passphrase(
        self, env, capsys
    ):
        """Only a credential this deployment *issues* can be minted here.

        A Karakeep API key is issued by Karakeep; generating one would store a
        value nothing on the other end recognises.
        """
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        with pytest.raises(SystemExit) as exc:
            cmd_secret(
                _Args(
                    config=str(cfg),
                    action="ensure",
                    user="alice",
                    service="karakeep",
                    key="api_key",
                    generate=True,
                )
            )
        assert exc.value.code == 1
        assert secrets_store.secret_exists(db_path, "alice", "karakeep", "api_key") is False


class TestTheFloor:
    def test_a_supplied_value_below_the_floor_is_refused(self, env, capsys):
        """Refused, not warned about. §5's own words, and the reason is there."""
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        with pytest.raises(SystemExit) as exc:
            cmd_secret(
                _Args(
                    config=str(cfg),
                    action="ensure",
                    user="alice",
                    service="vault",
                    key="passphrase",
                    value="correct horse battery",
                )
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--generate" in err
        assert secrets_store.secret_exists(db_path, "alice", "vault", "passphrase") is False

    def test_a_supplied_value_at_the_floor_is_accepted(self, env):
        """The control: the floor refuses below it and nothing above it."""
        from istota.cli import cmd_secret
        from istota.secrets_vault import VAULT_PASSPHRASE_MIN_CHARS

        cfg, db_path, _mount = env
        value = "q" * VAULT_PASSPHRASE_MIN_CHARS
        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="vault",
                key="passphrase",
                value=value,
            )
        )
        assert secrets_store.get_secret(db_path, "alice", "vault", "passphrase") == value

    def test_the_floor_does_not_apply_to_other_services(self, env):
        """A short Karakeep API key is Karakeep's business, not this floor's."""
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="karakeep",
                key="api_key",
                value="short",
            )
        )
        assert secrets_store.get_secret(db_path, "alice", "karakeep", "api_key") == "short"


class TestTheFloorAppliesToTheSuppliedPathOnly:
    """The control the stage line asks for, and it is not decorative.

    `--generate` exists *because* of the floor. A floor applied to the generated
    value as well — or applied before the generation, to an absent `--value` —
    would refuse the one command §5 tells every operator to run, and the failure
    would look like the floor working.
    """

    def test_generate_is_unaffected_by_the_floor(self, env, monkeypatch, capsys):
        from istota import secrets_vault
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        # A floor above anything `--generate` can mint. If the generated value
        # were put through it, this refuses and the assertion below fails.
        monkeypatch.setattr(secrets_vault, "VAULT_PASSPHRASE_MIN_CHARS", 4096)
        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="vault",
                key="passphrase",
                generate=True,
            )
        )
        assert secrets_store.secret_exists(db_path, "alice", "vault", "passphrase")


# ---------------------------------------------------------------------------
# Open question 3: the refusal on a vault-owned service
# ---------------------------------------------------------------------------


class TestEnsureOnAVaultOwnedService:
    def test_a_vault_owned_service_is_refused(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")

        with pytest.raises(SystemExit) as exc:
            cmd_secret(
                _Args(
                    config=str(cfg),
                    action="ensure",
                    user="alice",
                    service="karakeep",
                    key="api_key",
                    value="typed-by-hand",
                )
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--force" in err
        assert secrets_store.secret_exists(db_path, "alice", "karakeep", "api_key") is False

    def test_force_writes_and_says_what_will_happen_to_it(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")

        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="karakeep",
                key="api_key",
                value="typed-by-hand",
                force=True,
            )
        )
        captured = capsys.readouterr()
        assert (
            secrets_store.get_secret(db_path, "alice", "karakeep", "api_key")
            == "typed-by-hand"
        )
        # OQ3: "--force stays for the operator who is deliberately testing a
        # value before putting it in the file, and it warns that the next vault
        # write will overwrite it."
        assert "overwritten" in (captured.out + captured.err).lower()

    def test_a_service_the_vault_does_not_own_is_untouched(self, env):
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env, services=("karakeep",))
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")

        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="ntfy",
                key="topic",
                value="my-topic",
            )
        )
        assert secrets_store.get_secret(db_path, "alice", "ntfy", "topic") == "my-topic"

    def test_the_passphrase_itself_is_never_vault_owned(self, env):
        """§4 subtracts `vault` from eligibility, which is what keeps the
        provisioning path open — a refusal there would make the feature
        unconfigurable."""
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env, services=("karakeep",))
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")

        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="vault",
                key="passphrase",
                generate=True,
            )
        )
        assert secrets_store.secret_exists(db_path, "alice", "vault", "passphrase")

    def test_a_user_with_no_resolving_vault_is_not_refused(self, env):
        """Ownership is `in vault_services` *and* the path resolving (§9).

        A configured-but-refused path owns nothing in practice, and refusing an
        operator's CLI write on the strength of a line that does not work is a
        refusal with no remedy behind it.

        The refused path here is a *relative* one naming a directory that is not
        there. An absolute path to a file that does not exist is deliberately not
        the same case: it resolves, so the vault still owns the service and the
        refusal still stands — the file not having been created yet is a state to
        fix rather than a licence to write around it.
        """
        from istota.cli import cmd_secret

        cfg, db_path, _mount = _with_vault(env, vault_path="no-such-dir/vault.kdbx")

        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="karakeep",
                key="api_key",
                value="typed-by-hand",
            )
        )
        assert (
            secrets_store.get_secret(db_path, "alice", "karakeep", "api_key")
            == "typed-by-hand"
        )


# ---------------------------------------------------------------------------
# vault-sync and vault-status
# ---------------------------------------------------------------------------


class TestVaultSync:
    def test_it_names_counts_and_never_a_value(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        cmd_secret(_Args(config=str(cfg), action="vault-sync", user="alice"))
        out = capsys.readouterr().out

        assert "2" in out  # two credentials written
        assert "api_key" in out or "karakeep" in out
        assert API_KEY_VALUE not in out
        assert BASE_URL_VALUE not in out
        assert PASSPHRASE not in out

    def test_it_parses_even_on_a_digest_a_previous_cycle_cached(self, env, capsys):
        """The operator's escape hatch from every cache-shaped surprise.

        §8's remedy strings name this command, so it must not be subject to the
        cache it exists to defeat. Driven in-process because the CLI's own module
        state is empty in a fresh process — which is precisely why the clearing
        has to live on `sync_user` rather than in the CLI branch.
        """
        from istota import secrets_vault
        from istota.cli import cmd_secret
        from istota.config import load_config

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        # Prime the cache the way an ordinary interval cycle would, then take the
        # credential out from under it. The digest has not moved, so an ordinary
        # cycle would skip and the row would stay gone.
        secrets_vault.sync_user(load_config(cfg), "alice")
        secrets_store.delete_secret(db_path, "alice", "karakeep", "api_key")

        cmd_secret(_Args(config=str(cfg), action="vault-sync", user="alice"))
        assert (
            secrets_store.get_secret(db_path, "alice", "karakeep", "api_key")
            == API_KEY_VALUE
        )

    def test_a_service_level_refusal_does_not_render_a_bare_slash(self):
        """`skipped` carries an empty key for a whole-service refusal.

        Rendering the triple blindly prints `monarch/` with nothing after the
        slash, which reads as a key named "" rather than as a service nobody may
        own.

        Driven against the formatter rather than through `vault-sync`, and that
        is the point rather than a shortcut: `_validate_vault_services` drops an
        ineligible name at config load, so the shape never reaches the CLI from a
        loaded config and a test going through it would assert nothing. The
        triple is still produced — `apply_vault` refuses the same names again on
        its own terms, for a caller that did not come through the loader — so the
        renderer has to handle it.
        """
        from istota.secrets_vault import (
            SKIP_INELIGIBLE_SERVICE,
            SKIP_UNKNOWN_KEY,
            format_skip,
        )

        assert format_skip("monarch", "") == "monarch"
        assert format_skip("karakeep", "apikey") == "karakeep/apikey"
        # The two reasons that produce each shape, named so a reader can see the
        # empty key is a real state and not a defensive branch.
        assert SKIP_INELIGIBLE_SERVICE and SKIP_UNKNOWN_KEY

    def test_a_user_with_no_vault_is_reported_rather_than_skipped(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, _db_path, _mount = env
        cmd_secret(_Args(config=str(cfg), action="vault-sync", user="alice"))
        out = capsys.readouterr().out
        assert "alice" in out


class TestVaultStatus:
    def test_an_unconfigured_user_exits_cleanly(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, _db_path, _mount = env
        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out
        assert "alice" in out

    def test_it_names_the_groups_it_found_and_no_value(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx", ntfy=True)
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out

        assert "karakeep" in out
        # An unowned group is what tells a user their group name matches nothing.
        assert "ntfy" in out
        assert API_KEY_VALUE not in out
        assert BASE_URL_VALUE not in out
        assert PASSPHRASE not in out

    def test_it_does_not_write_anything(self, env, capsys):
        """A status verb that applied would be a verb nobody could run safely."""
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        assert (
            secrets_store.secret_exists(db_path, "alice", "karakeep", "api_key")
            is False
        )

    def test_a_missing_passphrase_is_reported_without_parsing(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, _db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out.lower()
        assert "passphrase" in out
