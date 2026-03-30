"""Tests for the moneyman skill (HTTP client for Moneyman API)."""

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

    def test_excludes_accounting(self, tmp_path):
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        meta = index["moneyman"]
        assert "accounting" in meta.exclude_skills

    def test_accounting_excludes_moneyman(self, tmp_path):
        from istota.skills._loader import load_skill_index

        index = load_skill_index(skills_dir=_empty_skills_dir(tmp_path))
        meta = index["accounting"]
        assert "moneyman" in meta.exclude_skills

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


class TestMoneymanClient:
    """Test the HTTP client functions."""

    def test_client_requires_api_url(self):
        """_client() exits when MONEYMAN_API_URL is not set."""
        from istota.skills.moneyman import _client

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit):
                _client()

    def test_client_sets_auth_header(self):
        """_client() sets X-API-Key header when MONEYMAN_API_KEY is set."""
        from istota.skills.moneyman import _client

        env = {"MONEYMAN_API_URL": "http://localhost:8090", "MONEYMAN_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            client = _client()
            assert client.headers.get("X-API-Key") == "test-key"
            client.close()

    def test_client_no_auth_header_without_key(self):
        """_client() omits auth header when no key configured."""
        from istota.skills.moneyman import _client

        env = {"MONEYMAN_API_URL": "http://localhost:8090"}
        with patch.dict(os.environ, env, clear=True):
            client = _client()
            assert "X-API-Key" not in client.headers
            client.close()

    def test_client_prefers_socket(self):
        """_client() uses Unix socket transport when MONEYMAN_API_SOCKET is set."""
        from istota.skills.moneyman import _client
        import httpx

        env = {
            "MONEYMAN_API_SOCKET": "/tmp/test.sock",
            "MONEYMAN_API_URL": "http://localhost:8090",
            "MONEYMAN_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True):
            client = _client()
            # When using UDS, base_url is http://localhost
            assert str(client.base_url) == "http://localhost"
            transport = client._transport
            assert isinstance(transport, httpx.HTTPTransport)
            client.close()

    def test_client_socket_without_url(self):
        """_client() works with only socket path, no URL."""
        from istota.skills.moneyman import _client

        env = {"MONEYMAN_API_SOCKET": "/tmp/test.sock", "MONEYMAN_API_KEY": "key"}
        with patch.dict(os.environ, env, clear=True):
            client = _client()
            assert str(client.base_url) == "http://localhost"
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


class TestMoneymanCommands:
    """Test CLI command dispatch and API calls."""

    def _mock_env(self):
        return {"MONEYMAN_API_URL": "http://localhost:8090", "MONEYMAN_API_KEY": "test-key"}

    def _mock_response(self, data, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        return resp

    def test_cmd_list_ledgers(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "ledgers": [{"name": "Personal"}]}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["list"])
                client.get.assert_called_once_with("/api/ledgers")

    def test_cmd_check(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "errors": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["check", "--ledger", "Personal"])
                client.get.assert_called_once_with("/api/check", params={"ledger": "Personal"})

    def test_cmd_balances(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "balances": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["balances", "--ledger", "Personal", "--account", "Assets:Bank"])
                client.get.assert_called_once_with(
                    "/api/balances",
                    params={"ledger": "Personal", "account": "Assets:Bank"},
                )

    def test_cmd_balances_no_filters(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "balances": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["balances"])
                client.get.assert_called_once_with("/api/balances", params={})

    def test_cmd_query(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "rows": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["query", "SELECT date, narration FROM entries", "--ledger", "Personal"])
                client.post.assert_called_once_with(
                    "/api/query",
                    json={"bql": "SELECT date, narration FROM entries", "ledger": "Personal"},
                )

    def test_cmd_report(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "report": {}}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["report", "income-statement", "--year", "2026", "--ledger", "Personal"])
                client.get.assert_called_once_with(
                    "/api/reports/income-statement",
                    params={"year": 2026, "ledger": "Personal"},
                )

    def test_cmd_lots(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "lots": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["lots", "VTI", "--ledger", "Trading"])
                client.get.assert_called_once_with(
                    "/api/lots/VTI",
                    params={"ledger": "Trading"},
                )

    def test_cmd_wash_sales(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "violations": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["wash-sales", "--year", "2025", "--ledger", "Trading"])
                client.get.assert_called_once_with(
                    "/api/wash-sales",
                    params={"year": 2025, "ledger": "Trading"},
                )

    def test_cmd_add_transaction(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok"}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main([
                    "add-transaction",
                    "--date", "2026-02-01",
                    "--payee", "Whole Foods",
                    "--narration", "Groceries",
                    "--debit", "Expenses:Food",
                    "--credit", "Assets:Bank:Checking",
                    "--amount", "85.50",
                    "--ledger", "Personal",
                ])
                client.post.assert_called_once_with(
                    "/api/transactions",
                    json={
                        "date": "2026-02-01",
                        "payee": "Whole Foods",
                        "narration": "Groceries",
                        "debit": "Expenses:Food",
                        "credit": "Assets:Bank:Checking",
                        "amount": 85.50,
                        "currency": "USD",
                        "ledger": "Personal",
                    },
                )

    def test_cmd_sync_monarch(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "synced": 5}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["sync-monarch", "--ledger", "Personal", "--dry-run"])
                client.post.assert_called_once_with(
                    "/api/sync/monarch",
                    json={"ledger": "Personal", "dry_run": True},
                )

    def test_cmd_import_csv(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "imported": 10}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main([
                    "import-csv",
                    "/path/to/export.csv",
                    "--account", "Assets:Bank:Checking",
                    "--ledger", "Personal",
                ])
                client.post.assert_called_once()
                call_args = client.post.call_args
                assert call_args[0][0] == "/api/import/csv"
                body = call_args[1]["json"]
                assert body["file_path"] == "/path/to/export.csv"
                assert body["account"] == "Assets:Bank:Checking"
                assert body["ledger"] == "Personal"

    def test_cmd_invoice_generate(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "invoices": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["invoice", "generate", "--period", "2026-02", "--client", "acme"])
                client.post.assert_called_once_with(
                    "/api/invoices/generate",
                    json={"period": "2026-02", "client": "acme", "dry_run": False},
                )

    def test_cmd_invoice_list(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "invoices": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["invoice", "list", "--client", "acme", "--all"])
                client.get.assert_called_once_with(
                    "/api/invoices",
                    params={"client": "acme", "all": True},
                )

    def test_cmd_invoice_paid(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok"}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["invoice", "paid", "INV-000001", "--date", "2026-02-15", "--bank", "Assets:Bank:Savings"])
                client.post.assert_called_once_with(
                    "/api/invoices/INV-000001/paid",
                    json={"date": "2026-02-15", "bank": "Assets:Bank:Savings", "no_post": False},
                )

    def test_cmd_invoice_create(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "invoice_number": "INV-000001"}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["invoice", "create", "acme", "--service", "consulting", "--qty", "40"])
                client.post.assert_called_once_with(
                    "/api/invoices",
                    json={"client": "acme", "service": "consulting", "qty": 40.0},
                )

    def test_cmd_invoice_create_with_items(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "invoice_number": "INV-000001"}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.post.return_value = self._mock_response(response_data)
                main(["invoice", "create", "acme", "--item", "Travel expenses 340.50"])
                client.post.assert_called_once_with(
                    "/api/invoices",
                    json={"client": "acme", "items": [{"description": "Travel expenses", "amount": 340.50}]},
                )

    def test_cmd_work_list(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "entries": []}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.get.return_value = self._mock_response(response_data)
                main(["work", "list", "--client", "acme", "--period", "2026-02", "--uninvoiced"])
                client.get.assert_called_once_with(
                    "/api/work",
                    params={"client": "acme", "period": "2026-02", "uninvoiced": True},
                )

    def test_cmd_work_add(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok", "id": 1}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
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
                    "/api/work",
                    json={
                        "date": "2026-02-01",
                        "client": "acme",
                        "service": "consulting",
                        "qty": 4.0,
                        "description": "Architecture review",
                    },
                )

    def test_cmd_work_update(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok"}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.put.return_value = self._mock_response(response_data)
                main(["work", "update", "5", "--qty", "8", "--description", "Updated"])
                client.put.assert_called_once_with(
                    "/api/work/5",
                    json={"qty": 8.0, "description": "Updated"},
                )

    def test_cmd_work_remove(self):
        from istota.skills.moneyman import main

        response_data = {"status": "ok"}
        with patch.dict(os.environ, self._mock_env(), clear=True):
            with patch("istota.skills.moneyman._client") as mock_client_fn:
                client = MagicMock()
                mock_client_fn.return_value.__enter__ = MagicMock(return_value=client)
                mock_client_fn.return_value.__exit__ = MagicMock(return_value=False)
                client.delete.return_value = self._mock_response(response_data)
                main(["work", "remove", "3"])
                client.delete.assert_called_once_with("/api/work/3")

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
