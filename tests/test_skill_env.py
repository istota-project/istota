"""Tests for istota.skills._env (declarative env var resolver)."""

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

from istota.skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
from istota.skills._types import EnvSpec, SkillMeta


def _make_ctx(
    tmp_path: Path,
    config: object | None = None,
    user_resources: list | None = None,
    user_config: object | None = None,
    is_admin: bool = True,
) -> EnvContext:
    """Create an EnvContext for testing."""
    if config is None:
        config = _make_config(tmp_path)
    return EnvContext(
        config=config,
        task=MagicMock(id=1, user_id="alice", conversation_token="room1"),
        user_resources=user_resources or [],
        user_config=user_config,
        user_temp_dir=tmp_path / "temp",
        is_admin=is_admin,
    )


@dataclass
class _MockBrowser:
    enabled: bool = False
    api_url: str = "http://localhost:9223"
    vnc_url: str = ""


@dataclass
class _MockConfig:
    nextcloud_mount_path: Path | None = None
    bot_dir_name: str = "istota"
    browser: _MockBrowser = field(default_factory=_MockBrowser)


def _make_config(tmp_path: Path, mount: bool = True) -> _MockConfig:
    mount_path = tmp_path / "mount" if mount else None
    if mount_path:
        mount_path.mkdir(parents=True, exist_ok=True)
    return _MockConfig(nextcloud_mount_path=mount_path)


@dataclass
class _MockResource:
    resource_type: str
    resource_path: str
    display_name: str | None = None


@dataclass
class _MockResourceConfig:
    type: str
    path: str = ""
    name: str = ""
    permissions: str = "read"
    base_url: str = ""
    api_key: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class _MockUserConfig:
    resources: list = field(default_factory=list)


class TestBuildSkillEnvConfig:
    """Tests for 'config' source type."""

    def test_resolves_dotted_config_path(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.enabled = True
        config.browser.api_url = "http://custom:1234"
        ctx = _make_ctx(tmp_path, config=config)

        meta = SkillMeta(
            name="browse",
            description="Browser",
            env_specs=[EnvSpec(
                var="BROWSER_API_URL",
                source="config",
                config_path="browser.api_url",
                when="browser.enabled",
            )],
        )
        env = build_skill_env(["browse"], {"browse": meta}, ctx)
        assert env["BROWSER_API_URL"] == "http://custom:1234"

    def test_skips_when_guard_false(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.enabled = False
        ctx = _make_ctx(tmp_path, config=config)

        meta = SkillMeta(
            name="browse",
            description="Browser",
            env_specs=[EnvSpec(
                var="BROWSER_API_URL",
                source="config",
                config_path="browser.api_url",
                when="browser.enabled",
            )],
        )
        env = build_skill_env(["browse"], {"browse": meta}, ctx)
        assert "BROWSER_API_URL" not in env

    def test_skips_missing_config_path(self, tmp_path):
        config = _make_config(tmp_path)
        ctx = _make_ctx(tmp_path, config=config)

        meta = SkillMeta(
            name="test",
            description="Test",
            env_specs=[EnvSpec(
                var="NONEXISTENT",
                source="config",
                config_path="does.not.exist",
            )],
        )
        env = build_skill_env(["test"], {"test": meta}, ctx)
        assert "NONEXISTENT" not in env


class TestBuildSkillEnvResource:
    """Tests for 'resource' source type."""

    def test_resolves_first_resource_path(self, tmp_path):
        config = _make_config(tmp_path)
        resources = [
            _MockResource("ledger", "/Users/alice/finance/main.beancount", "Main"),
            _MockResource("ledger", "/Users/alice/finance/extra.beancount", "Extra"),
        ]
        ctx = _make_ctx(tmp_path, config=config, user_resources=resources)

        meta = SkillMeta(
            name="accounting",
            description="Accounting",
            env_specs=[EnvSpec(
                var="LEDGER_PATH",
                source="resource",
                resource_type="ledger",
            )],
        )
        env = build_skill_env(["accounting"], {"accounting": meta}, ctx)
        assert env["LEDGER_PATH"] == str(config.nextcloud_mount_path / "Users/alice/finance/main.beancount")

    def test_skips_when_no_mount(self, tmp_path):
        config = _make_config(tmp_path, mount=False)
        resources = [_MockResource("ledger", "/Users/alice/finance/main.beancount")]
        ctx = _make_ctx(tmp_path, config=config, user_resources=resources)

        meta = SkillMeta(
            name="accounting",
            description="Accounting",
            env_specs=[EnvSpec(var="LEDGER_PATH", source="resource", resource_type="ledger")],
        )
        env = build_skill_env(["accounting"], {"accounting": meta}, ctx)
        assert "LEDGER_PATH" not in env

    def test_skips_when_no_matching_resource(self, tmp_path):
        config = _make_config(tmp_path)
        ctx = _make_ctx(tmp_path, config=config, user_resources=[])

        meta = SkillMeta(
            name="accounting",
            description="Accounting",
            env_specs=[EnvSpec(var="LEDGER_PATH", source="resource", resource_type="ledger")],
        )
        env = build_skill_env(["accounting"], {"accounting": meta}, ctx)
        assert "LEDGER_PATH" not in env


class TestBuildSkillEnvResourceJson:
    """Tests for 'resource_json' source type."""

    def test_returns_json_array(self, tmp_path):
        import json

        config = _make_config(tmp_path)
        resources = [
            _MockResource("ledger", "/Users/alice/finance/main.beancount", "Main"),
            _MockResource("ledger", "/Users/alice/finance/extra.beancount", "Extra"),
        ]
        ctx = _make_ctx(tmp_path, config=config, user_resources=resources)

        meta = SkillMeta(
            name="accounting",
            description="Accounting",
            env_specs=[EnvSpec(var="LEDGER_PATHS", source="resource_json", resource_type="ledger")],
        )
        env = build_skill_env(["accounting"], {"accounting": meta}, ctx)
        parsed = json.loads(env["LEDGER_PATHS"])
        assert len(parsed) == 2
        assert parsed[0]["name"] == "Main"
        assert parsed[1]["name"] == "Extra"


class TestBuildSkillEnvUserResourceConfig:
    """Tests for 'user_resource_config' source type."""

    def test_resolves_named_field(self, tmp_path):
        config = _make_config(tmp_path)
        user_config = _MockUserConfig(resources=[
            _MockResourceConfig(type="karakeep", base_url="https://keep.example.com/api/v1", api_key="secret123"),
        ])
        ctx = _make_ctx(tmp_path, config=config, user_config=user_config)

        meta = SkillMeta(
            name="bookmarks",
            description="Bookmarks",
            env_specs=[
                EnvSpec(var="KARAKEEP_BASE_URL", source="user_resource_config", resource_type="karakeep", field="base_url"),
                EnvSpec(var="KARAKEEP_API_KEY", source="user_resource_config", resource_type="karakeep", field="api_key"),
            ],
        )
        env = build_skill_env(["bookmarks"], {"bookmarks": meta}, ctx)
        assert env["KARAKEEP_BASE_URL"] == "https://keep.example.com/api/v1"
        assert env["KARAKEEP_API_KEY"] == "secret123"

    def test_resolves_extra_field(self, tmp_path):
        config = _make_config(tmp_path)
        user_config = _MockUserConfig(resources=[
            _MockResourceConfig(type="custom_service", extra={"token": "abc123"}),
        ])
        ctx = _make_ctx(tmp_path, config=config, user_config=user_config)

        meta = SkillMeta(
            name="custom",
            description="Custom",
            env_specs=[
                EnvSpec(var="CUSTOM_TOKEN", source="user_resource_config", resource_type="custom_service", field="token"),
            ],
        )
        env = build_skill_env(["custom"], {"custom": meta}, ctx)
        assert env["CUSTOM_TOKEN"] == "abc123"

    def test_skips_when_no_user_config(self, tmp_path):
        config = _make_config(tmp_path)
        ctx = _make_ctx(tmp_path, config=config, user_config=None)

        meta = SkillMeta(
            name="bookmarks",
            description="Bookmarks",
            env_specs=[
                EnvSpec(var="KARAKEEP_BASE_URL", source="user_resource_config", resource_type="karakeep", field="base_url"),
            ],
        )
        env = build_skill_env(["bookmarks"], {"bookmarks": meta}, ctx)
        assert "KARAKEEP_BASE_URL" not in env

    def test_skips_when_no_matching_resource(self, tmp_path):
        config = _make_config(tmp_path)
        user_config = _MockUserConfig(resources=[
            _MockResourceConfig(type="other"),
        ])
        ctx = _make_ctx(tmp_path, config=config, user_config=user_config)

        meta = SkillMeta(
            name="bookmarks",
            description="Bookmarks",
            env_specs=[
                EnvSpec(var="KARAKEEP_BASE_URL", source="user_resource_config", resource_type="karakeep", field="base_url"),
            ],
        )
        env = build_skill_env(["bookmarks"], {"bookmarks": meta}, ctx)
        assert "KARAKEEP_BASE_URL" not in env


class TestBuildSkillEnvMultipleSkills:
    """Tests for env resolution across multiple skills."""

    def test_merges_env_from_multiple_skills(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.enabled = True
        user_config = _MockUserConfig(resources=[
            _MockResourceConfig(type="karakeep", base_url="https://keep.example.com", api_key="key1"),
        ])
        ctx = _make_ctx(tmp_path, config=config, user_config=user_config)

        index = {
            "bookmarks": SkillMeta(
                name="bookmarks",
                description="Bookmarks",
                env_specs=[
                    EnvSpec(var="KARAKEEP_BASE_URL", source="user_resource_config", resource_type="karakeep", field="base_url"),
                ],
            ),
            "browse": SkillMeta(
                name="browse",
                description="Browser",
                env_specs=[
                    EnvSpec(var="BROWSER_API_URL", source="config", config_path="browser.api_url", when="browser.enabled"),
                ],
            ),
        }
        env = build_skill_env(["bookmarks", "browse"], index, ctx)
        assert env["KARAKEEP_BASE_URL"] == "https://keep.example.com"
        assert env["BROWSER_API_URL"] == "http://localhost:9223"

    def test_skips_skills_not_in_index(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        env = build_skill_env(["nonexistent"], {}, ctx)
        assert env == {}

    def test_skips_skills_without_env_specs(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        index = {"files": SkillMeta(name="files", description="Files")}
        env = build_skill_env(["files"], index, ctx)
        assert env == {}


class TestDispatchSetupEnvHooks:
    """Tests for setup_env() hook dispatch."""

    def test_skips_skills_without_skill_dir(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        index = {"files": SkillMeta(name="files", description="Files")}
        env = dispatch_setup_env_hooks(["files"], index, ctx)
        assert env == {}

    def test_skips_skills_without_setup_env_function(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        # calendar module exists but doesn't export setup_env
        index = {
            "calendar": SkillMeta(
                name="calendar",
                description="Calendar",
                skill_dir=str(tmp_path / "calendar"),
            ),
        }
        env = dispatch_setup_env_hooks(["calendar"], index, ctx)
        assert env == {}

    def test_handles_import_error_gracefully(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        index = {
            "nonexistent_module_xyz": SkillMeta(
                name="nonexistent_module_xyz",
                description="Won't import",
                skill_dir=str(tmp_path / "nonexistent"),
            ),
        }
        env = dispatch_setup_env_hooks(["nonexistent_module_xyz"], index, ctx)
        assert env == {}


class TestResourceConfigExtra:
    """Tests for the extra dict on ResourceConfig."""

    def test_extra_field_populated_from_config(self):
        from istota.config import _parse_user_data

        user_data = {
            "resources": [{
                "type": "custom_service",
                "path": "/data",
                "custom_token": "abc123",
                "custom_url": "https://example.com",
            }],
        }
        uc = _parse_user_data(user_data, "test_user")
        assert len(uc.resources) == 1
        rc = uc.resources[0]
        assert rc.type == "custom_service"
        assert rc.path == "/data"
        assert rc.extra == {"custom_token": "abc123", "custom_url": "https://example.com"}

    def test_known_fields_not_in_extra(self):
        from istota.config import _parse_user_data

        user_data = {
            "resources": [{
                "type": "karakeep",
                "base_url": "https://keep.example.com",
                "api_key": "secret",
            }],
        }
        uc = _parse_user_data(user_data, "test_user")
        rc = uc.resources[0]
        assert rc.base_url == "https://keep.example.com"
        assert rc.api_key == "secret"
        assert rc.extra == {}

    def test_empty_resources_no_crash(self):
        from istota.config import _parse_user_data

        user_data = {"resources": []}
        uc = _parse_user_data(user_data, "test_user")
        assert uc.resources == []
