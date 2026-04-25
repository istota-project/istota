"""Tests for the money skill (in-process facade over the vendored money package)."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _empty_skills_dir(tmp_path):
    d = tmp_path / "_empty_skills"
    d.mkdir(exist_ok=True)
    return d


class TestMoneySkillManifest:
    """skill.md is loaded with the new name and resource types."""

    def test_load_skill(self, tmp_path):
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        assert "money" in index
        meta = index["money"]
        assert meta.cli is True
        # Both new and legacy resource types are accepted
        assert "money" in meta.resource_types
        assert "moneyman" in meta.resource_types
        assert "accounting" in meta.keywords

    def test_selected_with_money_resource(self, tmp_path):
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="check my balances",
            source_type="talk",
            user_resource_types={"money"},
            skill_index=index,
        )
        assert "money" in selected

    def test_selected_with_legacy_moneyman_resource(self, tmp_path):
        """Existing user configs that still declare type=moneyman keep working."""
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="check my balances",
            source_type="talk",
            user_resource_types={"moneyman"},
            skill_index=index,
        )
        assert "money" in selected

    def test_not_selected_without_resource_or_keyword(self, tmp_path):
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="hello there",
            source_type="talk",
            user_resource_types=set(),
            skill_index=index,
        )
        assert "money" not in selected

    def test_env_specs(self, tmp_path):
        """MONEY_CONFIG (legacy) and MONEY_USER are declarative;
        MONEY_WORKSPACE is emitted by the setup_env hook."""
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        meta = index["money"]
        env_vars = {spec.var for spec in meta.env_specs}
        assert env_vars == {"MONEY_CONFIG", "MONEY_USER"}

        # MONEY_CONFIG must accept both the new and legacy resource types
        money_config_spec = next(s for s in meta.env_specs if s.var == "MONEY_CONFIG")
        accepted = set(money_config_spec.resource_types) | (
            {money_config_spec.resource_type} if money_config_spec.resource_type else set()
        )
        assert {"money", "moneyman"} <= accepted


class TestRunInProcess:
    """The _run helper invokes money.cli.cli through Click's CliRunner."""

    def test_run_returns_parsed_json(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = '{"status": "ok", "data": [1, 2]}\n'

        env = {"MONEY_CONFIG": "/etc/money/config.toml", "MONEY_USER": "alice"}
        with patch.dict(os.environ, env, clear=True):
            with patch("click.testing.CliRunner") as MockRunner:
                MockRunner.return_value.invoke.return_value = fake_result
                result = _run(["list"])

        assert result == {"status": "ok", "data": [1, 2]}
        # Threaded through -c and -u
        invoke_args = MockRunner.return_value.invoke.call_args
        passed_args = invoke_args[0][1]
        assert passed_args[:4] == ["-c", "/etc/money/config.toml", "-u", "alice"]
        assert passed_args[-1] == "list"

    def test_run_returns_error_on_nonzero_exit(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 2
        fake_result.output = "boom\n"

        with patch.dict(os.environ, {}, clear=True):
            with patch("click.testing.CliRunner") as MockRunner:
                MockRunner.return_value.invoke.return_value = fake_result
                result = _run(["list"])

        assert result["status"] == "error"
        assert "boom" in result["error"]

    def test_run_returns_error_on_exception(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = RuntimeError("kaboom")
        fake_result.exit_code = 1
        fake_result.output = ""

        with patch.dict(os.environ, {}, clear=True):
            with patch("click.testing.CliRunner") as MockRunner:
                MockRunner.return_value.invoke.return_value = fake_result
                result = _run(["list"])

        assert result["status"] == "error"
        assert "kaboom" in result["error"]

    def test_run_returns_error_on_invalid_json(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = "not json"

        with patch.dict(os.environ, {}, clear=True):
            with patch("click.testing.CliRunner") as MockRunner:
                MockRunner.return_value.invoke.return_value = fake_result
                result = _run(["list"])

        assert result["status"] == "error"
        assert "invalid JSON" in result["error"]


class TestCommandDispatch:
    """End-to-end: each cmd_X composes args and routes through _run."""

    @pytest.fixture
    def captured(self):
        captured = []

        def fake_run(args):
            captured.append(args)
            return {"status": "ok"}

        with patch("istota.skills.money._run", side_effect=fake_run):
            yield captured

    def test_list(self, captured):
        from istota.skills.money import main

        main(["list"])
        assert captured[-1] == ["list"]

    def test_balances_with_filters(self, captured):
        from istota.skills.money import main

        main(["balances", "--ledger", "personal", "--account", "Expenses:Food"])
        args = captured[-1]
        assert args[0] == "balances"
        assert "--ledger" in args and "personal" in args
        assert "--account" in args and "Expenses:Food" in args

    def test_invoice_void_with_force(self, captured):
        from istota.skills.money import main

        main(["invoice", "void", "INV-000001", "--force", "--delete-pdf"])
        args = captured[-1]
        assert args[:3] == ["invoice", "void", "INV-000001"]
        assert "--force" in args
        assert "--delete-pdf" in args

    def test_work_add(self, captured):
        from istota.skills.money import main

        main([
            "work", "add",
            "--date", "2026-02-01",
            "--client", "acme",
            "--service", "dev",
            "--qty", "4",
        ])
        args = captured[-1]
        assert args[:2] == ["work", "add"]
        assert "--date" in args and "2026-02-01" in args
        # --qty is parsed as float by argparse; str(4.0) -> "4.0"
        assert "--qty" in args and "4.0" in args

    def test_unknown_command_exits(self):
        from istota.skills.money import main

        with pytest.raises(SystemExit):
            main(["nonexistent-command"])


class TestExecutorIntegration:
    """The in-process skill needs neither an API-key proxy var nor a network host."""

    def test_no_money_api_key_in_proxy_vars(self):
        from istota.executor import _PROXY_CREDENTIAL_VARS

        # Legacy out-of-process names that used to live here.
        assert "MONEYMAN_API_KEY" not in _PROXY_CREDENTIAL_VARS
        assert "MONEY_API_KEY" not in _PROXY_CREDENTIAL_VARS

    def test_no_money_api_key_in_credential_skill_map(self):
        from istota.executor import _CREDENTIAL_SKILL_MAP

        assert "MONEYMAN_API_KEY" not in _CREDENTIAL_SKILL_MAP
        assert "MONEY_API_KEY" not in _CREDENTIAL_SKILL_MAP


class TestSetupEnvHook:
    """The money skill's setup_env() hook injects MONEY_WORKSPACE for workspace mode.

    The web-side loader (istota.web_app._install_money_loader) supports both
    legacy mode (extra.config_path on the resource) and workspace mode
    (synthesize from {nextcloud_mount}/Users/{user_id}/{bot_dir}). The CLI
    side mirrors that: when no config_path is set, setup_env emits
    MONEY_WORKSPACE so money.cli.load_context() can synthesize a UserContext.
    """

    def _make_ctx(self, tmp_path, *, resources=None, mount=True, user_id="stefan"):
        from dataclasses import dataclass, field
        from unittest.mock import MagicMock

        from istota.skills._env import EnvContext

        @dataclass
        class _Cfg:
            nextcloud_mount_path: object = None
            bot_dir_name: str = "istota"

        @dataclass
        class _RC:
            type: str
            extra: dict = field(default_factory=dict)
            path: str = ""
            name: str = ""
            permissions: str = "read"
            base_url: str = ""
            api_key: str = ""

        @dataclass
        class _UC:
            resources: list = field(default_factory=list)

        cfg = _Cfg()
        if mount:
            mount_path = tmp_path / "mount"
            mount_path.mkdir()
            cfg.nextcloud_mount_path = mount_path

        return EnvContext(
            config=cfg,
            task=MagicMock(id=1, user_id=user_id, conversation_token=None),
            user_resources=[],
            user_config=_UC(resources=resources or []),
            user_temp_dir=tmp_path / "tmp",
            is_admin=True,
        ), cfg, _RC

    def test_emits_money_workspace_when_resource_has_no_config_path(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, cfg, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(RC(type="money"))

        env = setup_env(ctx)
        expected = cfg.nextcloud_mount_path / "Users" / "stefan" / "istota"
        assert env["MONEY_WORKSPACE"] == str(expected)

    def test_emits_money_workspace_for_legacy_moneyman_type(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, cfg, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(RC(type="moneyman"))

        env = setup_env(ctx)
        assert "MONEY_WORKSPACE" in env

    def test_skips_when_no_money_resource(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, _, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(RC(type="karakeep"))

        env = setup_env(ctx)
        assert env == {}

    def test_skips_when_resource_has_config_path(self, tmp_path):
        """Legacy mode: config_path is set, so MONEY_CONFIG covers it; no MONEY_WORKSPACE."""
        from istota.skills.money import setup_env

        ctx, _, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(
            RC(type="money", extra={"config_path": "/etc/money/config.toml"}),
        )

        env = setup_env(ctx)
        assert "MONEY_WORKSPACE" not in env

    def test_skips_when_no_nextcloud_mount(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, _, RC = self._make_ctx(tmp_path, mount=False)
        ctx.user_config.resources.append(RC(type="money"))

        env = setup_env(ctx)
        assert "MONEY_WORKSPACE" not in env

    def test_emits_data_dir_override(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, _, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(
            RC(type="money", extra={"data_dir": "/srv/money/stefan"}),
        )

        env = setup_env(ctx)
        assert env["MONEY_DATA_DIR"] == "/srv/money/stefan"

    def test_emits_config_dir_override(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, _, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(
            RC(type="money", extra={"config_dir": "/srv/money/stefan/config"}),
        )

        env = setup_env(ctx)
        assert env["MONEY_CONFIG_DIR"] == "/srv/money/stefan/config"

    def test_emits_ledgers_as_json(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, _, RC = self._make_ctx(tmp_path)
        ctx.user_config.resources.append(
            RC(type="money", extra={"ledgers": ["personal", "business"]}),
        )

        env = setup_env(ctx)
        assert json.loads(env["MONEY_LEDGERS"]) == ["personal", "business"]

    def test_skips_user_config_none(self, tmp_path):
        from istota.skills.money import setup_env

        ctx, _, _ = self._make_ctx(tmp_path)
        ctx.user_config = None

        env = setup_env(ctx)
        assert env == {}
