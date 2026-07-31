"""Tests for the admin log reader (`istota.admin_logs`)."""

from pathlib import Path

import pytest

from istota import admin_logs, db
from istota.config import Config, LoggingConfig


def _line(ts: str, level: str, logger: str, message: str) -> str:
    """Render a line in the exact format `logging_setup` writes to file."""
    return f"{ts} {level:<5} [{logger:<18}] {message}\n"


def _config(tmp_path: Path, *, output: str = "both", file_name: str = "istota.log") -> Config:
    cfg = Config()
    cfg.logging = LoggingConfig(output=output, file=str(tmp_path / file_name))
    cfg.db_path = tmp_path / "istota.db"
    return cfg


class TestParseLine:
    def test_parses_a_standard_record(self):
        rec = admin_logs.parse_log_line(
            _line("2026-07-31 12:34:56", "INFO", "istota.scheduler", "Task claimed").rstrip("\n")
        )
        assert rec is not None
        assert rec.timestamp == "2026-07-31T12:34:56"
        assert rec.level == "INFO"
        assert rec.logger == "istota.scheduler"
        assert rec.message == "Task claimed"

    def test_logger_and_level_padding_is_stripped(self):
        rec = admin_logs.parse_log_line("2026-07-31 12:34:56 WARNING [istota.db          ] hi")
        assert rec is not None
        assert rec.level == "WARNING"
        assert rec.logger == "istota.db"
        assert rec.message == "hi"

    def test_continuation_line_is_not_a_record(self):
        assert admin_logs.parse_log_line('  File "x.py", line 3, in <module>') is None
        assert admin_logs.parse_log_line("Traceback (most recent call last):") is None

    def test_message_may_be_empty(self):
        rec = admin_logs.parse_log_line("2026-07-31 12:34:56 INFO  [istota.x           ] ")
        assert rec is not None
        assert rec.message == ""


class TestResolveAppLogChain:
    def test_returns_newest_first_with_rotation_siblings(self, tmp_path):
        (tmp_path / "istota.log").write_text("cur\n")
        (tmp_path / "istota.log.1").write_text("older\n")
        (tmp_path / "istota.log.2").write_text("oldest\n")
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        assert [p.name for p in chain] == ["istota.log", "istota.log.1", "istota.log.2"]

    def test_ignores_unrelated_and_compressed_siblings(self, tmp_path):
        (tmp_path / "istota.log").write_text("cur\n")
        (tmp_path / "istota.log.1.gz").write_bytes(b"\x1f\x8b")
        (tmp_path / "istota.log.bak").write_text("nope\n")
        (tmp_path / "other.log").write_text("nope\n")
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        assert [p.name for p in chain] == ["istota.log"]

    def test_empty_when_file_logging_is_off(self, tmp_path):
        cfg = _config(tmp_path, output="console")
        (tmp_path / "istota.log").write_text("cur\n")
        assert admin_logs.resolve_app_log_chain(cfg) == []

    def test_empty_when_no_file_configured(self, tmp_path):
        cfg = Config()
        cfg.logging = LoggingConfig(output="both", file="")
        assert admin_logs.resolve_app_log_chain(cfg) == []

    def test_skips_a_sibling_that_escapes_the_log_directory(self, tmp_path):
        """A symlinked rotation sibling pointing outside the log dir is refused.

        The chain is the one place a path reaches the reader, so it is where
        containment is enforced — a client never supplies a path at all.
        """
        logdir = tmp_path / "logs"
        logdir.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("PASSWORD=hunter2\n")
        (logdir / "istota.log").write_text("cur\n")
        (logdir / "istota.log.1").symlink_to(secret)
        cfg = _config(logdir)
        chain = admin_logs.resolve_app_log_chain(cfg)
        assert [p.name for p in chain] == ["istota.log"]


class TestListSources:
    def test_app_source_available_when_a_log_file_exists(self, tmp_path):
        (tmp_path / "istota.log").write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        sources = {s.id: s for s in admin_logs.list_sources(_config(tmp_path))}
        assert sources["app"].available is True
        assert sources["app"].kind == "file"
        assert sources["app"].bytes > 0

    def test_app_source_unavailable_with_a_reason_when_console_only(self, tmp_path):
        sources = {s.id: s for s in admin_logs.list_sources(_config(tmp_path, output="console"))}
        assert sources["app"].available is False
        assert "console" in sources["app"].detail.lower()

    def test_tasks_source_is_always_listed(self, tmp_path):
        sources = {s.id: s for s in admin_logs.list_sources(_config(tmp_path))}
        assert sources["tasks"].kind == "db"

    def test_unknown_source_id_is_rejected(self, tmp_path):
        assert admin_logs.get_source(_config(tmp_path), "app") is not None
        assert admin_logs.get_source(_config(tmp_path), "../../etc/passwd") is None
        assert admin_logs.get_source(_config(tmp_path), "nope") is None


class TestReadFilePage:
    def _write(self, tmp_path, count=10, logger="istota.scheduler", level="INFO"):
        # Roll into minutes past 60: a two-digit seconds field is what the
        # formatter writes, and a bare `{i:02d}` yields "10:00:100" past 99 —
        # which the line regex rightly refuses, silently turning every record
        # into a continuation line.
        lines = [
            _line(
                f"2026-07-31 10:{i // 60:02d}:{i % 60:02d}",
                level,
                logger,
                f"message {i}",
            )
            for i in range(count)
        ]
        (tmp_path / "istota.log").write_text("".join(lines))
        return admin_logs.resolve_app_log_chain(_config(tmp_path))

    def test_returns_the_tail_oldest_first(self, tmp_path):
        chain = self._write(tmp_path, count=10)
        page = admin_logs.read_file_page(chain, limit=3)
        assert [r.message for r in page.records] == ["message 7", "message 8", "message 9"]

    def test_paging_backward_walks_to_the_start(self, tmp_path):
        chain = self._write(tmp_path, count=10)
        first = admin_logs.read_file_page(chain, limit=4)
        second = admin_logs.read_file_page(chain, limit=4, before=first.next_before)
        third = admin_logs.read_file_page(chain, limit=4, before=second.next_before)
        assert [r.message for r in second.records] == [f"message {i}" for i in (2, 3, 4, 5)]
        assert [r.message for r in third.records] == ["message 0", "message 1"]
        assert third.next_before is None

    def test_pages_span_rotation_siblings(self, tmp_path):
        (tmp_path / "istota.log").write_text(
            _line("2026-07-31 11:00:00", "INFO", "a", "new-0")
        )
        (tmp_path / "istota.log.1").write_text(
            _line("2026-07-31 09:00:00", "INFO", "a", "old-0")
            + _line("2026-07-31 09:00:01", "INFO", "a", "old-1")
        )
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=10)
        assert [r.message for r in page.records] == ["old-0", "old-1", "new-0"]

    def test_multi_line_records_keep_their_continuation_lines(self, tmp_path):
        text = (
            _line("2026-07-31 10:00:00", "INFO", "a", "before")
            + _line("2026-07-31 10:00:01", "ERROR", "a", "boom")
            + "Traceback (most recent call last):\n"
            + '  File "x.py", line 3\n'
            + "ValueError: nope\n"
            + _line("2026-07-31 10:00:02", "INFO", "a", "after")
        )
        (tmp_path / "istota.log").write_text(text)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=10)
        assert [r.message.splitlines()[0] for r in page.records] == ["before", "boom", "after"]
        assert "ValueError: nope" in page.records[1].message

    def test_a_record_spanning_a_scan_window_is_not_split(self, tmp_path):
        """Backward scanning must not orphan a traceback's tail lines."""
        big = "x" * 400
        text = "".join(
            _line(f"2026-07-31 10:00:{i:02d}", "ERROR", "a", "head")
            + f"{big}\n" * 5
            for i in range(40)
        )
        (tmp_path / "istota.log").write_text(text)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=40, window_bytes=512)
        assert len(page.records) == 40
        for rec in page.records:
            assert rec.message.count(big) == 5

    def test_min_level_filters_below_the_threshold(self, tmp_path):
        text = (
            _line("2026-07-31 10:00:00", "DEBUG", "a", "d")
            + _line("2026-07-31 10:00:01", "INFO", "a", "i")
            + _line("2026-07-31 10:00:02", "WARNING", "a", "w")
            + _line("2026-07-31 10:00:03", "ERROR", "a", "e")
        )
        (tmp_path / "istota.log").write_text(text)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=10, min_level="WARNING")
        assert [r.message for r in page.records] == ["w", "e"]

    def test_unknown_level_is_kept_rather_than_hidden(self, tmp_path):
        (tmp_path / "istota.log").write_text(
            _line("2026-07-31 10:00:00", "TRACE", "a", "odd")
        )
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=10, min_level="ERROR")
        assert [r.message for r in page.records] == ["odd"]

    def test_query_matches_message_and_logger_case_insensitively(self, tmp_path):
        text = (
            _line("2026-07-31 10:00:00", "INFO", "istota.scheduler", "claimed")
            + _line("2026-07-31 10:00:01", "INFO", "istota.feeds", "polled")
        )
        (tmp_path / "istota.log").write_text(text)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        assert [r.message for r in admin_logs.read_file_page(chain, limit=10, q="CLAIM").records] == ["claimed"]
        assert [r.message for r in admin_logs.read_file_page(chain, limit=10, q="feeds").records] == ["polled"]

    def test_logger_filter_is_a_prefix_match(self, tmp_path):
        text = (
            _line("2026-07-31 10:00:00", "INFO", "istota.scheduler", "a")
            + _line("2026-07-31 10:00:01", "INFO", "istota.scheduler.stats", "b")
            + _line("2026-07-31 10:00:02", "INFO", "istota.feeds", "c")
        )
        (tmp_path / "istota.log").write_text(text)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=10, logger="istota.scheduler")
        assert [r.message for r in page.records] == ["a", "b"]

    def test_scan_cap_stops_a_filter_that_matches_nothing(self, tmp_path):
        self._write(tmp_path, count=500)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(
            chain, limit=10, q="no-such-text", window_bytes=512, max_scan_bytes=2048
        )
        assert page.records == []
        assert page.truncated is True
        # Stopped short, so there *is* more to look at — offer the older page.
        assert page.next_before is not None

    def test_a_spent_budget_that_also_consumed_the_chain_is_not_truncated(self, tmp_path):
        """"Truncated" must mean "stopped short", not "read everything cheaply"."""
        self._write(tmp_path, count=20)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(
            chain, limit=10, q="no-such-text", max_scan_bytes=8
        )
        assert page.records == []
        assert page.truncated is False
        assert page.next_before is None

    def test_a_page_filling_exactly_on_the_chain_start_offers_no_older_page(self, tmp_path):
        """Otherwise "Load older" is offered for a page that comes back empty."""
        chain = self._write(tmp_path, count=6)
        first = admin_logs.read_file_page(chain, limit=3)
        second = admin_logs.read_file_page(chain, limit=3, before=first.next_before)
        assert [r.message for r in second.records] == ["message 0", "message 1", "message 2"]
        assert second.next_before is None

    def test_not_truncated_when_the_chain_start_is_reached(self, tmp_path):
        self._write(tmp_path, count=3)
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        page = admin_logs.read_file_page(chain, limit=50)
        assert page.truncated is False
        assert page.next_before is None

    def test_empty_chain_yields_an_empty_page(self):
        page = admin_logs.read_file_page([], limit=10)
        assert page.records == []
        assert page.next_before is None
        assert page.tail_cursor is None

    def test_records_carry_a_tail_cursor_at_the_end_of_the_newest_file(self, tmp_path):
        chain = self._write(tmp_path, count=3)
        page = admin_logs.read_file_page(chain, limit=10)
        size = (tmp_path / "istota.log").stat().st_size
        assert page.tail_cursor == f"istota.log:{size}"


class TestReadFileTail:
    def test_returns_only_records_after_the_cursor(self, tmp_path):
        path = tmp_path / "istota.log"
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "first"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor

        with path.open("a") as fh:
            fh.write(_line("2026-07-31 10:00:01", "INFO", "a", "second"))
        tail = admin_logs.read_file_tail(chain, cursor)
        assert [r.message for r in tail.records] == ["second"]
        assert tail.reset is False

    def test_no_new_bytes_yields_nothing_and_keeps_the_cursor(self, tmp_path):
        (tmp_path / "istota.log").write_text(_line("2026-07-31 10:00:00", "INFO", "a", "only"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor
        tail = admin_logs.read_file_tail(chain, cursor)
        assert tail.records == []
        assert tail.cursor == cursor

    def test_a_shrunken_file_signals_a_reset_and_rereads_from_zero(self, tmp_path):
        """Rotation replaces the live file with a smaller one; the reader must
        not seek past its end and silently go deaf."""
        path = tmp_path / "istota.log"
        path.write_text("".join(
            _line(f"2026-07-31 10:00:{i:02d}", "INFO", "a", f"old {i}") for i in range(20)
        ))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=1).tail_cursor

        path.write_text(_line("2026-07-31 11:00:00", "INFO", "a", "fresh"))
        tail = admin_logs.read_file_tail(chain, cursor)
        assert tail.reset is True
        assert [r.message for r in tail.records] == ["fresh"]

    def test_a_partial_trailing_line_is_held_back(self, tmp_path):
        path = tmp_path / "istota.log"
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "complete"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor
        with path.open("a") as fh:
            fh.write("2026-07-31 10:00:01 INFO  [a  ] half-written")  # no newline
        tail = admin_logs.read_file_tail(chain, cursor)
        assert tail.records == []
        assert tail.cursor == cursor

    def test_tail_honours_filters(self, tmp_path):
        path = tmp_path / "istota.log"
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "first"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor
        with path.open("a") as fh:
            fh.write(_line("2026-07-31 10:00:01", "DEBUG", "a", "noise"))
            fh.write(_line("2026-07-31 10:00:02", "ERROR", "a", "boom"))
        tail = admin_logs.read_file_tail(chain, cursor, min_level="ERROR")
        assert [r.message for r in tail.records] == ["boom"]

    def test_an_over_long_line_advances_the_cursor_instead_of_wedging(self, tmp_path):
        """A line longer than the read window must not stall the tail forever.

        Returning the unchanged cursor made every subsequent poll re-read the
        same bytes: the tail went permanently silent while the UI still said
        "Live", with no reset and no error.
        """
        path = tmp_path / "istota.log"
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "first"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor

        with path.open("a") as fh:
            fh.write("x" * 5000 + "\n")
            fh.write(_line("2026-07-31 10:00:02", "INFO", "a", "after"))

        first = admin_logs.read_file_tail(chain, cursor, max_bytes=256)
        assert first.cursor != cursor, "cursor must advance past the over-long line"

        # And the tail keeps up: the record after it is reached rather than
        # stranded behind a cursor that never moves.
        seen: list[str] = [r.message for r in first.records]
        next_cursor = first.cursor
        for _ in range(40):
            tail = admin_logs.read_file_tail(chain, next_cursor, max_bytes=256)
            seen.extend(r.message for r in tail.records)
            if tail.cursor == next_cursor:
                break
            next_cursor = tail.cursor
        assert any("after" in m for m in seen)

    def test_a_line_past_the_growth_ceiling_is_skipped_and_reported(self, tmp_path, monkeypatch):
        """Above the ceiling the reader stops growing and consumes the bytes.

        Dropping them is the only way to make progress, so it says so rather
        than leaving the user with a silent gap.
        """
        monkeypatch.setattr(admin_logs, "_TAIL_MAX_WINDOW_BYTES", 512)
        path = tmp_path / "istota.log"
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "first"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor

        with path.open("a") as fh:
            fh.write("x" * 4000 + "\n")
            fh.write(_line("2026-07-31 10:00:02", "INFO", "a", "after"))

        tail = admin_logs.read_file_tail(chain, cursor, max_bytes=256)
        assert tail.cursor != cursor
        assert any("skipped" in r.message for r in tail.records)
        assert tail.records[0].level == "WARNING"

    def test_a_record_split_by_the_byte_cap_keeps_its_continuation_lines(self, tmp_path):
        """The cap must not cut mid-record: the next poll starts past the header
        and would discard the remaining traceback lines as leading orphans."""
        path = tmp_path / "istota.log"
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "first"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        cursor = admin_logs.read_file_page(chain, limit=10).tail_cursor

        body = "\n".join(f"  frame line {i}" for i in range(60))
        with path.open("a") as fh:
            fh.write(_line("2026-07-31 10:00:01", "ERROR", "a", "boom") + body + "\n")
            fh.write(_line("2026-07-31 10:00:02", "INFO", "a", "after"))

        collected: list[admin_logs.LogRecord] = []
        current = cursor
        for _ in range(40):
            tail = admin_logs.read_file_tail(chain, current, max_bytes=300)
            collected.extend(tail.records)
            if tail.cursor == current:
                break
            current = tail.cursor

        boom = [r for r in collected if r.message.startswith("boom")]
        assert len(boom) == 1
        assert boom[0].message.count("frame line") == 60

    def test_a_malformed_cursor_is_refused(self, tmp_path):
        (tmp_path / "istota.log").write_text("x\n")
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        for bad in ("", "nonsense", "istota.log", "../etc/passwd:0", "istota.log:-1", "other.log:0"):
            with pytest.raises(ValueError):
                admin_logs.read_file_tail(chain, bad)


class TestReadTaskLogPage:
    @pytest.fixture()
    def conn(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as c:
            yield c

    def _task(self, conn, user_id="alice", source_type="talk"):
        return db.create_task(conn, "prompt", user_id, source_type=source_type)

    def test_returns_newest_records_oldest_first(self, conn):
        tid = self._task(conn)
        for i in range(5):
            db.log_task(conn, tid, "info", f"m{i}")
        page = admin_logs.read_task_log_page(conn, limit=3)
        assert [r.message for r in page.records] == ["m2", "m3", "m4"]

    def test_records_carry_the_owning_user_and_task(self, conn):
        tid = self._task(conn, user_id="bob", source_type="email")
        db.log_task(conn, tid, "warn", "careful")
        rec = admin_logs.read_task_log_page(conn, limit=1).records[0]
        assert rec.task_id == tid
        assert rec.user_id == "bob"
        assert rec.source_type == "email"
        assert rec.level == "WARNING"

    def test_paging_backward_by_cursor(self, conn):
        tid = self._task(conn)
        for i in range(6):
            db.log_task(conn, tid, "info", f"m{i}")
        first = admin_logs.read_task_log_page(conn, limit=2)
        second = admin_logs.read_task_log_page(conn, limit=2, before=first.next_before)
        assert [r.message for r in second.records] == ["m2", "m3"]

    def test_next_before_is_none_at_the_start(self, conn):
        tid = self._task(conn)
        db.log_task(conn, tid, "info", "only")
        page = admin_logs.read_task_log_page(conn, limit=10)
        assert page.next_before is None

    def test_filters_by_level_user_task_and_query(self, conn):
        a = self._task(conn, user_id="alice")
        b = self._task(conn, user_id="bob")
        db.log_task(conn, a, "info", "alpha")
        db.log_task(conn, a, "error", "beta boom")
        db.log_task(conn, b, "info", "gamma")

        by_level = admin_logs.read_task_log_page(conn, limit=10, min_level="ERROR")
        assert [r.message for r in by_level.records] == ["beta boom"]

        by_user = admin_logs.read_task_log_page(conn, limit=10, user_id="bob")
        assert [r.message for r in by_user.records] == ["gamma"]

        by_task = admin_logs.read_task_log_page(conn, limit=10, task_id=a)
        assert [r.message for r in by_task.records] == ["alpha", "beta boom"]

        by_q = admin_logs.read_task_log_page(conn, limit=10, q="BOOM")
        assert [r.message for r in by_q.records] == ["beta boom"]

    def test_query_wildcards_are_matched_literally(self, conn):
        """A `%` typed into the search box must not become a LIKE wildcard."""
        tid = self._task(conn)
        db.log_task(conn, tid, "info", "100% done")
        db.log_task(conn, tid, "info", "unrelated")
        page = admin_logs.read_task_log_page(conn, limit=10, q="100%")
        assert [r.message for r in page.records] == ["100% done"]

    def test_tail_returns_only_rows_after_the_cursor(self, conn):
        tid = self._task(conn)
        db.log_task(conn, tid, "info", "first")
        cursor = admin_logs.read_task_log_page(conn, limit=10).tail_cursor
        db.log_task(conn, tid, "info", "second")
        tail = admin_logs.read_task_log_tail(conn, cursor)
        assert [r.message for r in tail.records] == ["second"]

    def test_tail_cursor_survives_an_empty_table(self, conn):
        page = admin_logs.read_task_log_page(conn, limit=10)
        assert page.records == []
        assert page.tail_cursor == "0"

    def test_a_malformed_tail_cursor_is_refused(self, conn):
        for bad in ("", "abc", "-1"):
            with pytest.raises(ValueError):
                admin_logs.read_task_log_tail(conn, bad)


class TestLevelHelpers:
    def test_level_rank_orders_the_standard_levels(self):
        ranks = [admin_logs.level_rank(name) for name in admin_logs.LEVELS]
        assert ranks == sorted(ranks)

    def test_warn_is_an_alias_for_warning(self):
        assert admin_logs.normalize_level("warn") == "WARNING"
        assert admin_logs.normalize_level("WARNING") == "WARNING"

    def test_unknown_level_is_preserved_uppercased(self):
        assert admin_logs.normalize_level("trace") == "TRACE"


class TestRecordSerialization:
    def test_to_dict_is_json_safe_and_carries_the_cursor(self, tmp_path):
        (tmp_path / "istota.log").write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        chain = admin_logs.resolve_app_log_chain(_config(tmp_path))
        rec = admin_logs.read_file_page(chain, limit=1).records[0]
        payload = rec.to_dict()
        assert payload["message"] == "x"
        assert payload["level"] == "INFO"
        assert payload["cursor"]
        assert set(payload) >= {"cursor", "timestamp", "level", "logger", "message"}
