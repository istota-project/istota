"""Configuration loading for istota.config module."""

from pathlib import Path

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
    load_user_configs,
    reload_user_configs,
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
        assert cfg.scheduler.email_poll_interval == 60
        assert cfg.scheduler.talk_poll_interval == 10
        assert cfg.scheduler.talk_poll_timeout == 30
        assert cfg.scheduler.progress_updates is True
        assert cfg.scheduler.task_timeout_minutes == 30
        assert cfg.scheduler.task_retention_days == 7

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
            'email_poll_interval = 120\n'
            'talk_poll_interval = 5\n'
            'progress_updates = false\n'
            'progress_min_interval = 15\n'
            'task_timeout_minutes = 60\n'
            'confirmation_timeout_minutes = 60\n'
            'task_retention_days = 14\n'
            'email_retention_days = 30\n'
        )
        cfg = load_config(p)
        assert cfg.scheduler.poll_interval == 10
        assert cfg.scheduler.email_poll_interval == 120
        assert cfg.scheduler.talk_poll_interval == 5
        assert cfg.scheduler.progress_updates is False
        assert cfg.scheduler.progress_min_interval == 15
        assert cfg.scheduler.task_timeout_minutes == 60
        assert cfg.scheduler.confirmation_timeout_minutes == 60
        assert cfg.scheduler.task_retention_days == 14
        assert cfg.scheduler.email_retention_days == 30

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

    def test_karakeep_resource_with_credentials(self):
        rc = ResourceConfig(
            type="karakeep", name="Bookmarks",
            base_url="https://keep.example.com/api/v1",
            api_key="secret-key",
        )
        assert rc.type == "karakeep"
        assert rc.path == ""
        assert rc.base_url == "https://keep.example.com/api/v1"
        assert rc.api_key == "secret-key"

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


class TestPerUserConfigFiles:
    def test_load_user_configs_empty_dir(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        result = load_user_configs(users_dir)
        assert result == {}

    def test_load_user_configs_nonexistent_dir(self, tmp_path):
        result = load_user_configs(tmp_path / "nope")
        assert result == {}

    def test_load_single_user_file(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"
email_addresses = ["alice@example.com"]
timezone = "America/New_York"
""")
        result = load_user_configs(users_dir)
        assert "alice" in result
        assert result["alice"].display_name == "Alice"
        assert result["alice"].email_addresses == ["alice@example.com"]
        assert result["alice"].timezone == "America/New_York"

    def test_load_user_with_resources(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "stefan.toml").write_text("""
display_name = "Stefan"

[[resources]]
type = "folder"
path = "/stefan/Projects"
name = "Projects"
permissions = "write"

[[resources]]
type = "todo_file"
path = "/Users/stefan/TASKS.md"
name = "Tasks"
permissions = "write"

[[resources]]
type = "reminders_file"
path = "/Users/stefan/notes/REMINDERS.md"
name = "Reminders"
""")
        result = load_user_configs(users_dir)
        assert "stefan" in result
        stefan = result["stefan"]
        assert len(stefan.resources) == 3
        assert stefan.resources[0].type == "folder"
        assert stefan.resources[0].path == "/stefan/Projects"
        assert stefan.resources[0].name == "Projects"
        assert stefan.resources[0].permissions == "write"
        assert stefan.resources[1].type == "todo_file"
        assert stefan.resources[2].type == "reminders_file"
        assert stefan.resources[2].permissions == "read"

    def test_load_user_with_karakeep_resource(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"

[[resources]]
type = "karakeep"
name = "Bookmarks"
base_url = "https://keep.example.com/api/v1"
api_key = "kk-secret-token"
""")
        result = load_user_configs(users_dir)
        assert "alice" in result
        alice = result["alice"]
        assert len(alice.resources) == 1
        kk = alice.resources[0]
        assert kk.type == "karakeep"
        assert kk.name == "Bookmarks"
        assert kk.path == ""
        assert kk.base_url == "https://keep.example.com/api/v1"
        assert kk.api_key == "kk-secret-token"

    def test_load_user_with_briefings(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "bob.toml").write_text("""
display_name = "Bob"
timezone = "Europe/Berlin"

[[briefings]]
name = "morning"
cron = "0 7 * * *"
conversation_token = "room1"

[briefings.components]
calendar = true
""")
        result = load_user_configs(users_dir)
        bob = result["bob"]
        assert len(bob.briefings) == 1
        assert bob.briefings[0].name == "morning"
        assert bob.briefings[0].cron == "0 7 * * *"

    def test_load_user_ignores_sleep_cycle(self, tmp_path):
        """Sleep cycle is global, not per-user. Per-user sleep_cycle sections are ignored."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"
""")
        result = load_user_configs(users_dir)
        assert result["alice"].display_name == "Alice"

    def test_load_multiple_users(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        (users_dir / "bob.toml").write_text('display_name = "Bob"\n')
        result = load_user_configs(users_dir)
        assert len(result) == 2
        assert "alice" in result
        assert "bob" in result

    def test_per_user_files_override_main_config(self, tmp_path):
        # Main config with alice
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.alice]
display_name = "Alice Old"
timezone = "UTC"
""")
        # Per-user file overrides alice
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice New"
timezone = "America/New_York"
""")
        cfg = load_config(config_file)
        assert cfg.users["alice"].display_name == "Alice New"
        assert cfg.users["alice"].timezone == "America/New_York"

    def test_per_user_files_add_to_main_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.alice]
display_name = "Alice"
""")
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "bob.toml").write_text('display_name = "Bob"\n')
        cfg = load_config(config_file)
        assert "alice" in cfg.users
        assert "bob" in cfg.users

    def test_users_dir_set_on_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        cfg = load_config(config_file)
        assert cfg.users_dir == users_dir

    def test_users_dir_none_when_missing(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        cfg = load_config(config_file)
        assert cfg.users_dir is None

    def test_skip_non_toml_files(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        (users_dir / "readme.md").write_text("# Users\n")
        result = load_user_configs(users_dir)
        assert len(result) == 1
        assert "alice" in result

    def test_skip_example_files(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        (users_dir / "bob.example.toml").write_text('display_name = "Bob Example"\n')
        result = load_user_configs(users_dir)
        assert len(result) == 1
        assert "alice" in result
        assert "bob.example" not in result

    def test_resources_in_main_config_users_section(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.alice]
display_name = "Alice"

[[users.alice.resources]]
type = "folder"
path = "/alice/docs"
name = "Docs"
permissions = "write"
""")
        cfg = load_config(config_file)
        assert len(cfg.users["alice"].resources) == 1
        assert cfg.users["alice"].resources[0].type == "folder"

    def test_load_user_with_alerts_channel_and_trusted_senders(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "stefan.toml").write_text("""
display_name = "Stefan"
alerts_channel = "abc123"
trusted_email_senders = ["*@cynium.com", "alice@example.com"]
""")
        result = load_user_configs(users_dir)
        assert result["stefan"].alerts_channel == "abc123"
        assert result["stefan"].trusted_email_senders == ["*@cynium.com", "alice@example.com"]

    def test_load_user_without_alerts_channel_defaults(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "stefan.toml").write_text('display_name = "Stefan"\n')
        result = load_user_configs(users_dir)
        assert result["stefan"].alerts_channel == ""
        assert result["stefan"].trusted_email_senders == []


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

    def test_github_env_var_override(self, tmp_path):
        import os
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        old = os.environ.get("ISTOTA_GITHUB_TOKEN")
        try:
            os.environ["ISTOTA_GITHUB_TOKEN"] = "ghp_env_override"
            cfg = load_config(config_file)
            assert cfg.developer.github_token == "ghp_env_override"
        finally:
            if old is None:
                os.environ.pop("ISTOTA_GITHUB_TOKEN", None)
            else:
                os.environ["ISTOTA_GITHUB_TOKEN"] = old

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

    def test_user_config_site_enabled_default(self):
        uc = UserConfig()
        assert uc.site_enabled is False

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

    def test_user_site_enabled_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.alice]
display_name = "Alice"
site_enabled = true
""")
        cfg = load_config(config_file)
        assert cfg.users["alice"].site_enabled is True

    def test_user_site_enabled_from_per_user_file(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"
site_enabled = true
""")
        result = load_user_configs(users_dir)
        assert result["alice"].site_enabled is True

    def test_user_site_enabled_default_false_in_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[users.bob]
display_name = "Bob"
""")
        cfg = load_config(config_file)
        assert cfg.users["bob"].site_enabled is False


class TestUserJsonOverride:
    """Tests for .user.json files that override .toml config."""

    def test_json_overrides_scalar_fields(self, tmp_path):
        """JSON scalar fields override TOML equivalents."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text(
            'display_name = "Alice"\n'
            'timezone = "UTC"\n'
        )
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "display_name": "Alice Updated",
            "timezone": "America/New_York",
        }))
        result = load_user_configs(users_dir)
        assert result["alice"].display_name == "Alice Updated"
        assert result["alice"].timezone == "America/New_York"

    def test_json_replaces_briefings(self, tmp_path):
        """JSON briefings list completely replaces TOML briefings."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"
[[briefings]]
name = "morning"
cron = "0 6 * * *"
""")
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "briefings": [
                {"name": "evening", "cron": "0 18 * * *", "output": "email"},
            ],
        }))
        result = load_user_configs(users_dir)
        assert len(result["alice"].briefings) == 1
        assert result["alice"].briefings[0].name == "evening"
        assert result["alice"].briefings[0].output == "email"

    def test_json_appends_non_credential_resources(self, tmp_path):
        """JSON resources are appended to TOML credential resources."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"
[[resources]]
type = "karakeep"
name = "Bookmarks"
base_url = "https://keep.example.com"
api_key = "secret-key"
""")
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "resources": [
                {"type": "folder", "path": "/alice/Projects", "name": "Projects"},
            ],
        }))
        result = load_user_configs(users_dir)
        types = [r.type for r in result["alice"].resources]
        assert "karakeep" in types
        assert "folder" in types
        assert len(result["alice"].resources) == 2

    def test_json_credential_resources_ignored(self, tmp_path):
        """Credential resource types in JSON are ignored."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "resources": [
                {"type": "karakeep", "name": "Hacked", "api_key": "evil"},
                {"type": "monarch", "name": "Bad"},
                {"type": "miniflux", "name": "Bad", "api_key": "evil"},
                {"type": "moneyman", "name": "Bad", "api_key": "evil"},
                {"type": "folder", "path": "/ok", "name": "Legit"},
            ],
        }))
        result = load_user_configs(users_dir)
        types = [r.type for r in result["alice"].resources]
        assert types == ["folder"]

    def test_json_without_toml_ignored(self, tmp_path):
        """A .user.json without a matching .toml is ignored."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        import json
        (users_dir / "ghost.user.json").write_text(json.dumps({
            "display_name": "Ghost",
        }))
        result = load_user_configs(users_dir)
        assert "ghost" not in result

    def test_json_partial_override(self, tmp_path):
        """JSON only overrides fields present in the JSON; others keep TOML values."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text(
            'display_name = "Alice"\n'
            'timezone = "UTC"\n'
            'ntfy_topic = "alice-topic"\n'
        )
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "timezone": "Europe/Berlin",
        }))
        result = load_user_configs(users_dir)
        assert result["alice"].display_name == "Alice"  # from TOML
        assert result["alice"].timezone == "Europe/Berlin"  # from JSON
        assert result["alice"].ntfy_topic == "alice-topic"  # from TOML

    def test_json_disabled_skills_override(self, tmp_path):
        """JSON disabled_skills replaces TOML disabled_skills."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text(
            'display_name = "Alice"\n'
            'disabled_skills = ["browse", "whisper"]\n'
        )
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "disabled_skills": ["markets"],
        }))
        result = load_user_configs(users_dir)
        assert result["alice"].disabled_skills == ["markets"]

    def test_malformed_json_logged_and_skipped(self, tmp_path):
        """Malformed .user.json is logged and TOML config used as-is."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        (users_dir / "alice.user.json").write_text("{bad json")
        result = load_user_configs(users_dir)
        assert result["alice"].display_name == "Alice"

    def test_toml_credential_resources_preserved_when_json_has_briefings(self, tmp_path):
        """TOML credential resources are preserved even when JSON provides briefings."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text("""
display_name = "Alice"

[monarch]
session_token = "secret-token"

[[resources]]
type = "miniflux"
name = "Feeds"
base_url = "https://feeds.example.com"
api_key = "feed-key"

[[briefings]]
name = "old-briefing"
cron = "0 6 * * *"
""")
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({
            "briefings": [
                {"name": "new-briefing", "cron": "0 7 * * *"},
            ],
        }))
        result = load_user_configs(users_dir)
        # Briefings from JSON
        assert len(result["alice"].briefings) == 1
        assert result["alice"].briefings[0].name == "new-briefing"
        # Credential resources from TOML still present
        types = [r.type for r in result["alice"].resources]
        assert "monarch" in types
        assert "miniflux" in types


class TestReloadUserConfigs:
    def test_reload_detects_changed_file(self, tmp_path):
        """Reload returns True when a user config file changes."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        cfg = Config(users_dir=users_dir)
        cfg.users = load_user_configs(users_dir)
        assert cfg.users["alice"].display_name == "Alice"

        # Modify the file
        (users_dir / "alice.toml").write_text('display_name = "Alice Updated"\n')
        changed = reload_user_configs(cfg)
        assert changed is True
        assert cfg.users["alice"].display_name == "Alice Updated"

    def test_reload_returns_false_when_unchanged(self, tmp_path):
        """Reload returns False when nothing changed."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        cfg = Config(users_dir=users_dir)
        cfg.users = load_user_configs(users_dir)

        changed = reload_user_configs(cfg)
        assert changed is False

    def test_reload_picks_up_new_json_override(self, tmp_path):
        """Reload detects a new .user.json file appearing."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\ntimezone = "UTC"\n')
        cfg = Config(users_dir=users_dir)
        cfg.users = load_user_configs(users_dir)
        assert cfg.users["alice"].timezone == "UTC"

        # Add a JSON override
        import json
        (users_dir / "alice.user.json").write_text(json.dumps({"timezone": "Europe/Berlin"}))
        changed = reload_user_configs(cfg)
        assert changed is True
        assert cfg.users["alice"].timezone == "Europe/Berlin"

    def test_reload_no_users_dir(self):
        """Reload returns False when users_dir is None."""
        cfg = Config()
        assert reload_user_configs(cfg) is False

    def test_reload_preserves_main_config_users(self, tmp_path):
        """Users from [users] section in main config are not removed by reload."""
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        cfg = Config(users_dir=users_dir)
        # Simulate a user from main config [users] section
        cfg.users["bob"] = UserConfig(display_name="Bob")
        cfg.users.update(load_user_configs(users_dir))

        changed = reload_user_configs(cfg)
        assert changed is False
        assert "bob" in cfg.users  # bob preserved


class TestLoadAdminUsers:
    def test_missing_file_returns_empty_set(self, tmp_path):
        result = load_admin_users(str(tmp_path / "nonexistent"))
        assert result == set()

    def test_valid_file_parses_users(self, tmp_path):
        admins_file = tmp_path / "admins"
        admins_file.write_text("alice\nbob\n")
        result = load_admin_users(str(admins_file))
        assert result == {"alice", "bob"}

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        admins_file = tmp_path / "admins"
        admins_file.write_text("# Admin users\nalice\n\n# Another comment\nbob\n\n")
        result = load_admin_users(str(admins_file))
        assert result == {"alice", "bob"}

    def test_whitespace_stripped(self, tmp_path):
        admins_file = tmp_path / "admins"
        admins_file.write_text("  alice  \n  bob  \n")
        result = load_admin_users(str(admins_file))
        assert result == {"alice", "bob"}

    def test_empty_file_returns_empty_set(self, tmp_path):
        admins_file = tmp_path / "admins"
        admins_file.write_text("")
        result = load_admin_users(str(admins_file))
        assert result == set()

    def test_comments_only_returns_empty_set(self, tmp_path):
        admins_file = tmp_path / "admins"
        admins_file.write_text("# Only comments\n# Nothing else\n")
        result = load_admin_users(str(admins_file))
        assert result == set()

    def test_env_var_override(self, tmp_path, monkeypatch):
        admins_file = tmp_path / "custom_admins"
        admins_file.write_text("charlie\n")
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins_file))
        result = load_admin_users()
        assert result == {"charlie"}


class TestIsAdmin:
    def test_empty_set_means_all_admin(self):
        cfg = Config()
        assert cfg.is_admin("anyone") is True

    def test_user_in_admin_set(self):
        cfg = Config(admin_users={"alice", "bob"})
        assert cfg.is_admin("alice") is True

    def test_user_not_in_admin_set(self):
        cfg = Config(admin_users={"alice", "bob"})
        assert cfg.is_admin("charlie") is False


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
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text(
            'display_name = "Alice"\n'
            'max_foreground_workers = 2\n'
            'max_background_workers = 3\n'
        )
        p = tmp_path / "config.toml"
        p.write_text('')
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
        """User loaded from per-user TOML without explicit worker limits uses global default."""
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no_admins"))
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        (users_dir / "alice.toml").write_text('display_name = "Alice"\n')
        p = tmp_path / "config.toml"
        p.write_text(
            '[scheduler]\n'
            'user_max_foreground_workers = 3\n'
        )
        cfg = load_config(p)
        # alice's per-user file doesn't set worker limits, so effective should use global
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
