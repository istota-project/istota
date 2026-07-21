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


class TestBuildSkillEnvUserId:
    """Tests for 'user_id' source type."""

    def test_returns_task_user_id(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        meta = SkillMeta(
            name="money",
            description="Money",
            env_specs=[EnvSpec(var="MONEY_USER", source="user_id")],
        )
        env = build_skill_env(["money"], {"money": meta}, ctx)
        assert env["MONEY_USER"] == "alice"

    def test_skips_when_user_id_empty(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        ctx.task.user_id = ""
        meta = SkillMeta(
            name="money",
            description="Money",
            env_specs=[EnvSpec(var="MONEY_USER", source="user_id")],
        )
        env = build_skill_env(["money"], {"money": meta}, ctx)
        assert "MONEY_USER" not in env


class TestBuildSkillEnvMultipleSkills:
    """Tests for env resolution across multiple skills."""

    def test_merges_env_from_multiple_skills(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.enabled = True
        config.browser.api_url = "http://browse:9223"
        ctx = _make_ctx(tmp_path, config=config)

        index = {
            "browse": SkillMeta(
                name="browse",
                description="Browser",
                env_specs=[
                    EnvSpec(var="BROWSER_API_URL", source="config", config_path="browser.api_url", when="browser.enabled"),
                ],
            ),
            "files": SkillMeta(
                name="files",
                description="Files",
                env_specs=[
                    EnvSpec(var="BOT_DIR", source="config", config_path="bot_dir_name"),
                ],
            ),
        }
        env = build_skill_env(["browse", "files"], index, ctx)
        assert env["BROWSER_API_URL"] == "http://browse:9223"
        assert env["BOT_DIR"] == "istota"

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

        # Known fields (type/path/name/permissions) don't leak into extra.
        # After the Resources sunset base_url/api_key are no longer special
        # flat fields, so they land in extra alongside other unknown keys.
        user_data = {
            "resources": [{
                "type": "svc_a",
                "path": "/x",
                "name": "S",
                "permissions": "read",
                "base_url": "https://svc.example.com",
                "api_key": "secret",
            }],
        }
        uc = _parse_user_data(user_data, "test_user")
        rc = uc.resources[0]
        assert rc.type == "svc_a"
        assert rc.path == "/x"
        assert rc.name == "S"
        assert rc.permissions == "read"
        assert rc.extra == {"base_url": "https://svc.example.com", "api_key": "secret"}

    def test_empty_resources_no_crash(self):
        from istota.config import _parse_user_data

        user_data = {"resources": []}
        uc = _parse_user_data(user_data, "test_user")
        assert uc.resources == []


# ---------------------------------------------------------------------------
# Phase 2 — manifest migration regression tests
# ---------------------------------------------------------------------------


class TestWhenAcceptsList:
    """Phase 2.5: ``when`` accepts a list of paths (all must be truthy).

    Required for the developer manifest, which must gate tokens on BOTH
    ``developer.enabled`` AND ``developer.gitlab_token`` — neither alone is
    sufficient, and ``when`` was previously a single string.
    """

    def test_list_when_all_truthy_resolves(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.enabled = True
        config.browser.api_url = "http://x:1"
        ctx = _make_ctx(tmp_path, config=config)

        meta = SkillMeta(
            name="browse",
            description="Browser",
            env_specs=[EnvSpec(
                var="BROWSER_API_URL",
                source="config",
                config_path="browser.api_url",
                when=["browser.enabled", "browser.api_url"],
            )],
        )
        env = build_skill_env(["browse"], {"browse": meta}, ctx)
        assert env["BROWSER_API_URL"] == "http://x:1"

    def test_list_when_one_falsy_skips(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.enabled = False  # second gate path is falsy
        config.browser.api_url = "http://x:1"
        ctx = _make_ctx(tmp_path, config=config)

        meta = SkillMeta(
            name="browse",
            description="Browser",
            env_specs=[EnvSpec(
                var="BROWSER_API_URL",
                source="config",
                config_path="browser.api_url",
                when=["browser.enabled", "browser.api_url"],
            )],
        )
        env = build_skill_env(["browse"], {"browse": meta}, ctx)
        assert "BROWSER_API_URL" not in env


class TestEmptyStringConfigValueResolves:
    """Phase 2.8: ``_resolve_env_spec`` skips only on ``val is None``.

    The previous ``not val`` check silently dropped numeric zeros and
    empty strings — fine for most cases but a footgun for ``SMTP_PORT``
    type fields. The migration spec calls this out under risk #6.
    """

    def test_zero_int_value_resolves(self, tmp_path):
        config = _make_config(tmp_path)
        config.browser.api_url = 0  # numeric zero, not None
        ctx = _make_ctx(tmp_path, config=config)

        meta = SkillMeta(
            name="x",
            description="x",
            env_specs=[EnvSpec(var="VAL", source="config", config_path="browser.api_url")],
        )
        env = build_skill_env(["x"], {"x": meta}, ctx)
        assert env.get("VAL") == "0"


class TestSetupEnvSourceMetadataOnly:
    """Phase 2.6: ``from: setup_env`` is a metadata-only declaration.

    The actual value comes from the skill's ``setup_env`` hook; the
    EnvSpec exists so derive_credential_set / derive_skill_credential_map
    (Phase 3) see the var.
    """

    def test_setup_env_source_returns_none(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        meta = SkillMeta(
            name="gw",
            description="GW",
            env_specs=[EnvSpec(
                var="GOOGLE_WORKSPACE_CLI_TOKEN",
                source="setup_env",
                sensitive=True,
            )],
        )
        env = build_skill_env(["gw"], {"gw": meta}, ctx)
        # Spec resolves to None; var only appears via setup_env hook.
        assert "GOOGLE_WORKSPACE_CLI_TOKEN" not in env


class TestDispatcherIteratesFullIndex:
    """Phase 2.5: ``dispatch_setup_env_hooks`` iterates the full skill_index,
    not the ``selected_skills`` argument.

    Required so the developer hook fires whenever tokens are configured,
    regardless of whether Pass 1 / Pass 2 picked the skill.
    """

    def test_unselected_skill_with_setup_env_still_fires(self, tmp_path, monkeypatch):
        """A hook for a skill not in selected_skills still runs."""
        ctx = _make_ctx(tmp_path)
        called = {"count": 0}

        # Monkey-patch a fake skill's setup_env. We use the real ``ntfy``
        # module because it lives at a stable import path; we replace
        # its setup_env and clean up afterwards.
        import importlib
        import istota.skills.ntfy as ntfy_mod

        def fake_setup_env(_ctx):
            called["count"] += 1
            return {"FAKE_VAR": "yes"}

        monkeypatch.setattr(ntfy_mod, "setup_env", fake_setup_env, raising=False)

        # ntfy not in selected_skills, but it IS in skill_index.
        index = {
            "ntfy": SkillMeta(
                name="ntfy",
                description="ntfy",
                skill_dir=str(Path(ntfy_mod.__file__).parent),
            ),
        }
        env = dispatch_setup_env_hooks([], index, ctx)
        assert called["count"] == 1
        assert env["FAKE_VAR"] == "yes"


class TestGateHasDiscoveredCalendars:
    """Phase 2.1: ``gate_has_discovered_calendars`` preserves the
    per-user CalDAV credential privacy gate (ISSUE-015) — CALDAV_*
    only resolves when the user has at least one discovered calendar."""

    def test_resolves_when_calendars_discovered(self, tmp_path):
        config = _make_config(tmp_path)
        ctx = EnvContext(
            config=config,
            task=MagicMock(id=1, user_id="alice", conversation_token=""),
            user_resources=[],
            user_config=None,
            user_temp_dir=tmp_path / "temp",
            is_admin=True,
            discovered_calendars=[("Personal", "https://cal/x", True)],
        )

        meta = SkillMeta(
            name="x",
            description="x",
            env_specs=[EnvSpec(
                var="OWNER",
                source="user_id",
                gate_has_discovered_calendars=True,
            )],
        )
        env = build_skill_env(["x"], {"x": meta}, ctx)
        assert env.get("OWNER") == "alice"

    def test_skips_when_no_calendars(self, tmp_path):
        config = _make_config(tmp_path)
        ctx = EnvContext(
            config=config,
            task=MagicMock(id=1, user_id="alice", conversation_token=""),
            user_resources=[],
            user_config=None,
            user_temp_dir=tmp_path / "temp",
            is_admin=True,
            discovered_calendars=[],
        )

        meta = SkillMeta(
            name="x",
            description="x",
            env_specs=[EnvSpec(
                var="OWNER",
                source="user_id",
                gate_has_discovered_calendars=True,
            )],
        )
        env = build_skill_env(["x"], {"x": meta}, ctx)
        assert "OWNER" not in env


class TestPhase2ManifestAcceptance:
    """Spot-check the shipped manifests have the expected sensitive flags
    and gates after the Phase 2 migration. Pinned so a future manifest
    edit can't silently drop the sensitive marker.
    """

    def test_email_passwords_marked_sensitive(self):
        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent"), bundled_dir=None)
        meta = index.get("email")
        assert meta is not None
        smtp_pwd = next(s for s in meta.env_specs if s.var == "SMTP_PASSWORD")
        imap_pwd = next(s for s in meta.env_specs if s.var == "IMAP_PASSWORD")
        assert smtp_pwd.sensitive is True
        assert imap_pwd.sensitive is True

    def test_nextcloud_pass_marked_sensitive(self):
        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent"), bundled_dir=None)
        meta = index.get("nextcloud")
        assert meta is not None
        nc_pass = next(s for s in meta.env_specs if s.var == "NC_PASS")
        assert nc_pass.sensitive is True

    def test_caldav_password_gated_on_discovered_calendars(self):
        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent"), bundled_dir=None)
        meta = index.get("calendar")
        assert meta is not None
        caldav_pwd = next(s for s in meta.env_specs if s.var == "CALDAV_PASSWORD")
        assert caldav_pwd.sensitive is True
        assert caldav_pwd.gate_has_discovered_calendars is True

    def test_developer_tokens_gated_on_enabled_and_repos_dir(self):
        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent"), bundled_dir=None)
        meta = index.get("developer")
        assert meta is not None
        gitlab_token = next(s for s in meta.env_specs if s.var == "GITLAB_TOKEN")
        assert gitlab_token.sensitive is True
        assert isinstance(gitlab_token.when, list)
        assert "developer.enabled" in gitlab_token.when
        assert "developer.repos_dir" in gitlab_token.when
        assert "developer.gitlab_token" in gitlab_token.when

    def test_google_workspace_token_marked_sensitive_via_setup_env(self):
        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent"), bundled_dir=None)
        meta = index.get("google_workspace")
        assert meta is not None
        gws_token = next(s for s in meta.env_specs if s.var == "GOOGLE_WORKSPACE_CLI_TOKEN")
        assert gws_token.sensitive is True
        assert gws_token.source == "setup_env"

    def test_ntfy_token_and_password_marked_sensitive(self):
        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent"), bundled_dir=None)
        meta = index.get("ntfy")
        assert meta is not None
        ntfy_token = next(s for s in meta.env_specs if s.var == "NTFY_TOKEN")
        ntfy_pwd = next(s for s in meta.env_specs if s.var == "NTFY_PASSWORD")
        assert ntfy_token.sensitive is True
        assert ntfy_pwd.sensitive is True


class TestExecutorHardcodedCredentialBlockGone:
    """Phase 2 acceptance: the hardcoded credential injection block is
    deleted. A grep for ``env[...] =`` in executor.execute_task outside
    the build_clean_env path and the ISTOTA_* identity block returns
    nothing for credential-shaped vars (NC_*, CALDAV_*, SMTP/IMAP_*,
    GITLAB_*, GITHUB_*, KARAKEEP_*).
    """

    def test_hardcoded_credential_assignments_removed(self):
        executor_src = Path(__file__).parent.parent / "src" / "istota" / "executor.py"
        text = executor_src.read_text()
        # These literal substrings appeared in the deleted block; if any
        # come back, the manifest source-of-truth principle is broken.
        forbidden = [
            'env["NC_URL"] =',
            'env["NC_USER"] =',
            'env["NC_PASS"] =',
            'env["CALDAV_URL"] =',
            'env["CALDAV_USERNAME"] =',
            'env["CALDAV_PASSWORD"] =',
            'env["SMTP_HOST"] =',
            'env["SMTP_PORT"] =',
            'env["SMTP_PASSWORD"] =',
            'env["IMAP_PASSWORD"] =',
            'env["KARAKEEP_BASE_URL"] =',
            'env["KARAKEEP_API_KEY"] =',
            'env["GITLAB_TOKEN"] =',
            'env["GITHUB_TOKEN"] =',
            'env["GITLAB_URL"] =',
            'env["GITHUB_URL"] =',
            'env["DEVELOPER_REPOS_DIR"] =',
        ]
        for needle in forbidden:
            assert needle not in text, (
                f"executor.py still contains hardcoded credential injection "
                f"({needle!r}); Phase 2 should have deleted it."
            )
