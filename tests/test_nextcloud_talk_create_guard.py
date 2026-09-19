"""`nextcloud talk create` refuses to duplicate a room the registry already has.

ISSUE-509: a task asked to post a weekly digest into its own web chat room, could
not discover the room's token from any CLI, read the room as "does not exist
yet", and created a brand-new Talk conversation of the same name. That
conversation is bound to nothing — `_chat_promote_to_talk` is what writes a
`room_bindings` row — so the two are permanently unrelated, which is the
duplicate-room class ISSUE-342 already paid for once.

The guard is a name check against this user's own live registry rooms, and its
refusal names the descriptor to use instead. `--force` is the escape for someone
who really does want a second conversation under a name they already use.
"""

import json
from unittest.mock import patch

import pytest

from istota import db
from istota.skills.nextcloud import main


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture(autouse=True)
def _nc_env(monkeypatch, db_path):
    monkeypatch.setenv("NC_URL", "https://cloud.example.com")
    monkeypatch.setenv("NC_USER", "istota")
    monkeypatch.setenv("NC_PASS", "secret")
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))


def _run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None, code


def _make_room(db_path, token, user_id, *, origin, name, archived=False):
    with db.get_db(db_path) as conn:
        db.register_room(conn, token, user_id, origin=origin, name=name)
        if archived:
            db.set_room_archived(conn, token, True)


class TestTheDuplicateRoomGuard:
    """The reported incident: a same-named web room already exists."""

    def test_a_same_named_web_room_refuses_the_create(self, capsys, db_path):
        _make_room(db_path, "web-alice-3f21c4d90ab7", "alice",
                   origin="web", name="#weekly")
        with patch("istota.skills.nextcloud._talk_run") as run:
            out, code = _run(capsys, [
                "talk", "create", "--name", "#weekly", "--type", "group",
            ])
        run.assert_not_called()
        assert code == 1
        assert out["status"] == "error"
        assert out["reason"] == "room_exists"
        assert out["token"] == "web-alice-3f21c4d90ab7"
        assert out["origin"] == "web"
        # The refusal has to name something the model can actually do next.
        assert "web:web-alice-3f21c4d90ab7" in out["target"]

    def test_the_name_match_ignores_case_and_surrounding_space(self, capsys, db_path):
        _make_room(db_path, "web-alice-1", "alice", origin="web", name="#Weekly")
        with patch("istota.skills.nextcloud._talk_run") as run:
            out, code = _run(capsys, [
                "talk", "create", "--name", "  #weekly  ", "--type", "group",
            ])
        run.assert_not_called()
        assert code == 1
        assert out["reason"] == "room_exists"

    def test_a_talk_origin_room_refuses_too_and_names_its_own_token(
        self, capsys, db_path,
    ):
        _make_room(db_path, "p8vt2cnd", "alice", origin="talk", name="general")
        with patch("istota.skills.nextcloud._talk_run") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "general"])
        run.assert_not_called()
        assert code == 1
        assert out["origin"] == "talk"
        assert out["target"] == "talk:p8vt2cnd"

    def test_a_dismissed_room_still_blocks(self, capsys, db_path):
        """A hidden room is hidden, not gone: it keeps its bindings and
        delivery into it still works. `list_member_rooms` excludes it, which is
        right for the sidebar and reproduces the whole incident here — so the
        guard asks with `include_dismissed=True`."""
        _make_room(db_path, "web-alice-1", "alice", origin="web", name="#weekly")
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO room_dismissals (room_token, user_id) VALUES (?, ?)",
                ("web-alice-1", "alice"),
            )
            conn.commit()
        with patch("istota.skills.nextcloud._talk_run") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "#weekly"])
        run.assert_not_called()
        assert code == 1
        assert out["reason"] == "room_exists"
        # The refusal has to say why `rooms list` will not show it.
        assert "hidden" in out["error"]

    def test_force_creates_anyway(self, capsys, db_path):
        _make_room(db_path, "web-alice-1", "alice", origin="web", name="#weekly")
        with patch("istota.skills.nextcloud._talk_run", return_value="newtok") as run:
            out, code = _run(capsys, [
                "talk", "create", "--name", "#weekly", "--force",
            ])
        run.assert_called_once()
        assert code == 0
        assert out["status"] == "ok"
        assert out["token"] == "newtok"


class TestWhatTheGuardMustNotRefuse:
    """Each of these is a create that has to keep working."""

    def test_an_unused_name_creates(self, capsys, db_path):
        _make_room(db_path, "web-alice-1", "alice", origin="web", name="#weekly")
        with patch("istota.skills.nextcloud._talk_run", return_value="tok2") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "#berlin"])
        run.assert_called_once()
        assert code == 0
        assert out["token"] == "tok2"

    def test_another_user_s_room_of_that_name_is_not_this_user_s(
        self, capsys, db_path,
    ):
        _make_room(db_path, "web-bob-1", "bob", origin="web", name="#weekly")
        with patch("istota.skills.nextcloud._talk_run", return_value="tok2") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "#weekly"])
        run.assert_called_once()
        assert code == 0

    def test_an_archived_room_does_not_block(self, capsys, db_path):
        _make_room(db_path, "web-alice-1", "alice", origin="web",
                   name="#weekly", archived=True)
        with patch("istota.skills.nextcloud._talk_run", return_value="tok2") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "#weekly"])
        run.assert_called_once()
        assert code == 0

    def test_a_room_with_no_name_is_skipped_rather_than_matched(
        self, capsys, db_path, monkeypatch,
    ):
        """A NULL `rooms.name` must not compare equal to an empty `--name`;
        argparse requires a name, so this is about the SQL rather than the CLI."""
        _make_room(db_path, "web-alice-1", "alice", origin="web", name=None)
        with patch("istota.skills.nextcloud._talk_run", return_value="tok2") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "untitled"])
        run.assert_called_once()
        assert code == 0

    def test_no_database_path_degrades_open(self, capsys, db_path, monkeypatch):
        """A heartbeat shell command or an operator's own shell carries no
        `ISTOTA_DB_PATH`. The guard is a convenience in front of a verb that
        worked before it; it must not make the verb unusable where it cannot
        read the registry."""
        _make_room(db_path, "web-alice-1", "alice", origin="web", name="#weekly")
        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        with patch("istota.skills.nextcloud._talk_run", return_value="tok2") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "#weekly"])
        run.assert_called_once()
        assert code == 0

    def test_an_unreadable_database_degrades_open(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(tmp_path / "nope" / "istota.db"))
        with patch("istota.skills.nextcloud._talk_run", return_value="tok2") as run:
            out, code = _run(capsys, ["talk", "create", "--name", "#weekly"])
        run.assert_called_once()
        assert code == 0
