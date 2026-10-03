"""Tests for !command dispatch system."""

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from istota import db
from istota.usage import subscription as _subscription_usage
from istota.commands import (
    COMMANDS, _COMMAND_ALIASES, _MAX_TASK_ID_DIGITS,
    CommandContext, CommandResult,
    _build_export_metadata, _cancel_one, _format_history_markdown,
    _format_history_text, _parse_export_metadata, _parse_search_args,
    _search_memory, _search_talk_api,
    brain_for_room, cmd_brain, cmd_check, cmd_cron, cmd_export, cmd_help,
    cmd_memory, cmd_models, cmd_more, cmd_room, cmd_search, cmd_skills,
    cmd_status, cmd_stop, cmd_trust, cmd_untrust, cmd_usage,
    dispatch, is_model_prefix, parse_command, parse_task_id,
    model_prefix_usage, parse_model_prefix, resolve_model_prefix,
    resolve_room_name,
)
from istota.brain import BrainConfig, make_brain, set_alias_overrides
from istota.brain.claude_code import DEFAULT_ALIASES, HAIKU, OPUS, SONNET
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig
from istota.memory.knowledge_graph import add_fact, ensure_table

from .support.rooms import plain_talk_room

# The root conftest neutralizes `subscription_usage.get_snapshot` for the whole
# suite, so a doctor sweep on a laptop cannot read the real keychain or reach the
# real endpoint. `TestCmdUsage`'s shared-cache case is the one test here that
# wants the real policy, so the real function is captured at import — before that
# autouse fixture ever runs — and reinstated for that test alone.
_REAL_GET_SNAPSHOT = _subscription_usage.get_snapshot


@pytest.fixture
def brain():
    """Default brain instance for parser tests — ClaudeCodeBrain."""
    set_alias_overrides({})
    return make_brain(BrainConfig(kind="claude_code"))


@pytest.fixture
def db_path(tmp_path):
    """Create and initialize a temporary SQLite database."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path, tmp_path):
    """Create a Config object with tmp paths and test DB."""

    def _make(**overrides):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.nextcloud = NextcloudConfig(
            url="https://nc.test", username="istota", app_password="pass"
        )
        config.users = {"alice": UserConfig()}
        config.scheduler = SchedulerConfig()
        config.workspace_path = tmp_path / "mount"
        (config.workspace_path / "Users" / "alice" / "istota" / "config").mkdir(
            parents=True, exist_ok=True
        )
        (config.workspace_path / "Channels" / "room1").mkdir(
            parents=True, exist_ok=True
        )
        for key, val in overrides.items():
            setattr(config, key, val)
        return config

    return _make


def _ctx(config, conn, user_id="alice", conversation_token="room1", args="",
         surface="talk", registry=None):
    """Build a CommandContext for invoking handlers directly in tests."""
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=conversation_token, args=args,
        surface=surface, registry=registry,
    )


def _task(conn, prompt="Do something long", *, user_id="alice",
          source_type="talk", token="room1", status="running", **kwargs):
    """Create a task and move it to `status` (None leaves it pending)."""
    task_id = db.create_task(
        conn, prompt=prompt, user_id=user_id, source_type=source_type,
        conversation_token=token, **kwargs,
    )
    if status:
        db.update_task_status(conn, task_id, status)
    return task_id


def _background(conn, prompt="Compile a daily film-business digest", **kwargs):
    """A running background task, which sits in no room."""
    return _task(
        conn, prompt, source_type="scheduled", token=None, queue="background",
        **kwargs,
    )


def _set_created_at(conn, stamps):
    for task_id, created_at in stamps.items():
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, task_id)
        )
    conn.commit()


class _RecordingConn:
    """A real connection that writes `"commit"` into a shared list when it is
    committed, so a handler's ordering can be asserted rather than only the
    fact of a commit. Everything else is delegated."""

    def __init__(self, conn, order):
        self._conn = conn
        self._order = order

    def commit(self):
        self._order.append("commit")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# =============================================================================
# TestParseCommand
# =============================================================================


class TestParseCommand:
    @pytest.mark.parametrize("text,expected", [
        ("!stop", ("stop", "")),
        ("!status foo bar", ("status", "foo bar")),
        ("!HELP", ("help", "")),
        ("!Stop", ("stop", "")),
        ("hello world", None),
        ("", None),
        ("!", None),
        ("! space", None),
        ("  !help", ("help", "")),
        ("!cmd line1\nline2", ("cmd", "line1\nline2")),
    ])
    def test_parse_command(self, text, expected):
        assert parse_command(text) == expected


# =============================================================================
# TestParseModelPrefix
# =============================================================================


class TestParseModelPrefix:
    # !modelfoo / !models must not be parsed as a model prefix.
    @pytest.mark.parametrize(
        "text", ["hello world", "!stop", "", "!modelfoo bar", "!models"],
    )
    def test_not_a_model_prefix_returns_none(self, brain, text):
        assert parse_model_prefix(text, brain) is None

    @pytest.mark.parametrize("text,unknown,model,effort,remainder", [
        ("!model opus draft a spec for X", None, OPUS, None, "draft a spec for X"),
        ("!model opus:high tackle this", None, OPUS, "high", "tackle this"),
        ("!model haiku one-liner", None, HAIKU, None, "one-liner"),
        ("!model opus:xhigh think hard", None, OPUS, "xhigh", "think hard"),
        ("!model opus:max go deepest", None, OPUS, "max", "go deepest"),
        ("!model smart:low quick", None, OPUS, "low", "quick"),
        # HARD CUT: the old ``opus-high`` spelling no longer resolves.
        ("!model opus-high tackle this", "opus-high", None, None, "tackle this"),
        ("!model opus:turbo do it", "opus:turbo", None, None, "do it"),
        ("!model default just do it", None, None, None, "just do it"),
        # remainder still captured so the caller can decide what to do
        ("!model gpt-4 do a thing", "gpt-4", None, None, "do a thing"),
        ("!model", "", None, None, ""),
        ("!model opus", None, OPUS, None, ""),
        ("!MODEL OPUS draft something", None, OPUS, None, "draft something"),
        ("  !model sonnet hi", None, SONNET, None, "hi"),
        ("!model opus line1\nline2\nline3", None, OPUS, None, "line1\nline2\nline3"),
        # The retired ``opus-47-high`` need is met by canonical id + modifier.
        ("!model claude-opus-4-7:high run a job", None, "claude-opus-4-7", "high",
         "run a job"),
    ], ids=[
        "opus", "effort-high", "haiku", "effort-xhigh", "effort-max",
        "effort-on-tier", "removed-dash-effort", "unknown-effort", "default",
        "unknown-alias", "no-alias", "alias-only", "case-insensitive",
        "leading-whitespace", "multiline", "canonical-id-plus-effort",
    ])
    def test_parse(self, brain, text, unknown, model, effort, remainder):
        result = parse_model_prefix(text, brain)
        assert result is not None
        assert result.unknown_alias == unknown
        assert result.model == model
        assert result.effort == effort
        assert result.remainder == remainder

    @pytest.mark.parametrize("text,prefix,remainder", [
        ("!model smart think hard", "claude-opus-", "think hard"),
        ("!model fast quick answer", "claude-haiku-", "quick answer"),
        ("!model general write a draft", "claude-sonnet-", "write a draft"),
    ])
    def test_role_alias_resolves_with_no_effort(self, brain, text, prefix, remainder):
        result = parse_model_prefix(text, brain)
        assert result is not None
        assert result.unknown_alias is None
        assert result.model is not None and result.model.startswith(prefix)
        assert result.effort is None
        assert result.remainder == remainder

    def test_aliases_are_pinned_to_specific_model_ids(self):
        # Guard against accidental alias-table changes that drop pinning.
        # Reaches into the ClaudeCodeBrain's table directly since pinning
        # is a brain-implementation invariant, not user-facing behaviour.
        for alias, (model, _effort) in DEFAULT_ALIASES.items():
            if alias == "default":
                assert model is None, "default alias must not pin a model"
                continue
            assert model is not None, f"alias {alias} must pin a model"
            assert model.startswith("claude-"), (
                f"alias {alias} must use a versioned claude-* model id, got {model}"
            )

    def test_shipped_aliases_carry_no_baked_effort(self):
        # Effort is the orthogonal :effort modifier — no shipped alias bakes it.
        for alias, (_model, effort) in DEFAULT_ALIASES.items():
            assert effort is None, f"alias {alias} should not bake an effort"

    def test_usage_string_lists_all_aliases(self, brain):
        usage = model_prefix_usage(brain)
        for alias in DEFAULT_ALIASES:
            assert alias in usage
        # Roles surface via brain.list_aliases(), so they appear too.
        assert "smart" in usage
        assert "general" in usage
        assert "fast" in usage


# =============================================================================
# TestDispatch
# =============================================================================


class TestResolveModelPrefix:
    """The shared cross-surface !model rule (Talk + web both call this)."""

    def test_not_a_prefix(self, brain):
        out = resolve_model_prefix("just a normal message", brain)
        assert out.matched is False
        assert out.content == "just a normal message"
        assert out.model is None
        assert out.usage is None

    def test_valid_alias_with_prompt(self, brain):
        out = resolve_model_prefix("!model opus summarize this", brain)
        assert out.matched is True
        assert out.usage is None
        assert out.model == OPUS
        assert out.content == "summarize this"

    def test_unknown_alias_yields_usage(self, brain):
        out = resolve_model_prefix("!model bogus do it", brain)
        assert out.matched is True
        assert out.usage is not None
        assert "Aliases" in out.usage

    def test_alias_only_no_attachment_yields_usage(self, brain):
        out = resolve_model_prefix("!model opus", brain, has_attachments=False)
        assert out.matched is True
        assert out.usage is not None

    def test_alias_only_with_attachment_is_valid(self, brain):
        out = resolve_model_prefix("!model opus", brain, has_attachments=True)
        assert out.matched is True
        assert out.usage is None
        assert out.model == OPUS
        assert out.content == ""

    def test_default_alias_clears_overrides(self, brain):
        out = resolve_model_prefix("!model default keep going", brain)
        assert out.matched is True
        assert out.usage is None
        assert out.model is None
        assert out.effort is None
        assert out.content == "keep going"


class _FakePushTransport:
    """Minimal push transport that records what dispatch delivers to it."""

    def __init__(self):
        from istota.transport._types import TransportCapabilities
        self.capabilities = TransportCapabilities(surface_class="push")
        self.delivered: list[tuple[str, str]] = []

    async def deliver(self, target, text, **kwargs):
        self.delivered.append((target, text))
        return 1

    async def resolve_channel_name(self, token):
        return token


class _FakeRegistry:
    def __init__(self, transport):
        self._t = transport

    def get(self, name):
        return self._t


async def _dispatch(config, text, transport=None, **kwargs):
    with db.get_db(config.db_path) as conn:
        return await dispatch(
            config, "alice", "room1", text,
            conn=conn, registry=_FakeRegistry(transport), **kwargs,
        )


class TestDispatch:
    async def test_non_command_returns_not_handled(self, make_config):
        transport = _FakePushTransport()
        result = await _dispatch(make_config(), "hello world", transport)
        assert isinstance(result, CommandResult)
        assert result.handled is False
        assert transport.delivered == []

    async def test_known_command_delivered_on_push_surface(self, make_config):
        transport = _FakePushTransport()
        result = await _dispatch(make_config(), "!help", transport)
        assert result.handled is True
        assert result.delivered is True
        assert len(transport.delivered) == 1
        assert "!help" in transport.delivered[0][1]

    async def test_unknown_command_posts_error(self, make_config):
        transport = _FakePushTransport()
        result = await _dispatch(make_config(), "!nonexistent", transport)
        assert result.handled is True
        msg = transport.delivered[0][1]
        assert "Unknown command" in msg
        assert "!nonexistent" in msg
        assert "!help" in msg

    async def test_stream_surface_returns_text_undelivered(self, make_config):
        """On a stream surface (no push transport) the result comes back inline."""
        result = await _dispatch(make_config(), "!help", None, surface="web")
        assert result.handled is True
        assert result.delivered is False
        assert "!help" in (result.text or "")


# =============================================================================
# TestCmdHelp
# =============================================================================


class TestCmdHelp:
    async def test_lists_all_commands(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_help(_ctx(config, conn))

        # "opus": at least one alias surfaces so users can discover them from !help
        for needle in (
            "!help", "!stop", "!status", "!memory", "!model", "opus",
            "!check", "!export", "!search",
        ):
            assert needle in result, needle


# =============================================================================
# TestParseTaskId
# =============================================================================


class TestParseTaskId:
    """The one id parser the five id-taking commands share.

    Each had grown its own and they had drifted — `!retry` still tested
    `isdigit`, True for '²' and refused by `int()`, so a typo came back as
    "Command `!retry` failed: invalid literal for int()". All of them had the
    second half of the bug regardless: a long run of digits passes every
    character test, converts fine in Python, and raises `OverflowError` out of
    sqlite3 on the way into the query.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("5", 5),
            ("  5  ", 5),
            ("#5", 5),
            ("331505", 331505),
            ("", None),
            ("#", None),          # a lone hash is a malformed id, not "no id"
            ("##5", None),        # one hash is stripped, not a run of them
            ("please", None),
            ("12x", None),
            ("-1", None),
            ("²", None),          # isdigit accepts it; int() does not
            ("٥", None),          # isdecimal accepts it; the guard is ASCII
            ("9" * 30, None),     # OverflowError out of sqlite3
        ],
    )
    def test_it_accepts_only_a_bounded_ascii_decimal(self, text, expected):
        assert parse_task_id(text) == expected

    def test_the_bound_sits_inside_what_sqlite_can_store(self):
        """The guard has to refuse before `int()` reaches the driver, so the
        accepted maximum must itself be storable."""
        biggest = parse_task_id("9" * _MAX_TASK_ID_DIGITS)
        assert biggest is not None
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t (id) VALUES (?)", (biggest,))
        assert parse_task_id("9" * (_MAX_TASK_ID_DIGITS + 1)) is None


# =============================================================================
# TestCmdStop
# =============================================================================


def _cancelled(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.is_task_cancelled(conn, task_id)


def _status(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id).status


class TestCmdStop:
    async def test_no_active_task(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_stop(_ctx(config, conn))
        assert "No active task" in result

    async def test_cancels_running_task(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn)
            result = await cmd_stop(_ctx(config, conn))

        assert f"#{task_id}" in result
        assert "Cancelling" in result
        assert _cancelled(config, task_id) is True

    async def test_cancels_pending_confirmation(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "Do risky thing", status=None)
            db.set_task_confirmation(conn, task_id, "Are you sure?")
            result = await cmd_stop(_ctx(config, conn))

        assert f"#{task_id}" in result

        # `cancel_requested` is read only for `running`/`locked` rows
        # (`recover_orphaned_tasks`, the executor's cancel check), so flipping it
        # on a parked confirmation said "Cancelling" and left the task waiting
        # until `expire_stale_confirmations` reaped it two hours later, inbox row
        # still open. The web cancel button already declined through the shared
        # verb; this path now does too.
        assert _status(config, task_id) == "cancelled"

    async def test_only_cancels_own_tasks(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "Bob's task", user_id="bob")
            result = await cmd_stop(_ctx(config, conn))

        assert "No active task" in result
        assert _cancelled(config, task_id) is False

    # -- room scoping (ISSUE-487) -------------------------------------------

    async def test_bare_stop_ignores_a_newer_task_in_another_room(self, make_config):
        """The reported incident, reproduced.

        A background job queued five seconds after the user's own task won the
        `ORDER BY created_at DESC` and was cancelled instead — invisibly, since
        a background task is in no room. Both `created_at` values are set
        explicitly so the old ordering has an unambiguous winner and this cannot
        pass on a tie-break.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            watched = _task(conn, "The task being watched", source_type="web")
            background = _background(conn)
            _set_created_at(conn, {
                watched: "2026-09-11T00:45:04", background: "2026-09-11T00:45:09",
            })
            result = await cmd_stop(_ctx(config, conn))

        assert f"#{watched}" in result
        assert f"#{background}" not in result
        assert _cancelled(config, watched) is True
        assert _cancelled(config, background) is False

    async def test_bare_stop_resolves_a_surface_ref_to_the_canonical_room(
        self, make_config
    ):
        """Talk hands `dispatch` its raw conversation token while the task
        stores the canonical room token. Without `resolve_room_token` the two
        never match on a promoted room, and `!stop` there would answer that the
        room is idle."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "web-canonical", "alice", origin="web")
            db.add_room_binding(conn, "web-canonical", "talk", "talkref9")
            task_id = _task(conn, "Running in the promoted room", token="web-canonical")
            # A newer task the user owns elsewhere, so resolving the ref is what
            # decides the answer rather than there being only one candidate.
            elsewhere = _background(conn)
            _set_created_at(conn, {
                task_id: "2026-09-11T00:45:04", elsewhere: "2026-09-11T00:45:09",
            })
            result = await cmd_stop(
                _ctx(config, conn, "alice", "talkref9", "", surface="talk")
            )

        assert f"#{task_id}" in result
        assert _cancelled(config, task_id) is True
        assert _cancelled(config, elsewhere) is False

    async def test_an_idle_room_does_not_widen_to_the_whole_user(self, make_config):
        """No silent fallback. The old query's user scope *is* the bug, so
        reaching for it when the room is idle would reinstate it."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            elsewhere = _background(conn)
            conn.commit()
            result = await cmd_stop(_ctx(config, conn))

        assert "No active task in this room" in result
        # The id is offered so the user can aim the next one.
        assert f"#{elsewhere}" in result
        assert "!stop" in result
        assert _cancelled(config, elsewhere) is False

    async def test_the_idle_room_listing_carries_no_prompt_text(self, make_config):
        """The listing is posted into the room `!stop` was typed in, and every
        task on it is in some *other* room — on Talk, possibly in front of other
        people. So it carries ids and nothing else."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(
                conn, "the private prompt nobody here should read",
                source_type="scheduled", token="another-room", queue="background",
            )
            conn.commit()
            result = await cmd_stop(_ctx(config, conn))

        listing = [ln for ln in result.splitlines() if ln.startswith("- ")]
        assert len(listing) == 1
        assert f"#{task_id}" in listing[0]
        assert "private prompt" not in result
        assert "[scheduled]" in listing[0]

    async def test_a_held_email_never_has_its_withheld_body_echoed(
        self, make_config
    ):
        """`tasks.prompt` for a gated email is the body that has *not* been
        approved — the one string `confirmations.describe` exists not to print
        (`.claude/rules/notifications.md`). The cancel ack goes through
        `describe`, so it names the task without quoting it."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(
                conn, "SECRETBODY wire the money to account 12345",
                source_type="email", status=None,
            )
            db.set_task_confirmation(conn, task_id, "Process this email?")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn))

        assert f"#{task_id}" in result
        assert "SECRETBODY" not in result
        assert _status(config, task_id) == "cancelled"

    async def test_a_task_that_finished_mid_command_is_not_acknowledged(
        self, make_config
    ):
        """`_cancel_one` reads the row itself rather than trusting the status
        its caller selected on. Taking the caller's word is a race: a row that
        moved to `running` in the gap would be flipped to `cancelled` by
        `db.cancel_task` — which has no status predicate — and returned before
        the kill, leaving the worker alive with `cancel_requested` never set."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "Already finished", status="completed")
            conn.commit()
            # Exactly what a caller holding a stale `running` read would do.
            result = _cancel_one(_ctx(config, conn), task_id)

        assert "no longer active" in result
        assert _status(config, task_id) == "completed"
        assert _cancelled(config, task_id) is False

    # -- targeted stop (ISSUE-487) ------------------------------------------

    @pytest.mark.parametrize("prefix", ["", "#"], ids=["bare-id", "hash-prefix"])
    async def test_a_task_id_targets_that_task(self, make_config, prefix):
        """Deliberately *not* room-scoped: killing a runaway background job is
        the case an explicit id exists for. The target is the *older* task and
        sits in no room, so neither the room scope nor the `created_at`
        ordering can reach it by accident."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            background = _background(conn)
            in_room = _task(conn, "The task being watched", source_type="web")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args=f"{prefix}{background}"))

        assert f"#{background}" in result
        assert _cancelled(config, background) is True
        assert _cancelled(config, in_room) is False

    async def test_a_targeted_stop_refuses_another_users_task(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()
        # A non-empty admin list not naming alice: an *empty* one reads as
        # "everyone is admin" (`Config.is_admin`), which would exempt her from
        # the check this test is about.
        config.admin_users = ["carol"]
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "Bob's task", user_id="bob", token="room2")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args=str(task_id)))

        # One message for "no such task" and "not yours", so the command cannot
        # become an oracle for which ids exist — the rule `!confirm` already
        # applies to the same question.
        assert "isn't yours to stop" in result
        assert _cancelled(config, task_id) is False

    async def test_an_admin_may_target_another_users_task(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()
        config.admin_users = ["alice"]
        with db.get_db(config.db_path) as conn:
            task_id = _background(conn, "Bob's runaway task", user_id="bob")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args=str(task_id)))

        assert f"#{task_id}" in result
        assert _cancelled(config, task_id) is True

    async def test_an_unknown_id_cancels_nothing(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            in_room = _task(conn, "The task being watched", source_type="web")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args="999999"))

        assert "999999" in result
        assert _cancelled(config, in_room) is False

    async def test_a_finished_task_is_not_restopped(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            done = _task(conn, "Already finished", status="completed")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args=str(done)))

        assert "completed" in result
        assert _cancelled(config, done) is False

    @pytest.mark.parametrize(
        "junk",
        ["please", "now", "the digest", "12x", "²", "9" * 30, "#"],
        ids=["please", "now", "the-digest", "12x", "superscript", "enormous",
             "bare-hash"],
    )
    async def test_a_malformed_argument_cancels_nothing(self, make_config, junk):
        """`!stop please` used to cancel whatever the user-wide query found, so
        typing a word made the outcome less predictable rather than more.

        `isdecimal`, not `isdigit`: the latter is True for '²', which `int()`
        then refuses — turning a typo into a traceback. `isdecimal` is not
        enough on its own either: '9' * 30 converts fine in Python, then raises
        `OverflowError` out of sqlite3 on the way into the query. And
        `'#'.lstrip('#')` is the empty string, so a repeated strip would read
        `!stop #` as a bare `!stop` and cancel the room's task — a user who
        typed `#` was aiming at something.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn)
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args=junk))

        assert "Usage:" in result
        assert _cancelled(config, task_id) is False

    async def test_an_empty_room_token_does_not_match_tokenless_tasks(
        self, make_config
    ):
        """Heartbeat and briefing settings default `conversation_token` to `""`
        rather than NULL, and `= ?` does match `''`. No shipped caller passes an
        empty token, but `dispatch` neither defaults nor validates it, so the
        guard is local rather than spread across four transports."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = _task(
                conn, "A heartbeat task", source_type="heartbeat", token="",
                queue="background",
            )
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, "alice", "", ""))

        assert "No active task in this room" in result
        assert _cancelled(config, task_id) is False

    async def test_an_admin_may_not_discard_another_users_held_task(
        self, make_config
    ):
        """The admin exemption reaches a runaway *running* task, which is what
        it exists for, and stops at a held one. Discarding another user's
        `pending_confirmation` task throws away inbound mail they have not read
        and closes their notification row — and `!confirm`, the command for
        answering those, has no admin exemption at all (it scopes to
        `pending_for_user`). Reaching it through `!stop` would be a back door
        onto an action the front door refuses.
        """
        config = make_config()
        config.users["bob"] = UserConfig()
        config.admin_users = ["alice"]
        with db.get_db(config.db_path) as conn:
            task_id = _task(
                conn, "SECRETBODY from an unknown sender", user_id="bob",
                source_type="email", token="room2", status=None,
            )
            db.set_task_confirmation(conn, task_id, "Process this email?")
            conn.commit()
            result = await cmd_stop(_ctx(config, conn, args=str(task_id)))

        assert "bob" in result
        assert "SECRETBODY" not in result
        assert _status(config, task_id) == "pending_confirmation"


# =============================================================================
# TestCmdStatus
# =============================================================================


class TestCmdStatus:
    async def test_no_tasks(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_status(_ctx(config, conn))
        assert "No active or pending tasks" in result
        assert "System:" in result

    async def test_shows_user_tasks(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _task(conn, "Task one", token=None, status=None)
            _task(conn, "Task two", token=None)
            result = await cmd_status(_ctx(config, conn))

        assert "Your tasks (2)" in result
        assert "Task one" in result
        assert "Task two" in result
        assert "[running]" in result

    async def test_excludes_other_users(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            _task(conn, "Bob's task", user_id="bob", token=None, status=None)
            result = await cmd_status(_ctx(config, conn))

        assert "No active or pending tasks" in result
        # But system stats should show bob's pending task
        assert "1 queued" in result

    async def test_system_stats(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()

        with db.get_db(config.db_path) as conn:
            _task(conn, "Running", user_id="bob", token=None)
            _task(conn, "Pending", token=None, status=None)
            result = await cmd_status(_ctx(config, conn))

        assert "1 running" in result
        assert "1 queued" in result

    async def test_system_stats_hidden_for_non_admin(self, make_config):
        config = make_config()
        config.users["bob"] = UserConfig()
        # Non-empty admin_users with alice excluded → alice is non-admin
        config.admin_users = {"someone_else"}

        with db.get_db(config.db_path) as conn:
            _task(conn, "Running", user_id="bob", token=None)
            _task(conn, "Pending", user_id="bob", token=None, status=None)
            result = await cmd_status(_ctx(config, conn))

        assert "System:" not in result
        assert "running" not in result
        assert "queued" not in result

    async def test_groups_interactive_and_background(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="Talk task", user_id="alice", source_type="talk")
            db.create_task(conn, prompt="Scheduled job", user_id="alice", source_type="scheduled")
            db.create_task(conn, prompt="Briefing", user_id="alice", source_type="briefing")
            result = await cmd_status(_ctx(config, conn))

        assert "Your tasks (1)" in result
        assert "Talk task" in result
        assert "Background (2)" in result
        assert "[scheduled]" in result

    async def test_only_background_tasks(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, prompt="Cron job", user_id="alice", source_type="scheduled")
            result = await cmd_status(_ctx(config, conn))

        assert "Background (1)" in result
        assert "Your tasks" not in result


# =============================================================================
# TestCmdCron
# =============================================================================


_MODULE_JOB = "_module.feeds.run_scheduled"


def _insert_job(conn, name="digest", cron="0 * * * *", prompt="stuff", **columns):
    """One `scheduled_jobs` row for alice; `enabled` defaults to 1."""
    columns = {"enabled": 1, **columns}
    names = "".join(f", {column}" for column in columns)
    marks = "".join(", ?" for _ in columns)
    conn.execute(
        f"INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt{names}) "
        f"VALUES (?, ?, ?, ?{marks})",
        ("alice", name, cron, prompt, *columns.values()),
    )


def _insert_module_job(conn, **columns):
    _insert_job(
        conn, _MODULE_JOB, "*/5 * * * *", "", skill="feeds",
        skill_args='["run-scheduled"]', **columns,
    )


def _write_cron(config, job_toml):
    """A CRON.md holding one `[[jobs]]` entry; returns its path."""
    cron_path = (
        config.workspace_path / "Users" / "alice" / "istota" / "config" / "CRON.md"
    )
    cron_path.write_text(
        f"# Scheduled Jobs\n\n```toml\n[[jobs]]\n{job_toml}```\n"
    )
    return cron_path


def _job(config, name):
    with db.get_db(config.db_path) as conn:
        return db.get_scheduled_job_by_name(conn, "alice", name)


class TestCmdCron:
    async def test_no_jobs(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_cron(_ctx(config, conn))
        assert "No scheduled jobs" in result

    async def test_list_jobs(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "daily-check", "0 9 * * *", "check stuff")
            result = await cmd_cron(_ctx(config, conn))

        assert "daily-check" in result
        assert "0 9 * * *" in result
        assert "enabled" in result

    async def test_list_shows_failures(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "flaky", "0 * * * *", "flaky job", consecutive_failures=3)
            result = await cmd_cron(_ctx(config, conn))

        assert "3 failures" in result

    @pytest.mark.parametrize("enabled,suspended,expected", [
        (1, False, "enabled"),
        (0, False, "DISABLED"),
        (1, True, "SUSPENDED"),
        # Both: the user's own intent is the more informative of the two, and
        # the one they can act on from here.
        (0, True, "DISABLED"),
    ])
    async def test_the_listing_tells_the_three_states_apart(
        self, make_config, enabled, suspended, expected,
    ):
        """`DISABLED` is the user's doing, `SUSPENDED` is the scheduler's.

        They read the same to a user and need different things done about
        them, so a listing that renders both as "off" is the surface where the
        two-column split stops being visible.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(
                conn, "digest", "0 9 * * *", "p", enabled=enabled,
                auto_disabled_at="2026-08-30 04:05:06" if suspended else None,
            )
            result = await cmd_cron(_ctx(config, conn))

        other = {"enabled", "DISABLED", "SUSPENDED"} - {expected}
        assert expected in result
        assert not any(word in result for word in other)
        if expected == "SUSPENDED":
            assert "2026-08-30 04:05" in result

    async def test_the_listing_says_when_the_user_switched_a_job_off(
        self, make_config,
    ):
        """Symmetric with the SUSPENDED render, and the column's only human
        reader — otherwise `disabled_at` is write-only and an operator still
        cannot tell their own disable from the daemon's."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(
                conn, "digest", "0 9 * * *", "p", enabled=0,
                disabled_at="2026-08-30 04:05:06",
            )
            result = await cmd_cron(_ctx(config, conn))

        assert "DISABLED since 2026-08-30 04:05" in result
        assert "SUSPENDED" not in result

    async def test_the_listing_claims_no_author_for_an_unstamped_disable(
        self, make_config,
    ):
        """A row that predates the column, or was switched off some other way.
        It must stay a bare DISABLED rather than inventing a time."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "digest", "0 9 * * *", "p", enabled=0)
            result = await cmd_cron(_ctx(config, conn))

        assert "DISABLED]" in result
        assert "since" not in result

    async def test_the_user_disable_verb_does_not_set_the_daemon_column(
        self, make_config,
    ):
        """Through the real handler, because this is what a refactor breaks.

        `!cron disable` writes the file and `enabled`; collapsing it onto
        `suspend_scheduled_job` would put the user's intent in the daemon's
        column, where the module rescue lifts it an hour later.
        """
        config = make_config()
        _write_cron(config, 'name = "digest"\ncron = "0 * * * *"\nprompt = "stuff"\n')
        with db.get_db(config.db_path) as conn:
            _insert_job(conn)
            result = await cmd_cron(_ctx(config, conn, args="disable digest"))

        job = _job(config, "digest")
        assert "Disabled" in result
        assert job.enabled is False
        assert job.auto_disabled_at is None

    async def test_the_user_disable_verb_records_that_the_user_did_it(
        self, make_config,
    ):
        """ISSUE-392. `enabled = 0` alone does not say who wrote it.

        A `_module.*` row has no CRON.md entry, so the module sync's legacy
        rescue arm is left inferring the author from the failure count — and
        that inference reads a user's disable as the daemon's. `disabled_at`
        is the fact the arm reads instead.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_module_job(conn, consecutive_failures=3)
            result = await cmd_cron(_ctx(config, conn, args=f"disable {_MODULE_JOB}"))

        job = _job(config, _MODULE_JOB)
        assert job.enabled is False
        assert job.auto_disabled_at is None
        assert job.disabled_at is not None
        # A module row is in nobody's CRON.md, so the fallback branch's
        # "may not persist" note would be the opposite of true here.
        assert "Disabled module job" in result
        assert "may not persist" not in result

    async def test_enabling_a_module_job_does_not_warn_about_cron_md(
        self, make_config,
    ):
        """The converse of the branch above, and the same misleading note."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_module_job(
                conn, enabled=0, consecutive_failures=3,
                disabled_at="2026-08-30 04:05:06",
            )
            result = await cmd_cron(_ctx(config, conn, args=f"enable {_MODULE_JOB}"))

        job = _job(config, _MODULE_JOB)
        assert job.enabled is True
        assert job.disabled_at is None
        assert "Enabled module job" in result
        assert "may not persist" not in result

    async def test_the_enable_verb_clears_the_user_disable_record(
        self, make_config,
    ):
        """The converse: a running job must not carry a stale disable stamp."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, enabled=0, disabled_at="2026-08-30 04:05:06")
            await cmd_cron(_ctx(config, conn, args="enable digest"))

        job = _job(config, "digest")
        assert job.enabled is True
        assert job.disabled_at is None

    async def test_the_enable_verb_lifts_a_suspension(self, make_config):
        """The converse, and the only verb that writes both columns."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(
                conn, consecutive_failures=5, auto_disabled_at="2026-08-30 04:05:06",
            )
            await cmd_cron(_ctx(config, conn, args="enable digest"))
            assert [j.name for j in db.get_enabled_scheduled_jobs(conn)] == ["digest"]

        job = _job(config, "digest")
        assert job.enabled is True
        assert job.auto_disabled_at is None
        assert job.consecutive_failures == 0

    async def test_enable_job_updates_file_and_db(self, make_config):
        from istota.cron_loader import load_cron_jobs

        config = make_config()
        _write_cron(
            config,
            'name = "broken"\ncron = "0 * * * *"\nprompt = "stuff"\nenabled = false\n',
        )
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "broken", enabled=0, consecutive_failures=5)
            result = await cmd_cron(_ctx(config, conn, args="enable broken"))

        assert "Enabled" in result
        assert "DB-only" not in result
        job = _job(config, "broken")
        assert job.enabled is True
        assert job.consecutive_failures == 0
        assert load_cron_jobs(config, "alice")[0].enabled is True

    async def test_enable_job_resets_last_run_at(self, make_config):
        """Enabling a job resets last_run_at so it won't fire immediately as catch-up."""
        config = make_config()
        _write_cron(
            config,
            'name = "nightly"\ncron = "0 22 * * *"\nprompt = "stuff"\nenabled = false\n',
        )
        with db.get_db(config.db_path) as conn:
            # An old last_run_at, simulating a job disabled long ago.
            _insert_job(
                conn, "nightly", "0 22 * * *", enabled=0,
                last_run_at="2026-01-01 00:00:00",
            )
            result = await cmd_cron(_ctx(config, conn, args="enable nightly"))

        assert "Enabled" in result
        job = _job(config, "nightly")
        assert job.enabled is True
        assert job.last_run_at is not None
        assert "2026-01-01" not in job.last_run_at

    async def test_disable_job_updates_file_and_db(self, make_config):
        from istota.cron_loader import load_cron_jobs

        config = make_config()
        _write_cron(config, 'name = "active-job"\ncron = "0 * * * *"\nprompt = "stuff"\n')
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "active-job")
            result = await cmd_cron(_ctx(config, conn, args="disable active-job"))

        assert "Disabled" in result
        assert "DB-only" not in result
        assert _job(config, "active-job").enabled is False
        assert load_cron_jobs(config, "alice")[0].enabled is False

    async def test_enable_without_cron_file_warns(self, make_config):
        """Without CRON.md, enable falls back to DB-only with a warning."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "broken", enabled=0, consecutive_failures=5)
            result = await cmd_cron(_ctx(config, conn, args="enable broken"))

        assert "Enabled" in result
        assert "DB-only" in result
        assert _job(config, "broken").enabled is True

    @pytest.mark.requires_dac
    async def test_disable_warns_when_the_file_write_is_refused(self, make_config):
        """The fallback branch is reachable, not just for a missing file.

        ISSUE-369 defect 3: the writer swallowed a failed write and returned
        True, so this branch could only ever be reached by a CRON.md that was
        not there. On the rclone mount the daemon actually runs on, a refused
        write is the commoner case — and the user was told the job was
        disabled while the file still said otherwise, so the next sync tick
        switched it back on. `requires_dac` because the refusal is a
        permission bit and root walks through it.
        """
        config = make_config()
        cron_path = _write_cron(
            config, 'name = "active-job"\ncron = "0 * * * *"\nprompt = "stuff"\n',
        )
        config_dir = cron_path.parent
        original = cron_path.read_text()
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, "active-job")
            config_dir.chmod(0o555)
            try:
                result = await cmd_cron(_ctx(config, conn, args="disable active-job"))
            finally:
                config_dir.chmod(0o755)

        assert "Disabled" in result
        assert "DB-only" in result
        # The file is untouched, which is what the warning is about.
        assert cron_path.read_text() == original
        assert _job(config, "active-job").enabled is False

    async def test_enable_nonexistent(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_cron(_ctx(config, conn, args="enable nope"))
        assert "not found" in result or "No scheduled job" in result


# =============================================================================
# TestCmdMemory
# =============================================================================


async def _memory(config, args):
    with db.get_db(config.db_path) as conn:
        return await cmd_memory(_ctx(config, conn, args=args))


def _add_facts(config, facts, valid_until=None):
    with db.get_db(config.db_path) as conn:
        ensure_table(conn)
        for subject, predicate, obj in facts:
            add_fact(conn, "alice", subject, predicate, obj, valid_until=valid_until)
        conn.commit()


class TestCmdMemory:
    @pytest.mark.parametrize("args,expected", [
        ("", ["!memory user", "!memory channel", "!memory facts"]),
        ("user", ["User memory:** (empty)"]),
        ("channel", ["Channel memory:** (empty)"]),
        ("facts", ["no facts"]),
    ], ids=["usage", "user-empty", "channel-empty", "facts-empty"])
    async def test_empty_answers(self, make_config, args, expected):
        result = await _memory(make_config(), args)
        for needle in expected:
            assert needle in result

    # The user file is shown whole, not truncated.
    @pytest.mark.parametrize("relpath,content,args,expected", [
        ("Users/alice/istota/config/USER.md", "Alice likes coffee", "user",
         ["Alice likes coffee", "User memory**"]),
        ("Users/alice/istota/config/USER.md", "A" * 5000, "user", ["A" * 5000]),
        ("Channels/room1/CHANNEL.md", "This is the dev channel", "channel",
         ["This is the dev channel", "Channel memory**"]),
    ], ids=["user", "user-not-truncated", "channel"])
    async def test_file_contents(self, make_config, relpath, content, args, expected):
        config = make_config()
        (config.workspace_path / relpath).write_text(content)
        result = await _memory(config, args)
        for needle in expected:
            assert needle in result

    async def test_no_mount_configured(self, make_config):
        config = make_config()
        config.workspace_path = None
        assert "mount not configured" in await _memory(config, "user")

    async def test_facts_with_few(self, make_config):
        """Small fact sets show all facts inline."""
        config = make_config()
        _add_facts(config, [("alice", "works_at", "acme"), ("alice", "knows", "python")])
        result = await _memory(config, "facts")
        assert "Knowledge graph" in result
        assert "2 facts" in result
        assert "works_at" in result
        assert "python" in result

    async def test_facts_large_set_summarizes(self, make_config):
        """Large fact sets show entity summary instead of all facts."""
        config = make_config()
        _add_facts(config, [("alice", "knows", f"tech_{i}") for i in range(25)])
        result = await _memory(config, "facts")
        assert "25 facts" in result
        assert "Entities:" in result
        assert "alice (25)" in result
        # Should not dump all individual facts
        assert "tech_0" not in result

    async def test_facts_counts_the_future_expiry_it_renders(self, make_config):
        """ISSUE-472: a graph of only future-expiry facts read as empty.

        The header used to come from `get_fact_count`, which treated any
        `valid_until` as historical, while the body came from
        `get_current_facts`, which does not — so the count disagreed with the
        list it labelled, and reached "(no facts)" over a populated graph.
        """
        config = make_config()
        future = (date.today() + timedelta(days=30)).isoformat()
        _add_facts(
            config,
            [("alice", "interested_in", "sailing"), ("alice", "interested_in", "pottery")],
            valid_until=future,
        )
        result = await _memory(config, "facts")
        assert "no facts" not in result
        assert "2 facts" in result
        assert "sailing" in result
        assert "pottery" in result

    async def test_facts_entity_filter(self, make_config):
        """!memory facts <entity> shows facts for that entity only."""
        config = make_config()
        _add_facts(config, [("alice", "works_at", "acme"), ("bob", "works_at", "globex")])
        result = await _memory(config, "facts alice")
        assert "Facts about alice" in result
        assert "works_at" in result
        assert "globex" not in result

    async def test_facts_entity_not_found(self, make_config):
        config = make_config()
        _add_facts(config, [])
        assert "none found" in await _memory(config, "facts nobody")

    async def test_facts_no_mount_required(self, make_config):
        """Facts come from DB, not filesystem — works without mount."""
        config = make_config()
        config.workspace_path = None
        _add_facts(config, [("alice", "speaks", "portuguese")])
        result = await _memory(config, "facts")
        assert "Knowledge graph" in result
        assert "speaks" in result


_PRIVATE_TEXT = "alice private sentinel text"


def _shared_room(config, origin="web"):
    """A room alice and bob are both members of: shared by `room_is_shared`."""
    config.users = {"alice": UserConfig(), "bob": UserConfig()}
    with db.get_db(config.db_path) as conn:
        if origin == "talk":
            shape = plain_talk_room(conn, "alice")
            token, ref = shape.canonical, shape.talk_ref
        else:
            token = db.register_room(conn, None, "alice", origin=origin, name="g").token
            ref = token
        db.add_room_member(conn, token, "bob")
        conn.commit()
    return token, ref


def _private_talk_room(config):
    with db.get_db(config.db_path) as conn:
        shape = plain_talk_room(conn, "alice")
        conn.commit()
    return shape.canonical, shape.talk_ref


def _write_user_md(config, text=_PRIVATE_TEXT):
    (config.workspace_path / "Users/alice/istota/config/USER.md").write_text(text)


def _talk_sends(fake_talk, ref):
    return [c.args["message"] for c in fake_talk.calls_to(ref, method="send_message")]


class TestPersonalCommandsInSharedRooms:
    """ISSUE-609: a command whose reply is personal is refused in a shared room.

    On Talk a command's reply is posted into the conversation, so in a group it
    is read by everyone; the refusal holds on every surface so the rule is one
    rule.
    """

    @pytest.mark.parametrize("args", ["user", "facts", "facts alice"])
    async def test_a_talk_group_gets_the_refusal_and_none_of_the_memory(
        self, make_config, fake_talk, args,
    ):
        config = make_config()
        fake_talk.db_path = config.db_path
        _write_user_md(config)
        _add_facts(config, [("alice", "secret_is", _PRIVATE_TEXT)])
        _token, ref = _shared_room(config, origin="talk")

        result = await dispatch(config, "alice", ref, f"!memory {args}", surface="talk")

        assert result.delivered
        sends = _talk_sends(fake_talk, ref)
        assert len(sends) == 1
        assert "private chat with me" in sends[0]
        assert _PRIVATE_TEXT.lower() not in sends[0].lower()
        assert fake_talk.refusals == []

    @pytest.mark.parametrize("surface,origin", [("web", "web"), ("whatsapp", "whatsapp")])
    @pytest.mark.parametrize("args", ["user", "facts"])
    async def test_web_and_whatsapp_group_refuse_too(self, make_config, surface, origin, args):
        config = make_config()
        _write_user_md(config)
        _add_facts(config, [("alice", "secret_is", _PRIVATE_TEXT)])
        token, _ref = _shared_room(config, origin=origin)

        result = await dispatch(config, "alice", token, f"!memory {args}", surface=surface)

        assert "private chat with me" in result.text
        assert _PRIVATE_TEXT.lower() not in result.text.lower()

    @pytest.mark.parametrize("args", ["user", "facts"])
    async def test_a_private_talk_room_still_shows_the_memory(
        self, make_config, fake_talk, args,
    ):
        config = make_config()
        fake_talk.db_path = config.db_path
        _write_user_md(config)
        _add_facts(config, [("alice", "secret_is", _PRIVATE_TEXT)])
        _token, ref = _private_talk_room(config)

        await dispatch(config, "alice", ref, f"!memory {args}", surface="talk")

        sends = _talk_sends(fake_talk, ref)
        assert len(sends) == 1 and _PRIVATE_TEXT.lower() in sends[0].lower()

    async def test_channel_notes_stay_readable_in_a_shared_room(self, make_config):
        config = make_config()
        token, _ = _shared_room(config)
        channel = config.workspace_path / "Channels" / token
        channel.mkdir(parents=True, exist_ok=True)
        (channel / "CHANNEL.md").write_text("Team notes everyone reads")

        result = await dispatch(config, "alice", token, "!memory channel", surface="web")

        assert "Team notes everyone reads" in result.text

    @pytest.mark.parametrize("command,seed", [
        ("!status", "task"),
        ("!cron", "job"),
        ("!trust", "sender"),
        ("!usage", None),
        (f"!search {_PRIVATE_TEXT}", None),
    ])
    async def test_other_personal_commands_are_refused(self, make_config, command, seed):
        config = make_config()
        token, _ = _shared_room(config)
        with db.get_db(config.db_path) as conn:
            if seed == "task":
                _task(conn, _PRIVATE_TEXT, token="elsewhere", status="pending")
            elif seed == "job":
                _insert_job(conn, name=_PRIVATE_TEXT)
            elif seed == "sender":
                db.add_trusted_sender(conn, "alice", f"{_PRIVATE_TEXT.lower()}@example.com")
            conn.commit()

        result = await dispatch(config, "alice", token, command, surface="web")

        assert "private chat with me" in result.text
        assert _PRIVATE_TEXT.lower() not in result.text.lower()

    async def test_cron_enable_still_works_in_a_shared_room(self, make_config):
        config = make_config()
        token, _ = _shared_room(config)
        with db.get_db(config.db_path) as conn:
            _insert_job(conn, name="digest", enabled=0)
            conn.commit()

        result = await dispatch(config, "alice", token, "!cron enable digest", surface="web")

        assert "Enabled" in result.text

    async def test_more_shows_only_this_rooms_tasks_in_a_shared_room(self, make_config):
        config = make_config()
        token, _ = _shared_room(config)
        trace = json.dumps([{"type": "text", "text": "trace text"}])
        with db.get_db(config.db_path) as conn:
            elsewhere = _task(conn, _PRIVATE_TEXT, token="elsewhere", status="completed",
                              source_type="web")
            here = _task(conn, "a room question", token=token, status="completed",
                         source_type="web")
            conn.execute("UPDATE tasks SET execution_trace = ?", (trace,))
            conn.commit()

        refused = await dispatch(config, "alice", token, f"!more {elsewhere}", surface="web")
        shown = await dispatch(config, "alice", token, f"!more {here}", surface="web")

        assert "private chat with me" in refused.text
        assert _PRIVATE_TEXT not in refused.text
        assert "trace text" in shown.text


    @pytest.mark.parametrize("command", ["!confirm", "!confirm 1", "!yes", "!drafts"])
    async def test_held_questions_and_mail_are_refused(self, make_config, command):
        config = make_config()
        token, _ = _shared_room(config)
        with db.get_db(config.db_path) as conn:
            held = _task(conn, _PRIVATE_TEXT, token="elsewhere", status="pending_confirmation",
                         source_type="web")
            conn.execute("UPDATE tasks SET confirmation_prompt = ? WHERE id = ?", (_PRIVATE_TEXT, held))
            conn.commit()

        result = await dispatch(config, "alice", token, command, surface="web")

        assert "private chat with me" in result.text
        assert _PRIVATE_TEXT not in result.text
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "pending_confirmation"

    @pytest.mark.parametrize("verb", ["!retry", "!resume"])
    async def test_retry_does_not_quote_another_rooms_task(self, make_config, verb):
        config = make_config()
        token, _ = _shared_room(config)
        with db.get_db(config.db_path) as conn:
            elsewhere = _task(conn, _PRIVATE_TEXT, token="elsewhere", status="failed",
                              source_type="web")
            conn.commit()

        result = await dispatch(config, "alice", token, f"{verb} #{elsewhere}", surface="web")

        assert "private chat with me" in result.text
        assert _PRIVATE_TEXT not in result.text

    async def test_a_room_shared_only_through_its_roster_refuses(self, make_config):
        config = make_config()
        _write_user_md(config)
        with db.get_db(config.db_path) as conn:
            token = db.register_room(conn, None, "alice", origin="talk", name="g").token
            db.upsert_room_participant(
                conn, room_token=token, surface="talk", surface_ref="alice",
                kind="principal", user_id="alice",
            )
            db.upsert_room_participant(
                conn, room_token=token, surface="talk", surface_ref="guest/abc",
                kind="guest", display_name="Max",
            )
            conn.commit()

        result = await dispatch(config, "alice", token, "!memory user", surface="talk")

        assert "private chat with me" in result.text
        assert _PRIVATE_TEXT not in result.text

    async def test_more_refuses_a_turn_from_before_someone_joined(self, make_config):
        config = make_config()
        trace = json.dumps([{"type": "text", "text": "trace text"}])
        with db.get_db(config.db_path) as conn:
            token = db.register_room(conn, None, "alice", origin="web", name="r").token
            before = _task(conn, _PRIVATE_TEXT, token=token, status="completed", source_type="web")
            db.add_room_member(conn, token, "bob")
            db.upsert_room_participant(
                conn, room_token=token, surface="web", surface_ref="bob",
                kind="principal", user_id="bob",
            )
            after = _task(conn, "asked with bob here", token=token, status="completed",
                          source_type="web")
            conn.execute("UPDATE tasks SET execution_trace = ?", (trace,))
            conn.commit()
        config.users = {"alice": UserConfig(), "bob": UserConfig()}

        refused = await dispatch(config, "alice", token, f"!more {before}", surface="web")
        shown = await dispatch(config, "alice", token, f"!more {after}", surface="web")

        assert "private chat with me" in refused.text
        assert _PRIVATE_TEXT not in refused.text
        assert "trace text" in shown.text

    async def test_an_admins_check_in_a_shared_room_is_the_verdict_only(self, make_config):
        from istota.doctor import CheckResult

        config = make_config(admin_users=["alice"])
        token, _ = _shared_room(config)
        clean = [CheckResult(name="runtime.platform", status="ok", detail=_PRIVATE_TEXT)]

        with patch("istota.doctor.run_checks", return_value=clean):
            result = await dispatch(config, "alice", token, "!check", surface="web")

        assert "OK" in result.text
        assert "runtime.platform" not in result.text
        assert _PRIVATE_TEXT not in result.text

    async def test_the_surfaces_group_flag_is_enough(self, make_config):
        """A Talk group the registry has not recorded is still a group."""
        config = make_config()
        _write_user_md(config)

        result = await dispatch(
            config, "alice", "unrecorded", "!memory user", surface="web",
            is_group_chat=True,
        )

        assert "private chat with me" in result.text

    async def test_a_talk_group_whose_roster_failed_is_refused(self, make_config, fake_talk):
        """The poller passes its own reading when the roster fetch failed."""
        from istota.transport.talk.inbound import poll_talk_conversations

        config = make_config()
        fake_talk.db_path = config.db_path
        fake_talk.known_channels.add("grp609")
        _write_user_md(config)
        with patch("istota.transport.talk.inbound.get_talk_client") as MockTalkClient:
            mock_talk = MockTalkClient.return_value
            mock_talk.list_conversations = AsyncMock(
                return_value=[{"token": "grp609", "type": 2, "name": "Group"}]
            )
            mock_talk.get_participants = AsyncMock(side_effect=RuntimeError("503"))
            mock_talk.poll_messages = AsyncMock(return_value=[{
                "id": 101, "actorId": "alice", "actorType": "users",
                "message": "!memory user", "messageType": "comment",
                "messageParameters": {},
            }])
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "grp609", 50)

            await poll_talk_conversations(config)

        sends = _talk_sends(fake_talk, "grp609")
        assert len(sends) == 1
        assert "private chat with me" in sends[0]
        assert _PRIVATE_TEXT not in sends[0]


class TestMemoryReadsAreHardened:
    """ISSUE-609: `!memory` reads through the storage helpers, not `read_text`."""

    async def test_a_symlinked_user_md_is_not_followed(self, make_config, tmp_path):
        config = make_config()
        target = tmp_path / "elsewhere.txt"
        target.write_text(_PRIVATE_TEXT)
        (config.workspace_path / "Users/alice/istota/config/USER.md").symlink_to(target)

        result = await _memory(config, "user")

        assert _PRIVATE_TEXT not in result

    async def test_channel_notes_under_an_alias_are_found(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            token = db.register_room(conn, None, "alice", origin="web", name="r").token
            conn.execute(
                "INSERT INTO room_token_migration (old_token, new_token, migrated_at) "
                "VALUES (?, ?, datetime('now'))",
                ("oldtoken1", token),
            )
            conn.commit()
        old = config.workspace_path / "Channels" / "oldtoken1"
        old.mkdir(parents=True)
        (old / "CHANNEL.md").write_text("Notes kept under the alias")

        result = await dispatch(config, "alice", token, "!memory channel", surface="web")

        assert "Notes kept under the alias" in result.text


# =============================================================================
# TestDbHelpers
# =============================================================================


class TestDbHelpers:
    def test_update_task_pid(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            db.update_task_pid(conn, task_id, 12345)
            row = conn.execute(
                "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row[0] == 12345

    def test_is_task_cancelled(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            assert db.is_task_cancelled(conn, task_id) is False
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,)
            )
            assert db.is_task_cancelled(conn, task_id) is True


# =============================================================================
# TestPollerInterception
# =============================================================================


class TestPollerInterception:
    """Test that !commands are intercepted in the Talk poller and don't create tasks."""

    @staticmethod
    def _poll_client(MockTalkClient, msg_id, text):
        mock_talk = MockTalkClient.return_value
        mock_talk.list_conversations = AsyncMock(
            return_value=[{"token": "room1", "type": 1}]
        )
        mock_talk.poll_messages = AsyncMock(return_value=[{
            "id": msg_id, "actorId": "alice", "actorType": "users",
            "message": text, "messageType": "comment", "messageParameters": {},
        }])

    async def test_command_does_not_create_task(self, make_config):
        from istota.transport.talk.inbound import poll_talk_conversations

        config = make_config()
        with patch("istota.transport.talk.inbound.get_talk_client") as MockTalkClient, patch(
            "istota.transport.talk.get_talk_client"
        ) as MockDeliverClient:
            self._poll_client(MockTalkClient, 101, "!status")
            # Command delivery goes through TalkTransport.deliver, which pulls
            # the persistent client from istota.transport.talk.
            mock_deliver = MockDeliverClient.return_value
            mock_deliver.send_message = AsyncMock(return_value={"id": 1})

            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert result == []
        mock_deliver.send_message.assert_called_once()
        sent_msg = mock_deliver.send_message.call_args[0][1]
        assert "System:" in sent_msg  # !status output

    async def test_normal_message_still_creates_task(self, make_config):
        from istota.transport.talk.inbound import poll_talk_conversations

        config = make_config()
        with patch("istota.transport.talk.inbound.get_talk_client") as MockTalkClient:
            self._poll_client(MockTalkClient, 102, "What's the weather?")
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "room1", 50)

            result = await poll_talk_conversations(config)

        assert len(result) == 1


# =============================================================================
# TestCmdSkills
# =============================================================================


async def _skills(config, args=""):
    with db.get_db(config.db_path) as conn:
        return await cmd_skills(_ctx(config, conn, args=args))


class TestCmdSkills:
    async def test_lists_bundled_skills(self, make_config):
        result = await _skills(make_config())
        assert "Skills" in result
        assert "total" in result
        # Some well-known bundled skills should appear
        assert "files" in result
        assert "calendar" in result

    async def test_hides_admin_skills_from_non_admin(self, make_config):
        config = make_config()
        config.admin_users = {"bob"}  # alice is not admin
        # tasks skill is admin_only, should not appear for non-admin
        assert "**tasks**" not in await _skills(config)

    async def test_shows_admin_skills_to_admin(self, make_config):
        config = make_config()
        config.admin_users = set()  # empty = all admin
        assert "tasks" in await _skills(config)

    async def test_shows_unavailable_skills(self, make_config):
        def side_effect(meta):
            if meta.name == "whisper":
                return ("unavailable", "faster-whisper")
            return ("available", None)

        with patch("istota.skills._loader.get_skill_availability", side_effect=side_effect):
            result = await _skills(make_config())

        assert "Unavailable" in result
        assert "faster-whisper" in result

    async def test_shows_disabled_skills(self, make_config):
        config = make_config()
        config.disabled_skills = ["browse"]
        result = await _skills(config)
        assert "Disabled" in result
        assert "browse" in result

    async def test_capability_gated_skills_disabled_when_service_off(self, make_config):
        # browser + devbox off by default → the capability gate files browse and
        # devbox under Disabled (not Available), with no explicit disabled_skills.
        config = make_config()
        caps = config.available_capabilities()
        assert "browser" not in caps
        assert "devbox" not in caps
        result = await _skills(config)
        assert "**Disabled**" in result
        assert "- browse —" in result  # Disabled render (plain name)
        assert "- devbox —" in result
        assert "- **browse**:" not in result  # not in the Available (bold) section
        assert "- **devbox**:" not in result

    async def test_capability_gated_skills_available_when_service_on(self, make_config):
        from istota.config import BrowserConfig, DevboxConfig
        config = make_config(
            browser=BrowserConfig(enabled=True),
            devbox=DevboxConfig(enabled=True),
        )
        result = await _skills(config)
        assert "- **browse**:" in result  # Available render (bold name)
        assert "- **devbox**:" in result

    async def test_skill_detail_view(self, make_config):
        result = await _skills(make_config(), "calendar")
        assert "**calendar**" in result
        assert "Status:" in result
        assert "CalDAV" in result


# =============================================================================
# TestCmdCheck
# =============================================================================


class TestCmdCheck:
    """`!check` renders `doctor.run_checks` and asserts nothing of its own.

    Every case patches `doctor.run_checks` and reads the call, rather than
    running the registry: what the checks do is `tests/test_doctor.py`'s job,
    and the three properties that matter here are which flags the command asks
    for, what it does before it blocks, and what a non-admin is shown.
    """

    def _result(self, name, status, detail="detail", remedy=""):
        from istota.doctor import CheckResult as DoctorResult

        return DoctorResult(name, status, detail, remedy=remedy)

    def _clean(self):
        from istota.doctor import OK, SKIP

        return [
            self._result("runtime.platform", OK, "linux"),
            self._result("runtime.bwrap", SKIP, "sandbox disabled"),
        ]

    async def _check(self, config, results=None, side_effect=None, user_id="alice"):
        if side_effect is not None:
            patcher = patch("istota.doctor.run_checks", side_effect=side_effect)
        else:
            patcher = patch(
                "istota.doctor.run_checks",
                return_value=self._clean() if results is None else results,
            )
        with db.get_db(config.db_path) as conn, patcher:
            return await cmd_check(_ctx(config, conn, user_id))

    def _recorder(self, calls):
        def fake_run_checks(cfg, **kwargs):
            calls["kwargs"] = kwargs
            return self._clean()
        return fake_run_checks

    async def test_an_admin_gets_the_whole_registry_fenced(self, make_config):
        """`render_text`'s alignment is the information, and posted as markdown
        its two-space indents collapse into one paragraph. Hence the fence."""
        result = await self._check(make_config(admin_users=["alice"]))

        assert result.startswith("**Health Check**\n\n```\n")
        assert result.endswith("\n```")
        assert "runtime.platform" in result
        assert "runtime.bwrap" in result

    async def test_an_admin_asks_for_live_and_not_deep(self, make_config):
        """The resolved open question, pinned.

        `live=True` restores the execution test the hand-rolled probe ran.
        `deep=True` is deliberately absent: the web chat client aborts the very
        POST that runs this inline at 30s (`web/src/lib/api.ts`), and the Talk
        path runs dispatch on the process-global loop inside the poll batch's
        open write transaction. A second 30-second check does not fit either.
        Asserted as an absence, because a later tidy-up adding it back would
        otherwise go unnoticed until an admin's `!check` started timing out.
        """
        calls = {}
        await self._check(
            make_config(admin_users=["alice"]), side_effect=self._recorder(calls),
        )

        assert calls["kwargs"]["live"] is True
        assert calls["kwargs"].get("deep", False) is False

    async def test_a_non_admin_gets_one_line_and_neither_flag(self, make_config):
        calls = {}
        result = await self._check(
            make_config(admin_users=["boss"]), side_effect=self._recorder(calls),
        )

        assert calls["kwargs"].get("live", False) is False
        assert calls["kwargs"].get("deep", False) is False
        assert "```" not in result
        assert "OK" in result
        assert "1 ok" in result
        # One line of content under the header, not a registry.
        body = result.split("\n\n", 1)[1]
        assert "\n" not in body, f"the non-admin arm rendered more than a line: {body!r}"

    async def test_a_non_admin_is_told_when_something_failed(self, make_config):
        from istota.doctor import FAIL

        results = self._clean() + [self._result("web.static", FAIL, "missing")]
        result = await self._check(make_config(admin_users=["boss"]), results)

        assert "PROBLEMS" in result
        assert "1 fail" in result

    async def test_a_non_admin_is_told_no_cross_user_facts(self, make_config):
        """The reason for the split.

        The whole registry is a different thing from the five lines a non-admin
        used to get. `runtime.subscription_usage` reports plan utilization,
        which `cmd_usage` deliberately withholds from a non-admin;
        `config.skill_overlays` labels overlays by user id across users;
        `developer.*` describes other people's trees. `redact` covers
        configured credential values, not cross-user facts.
        """
        from istota.doctor import OK, WARN

        results = [
            self._result(
                "runtime.subscription_usage", WARN, "session window at 91% utilization"
            ),
            self._result(
                "config.skill_overlays", OK, "bob: 2 overlays; carol: 1 overlay"
            ),
        ]
        result = await self._check(make_config(admin_users=["boss"]), results)

        assert "91%" not in result
        assert "utilization" not in result
        assert "bob" not in result
        assert "carol" not in result
        assert "runtime.subscription_usage" not in result

    async def test_an_admin_does_see_those_details(self, make_config):
        """The control for the case above: without it, a non-admin assertion
        about an absent string passes against a command that renders nothing at
        all."""
        from istota.doctor import WARN

        results = [
            self._result(
                "runtime.subscription_usage", WARN, "session window at 91% utilization"
            )
        ]
        result = await self._check(make_config(admin_users=["alice"]), results)

        assert "91% utilization" in result

    async def test_the_admin_render_redacts(self, make_config):
        """`render_text` takes `secrets` and redacts internally, so the command
        passes `config_secrets` rather than calling `redact` as well."""
        from istota.doctor import FAIL

        config = make_config(
            admin_users=["alice"],
            nextcloud=NextcloudConfig(
                url="https://cloud.example.com",
                username="bot",
                app_password="s3cr3t-app-password",
            ),
        )
        results = [
            self._result("web.static", FAIL, "upstream said s3cr3t-app-password")
        ]
        result = await self._check(config, results)

        assert "s3cr3t-app-password" not in result
        assert "web.static" in result

    @pytest.mark.parametrize("user_id", ["alice", "nobody"])
    async def test_it_commits_before_it_blocks(self, make_config, user_id):
        """Both arms, since both block.

        The Talk poller wraps its whole batch in one write transaction and
        hands `dispatch` that connection already mid-write, so holding its lock
        across a multi-second probe stalls every other writer in the daemon on
        its busy timeout. `cmd_usage` does the same thing for the same reason.

        The ordering is the assertion, not the fact of a commit: a commit that
        happens after the probe is the bug wearing a label.
        """
        config = make_config(admin_users=["alice"])
        order = []

        def fake_run_checks(cfg, **kwargs):
            order.append("run_checks")
            return self._clean()

        with db.get_db(config.db_path) as conn:
            recording = _RecordingConn(conn, order)
            with patch("istota.doctor.run_checks", side_effect=fake_run_checks):
                await cmd_check(_ctx(config, recording, user_id, "room1", ""))

        assert order == ["commit", "run_checks"], (
            f"expected the caller's transaction committed before the probe, got {order}"
        )

    class _Broken:
        """Reads work; only the commit fails."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            raise sqlite3.OperationalError("cannot commit - no transaction is active")

    # A connection we could not commit is the caller's problem, not a reason to
    # refuse a read-only report. And `ctx.conn` is typed non-optional while
    # `cmd_usage` still guards for None, because a surface can build a context
    # without one.
    @pytest.mark.parametrize("broken", [True, False], ids=["uncommittable", "none"])
    async def test_an_unusable_connection_is_not_fatal(self, make_config, broken):
        config = make_config(admin_users=["alice"])

        with db.get_db(config.db_path) as real, \
                patch("istota.doctor.run_checks", return_value=self._clean()):
            conn = self._Broken(real) if broken else None
            result = await cmd_check(_ctx(config, conn, "alice", "room1", ""))

        assert "runtime.platform" in result

    async def test_it_runs_the_registry_off_the_event_loop(self, make_config):
        """`run_checks` is entirely synchronous and this coroutine runs on the
        loop that polls every Talk conversation. `cmd_usage`'s own comment
        names this command's bare `subprocess.run` as what it is not doing."""
        loop_thread = threading.get_ident()
        seen = {}

        def fake_run_checks(cfg, **kwargs):
            seen["thread"] = threading.get_ident()
            return self._clean()

        await self._check(make_config(admin_users=["alice"]), side_effect=fake_run_checks)

        assert seen["thread"] != loop_thread


# =============================================================================
# TestExportHelpers
# =============================================================================


class TestParseExportMetadata:
    @pytest.mark.parametrize("line,expected", [
        ("<!-- export:token=abc123,last_id=42,updated=2026-02-25T14:45:00Z -->",
         {"token": "abc123", "last_id": 42, "updated": "2026-02-25T14:45:00Z"}),
        ("# export:token=room1,last_id=100,updated=2026-02-25T14:45:00Z",
         {"token": "room1", "last_id": 100, "updated": "2026-02-25T14:45:00Z"}),
        ("# Just a heading", None),
        ("", None),
    ], ids=["markdown", "text", "heading", "empty"])
    def test_parse(self, line, expected):
        assert _parse_export_metadata(line) == expected

    def test_with_leading_whitespace(self):
        line = "  <!-- export:token=t,last_id=1,updated=2026-01-01T00:00:00Z -->"
        result = _parse_export_metadata(line)
        assert result is not None
        assert result["token"] == "t"


class TestBuildExportMetadata:
    def test_markdown_format(self):
        result = _build_export_metadata("room1", 42, "markdown")
        assert result.startswith("<!-- export:token=room1,last_id=42,updated=")
        assert result.endswith(" -->")

    def test_text_format(self):
        result = _build_export_metadata("room1", 42, "text")
        assert result.startswith("# export:token=room1,last_id=42,updated=")
        assert "-->" not in result


def _hist_msg(id, prompt, result, user_id="alice", created_at="2026-02-25T10:00:00Z"):
    return db.ConversationMessage(
        id=id, prompt=prompt, result=result, created_at=created_at, user_id=user_id,
    )


class TestFormatHistory:
    def test_markdown_turns(self):
        msgs = [_hist_msg(1, "Hello", "Hi there"), _hist_msg(2, "Bye", "Cya", "bob")]
        result = _format_history_markdown(msgs, "Istota")
        assert "**alice**" in result
        assert "Hello" in result
        assert "**Istota**" in result
        assert "Hi there" in result
        assert "**bob**" in result
        assert "---" in result

    def test_text_turns(self):
        result = _format_history_text([_hist_msg(1, "Hello", "Hi")], "Istota")
        assert "alice" in result
        assert "Hello" in result
        assert "Istota" in result
        assert "---" not in result  # plaintext doesn't use HR

    def test_empty(self):
        assert _format_history_markdown([], "Istota") == ""
        assert _format_history_text([], "Istota") == ""


# =============================================================================
# TestCmdExport
# =============================================================================


def _seed_conversation(conn, token="room1", user_id="alice", count=3, start=1):
    """Create ``count`` completed talk tasks in a conversation. Returns task ids."""
    ids = []
    for i in range(count):
        tid = db.create_task(
            conn, prompt=f"Prompt {start + i}", user_id=user_id,
            conversation_token=token, source_type="talk",
        )
        db.update_task_status(conn, tid, "completed", result=f"Reply {start + i}")
        ids.append(tid)
    return ids


def _export_dir(config):
    return config.workspace_path / "Users" / "alice" / "istota" / "exports" / "conversations"


def _add_unanswered(conn):
    return db.add_message(
        conn, "room1", role="user", body="just between us",
        origin_surface="talk", task_id=None, author_user_id="bob",
    )


class TestCmdExport:
    async def test_no_mount_configured(self, make_config):
        config = make_config()
        config.workspace_path = None
        with db.get_db(config.db_path) as conn:
            result = await cmd_export(_ctx(config, conn))
        assert "mount not configured" in result

    async def test_full_export_markdown(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=3)
            result = await cmd_export(_ctx(config, conn))

        assert "Exported 3 messages" in result
        assert "room1.md" in result

        export_path = _export_dir(config) / "room1.md"
        assert export_path.exists()
        content = export_path.read_text()
        assert "<!-- export:token=room1" in content
        # No registry → room name falls back to the token (surface-agnostic).
        assert "# room1" in content
        assert "Prompt 1" in content
        assert "Reply 1" in content
        assert "Prompt 3" in content

    async def test_full_export_text(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=2)
            result = await cmd_export(_ctx(config, conn, args="text"))

        assert "Exported 2 messages" in result
        export_path = _export_dir(config) / "room1.txt"
        assert export_path.exists()
        content = export_path.read_text()
        assert "# export:token=room1" in content
        assert "====" in content
        assert "---" not in content

    async def test_an_unanswered_last_turn_does_not_reset_the_export_cursor(
        self, make_config,
    ):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            ids = _seed_conversation(conn, count=2)
            db.backfill_room_messages_from_tasks(conn, "room1")
            _add_unanswered(conn)
            await cmd_export(_ctx(config, conn))

        content = (_export_dir(config) / "room1.md").read_text()
        assert f"last_id={ids[-1]}," in content.split("\n", 1)[0]
        assert "just between us" in content

    async def test_empty_channel(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_export(_ctx(config, conn))
        assert "No messages to export" in result

    async def test_excludes_incomplete_tasks(self, make_config):
        """Only completed tasks with a result are exported."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=1)
            # A pending task in the same room must not be exported.
            _task(conn, "not done yet", status=None)
            result = await cmd_export(_ctx(config, conn))
        assert "Exported 1 messages" in result

    async def test_web_surface_export(self, make_config):
        """Export works for a web-chat conversation with no Talk server at all."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _task(conn, "web question", source_type="web", token="webroom",
                        status=None)
            db.update_task_status(conn, tid, "completed", result="web answer")
            (config.workspace_path / "Channels" / "webroom").mkdir(parents=True, exist_ok=True)
            result = await cmd_export(
                _ctx(config, conn, "alice", "webroom", "", surface="web"),
            )
        assert "Exported 1 messages" in result
        content = (_export_dir(config) / "webroom.md").read_text()
        assert "web question" in content
        assert "web answer" in content

    async def test_incremental_export(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=2, start=1)
            await cmd_export(_ctx(config, conn))

        with db.get_db(config.db_path) as conn:
            new_ids = _seed_conversation(conn, count=2, start=3)
            result = await cmd_export(_ctx(config, conn))

        assert "Appended 2 new messages" in result
        updated_content = (_export_dir(config) / "room1.md").read_text()
        assert "Prompt 3" in updated_content
        assert "Reply 4" in updated_content
        # Original turns still present
        assert "Prompt 1" in updated_content
        # Metadata last_id is the newest exported task id
        meta = _parse_export_metadata(updated_content.split("\n")[0])
        assert meta["last_id"] == new_ids[-1]

    async def test_incremental_export_appends_an_unanswered_turn(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            _seed_conversation(conn, count=1)
            db.backfill_room_messages_from_tasks(conn, "room1")
            await cmd_export(_ctx(config, conn))
        with db.get_db(config.db_path) as conn:
            mid = _add_unanswered(conn)
            result = await cmd_export(_ctx(config, conn))
            again = await cmd_export(_ctx(config, conn))

        assert "Appended 1 new messages" in result
        content = (_export_dir(config) / "room1.md").read_text()
        assert content.count("just between us") == 1
        assert _parse_export_metadata(content.split("\n")[0])["last_msg_id"] == mid
        assert "No new messages" in again

    async def test_an_append_from_the_task_fallback_is_not_rewritten_later(
        self, make_config,
    ):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            _seed_conversation(conn, count=1, start=1)
            db.backfill_room_messages_from_tasks(conn, "room1")
            await cmd_export(_ctx(config, conn))
        # Unmirrored completed turns put the room on the `tasks` fallback.
        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=2, start=2)
            await cmd_export(_ctx(config, conn))
        # Mirroring them moves it back to the `messages` path.
        with db.get_db(config.db_path) as conn:
            db.backfill_room_messages_from_tasks(conn, "room1")
            result = await cmd_export(_ctx(config, conn))

        assert "No new messages" in result
        content = (_export_dir(config) / "room1.md").read_text()
        assert [content.count(f"Reply {n}") for n in (1, 2, 3)] == [1, 1, 1]

    async def test_incremental_no_new_messages(self, make_config):
        config = make_config()

        # Existing export file with a last_id higher than any seeded task.
        export_dir = _export_dir(config)
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "room1.md").write_text(
            "<!-- export:token=room1,last_id=100000,updated=2026-02-25T00:00:00Z -->\n\n# Room\n"
        )

        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=2)
            result = await cmd_export(_ctx(config, conn))

        assert "No new messages" in result

    async def test_format_aliases(self, make_config):
        """txt and plaintext should also work as format arguments."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=1)

        export_path = _export_dir(config) / "room1.txt"
        for fmt_arg in ("txt", "plaintext", "text"):
            if export_path.exists():
                export_path.unlink()
            with db.get_db(config.db_path) as conn:
                result = await cmd_export(_ctx(config, conn, args=fmt_arg))
            assert "room1.txt" in result

    async def test_different_format_creates_separate_file(self, make_config):
        """If existing export is .md but user asks for text, it creates .txt (new export)."""
        config = make_config()

        export_dir = _export_dir(config)
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "room1.md").write_text("<!-- export:token=room1,last_id=10,updated=2026-02-25T00:00:00Z -->\n")

        with db.get_db(config.db_path) as conn:
            _seed_conversation(conn, count=1)
            result = await cmd_export(_ctx(config, conn, args="text"))

        # Should create a new .txt file, not append to .md
        assert "room1.txt" in result
        assert "Exported 1 messages" in result
        assert (export_dir / "room1.txt").exists()
        assert (export_dir / "room1.md").exists()  # original still there


# ---------------------------------------------------------------------------
# TestCmdMore
# ---------------------------------------------------------------------------


def _finished(db_path, prompt, *, user_id="alice", status="completed", **fields):
    with db.get_db(db_path) as conn:
        task_id = db.create_task(conn, prompt=prompt, user_id=user_id)
        db.update_task_status(conn, task_id, status, **fields)
    return task_id


async def _more(config, args):
    with db.get_db(config.db_path) as conn:
        return await cmd_more(_ctx(config, conn, args=args))


class TestCmdMore:
    """Test !more command for viewing execution traces."""

    # `isdigit` is True for '²' and `int()` refuses it, so this guard used to
    # fall through and the user got `Command !more failed: invalid literal for
    # int()` rather than the usage line. Same guard as `!confirm` and `!stop`.
    @pytest.mark.parametrize("args", ["²", "notanumber"])
    async def test_a_malformed_id_returns_the_usage_line(self, make_config, args):
        assert "Usage:" in await _more(make_config(), args)

    async def test_shows_execution_trace(self, make_config, db_path):
        trace = json.dumps([
            {"type": "text", "text": "Let me look into that."},
            {"type": "tool", "text": "Read config.py"},
            {"type": "text", "text": "I see the issue. Let me fix it."},
            {"type": "tool", "text": "Edit config.py"},
        ])
        task_id = _finished(db_path, "Fix the config", result="Fixed it.",
                            execution_trace=trace)
        result = await _more(make_config(), str(task_id))

        assert f"Task #{task_id}" in result
        assert "Let me look into that." in result
        assert "Read config.py" in result
        assert "Edit config.py" in result
        assert "Fixed it." in result

    async def test_shows_the_question_a_confirmed_task_asked(self, make_config, db_path):
        trace = json.dumps([
            {"type": "tool", "text": "List files"},
            {"type": "gate", "text": "May I delete them?", "outcome": "approved"},
            {"type": "tool", "text": "Delete files"},
        ])
        task_id = _finished(db_path, "Clean up", result="Done", execution_trace=trace)
        result = await _more(make_config(), str(task_id))
        assert "May I delete them? (approved)" in result

    async def test_accepts_hash_prefix(self, make_config, db_path):
        trace = json.dumps([{"type": "tool", "text": "Read file"}])
        task_id = _finished(db_path, "Test", result="Done", execution_trace=trace)
        assert f"Task #{task_id}" in await _more(make_config(), f"#{task_id}")

    async def test_no_trace_available(self, make_config, db_path):
        task_id = _finished(db_path, "Old task", result="Done")
        assert "no execution trace" in await _more(make_config(), str(task_id))

    async def test_task_not_found(self, make_config):
        assert "not found" in await _more(make_config(), "99999")

    async def test_other_users_task_blocked(self, make_config, db_path):
        config = make_config()
        config.admin_users = {"bob"}  # alice is NOT admin
        task_id = _finished(db_path, "Secret", user_id="bob", result="Done")
        assert "another user" in await _more(config, str(task_id))

    async def test_running_task_shows_status(self, make_config, db_path):
        task_id = _finished(db_path, "In progress", status="running")
        assert "still running" in await _more(make_config(), str(task_id))


# =============================================================================
# TestCmdSearch
# =============================================================================


def _hit(summary, task_id=100, token="room1", date="Apr 1", **extra):
    return {
        "date": date, "room": token, "summary": summary, "task_id": task_id,
        "conversation_token": token, **extra,
    }


def _talk_hit(summary, token="room1", date="Apr 1", **extra):
    return {
        "date": date, "room": token, "summary": summary,
        "conversation_token": token, **extra,
    }


async def _identity_resolve(client, tokens):
    return {t: t for t in tokens}


async def _search(config, args, memory=(), talk=(), *, token="room1",
                  surface="talk", resolve=_identity_resolve):
    """Run `!search` against canned memory and Talk hits.

    Returns the reply and both search mocks, so a test can read their calls.
    """
    with (
        db.get_db(config.db_path) as conn,
        patch("istota.commands._search_memory", return_value=list(memory)) as mock_mem,
        patch("istota.commands._search_talk_api", return_value=list(talk)) as mock_talk,
        patch("istota.commands._resolve_room_names", side_effect=resolve),
    ):
        result = await cmd_search(
            _ctx(config, conn, conversation_token=token, args=args, surface=surface)
        )
    return result, mock_mem, mock_talk


class TestCmdSearch:
    """Test !search command for conversation history search."""

    @pytest.mark.parametrize("args", ["", "   "], ids=["empty", "whitespace"])
    async def test_empty_query_returns_usage(self, make_config, args):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_search(_ctx(config, conn, args=args))
        assert "Usage" in result
        assert "!search" in result
        # Usage string should mention the filter flags.
        assert "--since" in result
        assert "--memories" in result

    async def test_search_current_room_no_results(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            result = await cmd_search(_ctx(config, conn, args="nonexistent query xyz"))
        assert "No results" in result

    async def test_search_current_room_filters_by_token(self, make_config, db_path):
        """Results from other rooms should be excluded when searching current room."""
        config = make_config()
        with db.get_db(db_path) as conn:
            t1 = _task(conn, "parser bug discussion", status=None)
            db.update_task_status(conn, t1, "completed", result="Fixed the parser bug")
            t2 = _task(conn, "parser bug in other room", token="room2", status=None)
            db.update_task_status(conn, t2, "completed", result="Also about parser")

        result, _, _ = await _search(config, "parser bug", memory=[
            _hit("Parser bug in room1", t1),
            _hit("Parser bug in room2", t2, "room2", date="Mar 28"),
        ])

        assert "Parser bug in room1" in result
        assert "room2" not in result

    async def test_search_all_rooms(self, make_config):
        """--all flag should return results from all rooms."""
        result, _, _ = await _search(make_config(), "--all parser bug", memory=[
            _hit("Bug in room1", 100),
            _hit("Bug in room2", 200, "room2", date="Mar 28"),
        ])
        assert "room1" in result
        assert "room2" in result

    async def test_search_specific_room(self, make_config):
        """--room flag should filter to a specific room."""
        result, _, _ = await _search(
            make_config(), "--room otherroom some query", memory=[
                _hit("Result in room1", 100),
                _hit("Result in other", 200, "otherroom", date="Mar 28"),
            ],
        )
        assert "otherroom" in result
        assert "Result in room1" not in result

    async def test_output_format_includes_task_reference(self, make_config):
        """Results without talk_message_id should fall back to task ID reference."""
        result, _, _ = await _search(
            make_config(), "--all parser bug", memory=[_hit("Fixed parser bug", 46945)],
        )
        assert "#46945" in result

    async def test_web_surface_skips_talk_api(self, make_config):
        """On a non-Talk surface, search relies on the memory index only and
        never reaches for the Talk full-text API or builds Talk deep links."""
        result, _, mock_talk = await _search(
            make_config(), "--all web chat", token="webroom", surface="web",
            memory=[_hit("found in web chat", 5, "webroom", talk_message_id=999)],
        )

        mock_talk.assert_not_called()
        assert "found in web chat" in result
        # No Talk deep link on a non-Talk surface
        assert "/call/" not in result

    async def test_output_format_with_message_link(self, make_config):
        """Results with talk_message_id should show a deep link instead of task ref."""
        result, _, _ = await _search(make_config(), "--all parser bug", memory=[
            _hit("Fixed parser bug", 100, talk_message_id=38939),
        ])
        assert "https://nc.test/call/room1#message_38939" in result

    async def test_output_format_header(self, make_config):
        """Output should start with result count and query."""
        result, _, _ = await _search(
            make_config(), "--all test query", memory=[_hit("Found it")],
        )
        assert "1 result" in result
        assert "test query" in result

    async def test_talk_api_results_included(self, make_config):
        """Talk API results should be merged when memory search has no hits."""
        result, _, _ = await _search(make_config(), "--all recent chat", talk=[
            _talk_hit("Recent chat message",
                      talk_link="https://nc.test/call/room1#message_123"),
        ])
        assert "Recent chat message" in result
        assert "https://nc.test/call/room1#message_123" in result

    async def test_talk_api_results_filtered_by_room(self, make_config):
        """Talk API results should respect room scoping (current room by default)."""
        result, _, _ = await _search(make_config(), "some query", talk=[
            _talk_hit("In room1", talk_link="https://nc.test/call/room1#message_1"),
            _talk_hit("In room2", "room2",
                      talk_link="https://nc.test/call/room2#message_2"),
        ])
        assert "In room1" in result
        assert "In room2" not in result

    async def test_max_results_capped(self, make_config):
        """Should cap results at 8."""
        many_results = [
            _hit(f"Result {i}", i, date=f"Apr {i}") for i in range(15)
        ]
        result, _, _ = await _search(
            make_config(), "--all lots of results", memory=many_results,
        )
        numbered = re.findall(r"^\d+\.", result, re.MULTILINE)
        assert len(numbered) <= 8

    async def test_deduplication_between_sources(self, make_config):
        """Same task_id from memory and Talk should not appear twice."""
        result, _, _ = await _search(
            make_config(), "--all test",
            memory=[_hit("From memory", 100)], talk=[_hit("From talk", 100)],
        )
        assert "1 result" in result

    async def test_room_names_resolved(self, make_config):
        """Room tokens should be resolved to display names."""
        async def _named_resolve(client, tokens):
            return {t: {"room1": "General", "room2": "Dev Chat"}.get(t, t) for t in tokens}

        result, _, _ = await _search(
            make_config(), "--all discussion", memory=[_hit("Some discussion")],
            resolve=_named_resolve,
        )
        assert "in General" in result
        assert "room1" not in result  # token should not appear


class TestSearchMemory:
    """Test the _search_memory helper that wraps memory_search."""

    def test_maps_search_results_to_dicts(self, make_config, db_path):
        from istota.memory.search import SearchResult

        config = make_config()
        with db.get_db(db_path) as conn:
            # Create the task so we can resolve its conversation_token
            task_id = db.create_task(conn, prompt="Fix parser", user_id="alice",
                                     conversation_token="room1", source_type="talk",
                                     talk_message_id=12345)
            mock_results = [
                SearchResult(
                    chunk_id=1,
                    content="User: How do I fix the parser?\n\nBot: Check stream_parser.py",
                    score=0.8, source_type="conversation", source_id=str(task_id),
                    metadata={"task_id": str(task_id)},
                ),
            ]
            with patch("istota.commands.memory_search_mod.search", return_value=mock_results):
                results = _search_memory(config, conn, "alice", "fix parser")

        assert len(results) == 1
        assert results[0]["task_id"] == task_id
        assert results[0]["conversation_token"] == "room1"
        assert results[0]["talk_message_id"] == 12345
        assert len(results[0]["summary"]) > 0

    def test_returns_empty_when_no_results(self, make_config, db_path):
        config = make_config()
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands.memory_search_mod.search", return_value=[]),
        ):
            assert _search_memory(config, conn, "alice", "nothing here") == []

    def test_skips_results_without_task(self, make_config, db_path):
        """Memory results whose source_id doesn't map to a task should still work."""
        from istota.memory.search import SearchResult

        config = make_config()
        mock_results = [
            SearchResult(
                chunk_id=1, content="Some memory file content",
                score=0.5, source_type="memory_file", source_id="memories/2026-03-28.md",
                metadata={},
            ),
        ]
        with (
            db.get_db(db_path) as conn,
            patch("istota.commands.memory_search_mod.search", return_value=mock_results),
        ):
            results = _search_memory(config, conn, "alice", "memory content")

        # memory_file results have no task_id — should still appear with no task ref
        assert len(results) == 1
        assert results[0].get("task_id") is None


class TestSearchTalkApi:
    """Test the _search_talk_api helper.

    It shares its implementation with `nextcloud talk search` — both go through
    TalkClient.search_messages on the persistent singleton.
    """

    @staticmethod
    def _talk_client(data=None, error=None):
        client = MagicMock()
        client.search_messages = AsyncMock(
            side_effect=error) if error else AsyncMock(return_value=data)
        return patch("istota.async_runtime.get_talk_client", return_value=client)

    async def test_returns_formatted_results(self, make_config):
        # Mock the OCS response from Nextcloud unified search
        mock_ocs_data = {
            "entries": [
                {
                    "title": "Recent message about deployment",
                    "subline": "Let me check the deploy status",
                    "resourceUrl": "https://nc.test/call/room1#message_456",
                    "attributes": {"conversation": "room1", "messageId": "456"},
                },
            ],
        }
        with self._talk_client(mock_ocs_data):
            results = await _search_talk_api(make_config(), "deploy")

        assert len(results) == 1
        # subline is preferred over title (title is "username in room")
        assert "deploy status" in results[0]["summary"]
        assert results[0]["conversation_token"] == "room1"
        assert "message_456" in results[0]["talk_link"]

    @pytest.mark.parametrize("kwargs", [
        {"error": RuntimeError("Talk unreachable")},
        {"data": {"entries": []}},
    ], ids=["api-failure", "no-entries"])
    async def test_returns_empty(self, make_config, kwargs):
        with self._talk_client(**kwargs):
            assert await _search_talk_api(make_config(), "test") == []


class TestParseSearchArgs:
    """Test _parse_search_args with new --since, --week, --memories flags."""

    # `--since` with no date after it is query text; flags are order-independent.
    @pytest.mark.parametrize("args,scope,query,since,memories_only", [
        ("hello world", None, "hello world", None, False),
        ("--all some query", "all", "some query", None, False),
        ("--room abc123 some query", "abc123", "some query", None, False),
        ("--since 2026-03-25 deployment", None, "deployment", "2026-03-25", False),
        ("--memories something", None, "something", None, True),
        ("--all --since 2026-01-01 query here", "all", "query here", "2026-01-01", False),
        ("", None, "", None, False),
        ("--since", None, "--since", None, False),
        ("--memories --all query", "all", "query", None, True),
        ("--all --memories query", "all", "query", None, True),
    ])
    def test_parse(self, args, scope, query, since, memories_only):
        result = _parse_search_args(args)
        assert result.scope == scope
        assert result.query == query
        assert result.since == since
        assert result.memories_only is memories_only

    def test_week_flag(self):
        result = _parse_search_args("--week deployment")
        expected = (date.today() - timedelta(days=7)).isoformat()
        assert result.since == expected
        assert result.query == "deployment"

    def test_combined_flags(self):
        result = _parse_search_args("--all --week --memories deployment")
        assert result.scope == "all"
        assert result.since is not None
        assert result.memories_only is True
        assert result.query == "deployment"


class TestCmdSearchFiltering:
    """Test !search with --since, --week, --memories filtering."""

    async def test_memories_only_skips_talk_api(self, make_config):
        """--memories should skip Talk API search entirely."""
        result, _, mock_talk = await _search(
            make_config(), "--all --memories test", memory=[
                _hit("Memory result", None, date="Mar 28", source_type="memory_file"),
            ],
        )
        mock_talk.assert_not_called()
        assert "Memory result" in result

    async def test_memories_only_passes_source_types(self, make_config):
        """--memories passes the full memory source set (files + user + skill
        overlays + channel) to _search_memory — not just conversation-less
        memory_file. A skill overlay reaches a prompt only on a task that
        selected its skill, so this is the surface that finds one from a room."""
        _, mock_mem, _ = await _search(make_config(), "--all --memories test")
        assert mock_mem.call_args.kwargs.get("source_types") == [
            "memory_file", "user_memory", "skill_overlay",
            "channel_memory", "channel_memory_durable",
        ]

    async def test_since_passed_to_search_memory(self, make_config):
        """--since should be forwarded to _search_memory."""
        _, mock_mem, _ = await _search(make_config(), "--all --since 2026-03-01 test")
        assert mock_mem.call_args.kwargs.get("since") == "2026-03-01"

    async def test_since_filters_talk_results(self, make_config):
        """--since should filter out Talk API results older than the date."""
        result, _, _ = await _search(
            make_config(), "--all --since 2026-03-20 test", talk=[
                _talk_hit("Old result", date="2026-03-15"),
                _talk_hit("Recent result", date="2026-03-25"),
            ],
        )
        assert "Recent result" in result
        assert "Old result" not in result


# =============================================================================
# TestTrustCommand
# =============================================================================


async def _trust(config, args, command=cmd_trust):
    with db.get_db(config.db_path) as conn:
        return await command(_ctx(config, conn, args=args))


def _add_trusted(config, sender="joe@example.com"):
    with db.get_db(config.db_path) as conn:
        db.add_trusted_sender(conn, "alice", sender)


def _is_trusted(config, sender="joe@example.com"):
    with db.get_db(config.db_path) as conn:
        return db.is_sender_trusted_in_db(conn, "alice", sender)


class TestTrustCommand:
    async def test_trust_adds_sender(self, make_config):
        config = make_config()
        result = await _trust(config, "joe@example.com")
        assert "Trusted" in result
        assert "joe@example.com" in result
        assert _is_trusted(config) is True

    async def test_trust_duplicate(self, make_config):
        config = make_config()
        _add_trusted(config)
        assert "already trusted" in await _trust(config, "joe@example.com")

    async def test_trust_no_args_lists_senders(self, make_config):
        config = make_config()
        config.users["alice"] = UserConfig(trusted_email_senders=["*@corp.com"])
        _add_trusted(config)
        result = await _trust(config, "")
        assert "*@corp.com" in result
        assert "(config)" in result
        assert "joe@example.com" in result

    async def test_trust_invalid_email(self, make_config):
        assert "Usage" in await _trust(make_config(), "notanemail")

    async def test_untrust_removes_sender(self, make_config):
        config = make_config()
        _add_trusted(config)
        assert "Removed" in await _trust(config, "joe@example.com", cmd_untrust)
        assert _is_trusted(config) is False

    async def test_untrust_nonexistent(self, make_config):
        result = await _trust(make_config(), "nobody@example.com", cmd_untrust)
        assert "not in your trusted" in result

    async def test_trust_list_empty(self, make_config):
        assert "No trusted senders" in await _trust(make_config(), "")


# =============================================================================
# TestCmdUsage
# =============================================================================


def _seed_usage_row(
    conn,
    *,
    user_id,
    created_at,
    brain_kind="claude_code",
    billed=0,
    cache_read=0,
    output=0,
    cost_usd=0.0,
    cost_basis="subscription",
):
    """One `task_usage` row, stamped explicitly and committed.

    Raw SQL rather than `db.insert_task_usage`: these tests care only about the
    aggregates `!usage` renders, and `created_at` has to be moved to a chosen
    instant afterwards anyway. The literal is the ISO-Z format the column
    stores; a space-separated one sorts below every bound and matches nothing.

    Committed because the handler reads through its own connection — it does its
    DB work in a worker thread, where the caller's connection is unusable.
    """
    conn.execute(
        "INSERT INTO task_usage (created_at, user_id, brain_kind, has_totals,"
        " billed_input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " cost_usd, cost_basis) VALUES (?, ?, ?, 1, ?, ?, ?, 0, ?, ?)",
        (created_at, user_id, brain_kind, billed, output, cache_read, cost_usd, cost_basis),
    )
    conn.commit()


def _recent_iso(hours_ago=1):
    """An instant inside both the 24h and the 30d window, in the stored format."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _window(key="session", label="5-hour", percent=40.0, resets_at=None, resets_in=None):
    from istota.usage import subscription as su

    return su.UsageWindow(
        key=key,
        label=label,
        percent=percent,
        resets_at=resets_at,
        resets_in_seconds=resets_in,
    )


def _snapshot(windows=None, *, spend=None, source="cache", error="", age=0.0):
    import time

    from istota.usage import subscription as su

    return su.UsageSnapshot(
        fetched_at=time.time() - age,
        windows=tuple(windows if windows is not None else [_window()]),
        spend=spend,
        source=source,
        token_source="env",
        error=error,
    )


_RESET_0900 = _snapshot([_window(resets_at="2026-08-22T09:00:00Z", resets_in=3600)])


class TestCmdUsage:
    """`!usage`: token totals for everyone, the fleet split and the plan for admins.

    Every test sets `config.admin_users` explicitly. The default empty allowlist
    makes *everyone* an admin by `Config.is_admin`'s documented back-compat rule,
    so a test that omits it exercises no split at all and would pass against a
    handler with no gate in it.

    `get_snapshot` is patched per test rather than left to the suite-wide autouse
    guard in `tests/conftest.py`: that guard returns a no-credential snapshot,
    which is the one shape that makes an unrendered section 3 look correct.
    """

    def _patch_snapshot(self, monkeypatch, snapshot):
        """Patch `get_snapshot` and return the list it records its calls in."""
        calls = []

        def _fake(config, **kwargs):
            calls.append(kwargs)
            return snapshot

        monkeypatch.setattr(_subscription_usage, "get_snapshot", _fake)
        return calls

    def _admin_config(self, make_config, monkeypatch, snapshot=None):
        """A config whose only admin is bob, with `snapshot` patched in."""
        config = make_config()
        config.admin_users = {"bob"}
        calls = self._patch_snapshot(
            monkeypatch, _snapshot() if snapshot is None else snapshot
        )
        return config, calls

    async def _usage(self, config, user_id="bob", rows=()):
        with db.get_db(config.db_path) as conn:
            for row in rows:
                _seed_usage_row(conn, created_at=_recent_iso(), **row)
            return await cmd_usage(_ctx(config, conn, user_id))

    async def _render(self, make_config, db_path, monkeypatch, snapshot, user_id="bob"):
        """Run the handler as an admin against `snapshot`, with no usage rows."""
        config, _ = self._admin_config(make_config, monkeypatch, snapshot)
        return await self._usage(config, user_id)

    # -- the admin split -----------------------------------------------------

    async def test_non_admin_sees_only_their_own_token_totals(
        self, make_config, monkeypatch
    ):
        """And never asks for the snapshot at all — not "the reply carries no
        percentage": that assertion passes just as well against a handler that
        fetched the plan and forgot to print it."""
        config, calls = self._admin_config(make_config, monkeypatch)
        result = await self._usage(config, "alice", rows=[
            {"user_id": "alice", "billed": 1000},
            {"user_id": "bob", "billed": 5_000_000},
        ])

        assert "**Token usage**" in result
        assert "1,000 tokens" in result
        assert "5.0M" not in result
        assert "**By brain**" not in result
        assert "**Claude Code subscription**" not in result
        assert calls == []

    async def test_an_unattributable_caller_gets_nobodys_rows(
        self, make_config, monkeypatch
    ):
        """An empty `user_id` must not read as "no filter".

        `db._usage_filters` gates the `WHERE u.user_id = ?` clause on
        truthiness, so passing a falsy id straight through would drop the clause
        and hand a caller `is_admin` just refused the whole deployment's totals.
        """
        config, calls = self._admin_config(make_config, monkeypatch)
        result = await self._usage(config, "", rows=[
            {"user_id": "alice", "billed": 1000},
            {"user_id": "bob", "billed": 5_000_000},
        ])

        assert "No usage recorded." in result
        assert "1,000 tokens" not in result
        assert "5.0M" not in result
        assert "**By brain**" not in result
        assert calls == []

    async def test_admin_gets_all_three_sections_fleet_wide(
        self, make_config, monkeypatch
    ):
        config, calls = self._admin_config(make_config, monkeypatch)
        result = await self._usage(config, rows=[
            {"user_id": "alice", "billed": 1000, "brain_kind": "claude_code"},
            {"user_id": "bob", "billed": 2000, "brain_kind": "native",
             "cost_usd": 4.18, "cost_basis": "api"},
        ])

        assert len(calls) == 1
        assert "**Token usage** — fleet" in result
        assert "3,000 tokens" in result  # alice's row is inside bob's total
        assert "**By brain** (30d)" in result
        assert "- claude_code: 1,000 tokens, —" in result
        assert "- native: 2,000 tokens, $4.18" in result
        assert "**Claude Code subscription**" in result

    async def test_cost_rule_survives_the_move_to_usage_render(
        self, make_config, monkeypatch
    ):
        """A subscription-only group renders the placeholder, never a figure.

        The rule is imported from `usage_render` rather than written here; this
        is the case that notices if the import went to the wrong thing.
        """
        config, _ = self._admin_config(make_config, monkeypatch)
        result = await self._usage(config, rows=[
            {"user_id": "bob", "billed": 1000, "cost_usd": 12.34,
             "cost_basis": "subscription"},
        ])

        assert "12.34" not in result
        assert "- claude_code: 1,000 tokens, —" in result

    # -- rendering -----------------------------------------------------------

    async def test_bar_is_twenty_characters_at_every_extreme(
        self, make_config, db_path, monkeypatch
    ):
        snapshot = _snapshot([
            _window(key="a", label="Empty", percent=0.0),
            _window(key="b", label="Middle", percent=40.0),
            _window(key="c", label="Full", percent=100.0),
        ])
        result = await self._render(make_config, db_path, monkeypatch, snapshot)

        assert "- Empty: [--------------------] 0%" in result
        assert "- Middle: [########------------] 40%" in result
        assert "- Full: [####################] 100%" in result
        bars = [
            line.split("[", 1)[1].split("]", 1)[0]
            for line in result.splitlines()
            if line.startswith("- ") and "[" in line
        ]
        assert bars and all(len(bar) == 20 for bar in bars)

    # +08:45 (Eucla) is an offset no other zone this suite might pick up by
    # accident produces, so the first case fails if the conversion is skipped.
    # The line says which zone it rendered in, not why that zone was picked: a
    # UTC clock is labelled UTC however it was arrived at — configured, the
    # resolver's own fallback for a user with no profile, or an unparseable zone.
    @pytest.mark.parametrize("users,expected,absent", [
        ({"bob": UserConfig(timezone="Australia/Eucla")},
         "(resets Aug 22 17:45)", "UTC"),
        ({"bob": UserConfig(timezone="UTC")}, "(resets Aug 22 09:00 UTC)", None),
        ({}, "(resets Aug 22 09:00 UTC)", None),
        ({"bob": UserConfig(timezone="Mars/Olympus_Mons")},
         "(resets Aug 22 09:00 UTC)", None),
    ], ids=["users-timezone", "explicit-utc", "missing-profile", "unparseable-zone"])
    async def test_reset_renders_in_the_users_timezone(
        self, make_config, monkeypatch, users, expected, absent
    ):
        config, _ = self._admin_config(make_config, monkeypatch, _RESET_0900)
        config.users = users
        result = await self._usage(config)

        assert expected in result
        if absent:
            assert absent not in result

    async def test_reset_uses_the_live_profile_row_not_the_booted_config(
        self, make_config, db_path, monkeypatch
    ):
        """The DB row wins over the in-memory `UserConfig`, and must.

        `Config.resolve_user_timezone` prefers the live `user_profiles` row so a
        web-UI timezone edit takes effect without a scheduler restart
        (ISSUE-099). Pinned at the resolver rather than at a rendered string: the
        two sources are deliberately set to *different* zones, so swapping back
        to `config.get_user(user_id).timezone` renders 03:00 and fails here.
        """
        from istota import user_profiles

        config, _ = self._admin_config(make_config, monkeypatch, _RESET_0900)
        # What the daemon booted with...
        config.users = {"bob": UserConfig(timezone="America/Denver")}
        # ...and what the user has since set in the web UI. +08:45.
        user_profiles.ensure_profile(db_path, "bob", timezone="Australia/Eucla")
        result = await self._usage(config)

        assert "(resets Aug 22 17:45)" in result
        assert "03:00" not in result

    async def test_the_resolver_is_given_the_connection_already_open(
        self, make_config, monkeypatch
    ):
        """A caller holding a framework-DB connection must not make it open
        another — per-call FD churn on the FUSE-backed mount is why
        `resolve_user_timezone` takes a `conn` at all."""
        config, _ = self._admin_config(make_config, monkeypatch)

        seen = []
        real = config.resolve_user_timezone

        def _spy(user_id, *, conn=None):
            seen.append(conn)
            return real(user_id, conn=conn)

        config.resolve_user_timezone = _spy

        with db.get_db(config.db_path) as conn:
            await cmd_usage(_ctx(config, conn, "bob"))

        assert seen == [conn]

    async def test_a_range_edge_reset_drops_its_stamp_instead_of_raising(
        self, make_config, monkeypatch
    ):
        """`astimezone` raises `OverflowError` at the edge of the date range.

        `9999-12-31T23:00:00Z` is *canonical* — it survives
        `subscription_usage._normalize_resets_at` unchanged — and it is one
        keystroke from the sentinel expiry this codebase writes into credential
        files. Shifting it east of UTC overflows, and `dispatch` would post the
        raw exception to the room. The percentage is still the reading.
        """
        config, _ = self._admin_config(
            make_config, monkeypatch,
            _snapshot([_window(percent=40.0, resets_at="9999-12-31T23:00:00Z")]),
        )
        config.users = {"bob": UserConfig(timezone="Australia/Eucla")}
        result = await self._usage(config)

        assert "- 5-hour: [########------------] 40%" in result
        assert "resets" not in result

    async def test_the_bar_reserves_its_two_ends(
        self, make_config, db_path, monkeypatch
    ):
        """A quota display is read at the ends, so neither may lie.

        `round` fills all twenty blocks from 97.5% up and empties the bar below
        2.5% — drawing a window with headroom as exhausted, and one being
        consumed as untouched.
        """
        snapshot = _snapshot([
            _window(key="a", label="Nearly", percent=97.6),
            _window(key="b", label="Barely", percent=0.4),
        ])
        result = await self._render(make_config, db_path, monkeypatch, snapshot)

        assert "- Nearly: [###################-] 98%" in result
        assert "- Barely: [#-------------------] 0%" in result

    async def test_a_window_with_no_reset_says_nothing_about_one(
        self, make_config, db_path, monkeypatch
    ):
        result = await self._render(
            make_config, db_path, monkeypatch, _snapshot([_window(percent=0.0)])
        )

        assert "- 5-hour: [--------------------] 0%" in result
        assert "resets" not in result

    async def test_extra_usage_line_only_when_spend_is_enabled(
        self, make_config, db_path, monkeypatch
    ):
        su = _subscription_usage
        off = su.Spend(enabled=False, used_minor=0, limit_minor=2000, percent=0.0)
        result = await self._render(
            make_config, db_path, monkeypatch, _snapshot(spend=off)
        )
        assert "Extra usage" not in result

        on = su.Spend(enabled=True, used_minor=125, limit_minor=2000, percent=6.0)
        result = await self._render(
            make_config, db_path, monkeypatch, _snapshot(spend=on)
        )
        assert "**Extra usage:** $1.25 / $20.00 (6%)" in result

    async def test_extra_usage_takes_its_divisor_from_the_exponent(
        self, make_config, db_path, monkeypatch
    ):
        """A zero-decimal currency is not cents. Dividing by a hardcoded 100 is
        the bug the removed implementation carried."""
        spend = _subscription_usage.Spend(
            enabled=True, used_minor=500, limit_minor=2000,
            currency="JPY", exponent=0, percent=25.0,
        )
        result = await self._render(
            make_config, db_path, monkeypatch, _snapshot(spend=spend)
        )
        # Zero-decimal, so no fabricated ".00" either: the exponent sets the
        # divisor and the precision, and 500 yen is 500 yen.
        assert "**Extra usage:** 500 JPY / 2000 JPY (25%)" in result

    async def test_staleness_footer_only_on_an_old_reading(
        self, make_config, db_path, monkeypatch
    ):
        fresh = await self._render(make_config, db_path, monkeypatch, _snapshot())
        assert "Reading is" not in fresh

        stale = await self._render(
            make_config,
            db_path,
            monkeypatch,
            _snapshot(source="stale-cache", error="could not reach api.anthropic.com", age=125.0),
        )
        assert "_Reading is 2m old._" in stale

    async def test_the_staleness_footer_never_carries_the_fetch_error(
        self, make_config, db_path, monkeypatch
    ):
        """A chat reply is not where a diagnostic endpoint's failure is
        reported; `runtime.subscription_usage` is."""
        result = await self._render(
            make_config,
            db_path,
            monkeypatch,
            _snapshot(source="stale-cache", error="HTTP 403 from api.anthropic.com", age=300.0),
        )
        assert "403" not in result
        assert "_Reading is 5m old._" in result

    # -- degradation ---------------------------------------------------------

    async def test_an_unavailable_snapshot_omits_section_three_silently(
        self, make_config, monkeypatch
    ):
        su = _subscription_usage
        config, _ = self._admin_config(
            make_config, monkeypatch,
            su.UsageSnapshot(fetched_at=0.0, source="none", error=su.NO_CREDENTIAL_ERROR),
        )
        result = await self._usage(config, rows=[{"user_id": "bob", "billed": 1000}])

        assert "**Token usage**" in result
        assert "**By brain**" in result
        assert "**Claude Code subscription**" not in result
        assert "credential" not in result

    async def test_no_usage_rows_says_so_and_still_renders_the_plan(
        self, make_config, db_path, monkeypatch
    ):
        result = await self._render(make_config, db_path, monkeypatch, _snapshot())

        assert "No usage recorded." in result
        assert "**By brain**" not in result
        assert "**Claude Code subscription**" in result

    async def test_a_missing_task_usage_table_degrades_to_one_line(
        self, make_config, monkeypatch
    ):
        """`dispatch` would otherwise put `no such table: task_usage` into a
        chat room. The table is created on the next database open."""
        def _no_table(*args, **kwargs):
            raise sqlite3.OperationalError("no such table: task_usage")

        monkeypatch.setattr(db, "usage_summary", _no_table)
        config, _ = self._admin_config(make_config, monkeypatch)
        result = await self._usage(config)

        assert isinstance(result, str)
        assert "no such table" not in result
        assert "task_usage" in result
        # The remedy has to name something that actually creates the table.
        # `db.get_db` only connects; `init_db` runs the schema.
        assert "istota init" in result
        assert "**Claude Code subscription**" in result

    # Only the missing-table case degrades. A locked database is a real fault
    # and `dispatch` reports it, exactly as `istota usage` does; and "no such
    # table" alone would dress a different missing table up as a fresh
    # deployment, pointing the reader at a remedy that fixes nothing.
    @pytest.mark.parametrize("message", [
        "database is locked", "no such table: task_usage_models",
    ])
    async def test_any_other_operational_error_is_not_swallowed(
        self, make_config, monkeypatch, message
    ):
        def _raise(*args, **kwargs):
            raise sqlite3.OperationalError(message)

        monkeypatch.setattr(db, "usage_summary", _raise)
        config, _ = self._admin_config(make_config, monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            await self._usage(config)

    # -- the shared reading --------------------------------------------------

    async def test_the_handler_issues_no_fetch_of_its_own(
        self, make_config, monkeypatch
    ):
        """Two `!usage` calls against a fresh cache open no connection at all.

        This is the test that catches a fetch reimplemented inside the handler:
        the spy is on `urllib.request.urlopen`, not on the module's transport
        seam, so a second implementation that bypassed `subscription_usage`
        entirely would still trip it.
        """
        import time
        import urllib.request

        su = _subscription_usage
        # The root conftest replaces `get_snapshot`; this is the one test here
        # that wants the real policy, because the cache hit is what it asserts.
        monkeypatch.setattr(su, "get_snapshot", _REAL_GET_SNAPSHOT)

        opened = []

        def _spy(*args, **kwargs):
            opened.append(args)
            raise AssertionError("!usage opened a connection of its own")

        monkeypatch.setattr(urllib.request, "urlopen", _spy)
        monkeypatch.setattr(urllib.request.OpenerDirector, "open", _spy)

        config = make_config()
        config.admin_users = {"bob"}
        su.write_cache(
            su.cache_path(config.db_path.parent),
            _snapshot([_window(percent=40.0)]),
        )
        # The TTL comes off the config the handler will use, not a literal:
        # otherwise the precondition checks one policy and the behaviour under
        # test another, and the test passes only while the two agree.
        ttl = float(config.brain.claude_code.subscription_usage_cache_ttl_seconds)
        assert su.read_cache(
            su.cache_path(config.db_path.parent), ttl, now_ts=time.time()
        ) is not None

        first = await self._usage(config)
        second = await self._usage(config)

        assert opened == []
        assert "- 5-hour: [########------------] 40%" in first
        assert first == second

    # -- the credential ------------------------------------------------------

    async def test_the_token_value_never_reaches_the_reply(
        self, make_config, tmp_path, monkeypatch
    ):
        """A sentinel in every resolvable source, absent from the returned text.

        Nothing downstream would catch a leak here: this credential is not in the
        config, so the redaction pass covering configured secrets has never seen
        it. Driven through the *real* `get_snapshot` against a stub host, so the
        resolver and the fetch both actually run — a stubbed snapshot cannot leak
        a token it was never given, and the test would prove nothing.

        A stale cache is seeded deliberately. Without one, a refused credential
        produces no windows, section 3 is omitted, and "the token is absent" is
        true of a reply that rendered nothing at all. With one, the 403 lands on
        the stale-cache branch, so the section *and* the footer built beside
        `snapshot.error` both render — which is the only place a leak could go.
        """
        import subprocess as _subprocess

        su = _subscription_usage
        sentinel = "sk-ant-oat01-" + "z" * 40
        blob = json.dumps({"claudeAiOauth": {"accessToken": sentinel}})
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text(blob)

        sent_headers = []

        def _transport(url, headers, timeout):
            sent_headers.append(headers)
            # A 403 is the branch that has an error string to render, which is
            # where a leak would surface if one were going to. The body echoes
            # the token back, as a hostile endpoint could.
            return 403, b'{"error":{"message":"' + sentinel.encode() + b'"}}'

        # Darwin too, so the keychain branch is a resolvable source rather than
        # one skipped on a Linux runner: all three carry the sentinel.
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            su.subprocess,
            "run",
            lambda *a, **k: _subprocess.CompletedProcess(a[0] if a else [], 0, blob, ""),
        )

        def _real(config, *, now_ts, **kwargs):
            return _REAL_GET_SNAPSHOT(
                config, now_ts=now_ts, transport=_transport,
                env={"CLAUDE_CODE_OAUTH_TOKEN": sentinel, "USER": "someone"},
                home=home,
            )

        monkeypatch.setattr(su, "get_snapshot", _real)

        config = make_config()
        config.admin_users = {"bob"}
        # Older than the 300s TTL, so it is not served instead of the fetch, but
        # still there for the stale fallback the 403 takes.
        su.write_cache(
            su.cache_path(config.db_path.parent),
            _snapshot([_window(percent=40.0)], age=4000.0),
        )

        result = await self._usage(config)

        assert "**Claude Code subscription**" in result, "the leak-prone branch never ran"
        assert "_Reading is 1h 06m old._" in result
        assert sentinel not in result
        assert "sk-ant" not in result
        assert "403" not in result
        assert sent_headers, "the fetch never ran, so this proves nothing"
        assert sentinel in sent_headers[0]["Authorization"], (
            "the token belongs in the header and nowhere else"
        )

    # -- registration --------------------------------------------------------

    async def test_limits_is_a_hidden_alias_for_usage(self, make_config, monkeypatch):
        assert "usage" in COMMANDS
        assert _COMMAND_ALIASES["limits"] == "usage"
        assert "limits" not in COMMANDS

        config, _ = self._admin_config(make_config, monkeypatch)
        with db.get_db(config.db_path) as conn:
            result = await dispatch(
                config, "bob", "room1", "!limits",
                surface="web", conn=conn, registry=_FakeRegistry(None),
            )
        assert result.handled
        assert "**Claude Code subscription**" in (result.text or "")


# =============================================================================
# Per-room brain selection — `!brain`, and every model writer it binds
# =============================================================================


def _brain_config(*selectable, kind="claude_code", fallback=""):
    """A BrainConfig whose `room_selectable` names the kinds a test needs.

    `istota.config.BrainConfig`, not the protocol-side stub this module already
    imports under the same name for the alias parsers.
    """
    from istota.config import BrainConfig as RealBrainConfig

    return RealBrainConfig(
        kind=kind, fallback=fallback, room_selectable=list(selectable),
    )


def _room(conn, token="room1", user_id="alice", surface="talk"):
    db.register_room(conn, token, user_id, origin="web")
    db.add_room_binding(conn, token, surface, token)


@contextmanager
def _brain_room(make_config, db_path, *selectable, fallback="", brain=None,
                register=True):
    """A config offering `selectable`, and a connection holding `room1`
    (registered unless told otherwise, pinned to `brain` when given)."""
    config = make_config()
    config.brain = _brain_config(*selectable, fallback=fallback)
    with db.get_db(db_path) as conn:
        if register:
            _room(conn)
        if brain:
            db.set_room_brain(conn, "room1", brain)
        yield config, conn


def _room1(conn):
    return db.get_room(conn, "room1")


class TestRoomModelWriterFollowsTheRoomsBrain:
    """D5 Rule 2, the row the spec calls the worst of the five.

    `!room model` writes the *standing* default, so an id resolved in the wrong
    namespace is not one bad turn — it is every turn in the room until somebody
    notices. The assertion is deliberately against the stored `rooms.model`
    value rather than the reply text: a handler that says the right thing and
    writes the wrong value is exactly the failure being guarded, and a reply
    assertion passes against it.
    """

    async def test_native_room_never_stores_an_anthropic_id(self, make_config, db_path):
        with _brain_room(make_config, db_path, "native", brain="native") as (config, conn):
            await cmd_room(_ctx(config, conn, args="model sonnet"))
            room = _room1(conn)
        # `sonnet` is an anthropic shortcut; the native brain resolves no such
        # alias, so the only correct outcomes are "unchanged" or an
        # openai_compat id. What must never be here is `claude-sonnet-5`.
        assert room.model != SONNET
        assert room.model is None

    async def test_an_unpinned_room_still_resolves_in_the_deployment_namespace(
        self, make_config, db_path,
    ):
        """The converse, so the test above cannot pass against a writer that
        simply stopped resolving anything."""
        with _brain_room(make_config, db_path, "native") as (config, conn):
            await cmd_room(_ctx(config, conn, args="model sonnet"))
            assert _room1(conn).model == SONNET


class TestCmdBrain:
    """`!brain` — show, set, and clear a room's standing brain.

    Every assertion about a *write* reads the stored row rather than the reply:
    the reply is what the handler says it did, and the two are exactly what can
    disagree.
    """

    async def test_show_is_not_gated(self, make_config, db_path):
        """Reading is allowed to anyone. A room is shared, and every member is
        entitled to know which brain their turns run under — only writing
        chooses an isolation posture for somebody else."""
        with _brain_room(make_config, db_path, "native") as (config, conn):
            config.admin_users = {"someone-else"}
            out = await cmd_brain(_ctx(config, conn, user_id="alice"))
        assert "claude_code" in out
        assert "admin" not in out.lower()

    async def test_show_names_the_lane_rule_it_would_otherwise_take(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native") as (config, conn):
            config.brain.source_type_overrides = {"talk": "native"}
            out = await cmd_brain(_ctx(config, conn))
        assert "native" in out

    async def test_set_writes_the_column(self, make_config, db_path):
        with _brain_room(make_config, db_path, "native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="native"))
            assert _room1(conn).brain == "native"
        assert "native" in out

    async def test_default_clears_the_column(self, make_config, db_path):
        with _brain_room(make_config, db_path, "native", brain="native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="default"))
            assert _room1(conn).brain is None
        assert "claude_code" in out

    async def test_unknown_kind_is_refused_and_writes_nothing(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="gpt5"))
            assert _room1(conn).brain is None
        assert "gpt5" in out

    async def test_a_buildable_kind_the_operator_did_not_list_is_refused(
        self, make_config, db_path,
    ):
        """A separate branch from the unknown one: `tmux_claude` is a kind
        `make_brain` builds, so only the allowlist stands between it and the
        room."""
        # tmux deliberately absent from the allowlist
        with _brain_room(make_config, db_path, "native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="tmux_claude"))
            assert _room1(conn).brain is None
        assert "native" in out  # names what *is* on offer

    async def test_the_feature_is_off_until_the_operator_names_a_kind(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path) as (config, conn):  # room_selectable = []
            out = await cmd_brain(_ctx(config, conn, args="native"))
            assert _room1(conn).brain is None
        assert "room_selectable" in out
        # Names no kinds: with the list empty there is nothing to offer, and a
        # message listing the buildable kinds would read as a menu.
        assert "`native`" not in out

    async def test_a_non_admin_cannot_set_or_clear(self, make_config, db_path):
        with _brain_room(make_config, db_path, "native", brain="native") as (config, conn):
            config.admin_users = {"alice"}
            set_out = await cmd_brain(
                _ctx(config, conn, user_id="bob", args="claude_code")
            )
            clear_out = await cmd_brain(
                _ctx(config, conn, user_id="bob", args="default")
            )
            assert _room1(conn).brain == "native"
        assert "admin" in set_out.lower()
        assert "admin" in clear_out.lower()

    async def test_an_unregistered_room_is_told_to_send_a_message_first(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native", register=False) as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="native"))
        assert "registered" in out.lower()

    async def test_show_names_a_pin_the_operator_has_since_dropped(
        self, make_config, db_path,
    ):
        """The column keeps its value when `room_selectable` shortens — the
        operator may restore the list — so the show form has to say the room is
        running something other than what it is set to."""
        # nothing selectable any more
        with _brain_room(make_config, db_path, brain="native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn))
        assert "native" in out
        assert "claude_code" in out
        assert "ignor" in out.lower()


class TestBrainPinTurnsFailoverOff:
    """D12, as it reaches the user. The behaviour itself is
    `resolve_brain_kind`'s (stage 1); what is asserted here is that both
    `!brain` forms say so, because the cost is only acceptable while it is
    visible at the moment of choosing."""

    async def test_the_set_reply_names_the_consequence(self, make_config, db_path):
        with _brain_room(
            make_config, db_path, "native", fallback="claude_code",
        ) as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="native"))
        assert "ailover" in out
        assert "!brain default" in out

    async def test_an_unpinned_room_is_told_what_it_falls_back_to(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native", fallback="native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn))
        assert "Failover: `native`" in out

    async def test_a_pin_the_operator_dropped_keeps_its_failover(
        self, make_config, db_path,
    ):
        """The converse that stops the failover line tracking the *column*
        rather than the admission. A room pinned to the deployment's own kind
        runs that kind either way, so nothing but the allowlist distinguishes
        the two states."""
        # nothing selectable
        with _brain_room(
            make_config, db_path, fallback="native", brain="claude_code",
        ) as (config, conn):
            out = await cmd_brain(_ctx(config, conn))
        assert "Failover: `native`" in out


class TestBrainChangeClearsACrossNamespacePin:
    """D5 Rule 1, both halves.

    The clearing half alone is vacuous: it passes against a handler that always
    clears. The preserving half is what makes it an assertion about namespaces.
    """

    async def test_a_namespace_change_drops_the_model_pin(self, make_config, db_path):
        with _brain_room(make_config, db_path, "claude_code", "native") as (config, conn):
            db.set_room_model_effort(conn, "room1", OPUS, "high")
            out = await cmd_brain(_ctx(config, conn, args="native"))
            room = _room1(conn)
        assert room.brain == "native"
        assert room.model is None
        assert room.effort is None
        assert OPUS in out  # says what it dropped

    async def test_a_move_inside_one_namespace_keeps_the_pin(
        self, make_config, db_path,
    ):
        """`claude_code` and `tmux_claude` share `model_namespace ==
        "anthropic"`, so the same id runs under both and there is nothing to
        clear."""
        with _brain_room(
            make_config, db_path, "claude_code", "tmux_claude", brain="claude_code",
        ) as (config, conn):
            db.set_room_model_effort(conn, "room1", OPUS, "high")
            out = await cmd_brain(_ctx(config, conn, args="tmux_claude"))
            room = _room1(conn)
        assert room.brain == "tmux_claude"
        assert room.model == OPUS
        assert room.effort == "high"
        assert OPUS not in out

    async def test_clearing_the_pin_back_to_the_inherited_brain_also_drops_it(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native", brain="native") as (config, conn):
            db.set_room_model_effort(conn, "room1", "some-openai-compat-slug", None)
            await cmd_brain(_ctx(config, conn, args="default"))
            room = _room1(conn)
        assert room.brain is None
        assert room.model is None

    async def test_a_room_with_no_pin_reports_nothing_cleared(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="native"))
        assert "cleared" not in out.lower()


class TestModelSurfacesFollowTheRoomsBrain:
    """The rest of D5 Rule 2: everything that *offers* a model name, and the
    end-to-end path through `!brain` that the writer test above reaches by
    setting the column directly."""

    async def test_brain_then_room_model_never_leaves_an_anthropic_id(
        self, make_config, db_path,
    ):
        with _brain_room(make_config, db_path, "native") as (config, conn):
            await cmd_brain(_ctx(config, conn, args="native"))
            await cmd_room(_ctx(config, conn, args="model sonnet"))
            room = _room1(conn)
        assert room.model != SONNET
        assert room.model is None

    # `cmd_help` is a surface that offers model names, so D5 Rule 2 binds it
    # exactly as it binds the writers. It lists alias *names*, and the two
    # namespaces have different ones: native offers the three portable tiers,
    # anthropic those plus its own provider shortcuts. `opus` is the
    # discriminator. Each converse is there so the assertion is about the room
    # rather than about the handler having stopped listing anything.
    @pytest.mark.parametrize("handler,pinned_has,pinned_lacks,inherited_has", [
        (cmd_models, "some-endpoint/model-a", OPUS, OPUS),
        (cmd_help, "`smart`", "`opus`", "`opus`"),
    ], ids=["models", "help"])
    async def test_the_offered_names_follow_the_rooms_namespace(
        self, make_config, db_path, handler, pinned_has, pinned_lacks, inherited_has,
    ):
        with _brain_room(make_config, db_path, "native", brain="native") as (config, conn):
            config.brain.native.model = "some-endpoint/model-a"
            pinned = await handler(_ctx(config, conn))
            db.set_room_brain(conn, "room1", None)
            inherited = await handler(_ctx(config, conn))
        assert pinned_has in pinned
        assert pinned_lacks not in pinned
        assert inherited_has in inherited

    async def test_room_show_names_the_brain(self, make_config, db_path):
        with _brain_room(make_config, db_path, "native", brain="native") as (config, conn):
            out = await cmd_room(_ctx(config, conn))
        assert "Brain: `native`" in out


class TestBrainForRoom:
    def test_an_unreadable_room_falls_through_to_the_source_type_layer(
        self, make_config,
    ):
        """The pre-feature answer, which is the whole never-raise contract: this
        runs inside the Talk poll's inner loop and on the web send path."""
        config = make_config()
        config.brain = _brain_config("native")
        config.brain.source_type_overrides = {"talk": "native"}

        class _Exploding:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("no such table: rooms")

        resolved = brain_for_room(config, _Exploding(), "room1", "talk")
        assert resolved.kind == "native"

    def test_a_room_with_no_pin_returns_the_config_object_itself(
        self, make_config, db_path,
    ):
        """`resolve_brain_kind` returns the same object when nothing applies,
        which is what the executor's cheap no-routing check reads."""
        with _brain_room(make_config, db_path, "native") as (config, conn):
            assert brain_for_room(config, conn, "room1", "talk") is config.brain


class TestTheReviewFindings:
    """Cases added from the stage's own review. Each one is a defect the first
    cut had, kept as a test rather than as a comment."""

    async def test_a_pin_the_operator_dropped_still_loses_its_foreign_model(
        self, make_config, db_path,
    ):
        """The outgoing namespace comes from the outgoing *kind*, not from a
        routing pass that may refuse it.

        Room pinned `native` with an openai_compat id, operator drops `native`
        from the allowlist, admin moves the room to `claude_code`. Routing the
        outgoing kind through `resolve_brain_kind` answers with the deployment
        default's namespace — anthropic against anthropic — so the foreign id
        survives as the room's standing default under a brain that cannot run
        it, silently.
        """
        # native no longer offered
        with _brain_room(
            make_config, db_path, "claude_code", brain="native",
        ) as (config, conn):
            db.set_room_model_effort(conn, "room1", "endpoint/some-slug", "high")
            out = await cmd_brain(_ctx(config, conn, args="claude_code"))
            room = _room1(conn)
        assert room.brain == "claude_code"
        assert room.model is None
        assert "endpoint/some-slug" in out

    async def test_default_clears_even_with_the_allowlist_emptied(
        self, make_config, db_path,
    ):
        """Emptying `[brain] room_selectable` is the documented way to switch
        the feature off, and it is therefore exactly how a room ends up holding
        a pin nothing honours. Clearing is a narrowing, so it needs no entry."""
        # nothing selectable
        with _brain_room(make_config, db_path, brain="native") as (config, conn):
            out = await cmd_brain(_ctx(config, conn, args="default"))
            assert _room1(conn).brain is None
        assert "room_selectable" not in out

    async def test_the_reply_names_the_effort_it_dropped(self, make_config, db_path):
        """`set_room_model_effort` moves the pair, so a reply naming only the
        model under-reports what it just did."""
        with _brain_room(make_config, db_path, "native") as (config, conn):
            db.set_room_model_effort(conn, "room1", OPUS, "high")
            out = await cmd_brain(_ctx(config, conn, args="native"))
            assert _room1(conn).effort is None
        assert "high" in out

    async def test_a_bare_effort_survives_a_brain_change(self, make_config, db_path):
        """An effort level with no model pin is a semantic rung every brain
        reads, so nothing namespaced is lost and nothing is cleared."""
        with _brain_room(make_config, db_path, "native") as (config, conn):
            db.set_room_effort(conn, "room1", "high")
            out = await cmd_brain(_ctx(config, conn, args="native"))
            room = _room1(conn)
        assert room.brain == "native"
        assert room.effort == "high"
        assert "cleared" not in out.lower()


class TestBrainForRoomNeverRaises:
    def test_a_non_string_column_value_does_not_raise(self, make_config, db_path):
        """`rooms.brain` is `TEXT` with no `CHECK` and SQLite is dynamically
        typed, so the column can hold a number. `resolve_brain_kind` calls
        `.strip()` on it, which is why it has to be inside the guard rather than
        after it — the Talk poll's per-message loop runs inside the batch
        transaction with nothing above it to catch a raise, so one bad row would
        drop every remaining conversation's messages and roll the cursors back,
        every cycle.
        """
        with _brain_room(make_config, db_path, "native") as (config, conn):
            conn.execute("UPDATE rooms SET brain = 5 WHERE token = 'room1'")
            assert brain_for_room(config, conn, "room1", "talk").kind == "claude_code"


class TestIsModelPrefix:
    """The brain-free half of the `!model` grammar.

    It exists so the Talk poll can decide whether it needs a brain at all — the
    brain is the *room's* now, and building one constructs a provider client
    nothing closes, so doing it per message rather than per `!model` message is
    a cost paid on every turn in a native-pinned room.
    """

    def test_it_agrees_with_the_parser(self, brain):
        for content in (
            "!model opus do the thing", "!model", "  !MODEL opus x",
            "hello", "!models", "!room model opus", "", "!modelfoo",
        ):
            assert is_model_prefix(content) is (
                parse_model_prefix(content, brain) is not None
            ), content


class TestResolveRoomNameOnWeb:
    """`!export` titles and `!search` result headings name a room through this.

    A web room's name lives in two places: `rooms.name`, which every rename
    writes, and the per-user `web_chat_rooms` handle, which is a mint-time
    snapshot nothing refreshes. Reading the handle first showed the stale
    placeholder (ISSUE-474).
    """

    async def test_it_prefers_the_registry_name(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "talk-1", "alice", origin="talk", name=None)
            db.ensure_web_chat_handle(conn, "alice", "talk-1", "Talk room")
            db.rename_room(conn, "talk-1", "team")
            ctx = _ctx(config, conn, conversation_token="talk-1", surface="web")
            assert await resolve_room_name(ctx, "talk-1") == "team"

    async def test_it_falls_back_to_the_handle_then_the_token(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "talk-1", "alice", origin="talk", name=None)
            db.ensure_web_chat_handle(conn, "alice", "talk-1", "Talk room")
            ctx = _ctx(config, conn, conversation_token="talk-1", surface="web")
            assert await resolve_room_name(ctx, "talk-1") == "Talk room"
            assert await resolve_room_name(ctx, "unknown-1") == "unknown-1"
