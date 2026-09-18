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

    def test_an_ineligible_service_is_dropped_on_the_way_out(self, tmp_path, db_path):
        # `_validate_vault_services` filters the TOML list at load time and
        # cannot see a row written afterwards, so the accessor applies the same
        # predicate — `secrets_vault.service_refusal`, asked rather than
        # restated. Without this a row could name `vault` itself and the
        # eligibility rule would hold on one surface only.
        config = _config(tmp_path, db_path)
        uvc.set_vault_config(
            db_path, "alice", vault_path="v.kdbx",
            vault_services=["karakeep", "vault", "nonesuch"],
        )
        assert config.vault_services_for("alice") == ["karakeep"]


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

    def test_the_importers_are_the_three_this_table_admits(self):
        # `config.py` reads; `web_app.py` and `cli.py` write. A fourth name here
        # is a claim that something else may select which file the daemon
        # decrypts, and wants saying out loud rather than passing.
        assert self._importers() == ["cli.py", "config.py", "web_app.py"], (
            self._importers()
        )

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
