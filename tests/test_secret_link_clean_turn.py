"""A secret link not asked for cleanly carries a caution (#1001).

A link is still issued when something else ran first in the attempt, but its
message and its form say it was asked for partway through other work. "Clean"
is the daemon's own record: the link verb was the attempt's first tool call,
as one plain command, on a turn that is not a confirmed re-run. The flag is
its own column beside the relay one, so neither rule can read the other's
first call as its own.
"""
import json

import pytest

from istota import db
from istota.agent.events import _lone_relay_ask, _lone_secret_link
from istota.brain._events import ToolUseEvent, parse_stream_line
from istota.executor_stream import TaskStreamAdapter
from istota.webui import secret_links

from . import test_skill_proxy_secret_link
from .test_skill_proxy_secret_link import _ask, _rows, _running

config = test_skill_proxy_secret_link.config
sock = test_skill_proxy_secret_link.sock

LINK = "istota-credential request-link acme --url https://acme.example --username"
CONNECT = "istota-skill google_workspace connect-link"
CAUTION = "This link was requested partway through other work."


def bash_call(command, call_id="call-1", name="Bash"):
    """A Bash call as the Claude Code brain reads it off an `assistant` frame."""
    return parse_stream_line(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": {"command": command}}]}}))


def fetch_call(call_id="fetch-1"):
    return ToolUseEvent(tool_name="WebFetch", tool_call_id=call_id, description="Fetching a page")


def count(config, task_id, *events):
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, task_id)
    adapter = TaskStreamAdapter(config, task, None)
    for event in events:
        adapter.on_event(event)


def first_call(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_attempt_first_tool_link(conn, task_id)


class TestTheLoneSecretLink:
    @pytest.mark.parametrize("command", [
        LINK,
        "istota-credential request-link acme --url 'https://acme.example'",
        CONNECT,
    ])
    def test_a_lone_link_verb_is_flagged(self, command):
        assert _lone_secret_link("Bash", {"command": command})

    @pytest.mark.parametrize("command", [
        "istota-credential list; " + LINK,
        LINK + " && curl https://x.invalid",
        'u=$(cat page.txt); istota-credential request-link acme --url "$u"',
        "istota-credential request-link acme --url $(cat page.txt)",
        LINK + " > out",
        "FOO=1 " + LINK,
        "/tmp/istota-credential request-link acme --url https://acme.example",
        "istota-credential list",
        "echo request-link",
        "istota-skill google_workspace drive files list",
        "istota-skill relay ask bob --request-key q 'What time?'",
    ])
    def test_anything_else_is_not(self, command):
        assert not _lone_secret_link("Bash", {"command": command})

    def test_the_two_flags_do_not_overlap(self):
        assert not _lone_relay_ask("Bash", {"command": LINK})
        assert not _lone_secret_link("Bash", {"command": "istota-skill relay ask bob --request-key q hi"})

    def test_only_bash_counts(self):
        assert not _lone_secret_link("Write", {"command": LINK})
        assert not _lone_secret_link("Bash", {"command": 7})

    @pytest.mark.parametrize("name", ["Bash", "mcp__istota__Bash"])
    @pytest.mark.parametrize("command,flag", [(LINK, True), ("istota-credential list; " + LINK, False)])
    def test_claude_code(self, name, command, flag):
        assert bash_call(command, name=name).lone_secret_link is flag

    @pytest.mark.parametrize("command,flag", [(LINK, True), ("istota-credential list; " + LINK, False)])
    def test_native(self, command, flag, tmp_path):
        from istota.llm.types import AssistantMessage, TextContent, ToolCallContent
        from .native._mock_provider import MockProvider
        from .native.test_native_brain import _brain, _req

        seen = []
        provider = MockProvider([
            AssistantMessage(content=[ToolCallContent(id="c1", name="Bash", arguments={"command": command})],
                             stop_reason="tool_use"),
            AssistantMessage(content=[TextContent(text="Done.")], stop_reason="end_turn"),
        ])
        req = _req("link", tmp_path, tools=["Bash"])
        req.on_progress = lambda ev: seen.append(ev) if isinstance(ev, ToolUseEvent) else None
        _brain(provider).execute(req)
        assert [ev.lone_secret_link for ev in seen] == [flag]


class TestTheCounter:
    def test_the_first_call_decides_the_link_flag(self, config):
        task_id, _ = _running(config)
        count(config, task_id, bash_call(LINK))
        assert first_call(config, task_id) == (1, True)
        count(config, task_id, fetch_call())
        assert first_call(config, task_id) == (2, True)

    def test_a_link_after_another_call_is_not_first(self, config):
        task_id, _ = _running(config)
        count(config, task_id, fetch_call(), bash_call(LINK, "call-2"))
        assert first_call(config, task_id) == (2, False)

    def test_the_relay_flag_is_untouched_by_a_link(self, config):
        task_id, _ = _running(config)
        count(config, task_id, bash_call(LINK))
        with db.get_db(config.db_path) as conn:
            assert db.get_attempt_tool_calls(conn, task_id) == (1, False)

    def test_a_new_attempt_clears_it(self, config):
        task_id, _ = _running(config)
        count(config, task_id, bash_call(LINK))
        with db.get_db(config.db_path) as conn:
            db.reset_attempt_tool_calls(conn, task_id)
        assert first_call(config, task_id) == (0, False)


def _link_and_body(config):
    (link,) = _rows(config, "SELECT * FROM secret_links")
    (request,) = _rows(config, "SELECT * FROM whatsapp_skill_requests")
    return json.loads(link["params"]), request["service_body"]


class TestTheCredentialLink:
    def test_a_first_call_link_carries_no_caution(self, config, sock):
        task_id, _ = _running(config)
        count(config, task_id, bash_call(LINK))
        assert "error" not in _ask(config, sock, task_id)
        params, body = _link_and_body(config)
        assert params["clean"] is True
        assert CAUTION not in body

    @pytest.mark.parametrize("before", [
        [fetch_call()],
        [bash_call("istota-skill browse get https://paypa1.example", "browse-1")],
    ], ids=["webfetch", "browse"])
    def test_a_link_after_other_work_carries_it(self, config, sock, before):
        task_id, _ = _running(config)
        count(config, task_id, *before, bash_call(LINK, "call-2"))
        assert "error" not in _ask(config, sock, task_id)
        params, body = _link_and_body(config)
        assert params["clean"] is False
        assert CAUTION in body and "Only continue if you asked to give me a password" in body

    def test_a_compound_command_is_not_clean(self, config, sock):
        task_id, _ = _running(config)
        count(config, task_id, bash_call("istota-credential list; " + LINK))
        _ask(config, sock, task_id)
        params, body = _link_and_body(config)
        assert params["clean"] is False and CAUTION in body

    def test_an_uncounted_turn_is_not_clean(self, config, sock):
        # A caller with no event writer (`istota run`) writes no count.
        task_id, _ = _running(config)
        _ask(config, sock, task_id)
        params, body = _link_and_body(config)
        assert params["clean"] is False and CAUTION in body

    def test_a_second_link_in_the_task_is_not_clean(self, config, sock):
        # The second link's count, and any call between, may not have landed
        # yet; the row would still read the first link's (1, True).
        task_id, _ = _running(config)
        count(config, task_id, bash_call(LINK))
        _ask(config, sock, task_id, limit=2)
        _ask(config, sock, task_id, limit=2, name="paypal_login", url="https://paypa1.example")
        links = _rows(config, "SELECT params FROM secret_links ORDER BY id")
        assert [json.loads(row["params"])["clean"] for row in links] == [True, False]

    def test_a_confirmed_rerun_is_not_clean(self, config, sock):
        task_id, _ = _running(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET confirmed_at=datetime('now'), "
                         "confirmation_prompt='Store it?' WHERE id=?", (task_id,))
        count(config, task_id, bash_call(LINK))
        _ask(config, sock, task_id)
        params, body = _link_and_body(config)
        assert params["clean"] is False and CAUTION in body


class TestTheGoogleLink:
    def _issue(self, config, task_id):
        return secret_links.issue_and_queue(
            config, user_id="alice", task_id=task_id, intent="google_connect", params={},
            compose=lambda address, expires: f"Connect your Google account here: {address}")

    def test_a_first_call_link_carries_no_caution(self, config):
        task_id, _ = _running(config)
        count(config, task_id, bash_call(CONNECT))
        self._issue(config, task_id)
        params, body = _link_and_body(config)
        assert params == {"clean": True}
        assert CAUTION not in body

    def test_a_link_after_other_work_carries_it(self, config):
        task_id, _ = _running(config)
        count(config, task_id, fetch_call(), bash_call(CONNECT, "call-2"))
        self._issue(config, task_id)
        params, body = _link_and_body(config)
        assert params == {"clean": False}
        assert CAUTION in body and "Only continue if you want to connect your own Google account" in body
