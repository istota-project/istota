"""Web chat transcript paging + window ordering (ISSUE-131 / ISSUE-130).

The transcript loader (`web_app._chat_room_messages`) gained a `(created_at, id)`
keyset cursor so a room with more history than one window can be scrolled back,
page by page, to the start. These tests pin the load-bearing details the spec
called out: the window survives an id/created_at inversion (ISSUE-130), the
cursor advances in the *raw* stored format (not the `_iso_utc` display value),
the bands tile with no gap/overlap across a created_at tie, a failed turn at the
window boundary isn't dropped, an aux-only failed tail isn't stranded, and active
tasks resume only on the first load.
"""

import pytest

from istota import db
from istota.config import Config

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web = True
except ImportError:
    _has_web = False

_needs_web = pytest.mark.skipif(not _has_web, reason="web dependencies not installed")


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


def _loader(db_path):
    from istota import web_app
    web_app._config = Config()
    web_app._config.db_path = db_path
    return web_app._chat_room_messages


def _turn(
    conn, token, user_text, asst_text, *, created_at,
    status="completed", user="u", surface="web", spine=True, asst_spine=True,
):
    """Insert a turn: a task row (+ optional user / assistant spine rows), all
    stamped at `created_at`. `spine=False` omits the user spine row (a legacy,
    un-backfilled turn); `asst_spine=False` omits the assistant spine row (a
    failed/cancelled turn — the scheduler only stores successful assistant
    turns). Returns the task id. Insert order controls the auto-increment ids."""
    tid = int(conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (surface, user, token, user_text, asst_text, status, created_at),
    ).fetchone()["id"])
    if spine:
        db.store_turn_message(conn, token, role="user", body=user_text,
                              task_id=tid, origin_surface=surface)
    if spine and asst_spine:
        db.store_turn_message(conn, token, role="assistant", body=asst_text,
                              task_id=tid, origin_surface=surface)
    conn.execute("UPDATE messages SET created_at = ? WHERE task_id = ?",
                 (created_at, tid))
    return tid


def _system(conn, token, body, *, created_at):
    mid = db.add_message(conn, token, role="system", body=body, origin_surface="web")
    conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (created_at, mid))
    return mid


def _ts(minute: int) -> str:
    """A naive-UTC stored timestamp at a given minute, matching SQLite
    datetime('now') format (the format the cursor travels in)."""
    return f"2026-06-10 12:{minute:02d}:00"


def _texts(out):
    return [(m["role"], m["text"]) for m in out["messages"]]


def _user_texts(out):
    return [m["text"] for m in out["messages"] if m["role"] == "user"]


@_needs_web
class TestWindowOrdering:
    def test_issue_130_recent_by_time_survive_small_limit(self, db_path):
        # A backfilled room whose id order inverts created_at order: the recent
        # turns are inserted FIRST (low ids) with recent timestamps, the stale
        # turns LAST (high ids) with old timestamps. An `id DESC LIMIT` window
        # would admit the stale-but-high-id rows and drop the genuinely-recent
        # ones; `created_at DESC, id DESC` keeps the recent ones.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            _turn(conn, "tok", "recent-1", "a", created_at=_ts(50))
            _turn(conn, "tok", "recent-2", "a", created_at=_ts(55))
            # Stale turns inserted later → higher ids, older timestamps.
            _turn(conn, "tok", "stale-1", "a", created_at=_ts(10))
            _turn(conn, "tok", "stale-2", "a", created_at=_ts(15))
        out = _loader(db_path)("u", "tok", 2)
        users = _user_texts(out)
        assert "recent-1" in users and "recent-2" in users
        assert "stale-1" not in users and "stale-2" not in users


@_needs_web
class TestCursorPaging:
    def _seed(self, db_path, n):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            for i in range(n):
                _turn(conn, "tok", f"q{i}", f"a{i}", created_at=_ts(i))

    def test_three_pages_tile_contiguously(self, db_path):
        self._seed(db_path, 6)  # q0..q5, q5 newest
        load = _loader(db_path)
        p1 = load("u", "tok", 2)
        assert _user_texts(p1) == ["q4", "q5"]  # most-recent 2 turns
        assert p1["has_more"] is True
        assert p1["oldest_cursor"] is not None

        cur = p1["oldest_cursor"]
        p2 = load("u", "tok", 2, (cur["ts"], cur["id"]))
        assert _user_texts(p2) == ["q2", "q3"]  # immediately older, no gap/overlap
        assert p2["has_more"] is True

        cur2 = p2["oldest_cursor"]
        p3 = load("u", "tok", 2, (cur2["ts"], cur2["id"]))
        assert _user_texts(p3) == ["q0", "q1"]
        assert p3["has_more"] is False  # beginning of history
        assert p3["oldest_cursor"] is not None  # cursor still names the page floor

    def test_cursor_advances_raw_format(self, db_path):
        # Flaw #1: the cursor must travel in the raw stored format
        # (`YYYY-MM-DD HH:MM:SS`), never the `_iso_utc` display value (`…T…Z`).
        # A display-format cursor sorts as newer than its own row and re-returns
        # page 1 forever; this asserts the keyset actually advances.
        self._seed(db_path, 6)
        load = _loader(db_path)
        p1 = load("u", "tok", 2)
        cur1 = p1["oldest_cursor"]
        assert " " in cur1["ts"] and "T" not in cur1["ts"]  # raw, not display
        p2 = load("u", "tok", 2, (cur1["ts"], cur1["id"]))
        cur2 = p2["oldest_cursor"]
        # The cursor strictly advanced (page 2 is strictly older than page 1).
        assert (cur2["ts"], cur2["id"]) < (cur1["ts"], cur1["id"])
        # And page 2 is not a repeat of page 1.
        assert _user_texts(p2) != _user_texts(p1)

    def test_keyset_uniqueness_at_created_at_tie(self, db_path):
        # Many turns share one whole-second created_at; paging across that
        # boundary must neither skip nor duplicate a turn (the id tiebreaker
        # makes the keyset total).
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            for i in range(6):
                _turn(conn, "tok", f"q{i}", f"a{i}", created_at=_ts(30))  # all same ts
        load = _loader(db_path)
        seen = []
        out = load("u", "tok", 2)
        while True:
            seen.extend(_user_texts(out))
            if not out["has_more"]:
                break
            cur = out["oldest_cursor"]
            out = load("u", "tok", 2, (cur["ts"], cur["id"]))
        assert sorted(seen) == [f"q{i}" for i in range(6)]  # no skip, no dup
        assert len(seen) == len(set(seen))


@_needs_web
class TestAuxBands:
    def test_failed_turn_at_first_page_boundary_not_dropped(self, db_path):
        # Flaw #2: page 1's spine LIMIT fills with successful turns, while a
        # failed task sits just newer than the page's oldest spine row t1. The
        # old `id DESC LIMIT` aux read could miss it; the banded `>= t1` read
        # must surface it on page 1.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            _turn(conn, "tok", "old-ok", "a", created_at=_ts(10))
            # A failed turn (user spine row, no assistant spine; error answer
            # lives on the task row) at minute 20.
            tf = _turn(conn, "tok", "boom-q", None, created_at=_ts(20),
                       status="failed", asst_spine=False)
            conn.execute("UPDATE tasks SET error = 'kaboom' WHERE id = ?", (tf,))
            _turn(conn, "tok", "new-ok", "a", created_at=_ts(30))
        # limit 2 → spine window = the two successful turns' 4 rows; t1 = old-ok.
        out = _loader(db_path)("u", "tok", 2)
        assert any("kaboom" in (m["text"] or "") for m in out["messages"]), \
            "failed answer at the window boundary must render on page 1"

    def test_failed_turn_in_older_band_renders_once(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            tf = _turn(conn, "tok", "boom-q", None, created_at=_ts(5),
                       status="failed", asst_spine=False)
            conn.execute("UPDATE tasks SET error = 'older-boom' WHERE id = ?", (tf,))
            for i in range(4):
                _turn(conn, "tok", f"q{i}", f"a{i}", created_at=_ts(20 + i))
        load = _loader(db_path)
        seen = []
        out = load("u", "tok", 2)
        seen.append(out)
        while out["has_more"]:
            cur = out["oldest_cursor"]
            out = load("u", "tok", 2, (cur["ts"], cur["id"]))
            seen.append(out)
        booms = sum(
            1 for page in seen for m in page["messages"]
            if "older-boom" in (m["text"] or "")
        )
        assert booms == 1  # rendered exactly once, no dup across bands

    def test_aux_only_failed_tail_not_stranded(self, db_path):
        # Flaw #3: one successful (spine) turn sits above many failed turns that
        # have NO spine rows at all (legacy / un-backfilled). The spine empties
        # after page 1, so the failed tail must be paged through the aux-only
        # keyset path — has_more stays true until they're all surfaced.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            for i in range(5):  # older, spineless failed tasks
                tf = _turn(conn, "tok", f"fq{i}", None, created_at=_ts(i),
                           status="failed", spine=False)
                conn.execute("UPDATE tasks SET error = ? WHERE id = ?",
                             (f"fail{i}", tf))
            _turn(conn, "tok", "ok-q", "ok-a", created_at=_ts(40))  # newest, spine
        load = _loader(db_path)
        out = load("u", "tok", 2)
        # Page 1: only the successful turn (the failed tail is older than t1 and
        # spineless), but has_more must be true via the aux probe.
        assert out["has_more"] is True
        rendered_fails = set()
        for page in _paginate(load, out):
            for m in page["messages"]:
                for i in range(5):
                    if f"fail{i}" in (m["text"] or ""):
                        rendered_fails.add(i)
        assert rendered_fails == set(range(5))  # none stranded

    def test_system_message_in_older_page(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            _system(conn, "tok", "old alert", created_at=_ts(5))
            _turn(conn, "tok", "old-q", "old-a", created_at=_ts(6))
            for i in range(4):
                _turn(conn, "tok", f"q{i}", f"a{i}", created_at=_ts(20 + i))
        load = _loader(db_path)
        out = load("u", "tok", 2)
        all_text = []
        for page in [out, *_paginate(load, out)]:
            all_text.extend(m["text"] for m in page["messages"])
        assert any("old alert" in (t or "") for t in all_text)


@_needs_web
class TestFirstLoadInvariants:
    def test_empty_spine_room_not_paginated(self, db_path):
        # A tasks-only room (no spine rows) renders its first load as today, with
        # no cursor offered.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            _turn(conn, "tok", "legacy-q", "legacy-a", created_at=_ts(10), spine=False)
        out = _loader(db_path)("u", "tok", 50)
        assert ("user", "legacy-q") in _texts(out)
        assert ("assistant", "legacy-a") in _texts(out)
        assert out["has_more"] is False
        assert out["oldest_cursor"] is None

    def test_active_task_only_on_first_load(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "tok", "u", origin="talk")
            for i in range(4):
                _turn(conn, "tok", f"q{i}", f"a{i}", created_at=_ts(i))
            # An in-flight task (user spine row, running) — newest.
            _turn(conn, "tok", "live-q", None, created_at=_ts(40),
                  status="running", asst_spine=False)
        load = _loader(db_path)
        p1 = load("u", "tok", 2)
        assert any(at["status"] == "running" for at in p1["active_tasks"])
        # An older page must carry no active tasks (no double-resume).
        cur = p1["oldest_cursor"]
        p2 = load("u", "tok", 2, (cur["ts"], cur["id"]))
        assert p2["active_tasks"] == []
        assert p2["active_task"] is None


def _paginate(load, first):
    """Yield every older page after `first`, following its cursor to the end."""
    out = first
    while out["has_more"]:
        cur = out["oldest_cursor"]
        out = load("u", "tok", 2, (cur["ts"], cur["id"]))
        yield out
