"""Tests for the moneyman skill (dual-mode: CLI subprocess or HTTP client)."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _empty_skills_dir(tmp_path):
    d = tmp_path / "_empty_skills"
    d.mkdir(exist_ok=True)
    return d


class TestMoneymanSkillManifest:
    """Test skill.toml is loaded correctly."""

    def test_load_skill(self, tmp_path):
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        assert "moneyman" in index
        meta = index["moneyman"]
        assert meta.cli is True
        assert "moneyman" in meta.resource_types
        assert "accounting" in meta.keywords

    def test_selected_with_moneyman_resource(self, tmp_path):
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="check my balances",
            source_type="talk",
            user_resource_types={"moneyman"},
            skill_index=index,
        )
        assert "moneyman" in selected

    def test_not_selected_without_resource_or_keyword(self, tmp_path):
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="hello there",
            source_type="talk",
            user_resource_types=set(),
            skill_index=index,
        )
        assert "moneyman" not in selected

    def test_moneyman_and_accounting_mutually_exclusive(self, tmp_path):
        """When both resource types are present, exclude_skills prevents both loading."""
        from istota.skills._loader import load_skill_index, select_skills

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        selected = select_skills(
            prompt="check my ledger balances",
            source_type="talk",
            user_resource_types={"moneyman", "ledger"},
            skill_index=index,
        )
        has_moneyman = "moneyman" in selected
        has_accounting = "accounting" in selected
        assert not (has_moneyman and has_accounting), "Both moneyman and accounting should not be selected together"

    def test_env_specs_include_cli_path(self, tmp_path):
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        meta = index["moneyman"]
        env_vars = {spec.var for spec in meta.env_specs}
        assert "MONEYMAN_CLI_PATH" in env_vars
        assert "MONEYMAN_USER" in env_vars
        assert "MONEYMAN_API_URL" in env_vars
        assert "MONEYMAN_API_KEY" in env_vars


class TestModeDetection:
    """Test _mode() transport selection."""

    def test_cli_mode_when_cli_path_set(self):
        from istota.skills.moneyman import _mode

        env = {"MONEYMAN_CLI_PATH": "/usr/bin/moneyman"}
        with patch.dict(os.environ, env, clear=True):
            assert _mode() == "cli"

    def test_http_mode_when_api_url_set(self):
        from istota.skills.moneyman import _mode

        env = {"MONEYMAN_API_URL": "http://localhost:8090"}
        with patch.dict(os.environ, env, clear=True):
            assert _mode() == "http"

    def test_cli_preferred_over_http(self):
        from istota.skills.moneyman import _mode

        env = {"MONEYMAN_CLI_PATH": "/usr/bin/moneyman", "MONEYMAN_API_URL": "http://localhost:8090"}
        with patch.dict(os.environ, env, clear=True):
            assert _mode() == "cli"

    def test_exits_when_neither_set(self):
        from istota.skills.moneyman import _mode

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit):
                _mode()


class TestCliMode:
    """Test CLI subprocess transport."""

    def test_run_cli_success(self):
        from istota.skills.moneyman import _run_cli

        mock_result = MagicMock()
        mock_result.stdout = '{"status": "ok", "data": [1, 2]}\n'
        mock_result.returncode = 0
        mock_result.stderr = ""

        env = {"MONEYMAN_CLI_PATH": "/usr/bin/moneyman", "MONEYMAN_USER": "stefan"}
        with patch.dict(os.environ, env, clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=mock_result) as mock_run:
                result = _run_cli(["list"])
                assert result == {"status": "ok", "data": [1, 2]}
                cmd = mock_run.call_args[0][0]
                assert cmd == ["/usr/bin/moneyman", "-u", "stefan", "list"]

    def test_run_cli_with_config(self):
        from istota.skills.moneyman import _run_cli

        mock_result = MagicMock()
        mock_result.stdout = '{"status": "ok"}\n'
        mock_result.returncode = 0
        mock_result.stderr = ""

        env = {
            "MONEYMAN_CLI_PATH": "/usr/bin/moneyman",
            "MONEYMAN_USER": "stefan",
            "MONEYMAN_CONFIG": "/etc/moneyman/config.toml",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=mock_result) as mock_run:
                _run_cli(["check"])
                cmd = mock_run.call_args[0][0]
                assert cmd == ["/usr/bin/moneyman", "-c", "/etc/moneyman/config.toml", "-u", "stefan", "check"]

    def test_run_cli_error(self):
        from istota.skills.moneyman import _run_cli

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 1
        mock_result.stderr = "No data_dir configured"

        env = {"MONEYMAN_CLI_PATH": "/usr/bin/moneyman"}
        with patch.dict(os.environ, env, clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=mock_result):
                result = _run_cli(["list"])
                assert result["status"] == "error"
                assert "No data_dir" in result["error"]

    def test_run_cli_not_found(self):
        from istota.skills.moneyman import _run_cli

        env = {"MONEYMAN_CLI_PATH": "/nonexistent/moneyman"}
        with patch.dict(os.environ, env, clear=True):
            with patch("istota.skills.moneyman.subprocess.run", side_effect=FileNotFoundError):
                result = _run_cli(["list"])
                assert result["status"] == "error"
                assert "not found" in result["error"]


class TestHttpMode:
    """Test HTTP client transport."""

    def test_http_client_sets_headers(self):
        from istota.skills.moneyman import _http_client

        env = {"MONEYMAN_API_URL": "http://localhost:8090", "MONEYMAN_API_KEY": "test-key", "MONEYMAN_USER": "stefan"}
        with patch.dict(os.environ, env, clear=True):
            client = _http_client()
            assert client.headers.get("X-API-Key") == "test-key"
            assert client.headers.get("X-User") == "stefan"
            client.close()

    def test_http_client_no_key(self):
        from istota.skills.moneyman import _http_client

        env = {"MONEYMAN_API_URL": "http://localhost:8090"}
        with patch.dict(os.environ, env, clear=True):
            client = _http_client()
            assert "X-API-Key" not in client.headers
            client.close()

    def test_handle_response_success(self):
        from istota.skills.moneyman import _handle_response

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "ok", "data": [1, 2, 3]}
        result = _handle_response(resp)
        assert result == {"status": "ok", "data": [1, 2, 3]}

    def test_handle_response_error(self):
        from istota.skills.moneyman import _handle_response

        resp = MagicMock()
        resp.status_code = 422
        resp.json.return_value = {"detail": "Invalid input"}
        with pytest.raises(SystemExit):
            _handle_response(resp)


class TestCommandsCli:
    """Test CLI mode command dispatch."""

    def _cli_env(self):
        return {"MONEYMAN_CLI_PATH": "/usr/bin/moneyman", "MONEYMAN_USER": "stefan"}

    def _mock_cli_run(self, data):
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(data) + "\n"
        mock_result.returncode = 0
        mock_result.stderr = ""
        return mock_result

    def test_cmd_list(self):
        from istota.skills.moneyman import main

        with patch.dict(os.environ, self._cli_env(), clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=self._mock_cli_run({"status": "ok"})) as m:
                main(["list"])
                cmd = m.call_args[0][0]
                assert cmd[-1] == "list"

    def test_cmd_work_list(self):
        from istota.skills.moneyman import main

        with patch.dict(os.environ, self._cli_env(), clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=self._mock_cli_run({"status": "ok"})) as m:
                main(["work", "list", "--client", "acme", "--period", "2026-02", "--uninvoiced"])
                cmd = m.call_args[0][0]
                assert "work" in cmd
                assert "list" in cmd
                assert "--client" in cmd
                assert "acme" in cmd
                assert "--uninvoiced" in cmd

    def test_cmd_work_add(self):
        from istota.skills.moneyman import main

        with patch.dict(os.environ, self._cli_env(), clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=self._mock_cli_run({"status": "ok", "id": 1})) as m:
                main(["work", "add", "--date", "2026-02-01", "--client", "acme", "--service", "dev", "--qty", "4"])
                cmd = m.call_args[0][0]
                assert "work" in cmd
                assert "add" in cmd
                assert "--date" in cmd

    def test_cmd_invoice_generate(self):
        from istota.skills.moneyman import main

        with patch.dict(os.environ, self._cli_env(), clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=self._mock_cli_run({"status": "ok"})) as m:
                main(["invoice", "generate", "--period", "2026-02", "--client", "acme", "--dry-run"])
                cmd = m.call_args[0][0]
                assert "invoice" in cmd
                assert "generate" in cmd
                assert "--dry-run" in cmd

    def test_cmd_invoice_void(self):
        from istota.skills.moneyman import main

        with patch.dict(os.environ, self._cli_env(), clear=True):
            with patch("istota.skills.moneyman.subprocess.run", return_value=self._mock_cli_run({"status": "ok"})) as m:
                main(["invoice", "void", "INV-000001", "--force"])
                cmd = m.call_args[0][0]
                assert "invoice" in cmd
                assert "void" in cmd
                assert "INV-000001" in cmd
                assert "--force" in cmd


class TestCommandsHttp:
    """Test HTTP mode command dispatch."""

    def _http_env(self):
        return {"MONEYMAN_API_URL": "http://localhost:8090", "MONEYMAN_API_KEY": "test-key"}

    def _mock_response(self, data, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        return resp

    def test_cmd_list(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "ledgers": [{"name": "Personal"}]}
        with patch.dict(os.environ, self._http_env(), clear=True):
            with patch("istota.skills.moneyman._http_client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["list"])
                client.get.assert_called_once_with("/api/ledgers")

    def test_cmd_check(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "errors": []}
        with patch.dict(os.environ, self._http_env(), clear=True):
            with patch("istota.skills.moneyman._http_client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["check", "--ledger", "Personal"])
                client.get.assert_called_once_with("/api/check", params={"ledger": "Personal"})

    def test_cmd_balances(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "balances": []}
        with patch.dict(os.environ, self._http_env(), clear=True):
            with patch("istota.skills.moneyman._http_client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["balances", "--ledger", "Personal", "--account", "Assets:Bank"])
                client.get.assert_called_once_with(
                    "/api/balances",
                    params={"ledger": "Personal", "account": "Assets:Bank"},
                )

    def test_cmd_work_list(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "entries": []}
        with patch.dict(os.environ, self._http_env(), clear=True):
            with patch("istota.skills.moneyman._http_client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["work", "list", "--client", "acme", "--period", "2026-02", "--uninvoiced"])
                client.get.assert_called_once_with(
                    "/api/work/",
                    params={"client": "acme", "period": "2026-02", "uninvoiced": True},
                )

    def test_cmd_work_add(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "id": 1}
        with patch.dict(os.environ, self._http_env(), clear=True):
            with patch("istota.skills.moneyman._http_client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main([
                    "work", "add",
                    "--date", "2026-02-01",
                    "--client", "acme",
                    "--service", "consulting",
                    "--qty", "4",
                    "--description", "Architecture review",
                ])
                client.post.assert_called_once_with(
                    "/api/work/",
                    json={
                        "date": "2026-02-01",
                        "client": "acme",
                        "service": "consulting",
                        "qty": 4.0,
                        "description": "Architecture review",
                    },
                )

    def test_cmd_invoice_void(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "entries_voided": 3}
        with patch.dict(os.environ, self._http_env(), clear=True):
            with patch("istota.skills.moneyman._http_client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["invoice", "void", "INV-000001", "--force", "--delete-pdf"])
                client.post.assert_called_once_with(
                    "/api/invoices/void",
                    json={"invoice_number": "INV-000001", "force": True, "delete_pdf": True},
                )

    def test_unknown_command_exits(self):
        from istota.skills.moneyman import main

        with pytest.raises(SystemExit):
            main(["nonexistent-command"])


class TestExecutorIntegration:
    """Test executor.py credential and network integration."""

    def test_moneyman_api_key_in_proxy_vars(self):
        from istota.executor import _PROXY_CREDENTIAL_VARS

        assert "MONEYMAN_API_KEY" in _PROXY_CREDENTIAL_VARS

    def test_moneyman_api_key_in_credential_skill_map(self):
        from istota.executor import _CREDENTIAL_SKILL_MAP

        assert "MONEYMAN_API_KEY" in _CREDENTIAL_SKILL_MAP
        assert "moneyman" in _CREDENTIAL_SKILL_MAP["MONEYMAN_API_KEY"]
