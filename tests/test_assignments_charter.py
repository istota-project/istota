"""`assignments/charter.py`: validation, slugs, the rendered charter, the directory."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from istota.assignments import charter, store
from istota.config import AssignmentsConfig

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(make_config):
    return make_config()


def _create(conn, *, title="Heat pump quotes", user_id="alice", **overrides):
    fields = {
        "goal": "Three written quotes for a heat pump.",
        "autonomy": "May email installers on the approved list.",
        "budget": {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 25, "per_wake_turns": 60},
        "deadline": "2026-12-01T23:59:59.999Z",
    }
    fields.update(overrides)
    slug = charter.unique_slug(conn, user_id, title)
    assignment_id = store.insert_assignment(
        conn, user_id=user_id, slug=slug, title=title, dir_relpath=charter.dir_relpath(slug), **fields,
    )
    return store.get_assignment(conn, assignment_id)


def _fixed(conn):
    assignment = _create(conn)
    conn.execute(
        "UPDATE assignments SET created_at = '2026-10-25T09:00:00.000Z' WHERE id = ?", (assignment.id,),
    )
    store.insert_item(
        conn, assignment.id, text="Three quotes in quotes.csv", kind="mechanical",
        validator={"check": "csv_columns", "file": "quotes.csv", "columns": ["source", "date"], "min_rows": 3},
    )
    store.insert_item(conn, assignment.id, text="I have picked an installer", kind="confirmed")
    store.insert_frozen(conn, assignment.id, name="price_ceiling", value="1200 USD")
    return store.get_assignment(conn, assignment.id)


GOLDEN = """# Heat pump quotes

Assignment 1 · draft · created 2026-10-25

## Goal
Three written quotes for a heat pump.

## Done when
1. [mechanical] Three quotes in quotes.csv (check: csv_columns quotes.csv source,date; min 3 rows)
2. [confirmed] I have picked an installer

## May do without asking
May email installers on the approved list.

## Frozen commitments
- price_ceiling: 1200 USD

## Budget
$20.00, 40 wakes, 25 minutes and 60 turns per wake. Deadline 2026-12-01. Default check-in every 6h.
"""


class TestRender:
    def test_golden(self, db_conn):
        assignment = _fixed(db_conn)
        assert charter.render(
            assignment, store.list_items(db_conn, assignment.id), store.list_frozen(db_conn, assignment.id),
        ) == GOLDEN

    def test_render_current_reads_the_rows(self, db_conn):
        assignment = _fixed(db_conn)
        assert charter.render_current(db_conn, assignment.id) == GOLDEN

    def test_empty_sections_and_no_deadline(self, db_conn):
        assignment = _create(db_conn, autonomy="", deadline=None, cadence_minutes=90)
        text = charter.render(assignment, [], [])
        assert "## Done when\nNone yet.\n" in text
        assert "## May do without asking\nNothing beyond the charter.\n" in text
        assert "## Frozen commitments\nNone.\n" in text
        assert "Deadline" not in text
        assert "Default check-in every 90m." in text

    def test_a_deadline_with_a_time(self, db_conn):
        assignment = _create(db_conn, deadline="2026-12-01T15:30:00.000Z")
        assert "Deadline 2026-12-01 15:30 UTC." in charter.render(assignment, [], [])

    def test_header_scalars_are_one_line(self, db_conn):
        assignment = _create(db_conn, title="Quotes\n## Injected")
        store.insert_item(db_conn, assignment.id, text="a\nb", kind="confirmed")
        store.insert_frozen(db_conn, assignment.id, name="n\nx", value="v\ny")
        text = charter.render_current(db_conn, assignment.id)
        assert text.startswith("# Quotes ## Injected\n")
        assert "1. [confirmed] a b\n" in text
        assert "- n x: v y\n" in text

    def test_digest_is_the_sha256_of_the_render(self, db_conn):
        assignment = _fixed(db_conn)
        assert charter.charter_digest(db_conn, assignment.id) == hashlib.sha256(GOLDEN.encode()).hexdigest()

    def test_digest_moves_with_any_charter_change(self, db_conn):
        assignment = _fixed(db_conn)
        before = charter.charter_digest(db_conn, assignment.id)
        store.insert_frozen(db_conn, assignment.id, name="brand", value="any")
        assert charter.charter_digest(db_conn, assignment.id) != before


class TestSlug:
    @pytest.mark.parametrize("title,slug", [
        ("Heat pump quotes", "heat-pump-quotes"),
        ("  Café & crème: 2027!  ", "caf-cr-me-2027"),
        ("!!!", "assignment"),
        ("x" * 200, "x" * charter.SLUG_MAX_CHARS),
    ])
    def test_slugify(self, title, slug):
        assert charter.slugify(title) == slug

    def test_collision_appends_a_number(self, db_conn):
        assert _create(db_conn).slug == "heat-pump-quotes"
        assert _create(db_conn).slug == "heat-pump-quotes-2"
        assert _create(db_conn).slug == "heat-pump-quotes-3"

    def test_another_users_slug_does_not_collide(self, db_conn):
        _create(db_conn, user_id="bob")
        assert _create(db_conn).slug == "heat-pump-quotes"


class TestValidation:
    def test_text_fields(self):
        assert charter.validate_title("  Heat   pump\nquotes ") == "Heat pump quotes"
        assert charter.validate_goal(" Get quotes. ") == "Get quotes."
        assert charter.validate_autonomy("") == ""
        for fn, value in [
            (charter.validate_title, " "),
            (charter.validate_title, "x" * (charter.TITLE_MAX_CHARS + 1)),
            (charter.validate_goal, ""),
            (charter.validate_goal, "x" * (charter.GOAL_MAX_CHARS + 1)),
            (charter.validate_autonomy, "x" * (charter.GOAL_MAX_CHARS + 1)),
        ]:
            with pytest.raises(charter.CharterError) as err:
                fn(value)
            assert err.value.code == "invalid_charter"

    def test_budget_defaults(self):
        assert charter.validate_budget(AssignmentsConfig(), max_cost_usd=20, max_wakes=40) == {
            "max_cost_usd": 20.0, "max_wakes": 40,
            "per_wake_minutes": charter.DEFAULT_PER_WAKE_MINUTES,
            "per_wake_turns": charter.DEFAULT_PER_WAKE_TURNS,
        }

    @pytest.mark.parametrize("kwargs,code", [
        ({"max_cost_usd": 50.01, "max_wakes": 40}, "over_ceiling"),
        ({"max_cost_usd": 20, "max_wakes": 501}, "over_ceiling"),
        ({"max_cost_usd": 0, "max_wakes": 40}, "invalid_charter"),
        ({"max_cost_usd": float("nan"), "max_wakes": 40}, "invalid_charter"),
        ({"max_cost_usd": 20, "max_wakes": 0}, "invalid_charter"),
        ({"max_cost_usd": 20, "max_wakes": 2.5}, "invalid_charter"),
        ({"max_cost_usd": 20, "max_wakes": 40, "per_wake_minutes": 0}, "invalid_charter"),
        ({"max_cost_usd": 20, "max_wakes": 40, "per_wake_turns": -1}, "invalid_charter"),
    ])
    def test_budget_refusals(self, kwargs, code):
        with pytest.raises(charter.CharterError) as err:
            charter.validate_budget(AssignmentsConfig(), **kwargs)
        assert err.value.code == code

    def test_the_ceiling_comes_from_config(self):
        cfg = AssignmentsConfig(max_cost_usd_per_assignment=100.0)
        assert charter.validate_budget(cfg, max_cost_usd=80, max_wakes=1)["max_cost_usd"] == 80.0

    def test_deadline(self):
        assert charter.validate_deadline("2026-12-01", now=NOW) == "2026-12-01T23:59:59.999Z"
        assert charter.validate_deadline("2026-12-01T10:00:00+02:00", now=NOW) == "2026-12-01T08:00:00.000Z"
        assert charter.validate_deadline(
            "2026-12-01T10:00", now=NOW, tz=timezone(timedelta(hours=-5)),
        ) == "2026-12-01T15:00:00.000Z"
        assert charter.validate_deadline("2026-10-25", now=NOW) == "2026-10-25T23:59:59.999Z"

    def test_deadline_refusals(self):
        with pytest.raises(charter.CharterError) as err:
            charter.validate_deadline("2026-10-24", now=NOW)
        assert err.value.code == "deadline_passed"
        with pytest.raises(charter.CharterError) as err:
            charter.validate_deadline("next tuesday", now=NOW)
        assert err.value.code == "invalid_charter"

    def test_cadence(self):
        assert charter.validate_cadence(AssignmentsConfig(), 360) == 360
        with pytest.raises(charter.CharterError) as err:
            charter.validate_cadence(AssignmentsConfig(), 5)
        assert err.value.code == "invalid_charter"

    def test_item(self):
        assert charter.validate_item(" Three quotes ", "confirmed", None) == ("Three quotes", None)
        text, spec = charter.validate_item(
            "Quotes", "mechanical", {"check": "file_parses", "file": "q.csv", "format": "csv"},
        )
        assert spec == {"check": "file_parses", "file": "q.csv", "format": "csv"}

    @pytest.mark.parametrize("text,kind,spec", [
        ("Quotes", "mechanical", None),
        ("Quotes", "confirmed", {"check": "file_parses", "file": "q.csv", "format": "csv"}),
        ("Quotes", "mechanical", {"check": "file_parses", "file": "../q.csv", "format": "csv"}),
        ("Quotes", "mechanical", {"check": "file_parses", "file": "q.csv", "format": "xlsx"}),
    ])
    def test_item_refusals(self, text, kind, spec):
        with pytest.raises(charter.CharterError) as err:
            charter.validate_item(text, kind, spec)
        assert err.value.code == "invalid_validator"

    @pytest.mark.parametrize("text,kind", [("", "confirmed"), ("Quotes", "optional")])
    def test_item_text_and_kind(self, text, kind):
        with pytest.raises(charter.CharterError) as err:
            charter.validate_item(text, kind, None)
        assert err.value.code == "invalid_charter"

    def test_frozen(self):
        assert charter.validate_frozen(" price_ceiling ", " 1200 USD ") == ("price_ceiling", "1200 USD")
        for name, value in [("", "x"), ("n", ""), ("x" * 65, "v"), ("n", "x" * 501)]:
            with pytest.raises(charter.CharterError):
                charter.validate_frozen(name, value)


class TestDirectory:
    def _dir(self, config):
        return config.workspace_root("alice") / config.bot_dir_name / "assignments" / "heat-pump-quotes"

    def test_creates_the_directory_and_both_files(self, db_conn, config):
        assignment = _fixed(db_conn)
        assert charter.create_directory(config, db_conn, assignment) is True
        path = self._dir(config)
        assert (path / "SCRATCHPAD.md").read_text() == charter.SCRATCHPAD_SEED
        text = (path / "CHARTER.md").read_text()
        assert text == charter.CHARTER_FILE_HEADER + "\n\n" + GOLDEN
        for heading in ("Now", "Done", "Thinking", "Rejected", "Problems", "Findings"):
            assert f"## {heading}\n" in charter.SCRATCHPAD_SEED

    def test_does_not_overwrite_an_existing_scratchpad(self, db_conn, config):
        assignment = _fixed(db_conn)
        path = self._dir(config)
        path.mkdir(parents=True)
        (path / "SCRATCHPAD.md").write_text("my notes\n")
        assert charter.create_directory(config, db_conn, assignment) is True
        assert (path / "SCRATCHPAD.md").read_text() == "my notes\n"

    def test_rewrite_replaces_charter_md(self, db_conn, config):
        assignment = _fixed(db_conn)
        charter.create_directory(config, db_conn, assignment)
        (self._dir(config) / "CHARTER.md").write_text("edited by the user")
        store.update_assignment(db_conn, assignment.id, title="Heat pump quotes, revised")
        rendered = charter.rewrite_charter_file(config, db_conn, assignment.id)
        assert rendered.startswith("# Heat pump quotes, revised\n")
        assert (self._dir(config) / "CHARTER.md").read_text() == charter.CHARTER_FILE_HEADER + "\n\n" + rendered

    def test_a_symlinked_assignments_directory_is_refused(self, db_conn, config, tmp_path):
        assignment = _fixed(db_conn)
        outside = tmp_path / "outside"
        outside.mkdir()
        bot = config.workspace_root("alice") / config.bot_dir_name
        bot.mkdir(parents=True)
        (bot / "assignments").symlink_to(outside)
        assert charter.create_directory(config, db_conn, assignment) is False
        assert list(outside.iterdir()) == []

    def test_a_symlinked_charter_file_is_not_followed(self, db_conn, config, tmp_path):
        assignment = _fixed(db_conn)
        charter.create_directory(config, db_conn, assignment)
        outside = tmp_path / "victim.md"
        outside.write_text("keep")
        charter_md = self._dir(config) / "CHARTER.md"
        charter_md.unlink()
        charter_md.symlink_to(outside)
        assert charter.rewrite_charter_file(config, db_conn, assignment.id) is not None
        assert outside.read_text() == "keep"

    def test_no_workspace(self, db_conn, make_config):
        config = make_config(workspace_path=None, workspace_mount_path=None)
        assert charter.create_directory(config, db_conn, _fixed(db_conn)) is False

    def test_dir_relpath(self):
        assert charter.dir_relpath("heat-pump-quotes") == "assignments/heat-pump-quotes"
