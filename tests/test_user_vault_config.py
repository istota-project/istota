"""The per-user vault store, and the accessors that read it live.

`vault_path` and `vault_services` were TOML-only, and the reason was never that
TOML is where config lives — it was that `user_profiles` is the general settings
overlay and these two fields are a security control. `vault_path` selects which
file the daemon decrypts with a key it holds; `vault_services` selects which
credentials that file may overwrite and delete. Putting them on `user_profiles`
would have made them settable by whatever writes profiles.

`user_vault_config` is a table of its own for exactly that reason, and the
guards below are what make "of its own" a property rather than a comment:
`TestTheProfileTableGuard` in `tests/test_secrets_vault.py` stays green, no
skill CLI or deferred op reaches this table, and `Config` is the only place that
reads either field off a `UserConfig`.

**The web form is relative-only, and that is the boundary this file is mostly
about.** A relative path resolves under the user's own workspace, which is the
"edit it from a phone" case the feature exists for. The absolute form is the
escape hatch that puts the file somewhere no sandbox binds, it is checked
against `_sandbox_writable_roots` rather than against a user's own tree, and a
user-writable absolute path would be a new arbitrary-read primitive as the
daemon user — so it stays operator-only, refused at the write rather than at the
resolve.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from istota import db, user_vault_config as uvc
from istota.config import Config, UserConfig


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _config(tmp_path, db_path, **user_kwargs) -> Config:
    config = Config()
    config.db_path = db_path
    config.nextcloud_mount_path = str(tmp_path / "mount")
    config.users = {"alice": UserConfig(**user_kwargs)}
    return config


class TestTheTable:
    """The store itself: one row per user, both fields, nothing else."""

    def test_an_unset_user_reads_as_absent(self, db_path):
        assert uvc.get_vault_config(db_path, "alice") is None

    def test_a_written_row_reads_back(self, db_path):
        uvc.set_vault_config(
            db_path, "alice", vault_path="config/vault.kdbx",
            vault_services=["karakeep"],
        )
        row = uvc.get_vault_config(db_path, "alice")
        assert row is not None
        assert row.vault_path == "config/vault.kdbx"
        assert row.vault_services == ["karakeep"]

    def test_a_second_write_replaces_rather_than_adds(self, db_path):
        uvc.set_vault_config(db_path, "alice", vault_path="a.kdbx", vault_services=[])
        uvc.set_vault_config(db_path, "alice", vault_path="b.kdbx", vault_services=[])
        row = uvc.get_vault_config(db_path, "alice")
        assert row is not None and row.vault_path == "b.kdbx"
        with sqlite3.connect(db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM user_vault_config WHERE user_id = ?", ("alice",)
            ).fetchone()[0]
        assert n == 1

    def test_clearing_removes_the_row_rather_than_blanking_it(self, db_path):
        # An empty `vault_path` is "the feature is off for this user", and that
        # is also what no row means. Two spellings of one state is how a
        # precedence rule starts lying: a blank row would *win* over a TOML
        # value below, turning "I cleared the web field" into "the operator's
        # config.toml line is now inert" with nothing saying so.
        uvc.set_vault_config(db_path, "alice", vault_path="a.kdbx", vault_services=[])
        assert uvc.clear_vault_config(db_path, "alice") is True
        assert uvc.get_vault_config(db_path, "alice") is None
        assert uvc.clear_vault_config(db_path, "alice") is False

    def test_a_user_id_that_is_not_a_path_component_is_refused(self, db_path):
        # The value ends up joined under the workspace root by the resolver, and
        # `user_scope` already owns that rule; this is the store refusing to
        # hold a row that could never resolve.
        for bad in ("", ".", "..", "a/b"):
            with pytest.raises(ValueError):
                uvc.set_vault_config(
                    db_path, bad, vault_path="a.kdbx", vault_services=[]
                )


class TestPrecedence:
    """The DB row wins over `[users.X]`, which is the repo's existing rule."""

    def test_the_row_wins_over_toml(self, tmp_path, db_path):
        config = _config(
            tmp_path, db_path,
            vault_path="from-toml.kdbx", vault_services=["karakeep"],
        )
        uvc.set_vault_config(
            db_path, "alice", vault_path="from-db.kdbx", vault_services=[],
        )
        assert config.vault_path_for("alice") == "from-db.kdbx"
        assert config.vault_services_for("alice") == []

    def test_toml_stands_when_there_is_no_row(self, tmp_path, db_path):
        config = _config(
            tmp_path, db_path,
            vault_path="from-toml.kdbx", vault_services=["karakeep"],
        )
        assert config.vault_path_for("alice") == "from-toml.kdbx"
        assert config.vault_services_for("alice") == ["karakeep"]

    def test_a_user_in_neither_place_has_no_vault(self, tmp_path, db_path):
        config = _config(tmp_path, db_path)
        assert config.vault_path_for("nobody") == ""
        assert config.vault_services_for("nobody") == []

    def test_the_read_is_live_rather_than_load_time(self, tmp_path, db_path):
        # The point of an accessor rather than a load-time overlay: the
        # scheduler holds one `Config` for its whole life and syncs every 300s,
        # so a change made in the browser has to reach the next tick without a
        # restart.
        config = _config(tmp_path, db_path)
        assert config.vault_path_for("alice") == ""
        uvc.set_vault_config(db_path, "alice", vault_path="now.kdbx", vault_services=[])
        assert config.vault_path_for("alice") == "now.kdbx"

    def test_a_stored_list_is_passed_through_unfiltered(self, tmp_path, db_path):
        # The eligibility filter this accessor used to apply went with the
        # predicate behind it: a vault owns no typed service now, so there is no
        # ineligible name and nothing left to drop. Nothing reads the answer
        # either — the field, this reader and the stored column all leave in
        # stage 6 — so what is pinned here is that the list is inert rather than
        # silently re-filtered by something else.
        config = _config(tmp_path, db_path)
        uvc.set_vault_config(
            db_path, "alice", vault_path="v.kdbx",
            vault_services=["karakeep", "vault", "nonesuch"],
        )
        assert config.vault_services_for("alice") == [
            "karakeep", "vault", "nonesuch",
        ]


class TestTheDeploymentWideGate:
    """`any_vault_configured`, which the scheduler asks every dispatch tick.

    Shaped for that cadence rather than for readability: the obvious
    `any(config.vault_path_for(u) for u in config.users)` short-circuits on the
    first truthy value, so it is cheap exactly when somebody has a vault and
    costs one database open *per user* when nobody does — which is every
    deployment by default, on every tick, for ever.
    """

    def test_a_deployment_with_nothing_configured_says_so(self, tmp_path, db_path):
        assert _config(tmp_path, db_path).any_vault_configured() is False

    def test_a_toml_vault_turns_it_on_without_touching_the_database(
        self, tmp_path, db_path
    ):
        config = _config(tmp_path, db_path, vault_path="v.kdbx")
        # The TOML half is asked first and answers without a row, which is what
        # keeps an operator-configured deployment off the listing entirely.
        assert config.any_vault_configured() is True

    def test_a_stored_passphrase_turns_it_on(self, tmp_path, db_path, monkeypatch):
        """The database half asks for a passphrase now, not for a stored path.

        A vault is a file *and* a passphrase, and the passphrase is the half
        that cannot become true by accident — it is also what a user
        configuring a vault from the folder has, where they store no path at
        all. A stored row is not consulted: nothing writes one any more, and a
        row that predates that still reaches `vault_path_for`, so a *working*
        legacy vault has a passphrase behind it and is seen through that.
        """
        from istota import secrets_store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        config = _config(tmp_path, db_path)
        assert config.any_vault_configured() is False
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", "x" * 40)
        assert config.any_vault_configured() is True

    def test_it_costs_one_lookup_rather_than_one_per_user(
        self, tmp_path, db_path
    ):
        # The property the shape exists for, measured rather than asserted about
        # in a comment: with nobody configured — the case that cannot
        # short-circuit — the count must not scale with the user list.
        from istota import secrets_store as mod

        config = _config(tmp_path, db_path)
        config.users = {f"u{i}": UserConfig() for i in range(8)}

        calls = {"n": 0}
        real = mod.any_user_has_secret

        def counted(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        mod.any_user_has_secret = counted
        try:
            assert config.any_vault_configured() is False
        finally:
            mod.any_user_has_secret = real
        assert calls["n"] == 1

    def test_an_unreadable_store_reads_as_the_toml_answer(self, tmp_path):
        # No database at all. Degrading to the TOML half is the safe direction:
        # the other one switches a configured vault off, which is a deployment
        # that silently stops applying a file the user is still editing.
        config = Config()
        config.db_path = tmp_path / "absent.db"
        config.users = {"alice": UserConfig(vault_path="v.kdbx")}
        assert config.any_vault_configured() is True


class TestNothingDownstreamOfATaskWritesIt:
    """The property the separate table exists for.

    `user_profiles` is reachable from the settings UI and, in principle, from
    anything that grows a profile-writing verb. This table is written by the web
    endpoint and the operator CLI and by nothing else, and a guard naming its
    own subjects would only ever cover the ones its author thought of — so both
    halves below are sweeps.
    """

    # An *import* rather than any mention of the name: two modules now carry a
    # comment saying why they read through the accessor instead, and a
    # substring sweep would count those as writers. The import is what gives a
    # module the ability to write, and it is also what a table name in a raw
    # `conn.execute` would not need — so the SQL sweep below is the other half.
    IMPORT = re.compile(r"^\s*(?:from\s+\.[\w.]*\s+import\s+[^\n]*\buser_vault_config\b"
                        r"|import\s+istota\.user_vault_config"
                        r"|from\s+istota\s+import\s+[^\n]*\buser_vault_config\b)",
                        re.M)

    def _importers(self) -> list[str]:
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "istota"
        return sorted(
            str(p.relative_to(src))
            for p in src.rglob("*.py")
            if p.name != "user_vault_config.py"
            and self.IMPORT.search(p.read_text(encoding="utf-8", errors="replace"))
        )

    def test_no_skill_imports_the_module(self):
        offenders = [p for p in self._importers() if p.startswith("skills/")]
        assert offenders == [], (
            "a skill CLI runs host-side with the daemon's filesystem view and is "
            "reachable from a prompt-injected task; it may not write the row "
            f"that selects which file the daemon decrypts: {offenders}"
        )

    def test_no_deferred_op_reaches_the_table(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "istota"
        text = (src / "scheduler_deferred.py").read_text(encoding="utf-8")
        assert "user_vault_config" not in text, (
            "a deferred op is written by a sandboxed task and replayed by the "
            "daemon; it may not reach this table"
        )

    def test_the_importers_are_the_two_this_table_admits(self):
        # `config.py` reads; `cli.py` clears. A further name here is a claim
        # that something else may select which file the daemon decrypts, and
        # wants saying out loud rather than passing.
        #
        # `web_app.py` was the third and is gone: the settings form writes a
        # *filename* into the reserved `_vault_file` KV namespace now, so this
        # table has no writer left at all. The table itself goes in the stage
        # that retires `vault_services`, which is what the remaining readers
        # are still for.
        assert self._importers() == ["cli.py", "config.py"], self._importers()

    def test_nothing_outside_the_module_names_the_table_in_sql(self):
        # The import guard above is defeated by a raw `conn.execute` against the
        # table name, which needs no import at all.
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "istota"
        offenders = sorted(
            str(p.relative_to(src))
            for p in src.rglob("*.py")
            if p.name != "user_vault_config.py"
            and re.search(
                r"(?:FROM|INTO|UPDATE|TABLE)\s+user_vault_config",
                p.read_text(encoding="utf-8", errors="replace"),
                re.I,
            )
        )
        assert offenders == [], offenders


class TestConfigIsTheOnlyReaderOfTheRawFields:
    """One mechanism, not two.

    Every consumer used to read `getattr(user, "vault_path", "")` straight off
    the `UserConfig`, which is now the *fallback* half of the answer rather than
    the answer. A reader left on the attribute would silently ignore a row the
    user wrote in the browser — and it would do so by reading a real value, so
    nothing would look broken.
    """

    def test_no_module_outside_config_reads_the_attribute(self):
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "istota"
        # `vault_path=` / `vault_services=` as a keyword is fine (the store and
        # the dataclass), and so is the string inside a docstring. What is
        # refused is reading the field off an object.
        pattern = re.compile(
            r"""getattr\(\s*[^,]+,\s*["']vault_(?:path|services)["']"""
            r"""|\.vault_(?:path|services)\b"""
        )
        offenders = []
        for path in src.rglob("*.py"):
            if path.name in ("config.py", "user_vault_config.py"):
                continue
            for n, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(src)}:{n}: {line.strip()}")
        assert offenders == [], (
            "read the merged value through `Config.vault_path_for` / "
            "`Config.vault_services_for`; the attribute is TOML only:\n"
            + "\n".join(offenders)
        )
