"""`assignments/validators.py`: the three checks, their spec shapes, and confinement."""

from __future__ import annotations

import os

import pytest

from istota.assignments import store, validators

BUDGET = {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 20, "per_wake_turns": 60}


@pytest.fixture
def config(make_config):
    return make_config()


@pytest.fixture
def assignment(db_conn):
    assignment_id = store.insert_assignment(
        db_conn, user_id="alice", slug="quotes", title="Quotes", goal="Three quotes",
        budget=BUDGET, dir_relpath="assignments/quotes",
    )
    return store.get_assignment(db_conn, assignment_id)


@pytest.fixture
def adir(config, assignment):
    path = config.workspace_root("alice") / config.bot_dir_name / "assignments" / "quotes"
    path.mkdir(parents=True)
    return path


def _item(db_conn, assignment, spec):
    item_id = store.insert_item(
        db_conn, assignment.id, text="check", kind="mechanical", validator=validators.parse_spec(spec),
    )
    return store.get_item(db_conn, assignment.id, item_id)


def _run(db_conn, config, assignment, spec):
    return validators.run_item(config, assignment, _item(db_conn, assignment, spec))


class TestParseSpec:
    def test_normalizes_defaults(self):
        assert validators.parse_spec({"check": "csv_columns", "file": "q.csv", "columns": ["a", "b"]}) == {
            "check": "csv_columns", "file": "q.csv", "columns": ["a", "b"], "min_rows": 1,
        }
        assert validators.parse_spec({"check": "contains", "file": "n.md", "text": "done"}) == {
            "check": "contains", "file": "n.md", "text": "done", "present": True,
        }

    def test_a_nested_file_of_four_parts_is_accepted(self):
        spec = validators.parse_spec({"check": "file_parses", "file": "a/b/c/d.json", "format": "json"})
        assert spec["file"] == "a/b/c/d.json"

    @pytest.mark.parametrize("spec", [
        {"check": "regex", "file": "a.md", "text": "x"},
        {"check": "file_parses", "file": "a.md"},
        {"check": "file_parses", "file": "a.md", "format": "yaml"},
        {"check": "file_parses", "file": "../a.md", "format": "markdown"},
        {"check": "file_parses", "file": "x/../../a.md", "format": "markdown"},
        {"check": "file_parses", "file": "/etc/passwd", "format": "markdown"},
        {"check": "file_parses", "file": "./a.md", "format": "markdown"},
        {"check": "file_parses", "file": "a/b/c/d/e.md", "format": "markdown"},
        {"check": "file_parses", "file": "", "format": "markdown"},
        {"check": "file_parses", "file": "a\0b", "format": "markdown"},
        {"check": "csv_columns", "file": "q.csv"},
        {"check": "csv_columns", "file": "q.csv", "columns": []},
        {"check": "csv_columns", "file": "q.csv", "columns": ["a", ""]},
        {"check": "csv_columns", "file": "q.csv", "columns": ["a"], "min_rows": 0},
        {"check": "csv_columns", "file": "q.csv", "columns": ["a"], "min_rows": True},
        {"check": "contains", "file": "a.md", "text": ""},
        {"check": "contains", "file": "a.md", "text": "x", "present": "no"},
        {"check": "contains", "file": "a.md", "text": "x", "format": "csv"},
    ])
    def test_refuses_a_bad_spec(self, spec):
        with pytest.raises(validators.InvalidValidator):
            validators.parse_spec(spec)


class TestFileParses:
    @pytest.mark.parametrize("fmt,name,text", [
        ("csv", "q.csv", "a,b\n1,2\n"),
        ("json", "d.json", '{"a": 1}'),
        ("toml", "c.toml", 'a = 1\n'),
        ("markdown", "n.md", "# Notes\n"),
    ])
    def test_passes(self, db_conn, config, assignment, adir, fmt, name, text):
        (adir / name).write_text(text)
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": name, "format": fmt})
        assert result.status == "passing", result.detail

    @pytest.mark.parametrize("fmt,name,data,detail", [
        ("csv", "q.csv", b"a,b\n1,2,3\n", "row 2: expected 2 columns, found 3"),
        ("json", "d.json", b"{nope", "not valid JSON"),
        ("toml", "c.toml", b"a = = 1", "not valid TOML"),
        ("markdown", "n.md", b"  \n", "file is empty"),
        ("markdown", "n.md", b"\xff\xfe", "not valid UTF-8"),
    ])
    def test_fails(self, db_conn, config, assignment, adir, fmt, name, data, detail):
        (adir / name).write_bytes(data)
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": name, "format": fmt})
        assert result.status == "failing"
        assert result.detail.startswith(detail)

    def test_a_missing_file_fails(self, db_conn, config, assignment, adir):
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": "q.csv", "format": "csv"})
        assert (result.status, result.detail) == ("failing", "file missing")

    def test_a_file_in_a_subdirectory(self, db_conn, config, assignment, adir):
        (adir / "out").mkdir()
        (adir / "out" / "d.json").write_text("[]")
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": "out/d.json", "format": "json"})
        assert result.status == "passing"

    def test_the_size_cap(self, db_conn, config, assignment, adir):
        (adir / "n.md").write_bytes(b"x" * (validators.MAX_FILE_BYTES + 1))
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": "n.md", "format": "markdown"})
        assert (result.status, result.detail) == ("failing", "file larger than 1 MiB")

    def test_exactly_the_cap_is_read(self, db_conn, config, assignment, adir):
        (adir / "n.md").write_bytes(b"x" * validators.MAX_FILE_BYTES)
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": "n.md", "format": "markdown"})
        assert result.status == "passing"


class TestCsvColumns:
    SPEC = {"check": "csv_columns", "file": "q.csv", "columns": ["source", "date"], "min_rows": 2}

    def test_passes(self, db_conn, config, assignment, adir):
        (adir / "q.csv").write_text("source,price,date\nacme,100,2026-10-01\nbeta,90,2026-10-02\n")
        result = _run(db_conn, config, assignment, self.SPEC)
        assert (result.status, result.detail) == ("passing", "2 data rows")

    def test_a_missing_column(self, db_conn, config, assignment, adir):
        (adir / "q.csv").write_text("source,price\nacme,100\nbeta,90\n")
        result = _run(db_conn, config, assignment, self.SPEC)
        assert (result.status, result.detail) == ("failing", "missing column 'date'")

    def test_a_blank_value(self, db_conn, config, assignment, adir):
        (adir / "q.csv").write_text("source,date\nacme,2026-10-01\n ,2026-10-02\n")
        result = _run(db_conn, config, assignment, self.SPEC)
        assert (result.status, result.detail) == ("failing", "row 3: empty 'source'")

    def test_too_few_rows(self, db_conn, config, assignment, adir):
        (adir / "q.csv").write_text("source,date\nacme,2026-10-01\n")
        result = _run(db_conn, config, assignment, self.SPEC)
        assert (result.status, result.detail) == ("failing", "1 data rows, need 2")

    def test_an_empty_file(self, db_conn, config, assignment, adir):
        (adir / "q.csv").write_text("")
        result = _run(db_conn, config, assignment, self.SPEC)
        assert (result.status, result.detail) == ("failing", "file is empty")


class TestContains:
    def test_present(self, db_conn, config, assignment, adir):
        (adir / "n.md").write_text("Installer picked: Acme\n")
        assert _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "picked"}).status == "passing"
        failing = _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "signed"})
        assert (failing.status, failing.detail) == ("failing", "text not found")

    def test_absent(self, db_conn, config, assignment, adir):
        (adir / "n.md").write_text("TODO: call\n")
        spec = {"check": "contains", "file": "n.md", "text": "TODO", "present": False}
        result = _run(db_conn, config, assignment, spec)
        assert (result.status, result.detail) == ("failing", "text present")
        (adir / "n.md").write_text("all done\n")
        assert _run(db_conn, config, assignment, spec).status == "passing"

    def test_is_literal_not_a_pattern(self, db_conn, config, assignment, adir):
        (adir / "n.md").write_text("abc\n")
        assert _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "a.c"}).status == "failing"


class TestConfinement:
    def test_a_symlinked_deliverable_is_not_followed(self, db_conn, config, assignment, adir, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("secret\n")
        (adir / "n.md").symlink_to(outside)
        result = _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "secret"})
        assert (result.status, result.detail) == ("failing", "not a regular file")

    def test_a_link_back_inside_is_refused_too(self, db_conn, config, assignment, adir):
        (adir / "real.md").write_text("done\n")
        (adir / "n.md").symlink_to(adir / "real.md")
        result = _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "done"})
        assert (result.status, result.detail) == ("failing", "not a regular file")

    def test_a_symlinked_directory_component_is_not_followed(self, db_conn, config, assignment, adir, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "d.json").write_text("[]")
        (adir / "out").symlink_to(outside)
        result = _run(db_conn, config, assignment, {"check": "file_parses", "file": "out/d.json", "format": "json"})
        assert result.status == "failing"

    def test_a_symlinked_assignment_directory_is_not_followed(self, db_conn, config, assignment, adir, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "n.md").write_text("done\n")
        os.rename(adir, adir.with_name("quotes.real"))
        adir.symlink_to(outside)
        result = _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "done"})
        assert result.status == "failing"

    def test_a_fifo_does_not_block(self, db_conn, config, assignment, adir):
        os.mkfifo(adir / "n.md")
        result = _run(db_conn, config, assignment, {"check": "contains", "file": "n.md", "text": "x"})
        assert (result.status, result.detail) == ("failing", "not a regular file")


class TestRunItemNeverRaises:
    def test_a_stored_spec_that_no_longer_parses_is_failing(self, db_conn, config, assignment, adir):
        item_id = store.insert_item(
            db_conn, assignment.id, text="x", kind="mechanical",
            validator={"check": "file_parses", "file": "../escape.md", "format": "markdown"},
        )
        result = validators.run_item(config, assignment, store.get_item(db_conn, assignment.id, item_id))
        assert (result.status, result.detail) == ("failing", "validator error")

    def test_an_unexpected_error_is_failing(self, db_conn, config, assignment, adir, monkeypatch):
        (adir / "n.md").write_text("x")
        item = _item(db_conn, assignment, {"check": "contains", "file": "n.md", "text": "x"})

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(validators.confined, "open_regular_at", boom)
        result = validators.run_item(config, assignment, item)
        assert (result.status, result.detail) == ("failing", "validator error")

    def test_a_confirmed_item_is_not_run(self, db_conn, config, assignment):
        item_id = store.insert_item(db_conn, assignment.id, text="picked", kind="confirmed")
        result = validators.run_item(config, assignment, store.get_item(db_conn, assignment.id, item_id))
        assert result.status == "failing"

    def test_no_workspace_is_failing(self, db_conn, make_config, assignment):
        config = make_config(workspace_path=None, workspace_mount_path=None)
        item = _item(db_conn, assignment, {"check": "contains", "file": "n.md", "text": "x"})
        result = validators.run_item(config, assignment, item)
        assert (result.status, result.detail) == ("failing", "no workspace")


class TestDescribe:
    @pytest.mark.parametrize("spec,text", [
        ({"check": "csv_columns", "file": "quotes.csv", "columns": ["source", "date"], "min_rows": 3},
         "csv_columns quotes.csv source,date; min 3 rows"),
        ({"check": "file_parses", "file": "d.json", "format": "json"}, "file_parses d.json as json"),
        ({"check": "contains", "file": "n.md", "text": "picked"}, 'contains n.md "picked"'),
        ({"check": "contains", "file": "n.md", "text": "TODO", "present": False}, 'contains n.md "TODO" absent'),
    ])
    def test_describe(self, spec, text):
        assert validators.describe(validators.parse_spec(spec)) == text
