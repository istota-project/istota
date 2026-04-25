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
        """The skill resolves UserContext in-process; only MONEY_USER is declarative."""
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        meta = index["money"]
        env_vars = {spec.var for spec in meta.env_specs}
        assert env_vars == {"MONEY_USER"}


class TestRunInProcess:
    """The _run helper resolves UserContext in-process and invokes money.cli.cli."""

    def _patch_resolver(self, user_ctx=None):
        """Patch load_config + resolve_for_user to return a UserContext stub."""
        from contextlib import ExitStack
        from unittest.mock import patch

        stack = ExitStack()
        stack.enter_context(patch("istota.config.load_config", return_value=MagicMock()))
        stack.enter_context(patch(
            "istota.money.resolve_for_user",
            return_value=user_ctx or MagicMock(),
        ))
        return stack

    def test_run_returns_parsed_json(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = '{"status": "ok", "data": [1, 2]}\n'

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            result = _run(["list"])

        assert result == {"status": "ok", "data": [1, 2]}
        invoke_args = MockRunner.return_value.invoke.call_args
        passed_args = invoke_args[0][1]
        # User key flows through as -u
        assert passed_args[:2] == ["-u", "alice"]
        assert passed_args[-1] == "list"
        # Pre-built Context injected via obj=
        assert "obj" in invoke_args.kwargs

    def test_run_errors_when_user_not_set(self):
        from istota.skills.money import _run

        with patch.dict(os.environ, {}, clear=True):
            result = _run(["list"])

        assert result["status"] == "error"
        assert "MONEY_USER" in result["error"]

    def test_run_errors_when_user_not_resolved(self):
        from istota.skills.money import _run
        from istota.money import UserNotFoundError

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             patch("istota.config.load_config", return_value=MagicMock()), \
             patch(
                 "istota.money.resolve_for_user",
                 side_effect=UserNotFoundError("no money for alice"),
             ):
            result = _run(["list"])

        assert result["status"] == "error"
        assert "no money for alice" in result["error"]

    def test_run_returns_error_on_nonzero_exit(self):
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 2
        fake_result.output = "boom\n"

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
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

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
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

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            result = _run(["list"])

        assert result["status"] == "error"
        assert "invalid JSON" in result["error"]

    def test_run_threads_user_secrets_into_context(self):
        """Without this, scheduler-driven sync-monarch runs with no credentials."""
        from istota.skills.money import _run

        fake_result = MagicMock()
        fake_result.exception = None
        fake_result.exit_code = 0
        fake_result.output = '{"status": "ok"}'

        secrets = {"monarch": {"session_token": "tok-abc"}}

        with patch.dict(os.environ, {"MONEY_USER": "alice"}, clear=True), \
             self._patch_resolver(), \
             patch("istota.money.load_user_secrets", return_value=secrets), \
             patch("click.testing.CliRunner") as MockRunner:
            MockRunner.return_value.invoke.return_value = fake_result
            _run(["sync-monarch"])

        invoke_kwargs = MockRunner.return_value.invoke.call_args.kwargs
        passed_obj = invoke_kwargs["obj"]
        assert passed_obj.secrets == secrets


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


