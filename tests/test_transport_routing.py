"""Tests for transport.routing — descriptor parsing + delivery-plan resolution.

The parity tests assert resolve_delivery_plan reproduces the hardcoded
output_target fan-out that process_one_task did inline (the Stage-1
behavior-preserving contract).
"""

from __future__ import annotations

import pytest

from istota import db
from istota.config import (
    BriefingConfig,
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport.routing import (
    Destination,
    parse_output_target,
    plan_has_surface,
    resolve_delivery_plan,
)


class TestParseOutputTarget:
    def test_none_empty_and_none_literal(self):
        assert parse_output_target(None) == []
        assert parse_output_target("") == []
        assert parse_output_target("   ") == []
        assert parse_output_target("none") == []
        assert parse_output_target("NONE") == []

    def test_simple_surfaces(self):
        assert parse_output_target("talk") == [Destination("talk")]
        assert parse_output_target("email") == [Destination("email")]
        assert parse_output_target("ntfy") == [Destination("ntfy")]
        assert parse_output_target("istota_file") == [Destination("istota_file")]
        assert parse_output_target("stream") == [Destination("stream")]

    def test_legacy_both_alias(self):
        assert parse_output_target("both") == [
            Destination("talk"), Destination("email"),
        ]

    def test_legacy_all_alias(self):
        assert parse_output_target("all") == [
            Destination("talk"), Destination("email"), Destination("ntfy"),
        ]

    def test_explicit_channel(self):
        assert parse_output_target("talk:tok") == [Destination("talk", "tok")]
        assert parse_output_target("email:a@b.com") == [
            Destination("email", "a@b.com"),
        ]

    def test_matrix_room_with_colons(self):
        # Channel keeps internal colons (Matrix !room:homeserver).
        assert parse_output_target("matrix:!r:hs.example") == [
            Destination("matrix", "!r:hs.example"),
        ]

    def test_comma_separated(self):
        assert parse_output_target("talk,email") == [
            Destination("talk"), Destination("email"),
        ]
        assert parse_output_target("talk:a, email ") == [
            Destination("talk", "a"), Destination("email"),
        ]

    def test_surface_lowercased_channel_preserved(self):
        assert parse_output_target("TALK:RoomXYZ") == [
            Destination("talk", "RoomXYZ"),
        ]

    def test_trailing_colon_is_no_channel(self):
        assert parse_output_target("talk:") == [Destination("talk")]

    def test_exact_duplicates_collapsed(self):
        assert parse_output_target("talk,talk") == [Destination("talk")]
        # both expands to talk,email; appending talk is a dup of the expansion
        assert parse_output_target("both,talk") == [
            Destination("talk"), Destination("email"),
        ]

    def test_same_surface_different_channels_kept(self):
        assert parse_output_target("talk:a,talk:b") == [
            Destination("talk", "a"), Destination("talk", "b"),
        ]

    def test_skips_empty_tokens(self):
        assert parse_output_target("talk,,email,") == [
            Destination("talk"), Destination("email"),
        ]


class TestPlanHasSurface:
    def test_has_and_missing(self):
        plan = [Destination("talk", "x", "push"), Destination("email", None, "push")]
        assert plan_has_surface(plan, "talk")
        assert plan_has_surface(plan, "email")
        assert not plan_has_surface(plan, "ntfy")
        assert not plan_has_surface([], "talk")


def _config(tmp_path, **user_kwargs):
    users = {}
    if user_kwargs.pop("_with_user", True):
        users = {"alice": UserConfig(**user_kwargs)}
    return Config(
        db_path=tmp_path / "ignore.db",
        nextcloud=NextcloudConfig(url="https://nc.example"),
        talk=TalkConfig(),
        email=EmailConfig(),
        scheduler=SchedulerConfig(),
        temp_dir=tmp_path / "temp",
        users=users,
    )


def _task(**kwargs):
    defaults = dict(
        id=1, status="pending", source_type="talk", user_id="alice",
        prompt="x", conversation_token=None, priority=5,
        attempt_count=0, max_attempts=3,
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


class TestResolveDeliveryPlanParity:
    """The Stage-1 parity table: every (source_type, output_target) pair must
    reproduce today's delivery."""

    def test_talk_default_reply_origin(self, tmp_path):
        config = _config(tmp_path, alerts_channel="alerts")
        task = _task(source_type="talk", conversation_token="room1")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("talk", "room1", "push")]

    def test_email_default(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="email", conversation_token="r0om4Bcd")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("email", None, "push")]

    def test_briefing_default_talk(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="briefing", conversation_token="briefroom")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("talk", "briefroom", "push")]

    def test_scheduled_no_target_empty(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="scheduled", conversation_token=None,
                     output_target=None)
        assert resolve_delivery_plan(config, task, None) == []

    def test_subtask_with_talk_target(self, tmp_path):
        # Subtasks set output_target="talk" at creation when they have a token.
        config = _config(tmp_path)
        task = _task(source_type="subtask", conversation_token="parent",
                     output_target="talk")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("talk", "parent", "push")]

    def test_heartbeat_no_plan(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="heartbeat", conversation_token=None,
                     output_target=None)
        assert resolve_delivery_plan(config, task, None) == []

    def test_cli_no_plan(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="cli", conversation_token=None,
                     output_target=None)
        assert resolve_delivery_plan(config, task, None) == []

    def test_istota_file_default_inferred(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="istota_file", conversation_token=None,
                     output_target=None)
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("istota_file", None, "push")]

    def test_both_expands(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="talk", conversation_token="room1",
                     output_target="both")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [
            Destination("talk", "room1", "push"),
            Destination("email", None, "push"),
        ]

    def test_all_expands(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="talk", conversation_token="room1",
                     output_target="all")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [
            Destination("talk", "room1", "push"),
            Destination("email", None, "push"),
            Destination("ntfy", None, "push"),
        ]

    def test_explicit_ntfy(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="scheduled", conversation_token=None,
                     output_target="ntfy")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("ntfy", None, "push")]

    def test_email_synthetic_token_talk_fallback(self, tmp_path):
        # Email task explicitly routed to talk with a synthetic conversation
        # token resolves through the synthetic-token fallback to alerts.
        config = _config(tmp_path, alerts_channel="alerts")
        task = _task(source_type="email", conversation_token="a1b2c3d4e5f60718",
                     output_target="talk")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("talk", "alerts", "push")]


class TestResolveDeliveryPlanEdgeCases:
    def test_none_literal_is_empty_push_plan(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="scheduled", output_target="none")
        assert resolve_delivery_plan(config, task, None) == []

    def test_stream_kept_as_stream(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="repl", output_target="stream")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("stream", "stream", "stream")]

    def test_repl_default_stream(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="repl", output_target=None)
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("stream", "stream", "stream")]

    def test_unknown_surface_dropped(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="talk", conversation_token="room1",
                     output_target="talk,bogus")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("talk", "room1", "push")]

    def test_interactive_empty_plan_falls_back_to_reply_origin(self, tmp_path):
        # talk task routed only to an unknown surface → empty → fallback to talk.
        config = _config(tmp_path, alerts_channel="alerts")
        task = _task(source_type="talk", conversation_token="room1",
                     output_target="bogus")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("talk", "room1", "push")]

    def test_email_interactive_fallback(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="email", conversation_token="r0om4Bcd",
                     output_target="bogus")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [Destination("email", None, "push")]

    def test_talk_dropped_when_no_channel_resolves(self, tmp_path):
        # No nextcloud url, no conversation token, no resolvable channel.
        config = Config(
            db_path=tmp_path / "ignore.db",
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(), email=EmailConfig(),
            scheduler=SchedulerConfig(), temp_dir=tmp_path / "temp",
            users={"alice": UserConfig()},
        )
        task = _task(source_type="scheduled", conversation_token=None,
                     output_target="talk")
        assert resolve_delivery_plan(config, task, None) == []

    def test_same_surface_multiple_channels(self, tmp_path):
        config = _config(tmp_path)
        task = _task(source_type="talk", conversation_token="room1",
                     output_target="talk:a,talk:b")
        plan = resolve_delivery_plan(config, task, None)
        assert plan == [
            Destination("talk", "a", "push"),
            Destination("talk", "b", "push"),
        ]
