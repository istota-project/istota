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
    from pykeepass import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    group = kp.add_group(kp.root_group, "karakeep")
    kp.add_entry(group, "base_url", "", BASE_URL_VALUE)
    kp.add_entry(group, "api_key", "", API_KEY_VALUE)
    kp.save()


def _write_colliding_vault(path: Path, *, password=PASSPHRASE):
    """Two entries that slug to one name, so the read records a skip."""
    from pykeepass import create_database

    path.parent.mkdir(parents=True, exist_ok=True)
    kp = create_database(str(path), password=password)
    root = kp.add_group(kp.root_group, "istota")
    kp.add_entry(kp.add_group(root, "aws"), "key", "", API_KEY_VALUE)
    kp.add_entry(root, "AWS Key", "", BASE_URL_VALUE)
    kp.save()


def _write_vault(path: Path, *, password=PASSPHRASE, ntfy=False, group_name="karakeep"):
    from pykeepass import create_database

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
        """`--force` on the second, because a bare re-generate is refused —
        see `TestGenerateNeverSilentlyRotates`. What is asserted here is the
        generator, not the refusal."""
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
        cmd_secret(_Args(force=True, **args))
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


class TestGenerateNeverSilentlyRotates:
    """The data-loss path, reachable by re-running the documented command.

    Every credential in the vault file is encrypted under the stored passphrase.
    Minting a new one overwrites the only copy the server has and leaves the file
    unopenable — every later sync comes back `VaultLocked`, and the value that
    would fix it is gone. The subcommand advertises itself as idempotent, so an
    Ansible play re-running it is the ordinary case rather than the careless one.
    """

    def _provision(self, cfg, db_path):
        from istota.cli import cmd_secret

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
        return secrets_store.get_secret(db_path, "alice", "vault", "passphrase")

    def test_a_second_generate_is_refused_and_keeps_the_stored_value(
        self, env, capsys
    ):
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        first = self._provision(cfg, db_path)
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
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
        assert exc.value.code == 1
        # The assertion that matters: the value that opens the file is still
        # there. A refusal that had already written would be the defect with a
        # message attached.
        assert (
            secrets_store.get_secret(db_path, "alice", "vault", "passphrase") == first
        )
        assert "--force" in capsys.readouterr().err

    def test_force_rotates(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        first = self._provision(cfg, db_path)
        capsys.readouterr()

        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="vault",
                key="passphrase",
                generate=True,
                force=True,
            )
        )
        assert (
            secrets_store.get_secret(db_path, "alice", "vault", "passphrase") != first
        )

    def test_a_supplied_value_still_replaces_without_force(self, env):
        """Deliberately not refused, and the reason is the documented remedy.

        The way out of `VaultLocked` is exactly this command: re-provisioning a
        passphrase the operator already set in their KeePass client. Refusing it
        would break the one sequence the reporting design tells them to run. What
        cannot be right is minting a value nobody has encrypted anything with.
        """
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        self._provision(cfg, db_path)
        chosen = "a-passphrase-the-operator-already-set-in-keepassxc"

        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="vault",
                key="passphrase",
                value=chosen,
            )
        )
        assert (
            secrets_store.get_secret(db_path, "alice", "vault", "passphrase") == chosen
        )

    def test_the_refusal_does_not_reach_other_services(self, env):
        """`--generate` is vault-only anyway, so this is a control against the
        existence check being applied where it has no meaning."""
        from istota.cli import cmd_secret

        cfg, db_path, _mount = env
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "existing")
        cmd_secret(
            _Args(
                config=str(cfg),
                action="ensure",
                user="alice",
                service="karakeep",
                key="api_key",
                value="replacement",
            )
        )
        assert (
            secrets_store.get_secret(db_path, "alice", "karakeep", "api_key")
            == "replacement"
        )


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


class TestEnsureIsNoLongerRefusedByTheVault:
    """The 409-shaped CLI refusal went with the eligibility machinery.

    It refused a write on the claim that the next vault sync would overwrite it.
    The vault writes only its own `vault_entries` namespace now, so that claim
    is false for every typed service and the refusal would be a lie. What the
    class keeps is the control: a user who really does have a vault, so a
    reintroduced refusal would have something to fire on.
    """

    def test_a_service_the_vault_used_to_own_is_written(self, env):
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
            )
        )
        assert (
            secrets_store.get_secret(db_path, "alice", "karakeep", "api_key")
            == "typed-by-hand"
        )

    def test_the_passphrase_is_still_provisionable(self, env):
        """The provisioning path, which a refusal there would have closed."""
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
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

        # The counts themselves are the applying half's, which the change
        # landing beside this one restores over the flat `vault_entries`
        # namespace. That the line is printed at all, and carries no value, is
        # this renderer's.
        assert "alice" in out and "written" in out
        assert API_KEY_VALUE not in out
        assert BASE_URL_VALUE not in out
        assert PASSPHRASE not in out

    def test_it_parses_even_on_a_digest_a_previous_cycle_cached(
        self, env, capsys, monkeypatch
    ):
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

        # Prime the cache the way an ordinary interval cycle would. The digest
        # has not moved, so an ordinary cycle would skip the parse entirely.
        secrets_vault.sync_user(load_config(cfg), "alice")

        calls = []
        real = secrets_vault.parse_vault

        def counted(data, passphrase):
            calls.append(1)
            return real(data, passphrase)

        monkeypatch.setattr(secrets_vault, "parse_vault", counted)
        cmd_secret(_Args(config=str(cfg), action="vault-sync", user="alice"))

        # Counted rather than read off the rows: what the apply then writes is
        # the applying half's, which the change landing beside this one
        # restores. That the cache was defeated is this command's own subject.
        assert calls == [1]

    def test_a_name_cannot_forge_a_line_of_the_sync_report(self, env, capsys):
        """The service-level refusal's empty-key rendering went with the
        refusal — a skip is now one bounded name and one reason. What
        survives is the bound: every name here came out of the vault file, which
        a task in the user's own sandbox can overwrite, and the consumer is an
        operator's terminal.
        """
        from istota.cli import _print_vault_sync
        from istota.secrets_vault import (
            SKIP_DUPLICATE_NAME,
            VaultApplyResult,
            VaultSyncResult,
        )

        forged = "a\nSTATE: ok\n" + "b" * 200
        _print_vault_sync(
            VaultSyncResult(
                user_id="alice",
                outcome="ok",
                apply=VaultApplyResult(
                    created=1,
                    deleted_keys=[forged],
                    skipped=[(forged, SKIP_DUPLICATE_NAME)],
                ),
            )
        )
        out = capsys.readouterr().out

        assert "\nSTATE: ok" not in out
        assert "b" * 200 not in out
        assert SKIP_DUPLICATE_NAME in out

    def test_a_user_with_no_vault_is_reported_rather_than_skipped(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, _db_path, _mount = env
        cmd_secret(_Args(config=str(cfg), action="vault-sync", user="alice"))
        out = capsys.readouterr().out
        assert "alice" in out


class TestAnUnknownUser:
    def test_vault_sync_refuses_a_user_nobody_configured(self, env, capsys):
        """A typo'd `-u` used to read as "no vault configured", which is
        indistinguishable from a correctly spelled user with the feature off."""
        from istota.cli import cmd_secret

        cfg, _db_path, _mount = env
        with pytest.raises(SystemExit) as exc:
            cmd_secret(_Args(config=str(cfg), action="vault-sync", user="alicce"))
        assert exc.value.code == 1
        assert "alicce" in capsys.readouterr().err

    def test_vault_status_refuses_one_too(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, _db_path, _mount = env
        with pytest.raises(SystemExit) as exc:
            cmd_secret(_Args(config=str(cfg), action="vault-status", user="bob"))
        assert exc.value.code == 1


class TestVaultStatus:
    def test_an_unconfigured_user_exits_cleanly(self, env, capsys):
        from istota.cli import cmd_secret

        cfg, _db_path, _mount = env
        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out
        assert "alice" in out

    def test_it_reports_the_file_the_passphrase_and_the_names(self, env, capsys):
        """§10: the count, the names, and the skips.

        The name list is the feedback this feature has never had — a name here
        is a credential istota holds, and one the user expected and cannot see
        is an entry with a skip beside it. Values never appear, which the sweep
        below is what holds; unlike the interim version of this test it is no
        longer vacuous, because there is now something printed to sweep.
        """
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx", ntfy=True)
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out

        assert "vault.kdbx" in out and "passphrase: provisioned" in out
        assert "shared:     3 credential(s)" in out
        assert "karakeep_api_key" in out
        assert "karakeep_base_url" in out
        assert "ntfy_topic" in out
        # A scoped file says nothing about scope: the notice is for the other
        # case, and printing it always would make it noise.
        assert "unscoped" not in out
        assert API_KEY_VALUE not in out
        assert BASE_URL_VALUE not in out
        assert PASSPHRASE not in out

    def test_an_unscoped_file_says_so_with_a_count(self, env, capsys):
        """§1's notice on the surface an operator reaches for. The count is
        what makes it actionable: "all 412 of them" is a different sentence
        from "all 2 of them"."""
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_unscoped_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out

        assert "unscoped" in out
        assert "all 2 credential(s) in it are shared" in out
        assert API_KEY_VALUE not in out

    def test_a_skip_is_named_with_its_reason(self, env, capsys):
        """The other half of the feedback: a name the user expected and cannot
        see has a line saying why."""
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_colliding_vault(
            mount / "Users" / "alice" / "config" / "vault.kdbx"
        )
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out

        assert "skipped:    aws_key" in out
        assert "two entries produce the same name" in out

    def test_it_reports_the_last_cycle_the_daemon_settled(self, env, capsys):
        """The record, which is the only thing here that crosses a process.

        This command runs in a shell, so its own `_SYNC_STATE` is empty and
        always has been — the last cycle it can report on is one the scheduler
        ran, in another process and under Ansible in another systemd unit. Both
        halves are printed: the class names the condition and the sentence names
        the remedy, so printing the first alone reports a failure and withholds
        the actionable half of it.
        """
        from istota import db, secrets_vault
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)
        with db.get_db(db_path) as conn:
            db.kv_set(
                conn, "alice",
                secrets_vault.VAULT_SYNC_STATE_NAMESPACE,
                secrets_vault.VAULT_SYNC_STATE_KEY,
                secrets_vault.encode_sync_state(
                    secrets_vault.VaultLocked.__name__,
                    secrets_vault.notification_reason(
                        secrets_vault.VaultLocked.__name__
                    ),
                    now="2026-09-17T10:00:00Z",
                    previous=None,
                ),
            )

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out

        assert "VaultLocked" in out
        assert "does not match the file" in out

    def test_a_healthy_record_reports_a_sync_time_and_no_error(self, env, capsys):
        """The control, and the reason `last error` is conditional.

        A working vault must not print an error line, and `last sync` must carry
        the success stamp rather than the empty string a never-synced vault has.
        """
        from istota import db, secrets_vault
        from istota.cli import cmd_secret

        cfg, db_path, mount = _with_vault(env)
        _write_vault(mount / "Users" / "alice" / "config" / "vault.kdbx")
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", PASSPHRASE)
        with db.get_db(db_path) as conn:
            db.kv_set(
                conn, "alice",
                secrets_vault.VAULT_SYNC_STATE_NAMESPACE,
                secrets_vault.VAULT_SYNC_STATE_KEY,
                secrets_vault.encode_sync_state(
                    secrets_vault.OUTCOME_OK, "",
                    now="2026-09-17T10:00:00Z", previous=None,
                ),
            )

        cmd_secret(_Args(config=str(cfg), action="vault-status", user="alice"))
        out = capsys.readouterr().out

        assert "2026-09-17T10:00:00Z" in out
        assert "last error" not in out
        assert "last cycle" not in out

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
