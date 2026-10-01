"""The group primitive (groups spec Stage 1; multiplayer Stage 21).

A group is a named set of istota users with a store of its own. This file holds
the three tables, the `db` helpers, the id rule and the operator CLI. The rules
that matter are the ones a later stage builds an authorization gate on:

- membership is a history, never a set: ending one deletes no row, and a
  re-join is a second row;
- `is_group_member` answers for a *current* membership of a *live* group, and
  is False for anything it cannot establish;
- an archived group is out of every membership answer while its rows stay
  readable to the operator (the umbrella's answer to the groups spec's open
  question 3);
- `kind` and `role` are recorded and decide nothing.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from istota import cli, db

SRC = Path(__file__).resolve().parent.parent / "src" / "istota"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _rows(conn, sql, *params):
    return [tuple(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestTheMigration:
    TABLES = ("groups", "group_members", "group_kv")

    def test_the_migration_builds_what_schema_sql_declares(self, tmp_path):
        """Both copies of the DDL, executed: `_run_migrations` on a database
        missing the three tables, against `schema.sql` alone on an empty one.
        `init_db` runs the migrations first, so on any real install the
        migration's copy is the one that executes, and `schema.sql`'s only
        describes it; this is what holds the two equal."""
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        for table in self.TABLES:
            raw.execute(f"DROP TABLE {table}")
        raw.execute("DELETE FROM _migration_state WHERE name = 'groups_v1'")
        raw.commit()
        raw.row_factory = sqlite3.Row
        db._run_migrations(raw)
        raw.commit()
        marker = raw.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'groups_v1'"
        ).fetchone()
        raw.close()
        assert marker is not None
        declared = tmp_path / "declared.db"
        raw = sqlite3.connect(declared)
        raw.executescript((SRC.parent.parent / "schema.sql").read_text())
        raw.close()
        with db.get_db(declared) as fresh, db.get_db(old) as upgraded:
            for table in self.TABLES:
                a = [tuple(r) for r in fresh.execute(f"PRAGMA table_info({table})")]
                b = [tuple(r) for r in upgraded.execute(f"PRAGMA table_info({table})")]
                assert a and a == b, table
                ia = sorted(tuple(r)[1:4] for r in fresh.execute(
                    f"PRAGMA index_list({table})"))
                ib = sorted(tuple(r)[1:4] for r in upgraded.execute(
                    f"PRAGMA index_list({table})"))
                assert ia == ib, table

    def test_schema_sql_and_the_migration_carry_the_same_ddl(self):
        """The same property as text: every migration statement appears in
        `schema.sql`, comments and whitespace aside."""
        schema = (SRC.parent.parent / "schema.sql").read_text()
        squash = lambda s: re.sub(r"--[^\n]*", "", s)  # noqa: E731
        normal = lambda s: " ".join(squash(s).split()).rstrip(";")  # noqa: E731
        flat = " ".join(squash(schema).split())
        for statement in db._GROUPS_DDL:
            assert normal(statement) in flat, statement.split("(")[0]


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------


class TestTheId:
    @pytest.mark.parametrize("group_id", [
        "", ".", "..", "a/b", "/abs", "x" * 200, "Family", "a", " fam",
        "fam ", "fam\0", "-fam", ".fam", "fa m", None, 7,
    ])
    def test_an_unusable_id_is_refused(self, conn, group_id):
        assert not db.is_valid_group_id(group_id)
        with pytest.raises(ValueError):
            db.create_group(conn, group_id, kind="family", display_name="F",
                            created_by="op")
        assert _rows(conn, "SELECT * FROM groups") == []

    @pytest.mark.parametrize("group_id", ["family", "ops-team", "a1", "the.band",
                                          "x" * 64])
    def test_a_plain_id_is_accepted(self, group_id):
        assert db.is_valid_group_id(group_id)

    def test_the_lexical_rule_is_user_scopes_own(self, monkeypatch):
        """Reused, not a fourth copy: a refusal from `is_scopable_user_id` is a
        refusal here whatever the charset says."""
        monkeypatch.setattr(db, "is_scopable_user_id", lambda _v: False)
        assert not db.is_valid_group_id("family")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class TestGroups:
    def test_create_get_list(self, conn):
        db.create_group(conn, "family", kind="family", display_name="The Family",
                        created_by="op")
        group = db.get_group(conn, "family")
        assert group["group_id"] == "family"
        assert group["kind"] == "family"
        assert group["display_name"] == "The Family"
        assert group["created_by"] == "op"
        assert group["archived_at"] is None
        assert [g["group_id"] for g in db.list_groups(conn)] == ["family"]

    def test_a_duplicate_is_refused_and_changes_nothing(self, conn):
        db.create_group(conn, "family", kind="family", display_name="One",
                        created_by="op")
        with pytest.raises(ValueError):
            db.create_group(conn, "family", kind="team", display_name="Two",
                            created_by="op")
        assert db.get_group(conn, "family")["display_name"] == "One"

    def test_an_archived_id_cannot_be_reused(self, conn):
        db.create_group(conn, "family", kind="family", display_name="One",
                        created_by="op")
        assert db.archive_group(conn, "family")
        with pytest.raises(ValueError):
            db.create_group(conn, "family", kind="family", display_name="Two",
                            created_by="op")

    def test_archive_hides_the_group_and_keeps_the_row(self, conn):
        db.create_group(conn, "family", kind="family", display_name="F",
                        created_by="op")
        assert db.archive_group(conn, "family", at="2026-09-30 10:00:00")
        assert db.get_group(conn, "family") is None
        assert db.get_group(conn, "family", include_archived=True)[
            "archived_at"] == "2026-09-30 10:00:00"
        assert db.list_groups(conn) == []
        assert [g["group_id"] for g in db.list_groups(conn, include_archived=True)] \
            == ["family"]
        assert not db.archive_group(conn, "family")
        assert not db.archive_group(conn, "nobody")

    def test_get_group_is_none_for_an_unknown_id(self, conn):
        assert db.get_group(conn, "nobody") is None
        assert db.get_group(conn, "") is None


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@pytest.fixture
def family(conn):
    db.create_group(conn, "family", kind="family", display_name="F", created_by="op")
    return "family"


class TestMembership:
    def test_add_is_idempotent_on_a_current_membership(self, conn, family):
        db.add_group_member(conn, family, "alice", added_by="op")
        db.add_group_member(conn, family, "alice", role="owner", added_by="op")
        assert _rows(conn, "SELECT user_id, role FROM group_members") == [
            ("alice", "member")]
        assert db.is_group_member(conn, family, "alice")

    def test_two_open_rows_are_refused_by_the_schema(self, conn, family):
        """Not only by `add_group_member`'s logic: a raw second insert of an
        open membership is a constraint violation."""
        db.add_group_member(conn, family, "alice", added_by="op")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO group_members (group_id, user_id, added_by) "
                "VALUES ('family', 'alice', 'op')"
            )

    def test_ending_deletes_no_row(self, conn, family):
        db.add_group_member(conn, family, "alice", added_by="op")
        assert db.end_group_membership(conn, family, "alice", ended_by="op2",
                                       at="2026-09-30 11:00:00")
        rows = _rows(conn, "SELECT user_id, ended_at, ended_by FROM group_members")
        assert rows == [("alice", "2026-09-30 11:00:00", "op2")]
        assert not db.is_group_member(conn, family, "alice")
        assert db.list_group_members(conn, family) == []
        assert not db.end_group_membership(conn, family, "alice", ended_by="op")

    def test_rejoining_is_a_second_row_and_a_readable_history(self, conn, family):
        db.add_group_member(conn, family, "alice", added_by="op")
        db.end_group_membership(conn, family, "alice", ended_by="op")
        db.add_group_member(conn, family, "alice", role="owner", added_by="op")
        history = db.group_membership_history(conn, family)
        assert [(h["user_id"], h["role"], h["ended_at"] is None) for h in history] \
            == [("alice", "member", False), ("alice", "owner", True)]
        assert db.is_group_member(conn, family, "alice")
        assert db.list_group_members(conn, family) == ["alice"]

    def test_members_and_groups_are_current_and_sorted(self, conn, family):
        db.create_group(conn, "band", kind="band", display_name="B", created_by="op")
        for user in ("carol", "alice", "bob"):
            db.add_group_member(conn, family, user, added_by="op")
        db.add_group_member(conn, "band", "alice", added_by="op")
        db.end_group_membership(conn, family, "bob", ended_by="op")
        assert db.list_group_members(conn, family) == ["alice", "carol"]
        assert db.list_user_groups(conn, "alice") == ["band", "family"]
        assert db.list_user_groups(conn, "bob") == []

    def test_the_gate_is_false_for_an_ended_archived_or_unknown_group(
        self, conn, family,
    ):
        db.add_group_member(conn, family, "alice", added_by="op")
        db.add_group_member(conn, family, "bob", added_by="op")
        db.end_group_membership(conn, family, "bob", ended_by="op")
        assert db.is_group_member(conn, family, "alice")
        assert not db.is_group_member(conn, family, "bob")
        assert not db.is_group_member(conn, "nobody", "alice")
        assert not db.is_group_member(conn, family, "")
        assert not db.is_group_member(conn, "", "alice")
        assert not db.is_group_member(conn, family, None)
        db.archive_group(conn, family)
        assert not db.is_group_member(conn, family, "alice")
        assert db.list_user_groups(conn, "alice") == []

    def test_a_member_cannot_be_added_to_an_unknown_or_archived_group(
        self, conn, family,
    ):
        with pytest.raises(ValueError):
            db.add_group_member(conn, "nobody", "alice", added_by="op")
        db.archive_group(conn, family)
        with pytest.raises(ValueError):
            db.add_group_member(conn, family, "alice", added_by="op")
        assert _rows(conn, "SELECT * FROM group_members") == []

    @pytest.mark.parametrize("role", ["admin", "", "Owner"])
    def test_an_unknown_role_is_refused(self, conn, family, role):
        with pytest.raises(ValueError):
            db.add_group_member(conn, family, "alice", role=role, added_by="op")

    def test_an_unscopable_user_id_is_refused(self, conn, family):
        with pytest.raises(ValueError):
            db.add_group_member(conn, family, "../alice", added_by="op")


# ---------------------------------------------------------------------------
# group_kv
# ---------------------------------------------------------------------------


class TestGroupKv:
    def test_round_trip_and_scope(self, conn, family):
        db.create_group(conn, "band", kind="band", display_name="B", created_by="op")
        db.group_kv_set(conn, family, "ns", "k", '"one"', "alice")
        db.group_kv_set(conn, "band", "ns", "k", '"other"', "bob")
        got = db.group_kv_get(conn, family, "ns", "k")
        assert got["value"] == '"one"' and got["written_by"] == "alice"
        db.group_kv_set(conn, family, "ns", "k", '"two"', "carol")
        assert db.group_kv_get(conn, family, "ns", "k")["written_by"] == "carol"
        db.group_kv_set(conn, family, "ns", "a", "1", "alice")
        db.group_kv_set(conn, family, "other", "z", "1", "alice")
        assert [e["key"] for e in db.group_kv_list(conn, family, "ns")] == ["a", "k"]
        assert db.group_kv_namespaces(conn, family) == ["ns", "other"]
        assert db.group_kv_get(conn, "band", "ns", "k")["value"] == '"other"'
        assert db.group_kv_delete(conn, family, "ns", "k")
        assert not db.group_kv_delete(conn, family, "ns", "k")
        assert db.group_kv_get(conn, family, "ns", "k") is None
        assert _rows(conn, "SELECT COUNT(*) FROM shared_kv") == [(0,)]


# ---------------------------------------------------------------------------
# Operator CLI
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update({"config": None, "verbose": False})
        self.__dict__.update(kwargs)


@pytest.fixture
def cfg(tmp_path, db_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
        '[users.alice]\ndisplay_name = "Alice"\n'
        '[users.bob]\ndisplay_name = "Bob"\n'
    )
    return str(path)


def _run(capsys, fn, **kwargs):
    fn(_Args(**kwargs))
    return capsys.readouterr()


class TestTheCli:
    def test_create_add_show_remove(self, cfg, db_path, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name="The Family")
        _run(capsys, cli.cmd_group_add_member, config=cfg, group_id="family",
             user_id="alice", role="owner")
        _run(capsys, cli.cmd_group_add_member, config=cfg, group_id="family",
             user_id="bob", role="member")
        shown = json.loads(_run(capsys, cli.cmd_group_show, config=cfg,
                                group_id="family").out)
        assert shown["display_name"] == "The Family"
        assert shown["kind"] == "family"
        assert shown["members"] == ["alice", "bob"]

        out = _run(capsys, cli.cmd_group_remove_member, config=cfg,
                   group_id="family", user_id="bob").out
        history = json.loads(out)["history"]
        assert [(h["user_id"], h["ended_at"] is not None) for h in history] == [
            ("alice", False), ("bob", True)]
        with db.get_db(db_path) as conn:
            assert _rows(conn, "SELECT COUNT(*) FROM group_members") == [(2,)]
            assert db.list_group_members(conn, "family") == ["alice"]

    @pytest.mark.parametrize("group_id", ["Family", "..", "a/b", ""])
    def test_create_refuses_an_unusable_id(self, cfg, db_path, capsys, group_id):
        with pytest.raises(SystemExit) as exc:
            _run(capsys, cli.cmd_group_create, config=cfg, group_id=group_id,
                 kind="group", name=None)
        assert exc.value.code == 1
        with db.get_db(db_path) as conn:
            assert db.list_groups(conn, include_archived=True) == []

    def test_create_refuses_a_duplicate(self, cfg, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        with pytest.raises(SystemExit) as exc:
            _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
                 kind="team", name=None)
        assert exc.value.code == 1

    def test_add_member_refuses_an_unconfigured_user(self, cfg, db_path, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        with pytest.raises(SystemExit) as exc:
            _run(capsys, cli.cmd_group_add_member, config=cfg, group_id="family",
                 user_id="alcie", role="member")
        assert exc.value.code == 1
        with db.get_db(db_path) as conn:
            assert db.group_membership_history(conn, "family") == []

    def test_add_member_refuses_an_archived_group(self, cfg, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        _run(capsys, cli.cmd_group_archive, config=cfg, group_id="family")
        with pytest.raises(SystemExit):
            _run(capsys, cli.cmd_group_add_member, config=cfg, group_id="family",
                 user_id="alice", role="member")

    def test_remove_member_without_a_membership_fails(self, cfg, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        with pytest.raises(SystemExit):
            _run(capsys, cli.cmd_group_remove_member, config=cfg,
                 group_id="family", user_id="alice")

    def test_list_and_archive(self, cfg, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name="F")
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="band",
             kind="band", name="B")
        _run(capsys, cli.cmd_group_archive, config=cfg, group_id="band")
        listed = _run(capsys, cli.cmd_group_list, config=cfg, all=False).out
        assert "family" in listed and "band" not in listed
        listed = _run(capsys, cli.cmd_group_list, config=cfg, all=True).out
        assert "family" in listed and "band" in listed
        with pytest.raises(SystemExit):
            _run(capsys, cli.cmd_group_archive, config=cfg, group_id="band")

    def test_show_an_archived_group_says_so(self, cfg, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        _run(capsys, cli.cmd_group_archive, config=cfg, group_id="family")
        shown = json.loads(_run(capsys, cli.cmd_group_show, config=cfg,
                                group_id="family").out)
        assert shown["archived_at"] is not None

    def test_show_an_unknown_group_fails(self, cfg, capsys):
        with pytest.raises(SystemExit):
            _run(capsys, cli.cmd_group_show, config=cfg, group_id="nobody")

    def test_kv_reads_survive_archiving(self, cfg, db_path, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        with db.get_db(db_path) as conn:
            db.group_kv_set(conn, "family", "ns", "k", json.dumps({"a": 1}), "alice")
        _run(capsys, cli.cmd_group_archive, config=cfg, group_id="family")
        got = json.loads(_run(capsys, cli.cmd_group_kv_get, config=cfg,
                              group_id="family", namespace="ns", key="k").out)
        assert got == {"status": "ok", "value": {"a": 1}, "written_by": "alice"}
        listed = json.loads(_run(capsys, cli.cmd_group_kv_list, config=cfg,
                                 group_id="family", namespace="ns").out)
        assert [e["key"] for e in listed["entries"]] == ["k"]
        missing = json.loads(_run(capsys, cli.cmd_group_kv_get, config=cfg,
                                  group_id="family", namespace="ns", key="x").out)
        assert missing == {"status": "not_found"}

    def test_kv_reads_show_a_value_that_is_not_json(self, cfg, db_path, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        with db.get_db(db_path) as conn:
            db.group_kv_set(conn, "family", "ns", "a", "plain text", "alice")
            db.group_kv_set(conn, "family", "ns", "b", "2", "alice")
        got = json.loads(_run(capsys, cli.cmd_group_kv_get, config=cfg,
                              group_id="family", namespace="ns", key="a").out)
        assert got["value"] == "plain text"
        listed = json.loads(_run(capsys, cli.cmd_group_kv_list, config=cfg,
                                 group_id="family", namespace="ns").out)
        assert [e["value"] for e in listed["entries"]] == ["plain text", 2]

    def test_a_repeated_add_says_nothing_changed(self, cfg, db_path, capsys):
        _run(capsys, cli.cmd_group_create, config=cfg, group_id="family",
             kind="family", name=None)
        _run(capsys, cli.cmd_group_add_member, config=cfg, group_id="family",
             user_id="alice", role="member")
        out = _run(capsys, cli.cmd_group_add_member, config=cfg, group_id="family",
                   user_id="alice", role="owner").out
        assert "already a member" in out
        with db.get_db(db_path) as conn:
            assert [h["role"] for h in db.group_membership_history(conn, "family")] \
                == ["member"]

    def test_the_parser_reaches_every_verb(self, cfg, monkeypatch, capsys):
        """Through `main`, so the subparser and the dispatch table agree."""
        for argv in (
            ["group", "create", "family", "--kind", "family", "--name", "F"],
            ["group", "add-member", "family", "alice", "--role", "owner"],
            ["group", "list", "--all"],
            ["group", "show", "family"],
            ["group", "kv-list", "family", "ns"],
            ["group", "kv-get", "family", "ns", "k"],
            ["group", "remove-member", "family", "alice"],
            ["group", "archive", "family"],
        ):
            monkeypatch.setattr("sys.argv", ["istota", "-c", cfg, *argv])
            cli.main()
        assert "family" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `kind` and `role` decide nothing (D1, D2)
# ---------------------------------------------------------------------------


#: The helpers that hand a group's `kind` or a membership's `role` to a caller.
_GROUP_READERS = {"get_group", "list_groups", "group_membership_history"}
_GROUP_TABLE_SQL = re.compile(r"(?is)\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:groups|group_members)\b")
_DECIDING_SQL = {
    "kind": re.compile(
        r"(?is)\bgroups\b[^;]*?\b(?:WHERE|AND|OR|ORDER\s+BY|GROUP\s+BY|CASE|ON)\b[^;]*\bkind\b"
    ),
    "role": re.compile(
        r"(?is)\bgroup_members\b[^;]*?\b(?:WHERE|AND|OR|ORDER\s+BY|GROUP\s+BY|CASE|ON)\b[^;]*\brole\b"
    ),
}


def _field_reads(tree: ast.AST, field: str):
    """Every `<x>["field"]`, `<x>.get("field")` and `<x>.field`, with the
    receiver's source text."""
    for node in ast.walk(tree):
        receiver = None
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value == field:
            receiver = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args \
                and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == field:
            receiver = node.func.value
        elif isinstance(node, ast.Attribute) and node.attr == field:
            receiver = node.value
        if receiver is not None:
            yield node, ast.unparse(receiver)


def _deciding_nodes(tree: ast.AST) -> set[int]:
    """Ids of every node inside something that decides: a condition, a
    comparison, a match subject, or the index of a lookup (a dispatch table)."""
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            roots.append(node.test)
        elif isinstance(node, ast.comprehension):
            roots.extend(node.ifs)
        elif isinstance(node, ast.Match):
            roots.append(node.subject)
        elif isinstance(node, (ast.Compare, ast.BoolOp)):
            roots.append(node)
        elif isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Constant):
            roots.append(node.slice)
    return {id(n) for root in roots for n in ast.walk(root)}


def _reads_groups(func: ast.AST) -> bool:
    """Whether a function obtains group rows: calls a reader, or names a group
    table in its own SQL."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else \
                getattr(node.func, "id", "")
            if name in _GROUP_READERS:
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _GROUP_TABLE_SQL.search(node.value):
            return True
    return False


def _branching_reads(field: str, root: Path = SRC) -> list[str]:
    """Code that decides on a group's `field`.

    Three shapes. A read in a condition, comparison or lookup index whose
    receiver is named for a group, anywhere. The same with any receiver at all,
    inside a function that obtains group rows (`_GROUP_READERS`, or its own SQL
    on the group tables), which is where `row["kind"]` or `g["role"]` would be.
    And SQL that filters, joins, groups or orders on the field, matched over
    each string constant, so implicitly concatenated literals are one string.

    The blind spot, stated: a group dict passed to a function that neither
    reads groups itself nor names its parameter for one. Reads files off disk,
    so `scripts/qt` cannot select this by coverage (the residual AGENTS.md
    names); it is cheap and runs in the default suite.
    """
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        deciding = _deciding_nodes(tree)
        where = path.relative_to(root)
        hits = set()
        for node, name in _field_reads(tree, field):
            if id(node) in deciding and re.search(r"group(?!_chat)", name, re.I):
                hits.add((node.lineno, name))
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and _reads_groups(func):
                for node, name in _field_reads(func, field):
                    if id(node) in deciding:
                        hits.add((node.lineno, name))
        found.extend(f"{where}:{line} {name}.{field}" for line, name in sorted(hits))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and not re.match(r"(?i)\s*CREATE\b", node.value) \
                    and _DECIDING_SQL[field].search(node.value):
                found.append(f"{where}:{node.lineno} SQL on {field}")
    return found


def test_kind_is_display_only():
    """No code decides on a group's `kind` (D1). The day it gates behaviour,
    two features have been built where one was specced."""
    assert _branching_reads("kind") == []


def test_role_is_inert():
    """`group_members.role` is recorded and read by nothing in v1 (D2): every
    membership write goes through the already-privileged operator CLI."""
    assert _branching_reads("role") == []


_PLANTED = {
    "kind": [
        "def f(group):\n    if group['kind'] == 'family':\n        return 1\n",
        "def f(conn):\n    row = get_group(conn, 'x')\n"
        "    if row['kind'] == 'family':\n        return 1\n",
        "def f(conn):\n    return [g for g in list_groups(conn) if g['kind'] == 'x']\n",
        "def f(conn):\n    conn.execute(\"SELECT 1 FROM groups g \" \"WHERE g.kind = 1\")\n",
        "H = {}\ndef f(group):\n    return H[group['kind']]()\n",
        "def f(group):\n    match group.kind:\n        case 'x':\n            return 1\n",
    ],
    "role": [
        "def f(conn):\n    for h in group_membership_history(conn, 'x'):\n"
        "        if h['role'] == 'owner':\n            return 1\n",
        "def f(conn):\n    conn.execute(\"SELECT user_id FROM group_members \" "
        "\"WHERE role = 'owner'\")\n",
        "def f(group_row):\n    return group_row.get('role') == 'owner'\n",
    ],
}


@pytest.mark.parametrize("field,source", [
    (field, source) for field, sources in _PLANTED.items() for source in sources
])
def test_the_inertness_detector_finds_a_planted_branch(tmp_path, field, source):
    """Negative control, one shape per case: each is found on its own."""
    planted = tmp_path / "src"
    planted.mkdir()
    (planted / "planted.py").write_text(source)
    assert len(_branching_reads(field, planted)) == 1


def test_a_display_read_is_not_a_branch(tmp_path):
    """The positive control: printing the kind and role decides nothing."""
    planted = tmp_path / "src"
    planted.mkdir()
    (planted / "shown.py").write_text(
        "def f(conn):\n"
        "    for g in list_groups(conn):\n"
        "        print(f\"{g['group_id']} {g['kind']}\")\n"
        "    for h in group_membership_history(conn, 'x'):\n"
        "        print(h['role'])\n"
    )
    assert _branching_reads("kind", planted) == []
    assert _branching_reads("role", planted) == []
