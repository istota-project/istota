"""`rooms/egress.py`: what an entry may be, what a digest covers, who may write a
grant, the refusal list and the one-time seed."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.rooms import egress
from istota.skills._loader import load_skill_index
from istota.skills._types import SkillMeta

SRC = Path(__file__).resolve().parent.parent / "src" / "istota"
SKILLS = SRC / "skills"
USER = "alice"
DIGEST = "a" * 64
INDEX = {
    "browse": SkillMeta(name="browse", description="x", egress="external"),
    "calendar": SkillMeta(name="calendar", description="x", egress="local"),
}


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


@pytest.fixture
def conn(path):
    with db.get_db(path) as c:
        yield c


class TestParseEntry:
    @pytest.mark.parametrize("text,kind,value", [
        ("news.example.com", "host", "news.example.com"),
        ("NEWS.Example.COM", "host", "news.example.com"),
        ("  example.com\n", "host", "example.com"),
        ("xn--bcher-kva.example", "host", "xn--bcher-kva.example"),
        ("a-b.example.com", "host", "a-b.example.com"),
        ("skill:browse", "skill", "browse"),
        ("skill:google_workspace", "skill", "google_workspace"),
        (f"public:{DIGEST}", "public", DIGEST),
        (f"open:{DIGEST}", "open", DIGEST),
    ])
    def test_accepts(self, text, kind, value):
        entry = egress.parse_entry(text)
        assert (entry.kind, entry.value) == (kind, value)
        assert egress.parse_entry(entry.text) == entry

    @pytest.mark.parametrize("text", [
        "",
        "   ",
        "localhost",
        "https://example.com",
        "example.com/path",
        "example.com:443",
        "*.example.com",
        "example.*",
        "127.0.0.1",
        "1.2.3",
        "127.1",
        "0x7f.0.0.1",
        "foo.0x7f",
        "::1",
        "[::1]",
        "exa mple.com",
        "example.com\nevil.example",
        "user@example.com",
        "-bad.example.com",
        "bad-.example.com",
        "example..com",
        "example.com.",
        "bücher.example",
        "a" * 64 + ".example.com",
        ".".join(["a" * 60] * 5),
        "skill:",
        "skill:Browse",
        "skill:../x",
        "open:",
        "open:abc",
        f"open:{'A' * 64}",
        f"public:{DIGEST}0",
        "open",
        "public",
        "other:thing",
        None,
        5,
    ])
    def test_refuses(self, text):
        with pytest.raises(egress.EntryRefused) as caught:
            egress.parse_entry(text)
        assert str(caught.value)


class TestJobDigest:
    BASE = {"prompt": "p", "command": None, "skill": None, "skill_args": None, "cron_expression": "0 9 * * *"}

    @pytest.mark.parametrize("column,value", [
        ("prompt", "q"), ("command", "c"), ("skill", "s"), ("skill_args", '["a"]'),
    ])
    def test_changes_with_what_the_job_dispatches(self, column, value):
        assert egress.job_digest({**self.BASE, column: value}) != egress.job_digest(self.BASE)

    @pytest.mark.parametrize("column,value", [
        ("cron_expression", "* * * * *"), ("model", "m"), ("output_target", "talk"),
    ])
    def test_does_not_change_with_anything_else(self, column, value):
        assert egress.job_digest({**self.BASE, column: value}) == egress.job_digest(self.BASE)

    def test_is_length_prefixed_so_text_cannot_move_between_fields(self):
        first = {**self.BASE, "prompt": "ab", "command": "c"}
        second = {**self.BASE, "prompt": "a", "command": "bc"}
        assert egress.job_digest(first) != egress.job_digest(second)

    def test_null_and_empty_are_the_same_text(self):
        """An older row can hold '' where the sync writes NULL; reading those
        as a text edit would drop every seeded approval on the first tick."""
        assert egress.job_digest({**self.BASE, "command": ""}) == egress.job_digest(self.BASE)

    def test_reads_a_row_as_well_as_a_mapping(self, conn):
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
            "VALUES ('alice', 'j', '0 9 * * *', 'p')"
        )
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE name = 'j'").fetchone()
        assert egress.job_digest(row) == egress.job_digest(self.BASE)
        assert re.fullmatch(r"[0-9a-f]{64}", egress.job_digest(row))


class TestGrants:
    def test_hosts_are_stored_in_their_normal_form(self, conn):
        egress.add_grant(conn, USER, "cron:j", "News.Example.COM", decided_via="web")
        assert egress.approved_hosts(conn, USER, "cron:j") == {"news.example.com"}

    def test_a_malformed_row_written_by_hand_is_dropped_on_read(self, conn):
        conn.execute(
            "INSERT INTO egress_grants (user_id, subject, entry, decided_via) "
            "VALUES (?, 'cron:j', '*.example.com', 'cli')", (USER,),
        )
        assert egress.approved_hosts(conn, USER, "cron:j") == frozenset()

    def test_a_subject_of_none_has_no_hosts(self, conn):
        assert egress.approved_hosts(conn, USER, None) == frozenset()

    def test_a_skill_grant_is_not_a_host(self, conn):
        egress.add_grant(conn, USER, "cron:j", "skill:browse", decided_via="web", skill_index=INDEX)
        assert egress.approved_hosts(conn, USER, "cron:j") == frozenset()
        assert egress.approved_skills(conn, USER, "cron:j") == {"browse"}

    @pytest.mark.parametrize("entry", ["skill:calendar", "skill:nothing"])
    def test_a_local_or_unknown_skill_is_refused_at_add(self, conn, entry):
        with pytest.raises(egress.EntryRefused):
            egress.add_grant(conn, USER, "cron:j", entry, decided_via="web", skill_index=INDEX)

    def test_a_skill_grant_needs_an_index_to_be_checked_against(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.add_grant(conn, USER, "cron:j", "skill:browse", decided_via="web")

    @pytest.mark.parametrize("subject", ["briefing:b", "assignment:3", "tasks-file"])
    def test_only_a_cron_job_carries_a_digest(self, conn, subject):
        for kind in ("open", "public"):
            with pytest.raises(egress.EntryRefused):
                egress.add_grant(conn, USER, subject, f"{kind}:{DIGEST}", decided_via="web")

    @pytest.mark.parametrize("subject", ["", "cron:", "other:x", "assignment:abc", "briefing:", None])
    def test_an_unknown_subject_is_refused(self, conn, subject):
        with pytest.raises(egress.EntryRefused):
            egress.add_grant(conn, USER, subject, "example.com", decided_via="web")

    def test_an_unknown_decider_is_refused(self, conn):
        with pytest.raises(egress.EntryRefused):
            egress.add_grant(conn, USER, "cron:j", "example.com", decided_via="task")

    def test_removing_a_grant_takes_it_from_the_next_read(self, conn):
        egress.add_grant(conn, USER, "cron:j", "example.com", decided_via="web")
        egress.remove_grant(conn, USER, "cron:j", "example.com")
        assert egress.approved_hosts(conn, USER, "cron:j") == frozenset()


class TestTheWriterHasOneSetOfCallers:
    """Only an authenticated person can approve anything: the web route, the operator
    CLI and the one-time seed. A skill CLI or a deferred handler never."""

    ALLOWED = {"rooms/egress.py", "db.py", "webui/egress_routes.py", "cli.py"}
    WRITERS = re.compile(r"\badd_grant\b|\bapprove_job\b|INSERT[^\"']*\begress_grants\b", re.DOTALL)

    def test_only_the_listed_modules_name_the_writer(self):
        found = {
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if self.WRITERS.search(path.read_text())
        }
        assert found <= self.ALLOWED, f"unexpected writers of egress_grants: {found - self.ALLOWED}"
        assert "db.py" in found

    def test_no_skill_and_no_deferred_handler_imports_the_module(self):
        offenders = [
            str(path.relative_to(SRC))
            for path in [*SKILLS.rglob("*.py"), SRC / "scheduler_deferred.py"]
            if re.search(r"rooms\.egress|rooms import .*\begress\b", path.read_text())
        ]
        assert offenders == []


class TestRefusals:
    def test_a_repeat_counts_hits_on_one_row(self, conn):
        egress.note_refused(conn, USER, "cron:j", "evil.example")
        egress.note_refused(conn, USER, "cron:j", "evil.example")
        rows = egress.list_refused(conn, USER, "cron:j")
        assert [(r["entry"], r["hits"]) for r in rows] == [("evil.example", 2)]

    def test_at_most_twenty_rows_per_subject_oldest_evicted(self, conn):
        for n in range(25):
            egress.note_refused(conn, USER, "cron:j", f"h{n}.example")
            conn.execute(
                "UPDATE egress_refused SET last_seen = datetime('now', ?) WHERE entry = ?",
                (f"-{100 - n} minutes", f"h{n}.example"),
            )
        entries = {r["entry"] for r in egress.list_refused(conn, USER, "cron:j")}
        assert len(entries) == 20
        assert "h0.example" not in entries
        assert "h24.example" in entries

    def test_a_flood_of_hosts_does_not_evict_a_request_for_approval(self, conn):
        egress.note_refused(conn, USER, "cron:j", "open")
        conn.execute("UPDATE egress_refused SET last_seen = datetime('now', '-9 days')")
        for n in range(30):
            egress.note_refused(conn, USER, "cron:j", f"h{n}.example")
        entries = {r["entry"] for r in egress.list_refused(conn, USER, "cron:j")}
        assert "open" in entries
        assert len(entries) == 20

    def test_eviction_is_per_subject(self, conn):
        egress.note_refused(conn, USER, "cron:other", "kept.example")
        for n in range(25):
            egress.note_refused(conn, USER, "cron:j", f"h{n}.example")
        assert len(egress.list_refused(conn, USER, "cron:other")) == 1

    def test_a_refused_host_grants_nothing(self, conn):
        egress.note_refused(conn, USER, "cron:j", "evil.example")
        assert egress.approved_hosts(conn, USER, "cron:j") == frozenset()

    def test_dismissed_stays_dismissed_on_a_repeat(self, conn):
        egress.note_refused(conn, USER, "cron:j", "evil.example")
        conn.execute("UPDATE egress_refused SET dismissed = 1")
        egress.note_refused(conn, USER, "cron:j", "evil.example")
        assert egress.list_refused(conn, USER, "cron:j")[0]["dismissed"] == 1

    def test_reopen_asks_again(self, conn):
        egress.note_refused(conn, USER, "cron:j", "open")
        conn.execute("UPDATE egress_refused SET dismissed = 1")
        egress.note_refused(conn, USER, "cron:j", "open", reopen=True)
        assert egress.list_refused(conn, USER, "cron:j")[0]["dismissed"] == 0

    def test_a_name_past_the_dns_limit_is_not_stored(self, conn):
        egress.note_refused(conn, USER, "cron:j", "a" * 300 + ".example")
        assert egress.list_refused(conn, USER, "cron:j") == []

    def test_forgetting_a_subject_takes_its_grants_and_refusals(self, conn):
        egress.add_grant(conn, USER, "cron:j", "example.com", decided_via="web")
        egress.note_refused(conn, USER, "cron:j", "evil.example")
        egress.add_grant(conn, "bob", "cron:j", "bob.example", decided_via="web")
        egress.forget_subject(conn, USER, "cron:j")
        assert egress.approved_hosts(conn, USER, "cron:j") == frozenset()
        assert egress.list_refused(conn, USER, "cron:j") == []
        assert egress.approved_hosts(conn, "bob", "cron:j") == {"bob.example"}


class TestSubjectRemoval:
    def test_deleting_a_job_row_forgets_its_subject(self, conn):
        job_id = conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
            "VALUES (?, 'j', '0 9 * * *', 'p')", (USER,),
        ).lastrowid
        egress.add_grant(conn, USER, "cron:j", "example.com", decided_via="web")
        db.delete_scheduled_job(conn, job_id)
        assert egress.approved_hosts(conn, USER, "cron:j") == frozenset()


def _seedable(path) -> sqlite3.Connection:
    """A database as an older release left it: jobs and no `egress_grants`."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE egress_grants")
    return conn


def _insert_job(conn, user, name, **columns):
    columns = {"prompt": "p", **columns}
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO scheduled_jobs (user_id, name, cron_expression, {names}) "
        f"VALUES (?, ?, '0 9 * * *', {marks})",
        (user, name, *columns.values()),
    )


class TestTheSeedMigration:
    def test_a_populated_database_brings_every_job_in_as_open(self, path):
        conn = _seedable(path)
        _insert_job(conn, "alice", "digest", prompt="read the news")
        _insert_job(conn, "alice", "backup", prompt="", command="rsync a b")
        _insert_job(conn, "bob", "digest", prompt="", skill="feeds", skill_args='["run"]')
        conn.commit()
        conn.close()

        db.init_db(path)

        with db.get_db(path) as c:
            rows = c.execute("SELECT * FROM scheduled_jobs").fetchall()
            grants = {
                (g["user_id"], g["subject"], g["entry"], g["decided_via"])
                for g in c.execute("SELECT * FROM egress_grants")
            }
        assert grants == {
            (r["user_id"], f"cron:{r['name']}", f"open:{egress.job_digest(r)}", "migration")
            for r in rows
        }
        assert len(grants) == 3

    def test_a_seeded_job_runs_as_it_did(self, path):
        from istota.rooms.scopes import reach_for_task

        conn = _seedable(path)
        _insert_job(conn, "alice", "digest")
        conn.commit()
        conn.close()
        db.init_db(path)
        with db.get_db(path) as c:
            job_id = c.execute("SELECT id FROM scheduled_jobs").fetchone()["id"]
            task_id = db.create_task(
                c, prompt="p", user_id="alice", source_type="scheduled", scheduled_job_id=job_id,
            )
            reach = reach_for_task(c, db.get_task(c, task_id), skill_index=INDEX)
        assert reach.egress_hosts is None
        assert reach.isolated_browser is False

    def test_an_empty_database_seeds_nothing(self, path):
        _seedable(path).close()
        db.init_db(path)
        with db.get_db(path) as c:
            assert c.execute("SELECT COUNT(*) FROM egress_grants").fetchone()[0] == 0

    def test_a_fresh_database_seeds_nothing_and_has_the_tables(self, tmp_path):
        path = tmp_path / "fresh.db"
        conn = sqlite3.connect(path)
        conn.close()
        db.init_db(path)
        with db.get_db(path) as c:
            assert c.execute("SELECT COUNT(*) FROM egress_grants").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM egress_refused").fetchone()[0] == 0

    def test_it_runs_once(self, path):
        conn = _seedable(path)
        _insert_job(conn, "alice", "digest")
        conn.commit()
        conn.close()
        db.init_db(path)
        with db.get_db(path) as c:
            c.execute("DELETE FROM egress_grants")
        db.init_db(path)
        with db.get_db(path) as c:
            assert c.execute("SELECT COUNT(*) FROM egress_grants").fetchone()[0] == 0

    def test_a_failure_part_way_leaves_nothing_so_the_next_boot_seeds(self, path, monkeypatch):
        conn = _seedable(path)
        _insert_job(conn, "alice", "first")
        _insert_job(conn, "alice", "second")
        conn.commit()
        conn.close()

        real = egress.add_grant
        calls = []

        def failing(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kwargs)

        monkeypatch.setattr(egress, "add_grant", failing)
        with pytest.raises(sqlite3.OperationalError):
            db.init_db(path)
        monkeypatch.setattr(egress, "add_grant", real)

        with sqlite3.connect(path) as c:
            assert c.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'egress_grants'"
            ).fetchone() is None
        db.init_db(path)
        with db.get_db(path) as c:
            assert c.execute("SELECT COUNT(*) FROM egress_grants").fetchone()[0] == 2

    def test_the_migrations_table_matches_schema_sql(self, path, tmp_path):
        """Both spell the DDL: the migration has to create the table inside the
        seed's transaction, and `schema.sql` is the one a reader looks at."""
        schema = tmp_path / "schema.db"
        schema_sql = db._resolve_schema_path().read_text()
        with sqlite3.connect(schema) as c:
            c.executescript(schema_sql)
            expected = c.execute("PRAGMA table_info(egress_grants)").fetchall()
        with sqlite3.connect(path) as c:
            assert c.execute("PRAGMA table_info(egress_grants)").fetchall() == expected

    def test_an_upgraded_jobs_table_says_private(self, path):
        with db.get_db(path) as c:
            _insert_job(c, "alice", "digest")
            assert c.execute("SELECT reach FROM scheduled_jobs").fetchone()[0] == "private"


class TestSkillManifests:
    EXTERNAL = {"browse", "devbox", "relay", "wordpress", "google_workspace",
                "servarr", "nextcloud", "shares"}

    def test_every_bundled_skill_declares_a_value(self):
        offenders = []
        for manifest in sorted(SKILLS.glob("*/skill.md")):
            head = manifest.read_text().split("\n---", 1)[0]
            match = re.search(r"^egress:\s*(\S+)\s*$", head, re.MULTILINE)
            if match is None or match.group(1) not in ("local", "external"):
                offenders.append(manifest.parent.name)
        assert offenders == []

    def test_the_external_class_is_the_specs(self):
        index = load_skill_index(Path("/nonexistent-ops-skills-dir"))
        assert {n for n, m in index.items() if m.egress == "external"} == self.EXTERNAL

    def test_a_manifest_with_no_field_loads_as_external(self, tmp_path):
        skill = tmp_path / "ops" / "custom"
        skill.mkdir(parents=True)
        (skill / "skill.md").write_text("---\nname: custom\ndescription: x\n---\nbody\n")
        index = load_skill_index(tmp_path / "ops", bundled_dir=tmp_path / "none")
        assert index["custom"].egress == "external"

    def test_an_unknown_value_loads_as_external(self, tmp_path):
        skill = tmp_path / "ops" / "custom"
        skill.mkdir(parents=True)
        (skill / "skill.md").write_text("---\nname: custom\negress: lokal\n---\nbody\n")
        index = load_skill_index(tmp_path / "ops", bundled_dir=tmp_path / "none")
        assert index["custom"].egress == "external"

    def test_a_legacy_index_entry_loads_as_external(self, tmp_path):
        ops = tmp_path / "ops"
        ops.mkdir()
        (ops / "_index.toml").write_text('[old]\ndescription = "x"\n')
        index = load_skill_index(ops, bundled_dir=tmp_path / "none")
        assert index["old"].egress == "external"

    def test_a_declared_local_value_loads(self, tmp_path):
        skill = tmp_path / "ops" / "custom"
        skill.mkdir(parents=True)
        (skill / "skill.md").write_text("---\nname: custom\negress: local\n---\nbody\n")
        index = load_skill_index(tmp_path / "ops", bundled_dir=tmp_path / "none")
        assert index["custom"].egress == "local"
