"""#659: a followed task's final live frame is the room's stored row.

Two paths used to decide what the bot's turn in a room is. Storage picks the
body through a chain of transformations (an email thread records the composed
mail, a briefing its parsed body, a silent job its `ACTION:` remainder); the
live stream emitted the raw model result as `result` and streamed raw tokens as
`text_delta`. An open email thread room therefore showed the host's private
note as the bot's turn until a reload.

The rule now: a `result` frame carries text only when every stored row is the
raw result unchanged (`answer_is_stored_verbatim`), and no `text_delta` is
emitted otherwise; the client takes the body from the stored row. These tests
drive `process_one_task` per task kind with a fake brain that streams through
the real `TaskStreamAdapter`, and hold the invariant over what reached the
event log and the store.
"""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.brain._events import TextDeltaEvent, TextEvent
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.executor_stream import TaskStreamAdapter
from istota.scheduler import process_one_task

HOST = "carol"
HOST_ADDR = "carol@test.com"
BOT = "bot@test.com"
ALICE = "alice@ext.example"
BOB = "bob@ext.example"
ROOT = "<root-659@test.com>"
PRIVATE_NOTE = "Told them Thursday at 7, which clashes with your course."


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    config = Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="user", imap_password="pass",
            smtp_host="smtp.test", smtp_port=587,
            bot_email=BOT,
        ),
        scheduler=SchedulerConfig(stream_text_gate_chars=0),
        workspace_path=mount,
        temp_dir=temp,
        users={
            HOST: UserConfig(
                display_name="Carol",
                email_addresses=[HOST_ADDR],
                trusted_email_senders=["*@ext.example"],
            ),
        },
    )
    config.skills_dir = skills
    config.bot_name = "Zorg"
    return config


def _streaming_brain(result: str):
    """An `execute_task` stand-in that streams `result` the way a brain does,
    through the real adapter, so the delta gate is exercised for real."""

    def run(task, config, user_resources, dry_run=False, event_writer=None, **_kw):
        adapter = TaskStreamAdapter(config, task, event_writer)
        adapter.on_event(TextEvent(text=result))
        adapter.on_event(TextDeltaEvent(text=result))
        adapter.finish()
        return True, result, None, None

    return run


def _events(db_path, task_id):
    with db.get_db(db_path) as conn:
        return db.get_task_events(conn, task_id, 0)


def _stored_assistant_bodies(db_path, task_id):
    with db.get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT room_token, body FROM messages WHERE task_id = ? AND role = 'assistant'",
            (task_id,),
        ).fetchall()
    return {r["room_token"]: r["body"] for r in rows}


def _run(config, result, *, extra=()):
    with ExitStack() as stack:
        stack.enter_context(patch("istota.scheduler.execute_task",
                                  side_effect=_streaming_brain(result)))
        stack.enter_context(patch("istota.transport.email.outbound.reply_to_email",
                                  return_value="<out@test.com>"))
        for p in extra:
            stack.enter_context(p)
        return process_one_task(config)


def assert_invariant(db_path, task_id, raw):
    """The final live frame equals the stored row for every room it was
    stored to, or both are empty; no preview delta was emitted where a stored
    body differs from the raw result; the raw result is not in the log at all
    when nothing stores it verbatim."""
    events = _events(db_path, task_id)
    kinds = [e["kind"] for e in events]
    stored = _stored_assistant_bodies(db_path, task_id)
    verbatim = all(body == raw for body in stored.values())
    finals = [e for e in events if e["kind"] in ("result", "confirmation")]
    assert finals, f"no final frame in {kinds}"
    final = finals[-1]
    if final["kind"] == "result":
        text = final["payload"].get("text")
        if text:
            for room, body in stored.items():
                assert text == body, f"result frame differs from the row in {room}"
        if stored and not verbatim:
            assert not text, "a result frame carried a body the store did not keep"
    if not verbatim or (final["kind"] == "result" and not final["payload"].get("text")):
        assert "text_delta" not in kinds, "a preview streamed text the store transformed"
        if final["kind"] == "result":
            assert not any(raw in json.dumps(e["payload"]) for e in events), \
                "the raw result is readable in the task's event log"
    return events, stored


# ---------------------------------------------------------------------------
# Fixtures for each kind
# ---------------------------------------------------------------------------


def _web_task(db_path, room="webroom"):
    with db.get_db(db_path) as conn:
        db.register_room(conn, room, HOST, origin="web")
        db.add_room_binding(conn, room, "web", room)
        return db.create_task(
            conn, prompt="what's the weather?", user_id=HOST,
            source_type="web", conversation_token=room, output_target="web",
        )


def _talk_task(db_path, token="talkroom"):
    with db.get_db(db_path) as conn:
        db.register_room(conn, token, HOST, origin="talk")
        db.add_room_binding(conn, token, "talk", token)
        return db.create_task(
            conn, prompt="hi", user_id=HOST, source_type="talk",
            conversation_token=token,
        )


_UID = [900]


def _thread_task(config):
    """The host mails the bot and two friends: a thread room with a task."""
    from istota.skills.email import Email, EmailEnvelope
    from istota.transport.email.inbound import poll_emails

    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(
        id=uid, subject="Dinner", sender=HOST_ADDR,
        date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False,
    )
    email = Email(
        id=uid, subject="Dinner", sender=HOST_ADDR,
        date="Mon, 01 Jan 2026 12:00:00 +0000",
        body="Can we do Thursday?", attachments=[], message_id=ROOT,
        references=None, to=(BOT, ALICE), cc=(BOB,),
        authentication_results=None,
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        (task_id,) = poll_emails(config)
    return task_id


def _write_mail(config, task_id, body):
    from istota.executor import task_deferred_dir

    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, task_id)
    out = task_deferred_dir(config, task)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"task_{task.id}_email_output.json").write_text(json.dumps(
        {"subject": "", "body": body, "format": "plain"}))


def _thread_room(config):
    with db.get_db(config.db_path) as conn:
        return db.resolve_room_token(conn, "email", ROOT)


# ---------------------------------------------------------------------------
# The invariant, per kind
# ---------------------------------------------------------------------------


class TestTheFinalFrameIsTheStoredRow:
    def test_web_streams_and_its_result_is_the_row(self, config, db_path):
        task_id = _web_task(db_path)
        _run(config, "It's sunny.")
        events, stored = assert_invariant(db_path, task_id, "It's sunny.")
        assert stored == {"webroom": "It's sunny."}
        (result,) = [e for e in events if e["kind"] == "result"]
        assert result["payload"]["text"] == "It's sunny."

    def test_talk_result_is_the_row(self, config, db_path):
        task_id = _talk_task(db_path)
        _run(config, "Hello there.", extra=(
            patch("istota.scheduler.run_coro", return_value=123),
            patch("istota.scheduler.post_result_to_talk", new=MagicMock()),
        ))
        events, stored = assert_invariant(db_path, task_id, "Hello there.")
        assert stored == {"talkroom": "Hello there."}

    def test_thread_mail_shows_the_mail_never_the_note(self, config, db_path):
        task_id = _thread_task(config)
        _write_mail(config, task_id, "Thursday at 7 works.")
        _run(config, PRIVATE_NOTE)
        events, stored = assert_invariant(db_path, task_id, PRIVATE_NOTE)
        assert stored == {_thread_room(config): "Thursday at 7 works."}
        (result,) = [e for e in events if e["kind"] == "result"]
        assert "text" not in result["payload"]
        assert not any(PRIVATE_NOTE in json.dumps(e["payload"]) for e in events)

    def test_thread_no_action_has_no_row_and_no_text(self, config, db_path):
        task_id = _thread_task(config)
        raw = "NO_ACTION: nothing for anyone here."
        _run(config, raw)
        events, stored = assert_invariant(db_path, task_id, raw)
        assert stored == {}
        (result,) = [e for e in events if e["kind"] == "result"]
        assert "text" not in result["payload"]
        (done,) = [e for e in events if e["kind"] == "done"]
        assert "msg_id" not in done["payload"]

    def test_parked_confirmation_is_a_confirmation_frame(self, config, db_path):
        task_id = _web_task(db_path)
        raw = "Shall I delete the old drafts? Please confirm."
        _run(config, raw)
        events, _ = assert_invariant(db_path, task_id, raw)
        assert [e["kind"] for e in events if e["kind"] in ("result", "confirmation")] \
            == ["confirmation"]

    def test_held_guest_reply_is_a_confirmation_frame(self, config, db_path):
        from types import SimpleNamespace

        with db.get_db(db_path) as conn:
            db.register_room(conn, "shared", HOST, origin="web")
            db.add_room_binding(conn, "shared", "web", "shared")
            task_id = db.create_task(
                conn, prompt="is she coming?", user_id=HOST, source_type="web",
                conversation_token="shared", output_target="web",
                guest_participant_id=1,
            )
        proposal = SimpleNamespace(
            preview="Guest asked in Shared:\nis she coming?\n\nMessage:\nShe is.",
            parent_token="shared",
        )
        _run(config, "She is.", extra=(
            patch("istota.rooms.private_replies.guest_reply_mode", return_value="held"),
            patch("istota.rooms.private_replies.propose_guest_reply", return_value=proposal),
        ))
        events, stored = assert_invariant(db_path, task_id, "She is.")
        assert stored == {}
        assert [e["kind"] for e in events if e["kind"] in ("result", "confirmation")] \
            == ["confirmation"]

    def test_briefing_result_carries_no_raw_json(self, config, db_path):
        raw = json.dumps({"subject": "Morning", "body": "Rain at noon."})
        with db.get_db(db_path) as conn:
            db.register_room(conn, "briefroom", HOST, origin="web")
            db.add_room_binding(conn, "briefroom", "web", "briefroom")
            task_id = db.create_task(
                conn, prompt="brief", user_id=HOST, source_type="briefing",
                conversation_token="briefroom", output_target="web:briefroom",
                briefing_name="morning",
            )
        _run(config, raw, extra=(
            patch("istota.scheduler.run_coro", return_value=True),
            patch("istota.skills.briefing.save_briefing_digest"),
            patch("istota.scheduler._maybe_archive_briefing"),
        ))
        events, stored = assert_invariant(db_path, task_id, raw)
        assert stored == {"briefroom": "Rain at noon."}
        (result,) = [e for e in events if e["kind"] == "result"]
        assert "text" not in result["payload"]

    def test_silent_job_shows_its_action_not_the_prefix(self, config, db_path):
        raw = "ACTION: Water the plants."
        with db.get_db(db_path) as conn:
            db.register_room(conn, "jobroom", HOST, origin="talk")
            db.add_room_binding(conn, "jobroom", "talk", "jobroom")
            task_id = db.create_task(
                conn, prompt="check", user_id=HOST, source_type="scheduled",
                conversation_token="jobroom", heartbeat_silent=True,
            )
        _run(config, raw, extra=(
            patch("istota.scheduler.run_coro", return_value=True),
            patch("istota.scheduler.post_result_to_talk", new=MagicMock()),
        ))
        events, stored = assert_invariant(db_path, task_id, raw)
        assert stored == {"jobroom": "Water the plants."}
        (result,) = [e for e in events if e["kind"] == "result"]
        assert "text" not in result["payload"]

    def test_thread_park_is_a_confirmation_frame_and_no_thread_row(self, config, db_path):
        """The thread's parked question is the intended exception: a
        `confirmation` frame, answerable from the thread room, and no row."""
        task_id = _thread_task(config)
        raw = "Shall I tell Alice Thursday works? Please confirm."
        _run(config, raw, extra=(patch("istota.scheduler.run_coro", return_value=True),))
        events, stored = assert_invariant(db_path, task_id, raw)
        assert stored == {}
        assert [e["kind"] for e in events if e["kind"] in ("result", "confirmation")] \
            == ["confirmation"]


# ---------------------------------------------------------------------------
# The one decision, and its other readers
# ---------------------------------------------------------------------------


class TestTheVerbatimDecision:
    @pytest.mark.parametrize("source_type, silent, verbatim", [
        ("web", False, True),
        ("talk", False, True),
        ("repl", False, True),
        ("scheduled", False, True),
        ("email", False, False),
        ("briefing", False, False),
        ("scheduled", True, False),
    ])
    def test_which_tasks_store_the_result_unchanged(self, source_type, silent, verbatim):
        from istota.transport.registry import answer_is_stored_verbatim

        task = db.Task(id=1, status="running", source_type=source_type, user_id=HOST,
                       prompt="p", heartbeat_silent=silent)
        assert answer_is_stored_verbatim(task) is verbatim

    def test_a_transformed_task_streams_no_preview_even_on_a_stream_surface(self, config):
        task = db.Task(id=1, status="running", source_type="briefing", user_id=HOST,
                       prompt="p")
        writer = MagicMock()
        with patch("istota.transport.registry.task_is_stream_surface", return_value=True):
            adapter = TaskStreamAdapter(config, task, writer)
        adapter.on_event(TextDeltaEvent(text="raw json"))
        adapter.finish()
        kinds = [c.args[0] for c in writer.emit.call_args_list]
        assert "text_delta" not in kinds
        assert "thinking" not in kinds

    def test_the_synthetic_terminal_frame_follows_the_rule(self, config, db_path):
        pytest.importorskip("fastapi")
        from istota.webui import app as web_app

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="mail", user_id=HOST, source_type="email",
                conversation_token="thread-hash",
            )
            db.update_task_status(conn, task_id, "completed", result=PRIVATE_NOTE)
            web_id = db.create_task(
                conn, prompt="q", user_id=HOST, source_type="web",
                conversation_token="webroom", output_target="web",
            )
            db.update_task_status(conn, web_id, "completed", result="a")
        web_app._config = config
        frames = web_app._synthetic_terminal_events(task_id, after_seq=0)
        (result,) = [f for f in frames if f["kind"] == "result"]
        assert "text" not in result["payload"]
        assert PRIVATE_NOTE not in json.dumps(frames)
        frames = web_app._synthetic_terminal_events(web_id, after_seq=0)
        (result,) = [f for f in frames if f["kind"] == "result"]
        assert result["payload"]["text"] == "a"
