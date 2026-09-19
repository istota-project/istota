"""`vault_services` and `user_vault_config` are gone, and stay gone.

The credential vault used to be able to own a *typed* service. A user laid the
file out as `istota/<service>/<key>` against `secret_schema`'s field names, an
operator (or the user, through a web form) listed which services the file owned,
and the sync overwrote and deleted those services' credentials. That is retired:
the vault writes one flat `vault_entries` namespace of its own, so there is no
service to own, no eligibility question to ask, and no second place to say yes.

Removing it took a field, a validator, two `Config` readers, a module, a table,
a CLI flag, a web payload half, a Svelte branch, two refusals, both deployment
generators and a compose passthrough. This file is what keeps any of it from
coming back by halves.

**Why a sweep rather than a list of call sites.** The names had reached eight
directories and four languages, and the failure this guards against is not
somebody re-adding the feature — it is somebody re-adding one *mention* of it: a
comment naming a column that no longer exists, a `.env.example` line for a
variable nothing reads, a docs table row for a key the loader ignores. Each of
those is a reader sent somewhere real that does not exist, and none of them
would fail any other test.

**Two exemptions, named rather than pattern-matched**, because a pattern would
grow to fit whatever was added next:

- `src/istota/db.py` holds `DROP TABLE IF EXISTS user_vault_config`, which is
  the one place the name must survive in order for it to stop existing
  everywhere else. The exemption is a positive assertion rather than a skip: the
  file must carry the name exactly once, on that statement. That is what forced
  `_migrate_drop_retired_vault_table` to be named around the table rather than
  after it.
- `CHANGELOG.md` holds the upgrade note. One line, asserted by a count *and* by
  a fragment of its own text — the count is what a second bullet fails, and the
  fragment is what a bullet that merely names the retired key without saying
  what replaced it fails.

**Two trees are outside the sweep and neither is an oversight.** `tests/` would
have to exempt this file, which names all three spellings in its own prose, and
the cost of a stale mention there is a comment rather than an instruction to an
operator. `.claude/rules/` carries two surviving mentions in `sandbox.md`, both
correct historical references explaining what the removal removed — and rules
prose is read by whoever is changing the code rather than by whoever is running
it. Widen the sweep the day either claim stops holding; do not widen it and then
exempt the file that fails.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Every spelling the removed surface reached. `USER_VAULT_SERVICES` is the
#: Docker variable, `vault_services` the TOML key and the dataclass field, and
#: `user_vault_config` the table and its module. Substring matching rather than
#: word boundaries, deliberately: `_vault_services_value` and
#: `test_user_vault_config.py` are exactly the kind of survivor this is for.
RETIRED_NAMES = ("vault_services", "USER_VAULT_SERVICES", "user_vault_config")

#: The trees the names reached. `schema.sql` and `CHANGELOG.md` are files rather
#: than directories and are swept the same way; `web/vite-mock-api.ts` is
#: outside `web/src/` and is included by name, since it models the very endpoint
#: whose payload this change rewrote.
SWEPT = (
    "src",
    "web/src",
    "web/vite-mock-api.ts",
    "deploy",
    "docker",
    "config",
    "docs",
    "schema.sql",
    "CHANGELOG.md",
)

#: Files the sweep skips, each with its own assertion below.
EXEMPT = ("src/istota/db.py", "CHANGELOG.md")

#: Binary and generated things a text sweep must not open.
_SKIP_DIRS = {"__pycache__", "node_modules", ".svelte-kit", "build", "dist"}
_SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".ico", ".woff", ".woff2", ".webp"}


def _swept_files() -> list[Path]:
    out: list[Path] = []
    for entry in SWEPT:
        root = REPO / entry
        if root.is_file():
            out.append(root)
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            out.append(path)
    return out


def _hits(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return [
        (i, line)
        for i, line in enumerate(text.splitlines(), 1)
        if any(name in line for name in RETIRED_NAMES)
    ]


class TestTheNamesAreGone:
    """The sweep, and the two exemptions that make it exact."""

    def test_every_swept_entry_still_exists(self):
        """A `SWEPT` entry that stops existing leaves the sweep in silence.

        `_swept_files` joins the name onto `REPO`; a missing path is neither
        `is_file()` nor yields anything from `rglob`, so it contributes zero
        files and no error. Two entries are incidentally covered by their own
        exemption tests, which fail on an empty `_hits` — `CHANGELOG.md` and
        `src/istota/db.py`. `schema.sql` is covered by nothing else, so moving
        or renaming it would drop it out of the guard while the guard stayed
        green. This is the assertion that closes that.
        """
        missing = [e for e in SWEPT if not (REPO / e).exists()]
        assert missing == []

    def test_the_sweep_has_something_to_sweep(self):
        """The control. A sweep over an empty file list passes vacuously, and
        `rglob` over a renamed directory returns exactly that."""
        files = _swept_files()
        assert len(files) > 500, len(files)
        rels = {p.relative_to(REPO).as_posix() for p in files}
        roots = {r.split("/")[0] for r in rels}
        assert {"src", "web", "deploy", "docker", "config", "docs"} <= roots
        # The two single-file entries, which no directory root covers.
        assert {"schema.sql", "CHANGELOG.md"} <= rels

    def test_no_swept_file_names_the_retired_surface(self):
        found: list[str] = []
        for path in _swept_files():
            rel = path.relative_to(REPO).as_posix()
            if rel in EXEMPT:
                continue
            found += [f"{rel}:{n}: {line.strip()}" for n, line in _hits(path)]
        assert not found, "\n".join(found)

    def test_db_names_the_table_exactly_once_and_only_to_drop_it(self):
        hits = _hits(REPO / "src" / "istota" / "db.py")
        assert len(hits) == 1, hits
        _line_no, line = hits[0]
        assert line.strip() == '''conn.execute("DROP TABLE IF EXISTS user_vault_config")'''

    def test_the_changelog_names_it_only_in_the_upgrade_note(self):
        hits = _hits(REPO / "CHANGELOG.md")
        assert len(hits) == 1, hits
        _line_no, line = hits[0]
        assert line.startswith("- **Upgrade note:**"), line
        # Discriminating rather than `"vault" in line`, which every member of
        # `RETIRED_NAMES` satisfies by construction and which therefore could
        # not fail. The note's job is to say what replaced the thing it names,
        # so that is what is asserted.
        assert "istota/vault/" in line, line


class TestTheDroppedTable:
    """The migration, against both states a real deployment is in.

    `init_db` runs `_run_migrations` and *then* `executescript`s `schema.sql`,
    which is what makes the drop one-way: the schema no longer declares the
    table, so nothing recreates what the migration removed. Both cases below go
    through `init_db` rather than calling the migration directly, because the
    ordering is the half that could regress.
    """

    def _tables(self, db_path: Path) -> set[str]:
        with sqlite3.connect(db_path) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

    def test_a_database_that_has_the_table_loses_it(self, tmp_path):
        from istota import db

        db_path = tmp_path / "upgraded.db"
        # The pre-removal shape, written by hand: `schema.sql` no longer
        # declares it, so there is nothing in the tree to create it from.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE user_vault_config (
                    user_id        TEXT PRIMARY KEY,
                    vault_path     TEXT NOT NULL DEFAULT '',
                    vault_services TEXT NOT NULL DEFAULT '[]',
                    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_by     TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "INSERT INTO user_vault_config (user_id, vault_path) "
                "VALUES ('alice', 'config/vault.kdbx')"
            )
        assert "user_vault_config" in self._tables(db_path)

        db.init_db(db_path)

        tables = self._tables(db_path)
        assert "user_vault_config" not in tables
        # The control: `init_db` demonstrably ran, so the absence above is a
        # drop rather than a database nobody opened.
        assert "user_profiles" in tables

    def test_a_database_that_never_had_it_is_untouched(self, tmp_path):
        from istota import db

        db_path = tmp_path / "fresh.db"
        db.init_db(db_path)
        assert "user_vault_config" not in self._tables(db_path)

        # Idempotent: the statement runs on every `init_db`, so the second boot
        # of a migrated deployment is this case.
        db.init_db(db_path)
        assert "user_vault_config" not in self._tables(db_path)


class TestConfigIsTheOnlyReaderOfTheRawField:
    """`user.vault_path` is read through `Config.vault_path_for` and nowhere else.

    Nothing outranks the field any more — the table that used to is gone — so
    this is no longer about a stale half being read. It is about the field being
    *one of four* rules. `storage.vault_location_for` asks the accessor first and
    then falls through to the stored filename, the only file in the folder, and a
    refusal, so a consumer that reads the attribute sees a folder vault as no
    vault at all: the failure class that looks like nothing is wrong.

    An AST walk rather than a grep, so a mention in a docstring or a comment is
    not a hit and an attribute access is.
    """

    def _readers(self, *, exempt_config: bool = True) -> list[str]:
        out: list[str] = []
        for path in sorted((REPO / "src").rglob("*.py")):
            if exempt_config and path.name == "config.py" and path.parent.name == "istota":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "vault_path":
                    out.append(f"{path.relative_to(REPO)}:{node.lineno}")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "vault_path"
                ):
                    out.append(f"{path.relative_to(REPO)}:{node.lineno}")
        return out

    def test_the_walk_finds_config_itself(self):
        """The control, and it drives `_readers` rather than re-implementing it.

        An inline re-walk would pass against a broken `rglob` or a broken skip
        in `_readers`, leaving the assertion below vacuous while the control
        stayed green — which is this repository's most-logged failure mode.
        """
        assert self._readers(exempt_config=False), (
            "the walk found no reader at all, so the assertion below is vacuous"
        )

    def test_it_matches_a_plain_attribute_read_and_not_only_getattr(self, tmp_path):
        """The `ast.Attribute` branch is matched by nothing in `src/` today.

        Every read in `config.py` is the `getattr(user, "vault_path", "")`
        form, so the branch that would catch the *more likely* future spelling
        — a plain `user.vault_path` — has never fired. Driven against a
        synthetic module instead of waiting for one to appear.
        """
        module = tmp_path / "leaker.py"
        module.write_text("def f(user):\n    return user.vault_path\n")
        tree = ast.parse(module.read_text())
        hits = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "vault_path"
        ]
        assert len(hits) == 1

    def test_no_module_outside_config_reads_the_attribute(self):
        assert self._readers() == []


class TestAStaleLineInARenderedConfigIsInert:
    """A `config.toml` an older generator wrote still loads.

    Both generators have stopped writing the key, but a deployment's rendered
    file is only rewritten on the next converge or boot — and a rollback puts an
    old one back. What must not happen is a load failure, which on this codebase
    is every istota process at once: the scheduler, the web app, the webhook
    receiver and every host-side skill CLI the proxy spawns per call.

    **The spec said this would also produce an unknown-key warning and it does
    not**, which is recorded here rather than left to be discovered. `users` is
    in `config._HANDWRITTEN`, so `apply_section` neither maps nor reports it and
    `_parse_user_data` builds the block with `.get()` — an unrecognised key in a
    `[users.<id>]` block has always been dropped in silence, for every key, and
    making this one an exception would mean either a narrow retired-key list
    (which reintroduces the name the sweep above forbids) or unknown-key
    reporting for the whole section, which is its own change with its own noise.
    """

    def _load(self, tmp_path, text: str):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(text)
        return load_config(path)

    def test_it_loads_and_the_path_still_works(self, tmp_path):
        config = self._load(tmp_path, """
            [users.alice]
            display_name = "Alice"
            vault_path = "istota/vault/credentials.kdbx"
            vault_services = ["karakeep", "ntfy"]
        """)
        assert config.users["alice"].display_name == "Alice"
        assert config.vault_path_for("alice") == "istota/vault/credentials.kdbx"

    def test_the_field_is_not_resurrected_as_an_attribute(self, tmp_path):
        config = self._load(tmp_path, """
            [users.alice]
            vault_services = ["karakeep"]
        """)
        assert not hasattr(config.users["alice"], "vault_services")


class TestTheModuleIsGone:
    def test_it_cannot_be_imported(self):
        with pytest.raises(ImportError):
            __import__("istota.user_vault_config")

    def test_nothing_left_it_in_sys_modules(self):
        """A sibling test importing it would make the assertion above pass on a
        cached module object rather than on the file being absent."""
        assert "istota.user_vault_config" not in sys.modules
        assert not (REPO / "src" / "istota" / "user_vault_config.py").exists()
