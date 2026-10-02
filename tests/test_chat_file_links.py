"""ISSUE-559: a `/chat/files` link the endpoint would refuse must not reach the
transcript as a broken image.

A browse screenshot taken without `-o` lands in the task's temp directory, which
`/chat/files` refuses to serve. A reply that embedded it anyway finished
`completed` and rendered a `?` icon. The answer is now checked against the
endpoint's own rule when it is stored.
"""

from urllib.parse import quote

import pytest
from unittest.mock import patch

from istota import db
from istota.webui.chat_files import ChatFileError, check_chat_file_links, resolve_chat_file
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.scheduler import process_one_task


def _url(path: str, fragment: str = "") -> str:
    return f"/istota/api/chat/files?path={quote(path, safe='')}{fragment}"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    mount = tmp_path / "mount"
    (mount / "Users" / "testuser" / "istota" / "exports").mkdir(parents=True)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        workspace_path=mount,
        temp_dir=tmp_path / "temp",
        users={"testuser": UserConfig(display_name="Alice")},
    )


@pytest.fixture
def scratch_png(tmp_path):
    shots = tmp_path / "temp" / "testuser" / "screenshots"
    shots.mkdir(parents=True)
    png = shots / "screenshot-20260928-190717.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    return png


@pytest.fixture
def workspace_png(config):
    png = config.workspace_path / "Users" / "testuser" / "istota" / "exports" / "ok.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    return png


class TestCheckChatFileLinks:
    def test_image_outside_the_workspace_becomes_a_visible_note(
        self, config, scratch_png, caplog,
    ):
        text = f"Here it is:\n\n![Date picker]({_url(str(scratch_png), '#w=1425&h=805')})\n"
        out = check_chat_file_links(config, "testuser", text, task_id=7)
        assert "/api/chat/files" not in out
        assert "Date picker (image unavailable: outside your workspace)" in out
        assert "Here it is:" in out
        assert any("task 7" in r.getMessage() for r in caplog.records)

    def test_an_image_in_the_workspace_is_left_alone(self, config, workspace_png):
        text = f"![ok]({_url('/Users/testuser/istota/exports/ok.png', '#w=10&h=10')})"
        assert check_chat_file_links(config, "testuser", text) == text

    def test_a_missing_file_and_a_plain_link_are_both_rewritten(self, config):
        text = (
            f"[report.csv]({_url('/Users/testuser/istota/report.csv')}) and "
            f"![]({_url('/Users/testuser/istota/gone.png')})"
        )
        out = check_chat_file_links(config, "testuser", text)
        assert out == (
            "report.csv (file unavailable: file not found) and "
            "(image unavailable: file not found)"
        )

    def test_another_users_workspace_is_refused(self, config, tmp_path):
        other = config.workspace_path / "Users" / "bob" / "x.png"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"\x89PNG\r\n\x1a\n")
        text = f"![x]({_url('/Users/bob/x.png')})"
        assert "image unavailable: outside your workspace" in check_chat_file_links(
            config, "testuser", text,
        )

    def test_no_workspace_mount_leaves_the_text_unchanged(self, config, scratch_png):
        config.workspace_path = None
        text = f"![x]({_url('/Users/testuser/istota/gone.png')})"
        assert check_chat_file_links(config, "testuser", text) == text

    @pytest.mark.parametrize("wrap", [
        "```md\n{link}\n```",
        "~~~\n{link}\n~~~",
        "Write `{link}` to embed.",
        "Write ``{link}`` to embed.",
        "```\nunclosed\n{link}\n",
    ])
    def test_a_link_shown_as_code_is_left_alone(self, config, wrap):
        """markdown renders these as text, so nothing there was ever broken."""
        text = wrap.format(link=f"![x]({_url('/Users/testuser/istota/nope.png')})")
        assert check_chat_file_links(config, "testuser", text) == text

    def test_code_elsewhere_does_not_shield_a_real_link(self, config):
        text = (
            "Run `ls` first.\n\n```\ncode\n```\n\n"
            f"![x]({_url('/Users/testuser/istota/nope.png')})"
        )
        assert "(image unavailable: file not found)" in check_chat_file_links(
            config, "testuser", text,
        )

    def test_a_backslash_escaped_destination_is_read_as_the_browser_reads_it(
        self, config,
    ):
        shot = config.workspace_path / "Users" / "testuser" / "istota" / "exports" / "my_shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        text = "![s](/istota/api/chat/files?path=/Users/testuser/istota/exports/my\\_shot.png)"
        assert check_chat_file_links(config, "testuser", text) == text

    def test_an_absolute_url_is_not_judged(self, config):
        text = f"![x](https://other.example{_url('/Users/testuser/istota/nope.png')})"
        assert check_chat_file_links(config, "testuser", text) == text

    def test_many_unclosed_backticks_stay_linear(self, config):
        import time
        text = ("`" * 3 + "x ") * 20000 + f"![x]({_url('/Users/testuser/istota/nope.png')})"
        started = time.monotonic()
        check_chat_file_links(config, "testuser", text)
        assert time.monotonic() - started < 2.0

    def test_the_endpoint_and_the_check_share_one_rule(self, config, scratch_png):
        with pytest.raises(ChatFileError) as caught:
            resolve_chat_file(config, "testuser", str(scratch_png))
        assert caught.value.status == 403


class TestTheStoredAnswerIsChecked:
    def test_a_web_answer_embedding_a_scratch_capture_is_stored_rewritten(
        self, db_path, config, scratch_png,
    ):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "webroom", "testuser", origin="web")
            db.add_room_binding(conn, "webroom", "web", "webroom")
            task_id = db.create_task(
                conn, prompt="show me", user_id="testuser",
                source_type="web", conversation_token="webroom",
                output_target="web",
            )
        answer = f"Still open:\n\n![Date picker]({_url(str(scratch_png))})"

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, answer, None, None),
        ):
            outcome = process_one_task(config)
        assert outcome is not None and outcome[1] is True

        with db.get_db(db_path) as conn:
            msg_id = db.get_turn_message_id(conn, "webroom", task_id, "assistant")
            body = conn.execute(
                "SELECT body FROM messages WHERE id = ?", (msg_id,),
            ).fetchone()[0]
            events = db.get_task_events(conn, task_id, 0)
            task = db.get_task(conn, task_id)

        expected = "Still open:\n\nDate picker (image unavailable: outside your workspace)"
        assert body == expected
        assert task.result == expected
        result_events = [e for e in events if e["kind"] == "result"]
        assert result_events and result_events[-1]["payload"]["text"] == expected
