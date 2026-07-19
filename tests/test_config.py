"""Configuration loading for istota.config module."""

from pathlib import Path

import pytest

from istota.config import (
    BriefingConfig,
    ChannelSleepCycleConfig,
    Config,
    ConversationConfig,
    DeveloperConfig,
    EmailConfig,
    LoggingConfig,
    MemorySearchConfig,
    NextcloudConfig,
    ResourceConfig,
    SchedulerConfig,
    SiteConfig,
    SleepCycleConfig,
    TalkConfig,
    UserConfig,
    load_admin_users,
    load_config,
)


class TestConfigDefaults:
    def test_default_db_path(self):
        cfg = Config()
        assert cfg.db_path == Path("data/istota.db")

    def test_default_rclone_remote(self):
        cfg = Config()
        assert cfg.rclone_remote == "nextcloud"

    def test_default_nextcloud_config(self):
        cfg = Config()
        assert cfg.nextcloud.url == ""
        assert cfg.nextcloud.username == ""
        assert cfg.nextcloud.app_password == ""

    def test_default_talk_config(self):
        cfg = Config()
        assert cfg.talk.enabled is True
        assert cfg.talk.bot_username == "istota"

    def test_default_email_config(self):
        cfg = Config()
        assert cfg.email.enabled is False
        assert cfg.email.imap_host == ""
        assert cfg.email.imap_port == 993
        assert cfg.email.smtp_port == 587
        assert cfg.email.poll_folder == "INBOX"

    def test_default_conversation_config(self):
        cfg = Config()
        assert cfg.conversation.enabled is True
        assert cfg.conversation.lookback_count == 25
        assert cfg.conversation.selection_timeout == 30.0
        assert cfg.conversation.skip_selection_threshold == 3

    def test_default_scheduler_config(self):
        cfg = Config()
        assert cfg.scheduler.poll_interval == 2
        assert cfg.scheduler.dispatch_interval == 0.5
        assert cfg.scheduler.email_poll_interval == 60
        assert cfg.scheduler.talk_poll_interval == 10
        assert cfg.scheduler.talk_poll_timeout == 30
        assert cfg.scheduler.progress_updates is True
        assert cfg.scheduler.task_timeout_minutes == 30
        assert cfg.scheduler.task_retention_days == 7
        assert cfg.scheduler.worker_idle_timeout == 10
        assert cfg.scheduler.worker_idle_poll_interval == 0.5

    def test_default_logging_config(self):
        cfg = Config()
        assert cfg.logging.level == "INFO"
        assert cfg.logging.output == "console"
        assert cfg.logging.file == ""
        assert cfg.logging.rotate is True
        assert cfg.logging.max_size_mb == 10
        assert cfg.logging.backup_count == 5

    def test_default_no_users(self):
        cfg = Config()
        assert cfg.users == {}

    def test_use_mount_false_by_default(self):
        cfg = Config()
        assert cfg.nextcloud_mount_path is None
        assert cfg.use_mount is False

    def test_default_bot_name(self):
        cfg = Config()
        assert cfg.bot_name == "Istota"
        assert cfg.bot_dir_name == "istota"

    def test_bot_dir_name_with_spaces(self):
        cfg = Config(bot_name="Mister Jones")
        assert cfg.bot_dir_name == "mister_jones"

    def test_bot_dir_name_with_special_chars(self):
        cfg = Config(bot_name="My Bot!")
        assert cfg.bot_dir_name == "my_bot"

    def test_bot_dir_name_fallback(self):
        cfg = Config(bot_name="!!!")
        assert cfg.bot_dir_name == "istota"

    def test_bot_dir_name_strips_unicode(self):
        cfg = Config(bot_name="Café Bot")
        assert cfg.bot_dir_name == "caf_bot"

    def test_bot_dir_name_preserves_hyphens(self):
        cfg = Config(bot_name="My-Bot 2")
        assert cfg.bot_dir_name == "my-bot_2"

    def test_default_custom_system_prompt(self):
        cfg = Config()
        assert cfg.custom_system_prompt is False


class TestConfigLoading:
    def test_load_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.db_path == Path("data/istota.db")
        assert cfg.users == {}

    def test_load_minimal_config(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "mydb.sqlite"\n')
        cfg = load_config(p)
        assert cfg.db_path == Path("mydb.sqlite")

    def test_load_module_data_dir_and_backup_knobs(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            'module_data_dir = "/srv/local/modules"\n\n'
            "[scheduler]\n"
            "main_loop_read_timeout_ms = 1500\n"
            "db_backup_enabled = false\n"
            "db_backup_interval = 43200\n"
            'db_backup_dir = "/srv/backups"\n'
        )
        cfg = load_config(p)
        assert cfg.module_data_dir == Path("/srv/local/modules")
        assert cfg.scheduler.main_loop_read_timeout_ms == 1500
        assert cfg.scheduler.db_backup_enabled is False
        assert cfg.scheduler.db_backup_interval == 43200
        assert cfg.scheduler.db_backup_dir == "/srv/backups"

    def test_module_data_dir_defaults_none(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "d.sqlite"\n')
        cfg = load_config(p)
        assert cfg.module_data_dir is None
        # derives alongside the framework DB
        assert cfg.module_db_path("alice", "feeds") == (
            Path("d.sqlite").resolve().parent / "modules" / "alice" / "feeds.db"
        )

    def test_stale_skills_block_warns_not_fails(self, tmp_path, caplog):
        # The [skills] section is obsolete (single-axis selection has no knobs).
        # A stale block must keep loading, with a warning — never raise.
        import logging

        p = tmp_path / "config.toml"
        p.write_text(
            'db_path = "test.db"\n\n'
            "[skills]\n"
            "progressive_disclosure = false\n"
            "auto_lazy_threshold_chars = 4000\n"
        )
        with caplog.at_level(logging.WARNING):
            cfg = load_config(p)
        assert cfg.db_path == Path("test.db")
        assert not hasattr(cfg, "skills")
        assert any("[skills] block" in r.message for r in caplog.records)

    def test_load_custom_system_prompt_true(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('custom_system_prompt = true\n')
        cfg = load_config(p)
        assert cfg.custom_system_prompt is True

    def test_load_custom_system_prompt_default(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "test.db"\n')
        cfg = load_config(p)
        assert cfg.custom_system_prompt is False

    def test_load_nextcloud_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[nextcloud]\n'
            'url = "https://cloud.example.com"\n'
            'username = "bot"\n'
            'app_password = "secret123"\n'
        )
        cfg = load_config(p)
        assert cfg.nextcloud.url == "https://cloud.example.com"
        assert cfg.nextcloud.username == "bot"
        assert cfg.nextcloud.app_password == "secret123"

    def test_load_talk_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[talk]\n'
            'enabled = false\n'
            'bot_username = "mybot"\n'
        )
        cfg = load_config(p)
        assert cfg.talk.enabled is False
        assert cfg.talk.bot_username == "mybot"

    def test_load_email_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[email]\n'
            'enabled = true\n'
            'imap_host = "imap.example.com"\n'
            'imap_port = 993\n'
            'imap_user = "user@example.com"\n'
            'imap_password = "pass"\n'
            'smtp_host = "smtp.example.com"\n'
            'smtp_port = 465\n'
            'smtp_user = "smtpuser"\n'
            'smtp_password = "smtppass"\n'
            'poll_folder = "INBOX"\n'
            'bot_email = "bot@example.com"\n'
        )
        cfg = load_config(p)
        assert cfg.email.enabled is True
        assert cfg.email.imap_host == "imap.example.com"
        assert cfg.email.smtp_host == "smtp.example.com"
        assert cfg.email.smtp_port == 465
        assert cfg.email.smtp_user == "smtpuser"
        assert cfg.email.smtp_password == "smtppass"
        assert cfg.email.bot_email == "bot@example.com"

    def test_load_conversation_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[conversation]\n'
            'enabled = false\n'
            'lookback_count = 20\n'
            'selection_timeout = 15.0\n'
            'skip_selection_threshold = 5\n'
        )
        cfg = load_config(p)
        assert cfg.conversation.enabled is False
        assert cfg.conversation.lookback_count == 20
        assert cfg.conversation.selection_timeout == 15.0
        assert cfg.conversation.skip_selection_threshold == 5

    def test_load_scheduler_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'poll_interval = 10\n'
            'dispatch_interval = 0.25\n'
            'email_poll_interval = 120\n'
            'talk_poll_interval = 5\n'
            'progress_updates = false\n'
            'event_log_enabled = false\n'
            'task_timeout_minutes = 60\n'
            'confirmation_timeout_minutes = 60\n'
            'task_retention_days = 14\n'
            'email_retention_days = 30\n'
            'worker_idle_timeout = 20\n'
            'worker_idle_poll_interval = 1.5\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.poll_interval == 10
        assert cfg.scheduler.dispatch_interval == 0.25
        assert cfg.scheduler.email_poll_interval == 120
        assert cfg.scheduler.talk_poll_interval == 5
        assert cfg.scheduler.progress_updates is False
        assert cfg.scheduler.event_log_enabled is False
        assert cfg.scheduler.task_timeout_minutes == 60
        assert cfg.scheduler.confirmation_timeout_minutes == 60
        assert cfg.scheduler.task_retention_days == 14
        assert cfg.scheduler.email_retention_days == 30
        assert cfg.scheduler.worker_idle_timeout == 20
        assert cfg.scheduler.worker_idle_poll_interval == 1.5

    def test_load_logging_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[logging]\n'
            'level = "DEBUG"\n'
            'output = "both"\n'
            'file = "/var/log/istota.log"\n'
            'rotate = false\n'
            'max_size_mb = 50\n'
            'backup_count = 10\n'
        )
        cfg = load_config(p)
        assert cfg.logging.level == "DEBUG"
        assert cfg.logging.output == "both"
        assert cfg.logging.file == "/var/log/istota.log"
        assert cfg.logging.rotate is False
        assert cfg.logging.max_size_mb == 50
        assert cfg.logging.backup_count == 10

    def test_load_briefing_defaults_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[briefing_defaults.news]\n'
            'lookback_hours = 12\n'
            'sources = [\n'
            '    { type = "domain", value = "semafor.com" },\n'
            '    { type = "email", value = "briefing@nytimes.com" },\n'
            ']\n'
            '\n'
            '[briefing_defaults.headlines]\n'
            'sources = ["ap", "reuters", "guardian"]\n'
            '\n'
            '[briefing_defaults.markets]\n'
            'futures = ["ES=F", "NQ=F"]\n'
        )
        cfg = load_config(p)
        assert cfg.briefing_defaults.news.get("lookback_hours") == 12
        assert len(cfg.briefing_defaults.news.get("sources", [])) == 2
        assert cfg.briefing_defaults.headlines.get("sources") == ["ap", "reuters", "guardian"]
        assert cfg.briefing_defaults.markets.get("futures") == ["ES=F", "NQ=F"]

    def test_load_briefing_defaults_headlines_only(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[briefing_defaults.headlines]\n'
            'sources = ["ap", "guardian"]\n'
        )
        cfg = load_config(p)
        assert cfg.briefing_defaults.headlines.get("sources") == ["ap", "guardian"]
        assert cfg.briefing_defaults.news == {}
        assert cfg.briefing_defaults.markets == {}

    def test_load_users_section(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice Smith"\n'
            'email_addresses = ["alice@example.com", "alice@work.com"]\n'
            'timezone = "America/New_York"\n'
        )
        cfg = load_config(p)
        assert "alice" in cfg.users
        alice = cfg.users["alice"]
        assert alice.display_name == "Alice Smith"
        assert alice.email_addresses == ["alice@example.com", "alice@work.com"]
        assert alice.timezone == "America/New_York"
        assert alice.briefings == []

    def test_load_users_reminders_file_backward_compat(self, tmp_path):
        """Legacy reminders_file string is auto-migrated to a resource."""
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            'reminders_file = "/alice/REMINDERS.md"\n'
        )
        cfg = load_config(p)
        alice = cfg.users["alice"]
        reminder_resources = [r for r in alice.resources if r.type == "reminders_file"]
        assert len(reminder_resources) == 1
        assert reminder_resources[0].path == "/alice/REMINDERS.md"
        assert reminder_resources[0].name == "Reminders"

    def test_load_users_with_briefings(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.bob]\n'
            'display_name = "Bob"\n'
            'timezone = "Europe/Berlin"\n'
            '\n'
            '[[users.bob.briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "room1"\n'
            'output = "both"\n'
            '\n'
            '[users.bob.briefings.components]\n'
            'calendar = true\n'
            'todos = true\n'
            '\n'
            '[[users.bob.briefings]]\n'
            'name = "evening"\n'
            'cron = "0 18 * * *"\n'
        )
        cfg = load_config(p)
        bob = cfg.users["bob"]
        assert len(bob.briefings) == 2
        morning = bob.briefings[0]
        assert morning.name == "morning"
        assert morning.cron == "0 7 * * *"
        assert morning.conversation_token == "room1"
        assert morning.output == "both"
        assert morning.components == {"calendar": True, "todos": True}
        evening = bob.briefings[1]
        assert evening.name == "evening"
        assert evening.cron == "0 18 * * *"
        assert evening.conversation_token == ""
        assert evening.output == "talk"
        assert evening.components == {}

    def test_load_mount_path(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('nextcloud_mount_path = "/srv/mount/nextcloud/content"\n')
        cfg = load_config(p)
        assert cfg.nextcloud_mount_path == Path("/srv/mount/nextcloud/content")

    def test_load_skills_dir(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('skills_dir = "/opt/istota/skills"\n')
        cfg = load_config(p)
        assert cfg.skills_dir == Path("/opt/istota/skills")

    def test_load_security_skill_proxy(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[security]\n'
            'skill_proxy_enabled = true\n'
            'skill_proxy_timeout = 120\n'
        )
        cfg = load_config(p)
        assert cfg.security.skill_proxy_enabled is True
        assert cfg.security.skill_proxy_timeout == 120

    def test_load_security_skill_proxy_defaults(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[security]\nsandbox_enabled = true\n')
        cfg = load_config(p)
        assert cfg.security.skill_proxy_enabled is True
        assert cfg.security.skill_proxy_timeout == 300


class TestConfigMethods:
    def test_find_user_by_email_found(self):
        cfg = Config(users={
            "alice": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_find_user_by_email_case_insensitive(self):
        cfg = Config(users={
            "alice": UserConfig(email_addresses=["Alice@Example.COM"]),
        })
        assert cfg.find_user_by_email("alice@example.com") == "alice"

    def test_find_user_by_email_not_found(self):
        cfg = Config(users={
            "alice": UserConfig(email_addresses=["alice@example.com"]),
        })
        assert cfg.find_user_by_email("bob@example.com") is None

    def test_caldav_url(self):
        cfg = Config(nextcloud=NextcloudConfig(url="https://cloud.example.com"))
        assert cfg.caldav_url == "https://cloud.example.com/remote.php/dav"

    def test_caldav_url_empty(self):
        cfg = Config()
        assert cfg.caldav_url == ""

    def test_get_user_found(self):
        user = UserConfig(display_name="Alice")
        cfg = Config(users={"alice": user})
        assert cfg.get_user("alice") is user

    def test_get_user_not_found(self):
        cfg = Config()
        assert cfg.get_user("nobody") is None

    def test_use_mount_true(self):
        cfg = Config(nextcloud_mount_path=Path("/mnt/nc"))
        assert cfg.use_mount is True


class TestResolveUserTimezone:
    """`Config.resolve_user_timezone` is the single source of truth for a
    user's timezone, preferring the live ``user_profiles`` DB row over the
    in-memory ``UserConfig`` so web-UI edits take effect without a scheduler
    restart (ISSUE-099). Every timezone reader (prompt header, briefings,
    scheduled jobs, Garmin sync, subprocess env) routes through it.
    """

    def _make_config(self, tmp_path, *, user_tz="America/Los_Angeles"):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            users={"alice": UserConfig(timezone=user_tz)},
        )

    def test_db_row_wins_over_stale_in_memory_config(self, tmp_path):
        from istota import user_profiles
        cfg = self._make_config(tmp_path, user_tz="America/Los_Angeles")
        user_profiles.ensure_profile(cfg.db_path, "alice", timezone="Europe/Warsaw")
        assert cfg.resolve_user_timezone("alice") == "Europe/Warsaw"

    def test_falls_back_to_in_memory_when_no_db_row(self, tmp_path):
        cfg = self._make_config(tmp_path, user_tz="America/New_York")
        assert cfg.resolve_user_timezone("alice") == "America/New_York"

    def test_unknown_user_returns_utc(self, tmp_path):
        cfg = self._make_config(tmp_path)
        assert cfg.resolve_user_timezone("nobody") == "UTC"

    def test_no_db_path_uses_in_memory(self, tmp_path):
        cfg = Config(db_path=None, users={"alice": UserConfig(timezone="Asia/Tokyo")})
        assert cfg.resolve_user_timezone("alice") == "Asia/Tokyo"

    def test_does_not_validate_zone_name(self, tmp_path):
        # The Config helper returns the raw string; ZoneInfo validation is
        # the caller's job (so callers control the invalid-zone fallback).
        from istota import user_profiles
        cfg = self._make_config(tmp_path)
        user_profiles.ensure_profile(cfg.db_path, "alice", timezone="Not/AZone")
        assert cfg.resolve_user_timezone("alice") == "Not/AZone"

    def test_accepts_reused_connection(self, tmp_path):
        from istota import db, user_profiles
        cfg = self._make_config(tmp_path, user_tz="America/Los_Angeles")
        user_profiles.ensure_profile(cfg.db_path, "alice", timezone="Europe/Warsaw")
        with db.get_db(cfg.db_path) as conn:
            assert cfg.resolve_user_timezone("alice", conn=conn) == "Europe/Warsaw"


class TestEmailReplyRouting:
    def test_default_when_unset(self):
        cfg = Config(users={"stefan": UserConfig()})
        assert cfg.email_reply_routing_for("stefan") == "origin+thread"

    def test_default_for_unknown_user(self):
        cfg = Config()
        assert cfg.email_reply_routing_for("nobody") == "origin+thread"

    def test_valid_values_pass_through(self):
        for val in ("origin+thread", "origin", "thread"):
            cfg = Config(users={"stefan": UserConfig(email_reply_routing=val)})
            assert cfg.email_reply_routing_for("stefan") == val

    def test_invalid_value_falls_back(self):
        cfg = Config(users={"stefan": UserConfig(email_reply_routing="bogus")})
        assert cfg.email_reply_routing_for("stefan") == "origin+thread"


class TestTrustedEmailSenders:
    def test_own_email_always_trusted(self):
        cfg = Config(users={
            "stefan": UserConfig(email_addresses=["stefan@cynium.com"]),
        })
        assert cfg.is_trusted_email_sender("stefan", "stefan@cynium.com") is True

    def test_own_email_case_insensitive(self):
        cfg = Config(users={
            "stefan": UserConfig(email_addresses=["Stefan@Cynium.COM"]),
        })
        assert cfg.is_trusted_email_sender("stefan", "stefan@cynium.com") is True

    def test_exact_match(self):
        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=["alice@example.com"]),
        })
        assert cfg.is_trusted_email_sender("stefan", "Alice@Example.com") is True

    def test_domain_wildcard(self):
        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=["*@corp.com"]),
        })
        assert cfg.is_trusted_email_sender("stefan", "anyone@corp.com") is True
        assert cfg.is_trusted_email_sender("stefan", "anyone@sub.corp.com") is False

    def test_subdomain_wildcard(self):
        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=["*@*.corp.com"]),
        })
        assert cfg.is_trusted_email_sender("stefan", "x@sub.corp.com") is True
        assert cfg.is_trusted_email_sender("stefan", "x@corp.com") is False

    def test_no_match_returns_false(self):
        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=[]),
        })
        assert cfg.is_trusted_email_sender("stefan", "stranger@evil.com") is False

    def test_unknown_user_returns_false(self):
        cfg = Config(users={})
        assert cfg.is_trusted_email_sender("nobody", "a@b.com") is False

    def test_multiple_patterns(self):
        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=[
                "alice@example.com",
                "*@cynium.com",
            ]),
        })
        assert cfg.is_trusted_email_sender("stefan", "alice@example.com") is True
        assert cfg.is_trusted_email_sender("stefan", "bob@cynium.com") is True
        assert cfg.is_trusted_email_sender("stefan", "bob@evil.com") is False

    def test_alerts_channel_default_empty(self):
        uc = UserConfig()
        assert uc.alerts_channel == ""

    def test_trusted_email_senders_default_empty(self):
        uc = UserConfig()
        assert uc.trusted_email_senders == []

    def test_db_trusted_sender_checked_with_conn(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=[]),
        })

        with db.get_db(db_path) as conn:
            # Not trusted without DB entry
            assert cfg.is_trusted_email_sender("stefan", "joe@example.com", conn) is False

            # Add to DB
            db.add_trusted_sender(conn, "stefan", "joe@example.com")
            assert cfg.is_trusted_email_sender("stefan", "joe@example.com", conn) is True

    def test_db_trusted_sender_not_checked_without_conn(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config(users={
            "stefan": UserConfig(trusted_email_senders=[]),
        })

        with db.get_db(db_path) as conn:
            db.add_trusted_sender(conn, "stefan", "joe@example.com")

        # Without conn, DB is not checked (backward compat)
        assert cfg.is_trusted_email_sender("stefan", "joe@example.com") is False


class TestEmailConfig:
    def test_effective_smtp_user_fallback(self):
        ec = EmailConfig(imap_user="imap@example.com", smtp_user="")
        assert ec.effective_smtp_user == "imap@example.com"

    def test_effective_smtp_password_fallback(self):
        ec = EmailConfig(imap_password="imappass", smtp_password="")
        assert ec.effective_smtp_password == "imappass"

    def test_effective_smtp_user_explicit(self):
        ec = EmailConfig(imap_user="imap@example.com", smtp_user="smtp@example.com")
        assert ec.effective_smtp_user == "smtp@example.com"


class TestSleepCycleConfig:
    def test_defaults(self):
        sc = SleepCycleConfig()
        assert sc.enabled is True
        assert sc.cron == "0 2 * * *"
        assert sc.memory_retention_days == 0
        assert sc.lookback_hours == 24

    def test_config_default(self):
        cfg = Config()
        assert cfg.sleep_cycle.enabled is True

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[sleep_cycle]
enabled = true
cron = "0 3 * * *"
memory_retention_days = 60
lookback_hours = 36
""")
        cfg = load_config(config_file)
        assert cfg.sleep_cycle.enabled is True
        assert cfg.sleep_cycle.cron == "0 3 * * *"
        assert cfg.sleep_cycle.memory_retention_days == 60
        assert cfg.sleep_cycle.lookback_hours == 36

    def test_load_native_brain_overrides_and_compaction_knobs(self, tmp_path):
        # NB-4 / NB-14: [brain.native] model overrides + compaction sizing knobs.
        from istota.llm.catalog import get_model_info, set_model_overrides

        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[brain]
kind = "native"

[brain.native]
model = "qwen/qwen3-thinking"
compaction_reserve_tokens = 1000
compaction_keep_recent_tokens = 3000

[brain.native.model_overrides."qwen/qwen3-thinking"]
supports_thinking = true
context_window = 32000
""")
        try:
            cfg = load_config(config_file)
            assert cfg.brain.native.compaction_reserve_tokens == 1000
            assert cfg.brain.native.compaction_keep_recent_tokens == 3000
            assert cfg.brain.native.model_overrides["qwen/qwen3-thinking"][
                "supports_thinking"
            ] is True
            # Applied globally to the catalog at load time.
            info = get_model_info("qwen/qwen3-thinking")
            assert info.supports_thinking is True
            assert info.context_window == 32000
        finally:
            set_model_overrides({})

    def test_load_without_sleep_cycle(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.bob]
display_name = "Bob"
""")
        cfg = load_config(config_file)
        assert cfg.sleep_cycle.enabled is True

    def test_load_sleep_cycle_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[sleep_cycle]
enabled = true
""")
        cfg = load_config(config_file)
        sc = cfg.sleep_cycle
        assert sc.cron == "0 2 * * *"
        assert sc.memory_retention_days == 0
        assert sc.lookback_hours == 24


class TestChannelSleepCycleConfig:
    def test_defaults(self):
        csc = ChannelSleepCycleConfig()
        assert csc.enabled is True
        assert csc.cron == "0 3 * * *"
        assert csc.lookback_hours == 24
        assert csc.memory_retention_days == 0

    def test_config_default(self):
        cfg = Config()
        assert cfg.channel_sleep_cycle.enabled is True

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[channel_sleep_cycle]
enabled = true
cron = "0 4 * * *"
lookback_hours = 48
memory_retention_days = 60
""")
        cfg = load_config(config_file)
        assert cfg.channel_sleep_cycle.enabled is True
        assert cfg.channel_sleep_cycle.cron == "0 4 * * *"
        assert cfg.channel_sleep_cycle.lookback_hours == 48
        assert cfg.channel_sleep_cycle.memory_retention_days == 60

    def test_load_defaults_when_not_set(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.channel_sleep_cycle.enabled is True
        assert cfg.channel_sleep_cycle.cron == "0 3 * * *"


class TestResourceConfig:
    def test_defaults(self):
        rc = ResourceConfig(type="folder", path="/test")
        assert rc.type == "folder"
        assert rc.path == "/test"
        assert rc.name == ""
        assert rc.permissions == "read"

    def test_with_all_fields(self):
        rc = ResourceConfig(type="todo_file", path="/todo.md", name="Tasks", permissions="write")
        assert rc.type == "todo_file"
        assert rc.name == "Tasks"
        assert rc.permissions == "write"

    def test_defaults_service_credentials(self):
        rc = ResourceConfig(type="folder", path="/test")
        assert rc.base_url == ""
        assert rc.api_key == ""

    def test_obsolete_type_raises(self):
        """Direct construction with retired types raises — protects fixtures
        from drifting after the modules / connected services refactor (the
        c1423ba class of bugs)."""
        with pytest.raises(ValueError, match="retired"):
            ResourceConfig(type="karakeep", base_url="https://x")
        with pytest.raises(ValueError, match="retired"):
            ResourceConfig(type="feeds")
        with pytest.raises(ValueError, match="retired"):
            ResourceConfig(type="money")

    def test_obsolete_type_allowed_via_flag(self):
        """The TOML/DB loaders set ``_allow_obsolete=True`` so the migration
        step can absorb credentials before dropping the rows."""
        rc = ResourceConfig(
            type="karakeep", base_url="https://x", _allow_obsolete=True,
        )
        assert rc.type == "karakeep"

    def test_user_config_default_empty_resources(self):
        uc = UserConfig()
        assert uc.resources == []

    def test_user_config_with_resources(self):
        uc = UserConfig(resources=[
            ResourceConfig(type="folder", path="/projects"),
            ResourceConfig(type="todo_file", path="/todo.md", permissions="write"),
        ])
        assert len(uc.resources) == 2
        assert uc.resources[0].type == "folder"
        assert uc.resources[1].permissions == "write"


class TestDeveloperConfig:
    def test_defaults(self):
        dev = DeveloperConfig()
        assert dev.enabled is False
        assert dev.repos_dir == ""
        assert dev.gitlab_url == "https://gitlab.com"
        assert dev.gitlab_token == ""
        assert dev.gitlab_username == ""
        assert dev.github_url == "https://github.com"
        assert dev.github_token == ""
        assert dev.github_username == ""
        assert dev.github_default_owner == ""
        assert dev.github_reviewer == ""
        assert isinstance(dev.github_api_allowlist, list)
        assert len(dev.github_api_allowlist) > 0
        # Devbox proxy defaults.
        assert dev.api_timeout_seconds == 30
        assert dev.devbox_proxy_enabled is True
        assert dev.devbox_proxy_socket_dir == "/var/run/istota"
        assert dev.devbox_proxy_audit_log == ""

    def test_load_devbox_proxy_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
api_timeout_seconds = 60
devbox_proxy_enabled = false
devbox_proxy_socket_dir = "/run/istota"
devbox_proxy_audit_log = "/var/log/istota/devbox-proxy-audit.log"
""")
        cfg = load_config(config_file)
        assert cfg.developer.api_timeout_seconds == 60
        assert cfg.developer.devbox_proxy_enabled is False
        assert cfg.developer.devbox_proxy_socket_dir == "/run/istota"
        assert cfg.developer.devbox_proxy_audit_log == "/var/log/istota/devbox-proxy-audit.log"

    def test_config_default(self):
        cfg = Config()
        assert cfg.developer.enabled is False

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
gitlab_url = "https://gitlab.example.com"
gitlab_token = "glpat-test"
gitlab_username = "istota"
""")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is True
        assert cfg.developer.repos_dir == "/srv/repos"
        assert cfg.developer.gitlab_url == "https://gitlab.example.com"
        assert cfg.developer.gitlab_token == "glpat-test"
        assert cfg.developer.gitlab_username == "istota"

    def test_load_github_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_url = "https://github.example.com"
github_token = "ghp_test123"
github_username = "botuser"
github_default_owner = "myorg"
github_reviewer = "reviewer-user"
""")
        cfg = load_config(config_file)
        assert cfg.developer.github_url == "https://github.example.com"
        assert cfg.developer.github_token == "ghp_test123"
        assert cfg.developer.github_username == "botuser"
        assert cfg.developer.github_default_owner == "myorg"
        assert cfg.developer.github_reviewer == "reviewer-user"

    def test_load_github_custom_allowlist(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
github_api_allowlist = ["GET /repos/*"]
""")
        cfg = load_config(config_file)
        assert cfg.developer.github_api_allowlist == ["GET /repos/*"]

    def test_github_env_var_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        monkeypatch.setenv("ISTOTA_DEVELOPER_GITHUB_TOKEN", "ghp_env_override")
        cfg = load_config(config_file)
        assert cfg.developer.github_token == "ghp_env_override"

    def test_load_defaults_when_not_set(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is False
        assert cfg.developer.gitlab_url == "https://gitlab.com"
        assert cfg.developer.github_url == "https://github.com"

    def test_partial_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[developer]
enabled = true
repos_dir = "/srv/repos"
""")
        cfg = load_config(config_file)
        assert cfg.developer.enabled is True
        assert cfg.developer.repos_dir == "/srv/repos"
        assert cfg.developer.gitlab_url == "https://gitlab.com"
        assert cfg.developer.gitlab_token == ""
        assert cfg.developer.github_url == "https://github.com"
        assert cfg.developer.github_token == ""


class TestSiteConfig:
    def test_defaults(self):
        sc = SiteConfig()
        assert sc.enabled is False
        assert sc.hostname == ""
        assert sc.base_path == ""

    def test_config_default(self):
        cfg = Config()
        assert cfg.site.enabled is False

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[site]
enabled = true
hostname = "istota.example.com"
base_path = "/srv/app/istota/html"
""")
        cfg = load_config(config_file)
        assert cfg.site.enabled is True
        assert cfg.site.hostname == "istota.example.com"
        assert cfg.site.base_path == "/srv/app/istota/html"

    def test_load_defaults_when_not_set(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.site.enabled is False
        assert cfg.site.hostname == ""


        assert cfg.config_path == config_file

    def test_load_config_default_has_no_path(self):
        cfg = Config()
        assert cfg.config_path is None

    def test_load_config_honors_env_var(self, tmp_path, monkeypatch):
        """ISTOTA_CONFIG_PATH lets a subprocess find the parent's config."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        config_file = tmp_path / "from_env.toml"
        config_file.write_text('bot_name = "FromEnv"\n')
        # cwd is somewhere without a config/config.toml on the search list.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(config_file))
        cfg = load_config()
        assert cfg.config_path == config_file
        assert cfg.bot_name == "FromEnv"

    def test_load_config_explicit_path_overrides_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        env_cfg = tmp_path / "env.toml"
        env_cfg.write_text('bot_name = "FromEnv"\n')
        explicit_cfg = tmp_path / "explicit.toml"
        explicit_cfg.write_text('bot_name = "Explicit"\n')
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(env_cfg))
        cfg = load_config(explicit_cfg)
        assert cfg.config_path == explicit_cfg
        assert cfg.bot_name == "Explicit"

    def test_load_config_env_var_missing_file_falls_through(self, tmp_path, monkeypatch):
        """If ISTOTA_CONFIG_PATH points at a missing file, search continues."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "does_not_exist.toml"))
        # Isolate the search order from the developer/CI home: two candidates are
        # ~/src/config/config.toml and ~/.config/istota/config.toml (via
        # Path.home() → $HOME). Point HOME at the empty tmp dir so a real local
        # config (e.g. from a standalone `istota setup`) doesn't get picked up and
        # break the "nothing found" assertion. chdir handles the relative
        # config/config.toml candidate.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        cfg = load_config()
        # Default Config — config_path stays None because nothing was loaded.
        assert cfg.config_path is None


class TestAdminUsersLoadConfig:
    def test_load_config_loads_admin_users(self, tmp_path, monkeypatch):
        admins_file = tmp_path / "admins"
        admins_file.write_text("alice\n")
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins_file))
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.admin_users == {"alice"}

    def test_load_config_no_admins_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "nonexistent"))
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.admin_users == set()


class TestWorkerConcurrencyConfig:
    def test_scheduler_new_worker_fields_from_toml(self, tmp_path, monkeypatch):
        """Explicit max_foreground_workers/max_background_workers parsed from TOML."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'max_foreground_workers = 8\n'
            'max_background_workers = 4\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.max_foreground_workers == 8
        assert cfg.scheduler.max_background_workers == 4

    def test_scheduler_defaults(self):
        """Default values for new fields."""
        cfg = Config()
        assert cfg.scheduler.max_foreground_workers == 5
        assert cfg.scheduler.max_background_workers == 3

    def test_user_config_worker_limits(self, tmp_path, monkeypatch):
        """Per-user worker limits parsed from TOML."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            'max_foreground_workers = 2\n'
            'max_background_workers = 3\n'
        )
        cfg = load_config(p)
        assert cfg.users["alice"].max_foreground_workers == 2
        assert cfg.users["alice"].max_background_workers == 3

    def test_user_config_worker_limits_defaults(self):
        """UserConfig defaults to 0/0 (use global default)."""
        from istota.config import UserConfig
        uc = UserConfig()
        assert uc.max_foreground_workers == 0
        assert uc.max_background_workers == 0

    def test_global_user_worker_defaults_from_toml(self, tmp_path, monkeypatch):
        """Global per-user worker defaults parsed from scheduler section."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'user_max_foreground_workers = 3\n'
            'user_max_background_workers = 2\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.user_max_foreground_workers == 3
        assert cfg.scheduler.user_max_background_workers == 2

    def test_global_user_worker_defaults(self):
        """Default global per-user limits are 2/1."""
        cfg = Config()
        assert cfg.scheduler.user_max_foreground_workers == 2
        assert cfg.scheduler.user_max_background_workers == 1

    def test_load_config_user_worker_defaults_match_dataclass(self, tmp_path, monkeypatch):
        """load_config() without explicit settings should match Config() defaults."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text('[scheduler]\n')
        cfg = load_config(p)
        defaults = Config()
        assert cfg.scheduler.user_max_foreground_workers == defaults.scheduler.user_max_foreground_workers
        assert cfg.scheduler.user_max_background_workers == defaults.scheduler.user_max_background_workers

    def test_effective_user_workers_uses_global_default(self):
        """When user has 0 (not set), effective value comes from global default."""
        from istota.config import UserConfig
        cfg = Config()
        cfg.scheduler.user_max_foreground_workers = 3
        cfg.scheduler.user_max_background_workers = 2
        cfg.users["alice"] = UserConfig()  # 0/0 = use global
        assert cfg.effective_user_max_fg_workers("alice") == 3
        assert cfg.effective_user_max_bg_workers("alice") == 2

    def test_effective_user_workers_per_user_override(self):
        """Per-user setting overrides global default."""
        from istota.config import UserConfig
        cfg = Config()
        cfg.scheduler.user_max_foreground_workers = 1
        cfg.scheduler.user_max_background_workers = 1
        cfg.users["alice"] = UserConfig(max_foreground_workers=4, max_background_workers=2)
        assert cfg.effective_user_max_fg_workers("alice") == 4
        assert cfg.effective_user_max_bg_workers("alice") == 2

    def test_effective_user_workers_unknown_user(self):
        """Unknown user gets global default."""
        cfg = Config()
        cfg.scheduler.user_max_foreground_workers = 2
        cfg.scheduler.user_max_background_workers = 3
        assert cfg.effective_user_max_fg_workers("unknown") == 2
        assert cfg.effective_user_max_bg_workers("unknown") == 3

    def test_parsed_user_defaults_to_global_workers(self, tmp_path, monkeypatch):
        """User loaded without explicit worker limits uses global default."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'user_max_foreground_workers = 3\n'
            '\n'
            '[users.alice]\n'
            'display_name = "Alice"\n'
        )
        cfg = load_config(p)
        # alice doesn't set worker limits, so effective should use global
        assert cfg.effective_user_max_fg_workers("alice") == 3
        assert cfg.users["alice"].max_foreground_workers == 0  # sentinel, not 1


# ---------------------------------------------------------------------------
# TestMemorySystemConfigDefaults
# ---------------------------------------------------------------------------


class TestMemorySystemConfigDefaults:
    def test_sleep_cycle_auto_load_dated_days_default(self):
        cfg = SleepCycleConfig()
        assert cfg.auto_load_dated_days == 3

    def test_sleep_cycle_curate_user_memory_default(self):
        cfg = SleepCycleConfig()
        assert cfg.curate_user_memory is False

    def test_memory_search_auto_recall_default(self):
        cfg = MemorySearchConfig()
        assert cfg.auto_recall is False

    def test_memory_search_auto_recall_limit_default(self):
        cfg = MemorySearchConfig()
        assert cfg.auto_recall_limit == 5

    def test_memory_search_enabled_by_default(self):
        cfg = MemorySearchConfig()
        assert cfg.enabled is True

    def test_config_max_memory_chars_default(self):
        cfg = Config()
        assert cfg.max_memory_chars == 0


class TestMemorySystemConfigLoading:
    def test_load_sleep_cycle_auto_load_dated_days(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[sleep_cycle]\n'
            'enabled = true\n'
            'auto_load_dated_days = 7\n'
            'curate_user_memory = true\n'
        )
        cfg = load_config(p)
        assert cfg.sleep_cycle.auto_load_dated_days == 7
        assert cfg.sleep_cycle.curate_user_memory is True

    def test_load_memory_search_auto_recall(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text(
            '[memory_search]\n'
            'enabled = true\n'
            'auto_recall = true\n'
            'auto_recall_limit = 10\n'
        )
        cfg = load_config(p)
        assert cfg.memory_search.auto_recall is True
        assert cfg.memory_search.auto_recall_limit == 10

    def test_load_max_memory_chars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text('max_memory_chars = 5000\n')
        cfg = load_config(p)
        assert cfg.max_memory_chars == 5000

    def test_load_defaults_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        p = tmp_path / "config.toml"
        p.write_text('db_path = "test.db"\n')
        cfg = load_config(p)
        assert cfg.sleep_cycle.auto_load_dated_days == 3
        assert cfg.sleep_cycle.curate_user_memory is False
        assert cfg.memory_search.auto_recall is False
        assert cfg.memory_search.auto_recall_limit == 5
        assert cfg.max_memory_chars == 0


class TestConfigToplevelKeyOrdering:
    """Regression: a table header in the rendered config or example file must
    not appear above top-level keys, or those keys get parsed as members of
    the table (TOML rule). This bit us once — `[brain]` placed above
    `db_path` in the Ansible template silently nested `db_path` under
    `brain`, causing the scheduler to fall back to the default DB path and
    log "unable to open database file" forever.
    """

    def test_example_config_db_path_at_root(self, tmp_path, monkeypatch):
        """Loading config/config.example.toml must yield root-level db_path,
        temp_dir, skills_dir — not nested under any table."""
        import tomli
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        example = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"
        with open(example, "rb") as f:
            data = tomli.load(f)
        # These must be at the root, not under [brain] or any other table.
        for key in ("db_path", "temp_dir", "skills_dir", "rclone_remote"):
            assert key in data, f"{key} not at root in config.example.toml"
        # And they must NOT be inside the brain table.
        if "brain" in data:
            for key in ("db_path", "temp_dir", "skills_dir", "rclone_remote"):
                assert key not in data["brain"], (
                    f"{key} ended up under [brain] — table header is positioned wrong"
                )

    def test_ansible_template_brain_below_toplevel_keys(self):
        """Verify the [brain] header in deploy/ansible/templates/config.toml.j2
        appears AFTER all top-level key assignments (db_path, temp_dir, etc.).
        TOML places every key after a table header into that table.
        """
        template = Path(__file__).resolve().parent.parent / "deploy" / "ansible" / "templates" / "config.toml.j2"
        text = template.read_text()
        brain_idx = text.find("\n[brain]\n")
        assert brain_idx >= 0, "[brain] section missing from Ansible template"
        # Top-level keys that must be defined before any table header.
        for key in ("db_path", "temp_dir", "skills_dir", "rclone_remote"):
            key_idx = text.find(f"\n{key} = ")
            assert key_idx >= 0, f"{key} assignment missing from template"
            assert key_idx < brain_idx, (
                f"{key} is defined AFTER [brain] in the Ansible template — "
                "it will be parsed as brain.{key}, breaking config loading"
            )


class TestAnsibleValidateConfigScript:
    """ISSUE-058: deploy/ansible/files/validate_config.py is the post-render
    structural check the role runs before restarting the scheduler.
    """

    @staticmethod
    def _run(tmp_path, cfg_text, expected_db, expected_tmp):
        import subprocess
        import sys

        cfg = tmp_path / "config.toml"
        cfg.write_text(cfg_text)
        script = (
            Path(__file__).resolve().parent.parent
            / "deploy" / "ansible" / "files" / "validate_config.py"
        )
        proc = subprocess.run(
            [sys.executable, str(script), str(cfg), "istota", expected_db, expected_tmp],
            capture_output=True, text=True,
        )
        return proc

    def test_passes_on_well_formed_config(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/zorg/data/istota.db"\n'
            'temp_dir = "/srv/app/zorg/tmp"\n'
            "\n[brain]\nkind = \"claude_code\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/zorg/data/istota.db", "/srv/app/zorg/tmp")
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_fails_when_root_keys_leak_under_brain(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            "\n[brain]\n"
            'kind = "claude_code"\n'
            'db_path = "/srv/app/zorg/data/istota.db"\n'
            'temp_dir = "/srv/app/zorg/tmp"\n'
        )
        proc = self._run(tmp_path, cfg, "/srv/app/zorg/data/istota.db", "/srv/app/zorg/tmp")
        assert proc.returncode == 1
        assert "leaked under [brain]" in proc.stderr
        assert "db_path" in proc.stderr and "temp_dir" in proc.stderr

    def test_fails_when_db_path_does_not_match_expected(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/wrong/path.db"\n'
            'temp_dir = "/srv/app/zorg/tmp"\n'
            "\n[brain]\nkind = \"claude_code\"\n"
        )
        proc = self._run(tmp_path, cfg, "/srv/app/zorg/data/istota.db", "/srv/app/zorg/tmp")
        assert proc.returncode == 1
        assert "db_path" in proc.stderr and "/wrong/path.db" in proc.stderr

    def test_fails_on_unparseable_toml(self, tmp_path):
        proc = self._run(tmp_path, "this is not [valid TOML\n", "x", "y")
        assert proc.returncode == 1
        assert "TOML parse error" in proc.stderr

    def test_brain_kind_alone_does_not_trip_leak_check(self, tmp_path):
        cfg = (
            'bot_name = "Test"\n'
            'db_path = "/srv/app/zorg/data/istota.db"\n'
            'temp_dir = "/srv/app/zorg/tmp"\n'
            "\n[brain]\n"
            'kind = "claude_code"\n'
        )
        proc = self._run(tmp_path, cfg, "/srv/app/zorg/data/istota.db", "/srv/app/zorg/tmp")
        assert proc.returncode == 0, proc.stderr


class TestApplyUserResources:
    """`_apply_user_resources` overlays DB resource rows onto loaded UserConfig.

    The runtime invariant is: every resource the operator has provisioned —
    whether via TOML or via `istota resource ensure` / web UI — appears in
    ``config.users[uid].resources`` so existing call sites (executor merge,
    webhook_receiver, money/feeds loaders, secrets_store import) work
    uniformly. DB rows win when the (type, path) pair matches a TOML row.
    """

    def _write_minimal_config(self, tmp_path: Path, db_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        return cfg

    def test_db_resource_appears_in_user_config(self, tmp_path, monkeypatch):
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
                extras={"meta_key": "meta-val", "meta_count": 75},
            )
        cfg_path = self._write_minimal_config(tmp_path, db_path)
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg_path)
        resources = config.users["alice"].resources
        folders = [r for r in resources if r.type == "folder"]
        assert len(folders) == 1
        assert folders[0].extra == {"meta_key": "meta-val", "meta_count": 75}
        assert folders[0].name == "Docs"

    def test_db_row_dedupes_against_matching_toml_row(self, tmp_path, monkeypatch):
        # Same (type, path): DB row replaces TOML row. Without dedupe the
        # executor would see two ResourceConfig entries for one logical
        # resource and double-count.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs (DB)",
                extras={"meta_key": "from-db"},
            )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.resources]]\n"
            'type = "folder"\n'
            'path = "/Docs"\n'
            'name = "Docs (TOML)"\n'
            'meta_key = "from-toml"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        resources = config.users["alice"].resources
        folders = [r for r in resources if r.type == "folder"]
        assert len(folders) == 1
        # DB wins because once the row exists, operators expect it to be
        # authoritative — same precedence as user_profiles.
        assert folders[0].extra["meta_key"] == "from-db"
        assert folders[0].name == "Docs (DB)"

    def test_distinct_paths_keep_both_resources(self, tmp_path, monkeypatch):
        # Two folders with different paths must coexist — dedupe is keyed on
        # (type, path), not type alone.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Documents", display_name="Docs",
            )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.resources]]\n"
            'type = "folder"\n'
            'path = "/Pictures"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        folders = sorted(
            (r.path for r in config.users["alice"].resources if r.type == "folder")
        )
        assert folders == ["/Documents", "/Pictures"]

    def test_synthesises_user_when_only_db_row_exists(self, tmp_path, monkeypatch):
        # A user with no TOML stanza but a DB resource row must still be
        # reachable through config.users[uid].resources. Mirrors the
        # _apply_user_profiles pattern for synthesised UserConfigs.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="bob", resource_type="folder",
                resource_path="/Bob", display_name="Bob's folder",
            )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert "bob" in config.users
        assert any(r.type == "folder" and r.path == "/Bob"
                   for r in config.users["bob"].resources)

    def test_missing_db_does_not_fail_load(self, tmp_path, monkeypatch):
        # Same best-effort contract as _apply_user_profiles: callers like
        # `istota init` run before the DB exists.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{tmp_path / "does-not-exist.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.resources]]\n"
            'type = "folder"\n'
            'path = "/x"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert any(r.type == "folder" for r in config.users["alice"].resources)


class TestApplyUserBriefings:
    """`_apply_user_briefings` overlays DB briefing rows onto loaded UserConfig.

    DB row replaces the matching TOML briefing by ``name``; new DB rows are
    added on top. Disabled rows drop the matching TOML name without
    scheduling a replacement, so the web UI can mute a TOML-templated
    briefing without re-templating.
    """

    def _write_minimal_config(self, tmp_path: Path, db_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        return cfg

    def test_db_briefing_appears_in_user_config(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * 1-5", conversation_token="tok123",
            output="talk", components={"calendar": True},
        )
        cfg_path = self._write_minimal_config(tmp_path, db_path)
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg_path)
        briefings = config.users["alice"].briefings
        assert len(briefings) == 1
        assert briefings[0].name == "morning"
        assert briefings[0].cron == "0 7 * * 1-5"
        assert briefings[0].components == {"calendar": True}

    def test_db_row_replaces_matching_toml_row(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 8 * * *", conversation_token="db-room",
            output="talk", components={"calendar": True},
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.briefings]]\n"
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "toml-room"\n'
            'output = "talk"\n'
            "[users.alice.briefings.components]\n"
            "todos = true\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        briefings = config.users["alice"].briefings
        assert len(briefings) == 1
        # DB wins
        assert briefings[0].cron == "0 8 * * *"
        assert briefings[0].conversation_token == "db-room"

    def test_distinct_names_coexist(self, tmp_path, monkeypatch):
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="evening",
            cron="0 19 * * *", conversation_token="t", output="talk",
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.briefings]]\n"
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "t"\n'
            'output = "talk"\n'
            "[users.alice.briefings.components]\n"
            "calendar = true\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        names = {b.name for b in config.users["alice"].briefings}
        assert names == {"morning", "evening"}

    def test_disabled_db_row_drops_toml_briefing(self, tmp_path, monkeypatch):
        # Operator can mute a TOML-templated briefing via the web UI by
        # toggling the row off. Without this the TOML would resurrect it.
        from istota import db, user_briefings

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 7 * * *", conversation_token="t", output="talk",
            enabled=False,
        )
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
            "\n[[users.alice.briefings]]\n"
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "t"\n'
            'output = "talk"\n'
            "[users.alice.briefings.components]\n"
            "calendar = true\n"
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert config.users["alice"].briefings == []

    def test_missing_db_does_not_fail_load(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{tmp_path / "does-not-exist.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        config = load_config(cfg)
        assert "alice" in config.users


class TestDisabledModules:
    """Phase 1 of the modules / connected services refactor."""

    def test_user_config_default_empty(self):
        assert UserConfig().disabled_modules == []

    def test_parsed_from_toml(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            '[users.alice]\n'
            'display_name = "Alice"\n'
            'disabled_modules = ["feeds", "money"]\n'
        )
        cfg = load_config(p)
        assert cfg.users["alice"].disabled_modules == ["feeds", "money"]

    def test_is_module_enabled_default_on(self):
        cfg = Config()
        cfg.users["alice"] = UserConfig()
        assert cfg.is_module_enabled("alice", "feeds") is True
        assert cfg.is_module_enabled("alice", "money") is True
        assert cfg.is_module_enabled("alice", "location") is True

    def test_is_module_enabled_unknown_user_default_on(self):
        cfg = Config()
        # No users configured at all — default-on still applies (docker
        # auto-seeding flow can hit this path before profiles are written).
        assert cfg.is_module_enabled("ghost", "feeds") is True

    def test_is_module_enabled_disabled_for_user(self):
        cfg = Config()
        cfg.users["alice"] = UserConfig(disabled_modules=["feeds"])
        assert cfg.is_module_enabled("alice", "feeds") is False
        assert cfg.is_module_enabled("alice", "money") is True

    def test_is_module_enabled_false_when_dependency_missing(self, monkeypatch):
        """A module whose optional install extra is absent is unavailable —
        hidden from the web UI and skipped by the scheduler — so a lean install
        (e.g. `local` without the money extra) shows no broken Money tab."""
        from istota import modules

        monkeypatch.setitem(
            modules.MODULE_DEPENDENCIES, "money", ("a_pkg_not_installed_zzz",),
        )
        monkeypatch.setattr(modules, "_AVAILABILITY_CACHE", {})
        cfg = Config()
        cfg.users["alice"] = UserConfig()
        assert cfg.is_module_enabled("alice", "money") is False
        # A module with its deps present is unaffected.
        assert cfg.is_module_enabled("alice", "feeds") is True

    def test_module_available_true_when_no_deps_declared(self, monkeypatch):
        from istota import modules

        monkeypatch.setattr(modules, "_AVAILABILITY_CACHE", {})
        assert modules.module_available("feeds") is True

    def test_is_module_enabled_unknown_module(self):
        # Unknown module names are never "enabled" — guard against typos
        # leaking into user-supplied data.
        cfg = Config()
        cfg.users["alice"] = UserConfig()
        assert cfg.is_module_enabled("alice", "ghost") is False

    def test_is_module_enabled_reads_live_db_row(self, tmp_path):
        # Cross-process scenario: web_app writes user_profiles.disabled_modules
        # via the settings UI; the scheduler (its own Config instance) must
        # pick up the new value on the next is_module_enabled call without
        # any reload. The in-memory UserConfig deliberately disagrees with
        # the DB row to prove the DB is consulted, not the in-memory copy.
        from istota import db, user_profiles

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")

        cfg = Config()
        cfg.db_path = db_path
        cfg.users["alice"] = UserConfig(disabled_modules=["feeds"])  # stale in-memory

        # DB row has feeds enabled (default empty list) — DB wins.
        assert cfg.is_module_enabled("alice", "feeds") is True

        # External writer disables money in the DB; next call reflects it.
        user_profiles.update_profile(db_path, "alice", disabled_modules=["money"])
        assert cfg.is_module_enabled("alice", "money") is False
        assert cfg.is_module_enabled("alice", "feeds") is True

    def test_is_module_enabled_falls_back_when_no_db_row(self, tmp_path):
        # A user with no user_profiles row (e.g. mid-init, before auto-seed)
        # falls back to the in-memory UserConfig.disabled_modules list.
        from istota import db

        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        cfg = Config()
        cfg.db_path = db_path
        cfg.users["alice"] = UserConfig(disabled_modules=["feeds"])
        assert cfg.is_module_enabled("alice", "feeds") is False


class TestCleanupObsoleteResources:
    """db.cleanup_obsolete_resources removes retired resource types."""

    def test_drops_retired_types(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for rtype in ("feeds", "money", "monarch", "moneyman", "karakeep", "overland"):
                db.add_user_resource(
                    conn, user_id="alice", resource_type=rtype,
                    resource_path=rtype, display_name=f"{rtype} display",
                )
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
            )
        removed = db.cleanup_obsolete_resources(db_path)
        assert removed == 6
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert [r.resource_type for r in rows] == ["folder"]

    def test_idempotent(self, tmp_path):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="folder",
                resource_path="/Docs", display_name="Docs",
            )
        # Second run is a no-op — operators can leave the call wired into
        # startup without worrying about duplicate work.
        assert db.cleanup_obsolete_resources(db_path) == 0
        assert db.cleanup_obsolete_resources(db_path) == 0

    def test_missing_db_is_noop(self, tmp_path):
        from istota import db
        # Mirrors the best-effort contract used by _apply_user_profiles —
        # callers like `istota init` may run before the DB exists.
        assert db.cleanup_obsolete_resources(tmp_path / "no.db") == 0

    def test_load_config_runs_cleanup(self, tmp_path, monkeypatch):
        from istota import db
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.add_user_resource(
                conn, user_id="alice", resource_type="overland",
                resource_path="overland", display_name="GPS",
                extras={"ingest_token": "tok-xyz"},
            )

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            "\n[users.alice]\n"
            'display_name = "Alice"\n'
        )
        monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = load_config(cfg)

        # The retired row no longer surfaces in the in-memory resources
        # list (the load_config-time filter caught it).
        assert all(r.type != "overland" for r in config.users["alice"].resources)

        # And the row is gone from the DB.
        with db.get_db(db_path) as conn:
            rows = db.get_user_resources(conn, "alice")
        assert all(r.resource_type != "overland" for r in rows)

        # The credential was migrated into the secrets table during the
        # same load — webhook_receiver.reload_config picks it up from there.
        from istota import secrets_store
        assert secrets_store.get_secret(
            db_path, "alice", "overland", "ingest_token",
        ) == "tok-xyz"
