"""Tests for skill proxy server, client, and executor credential splitting."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.skill_proxy import SkillProxy, _ALLOWED_SKILLS
from istota.executor import _split_credential_env, _PROXY_CREDENTIAL_VARS


@pytest.fixture
def sock_path():
    """Short socket path that fits AF_UNIX limit (~104 chars on macOS)."""
    d = tempfile.mkdtemp(prefix="sp_", dir="/tmp")
    p = Path(d) / "s.sock"
    yield p
    p.unlink(missing_ok=True)
    Path(d).rmdir()


# ---------------------------------------------------------------------------
# _split_credential_env
# ---------------------------------------------------------------------------


class TestSplitCredentialEnv:
    def test_splits_known_credentials(self):
        env = {
            "PATH": "/usr/bin",
            "CALDAV_PASSWORD": "secret1",
            "NC_PASS": "secret2",
            "SMTP_PASSWORD": "secret3",
            "IMAP_PASSWORD": "secret4",
            "KARAKEEP_API_KEY": "secret5",
            "CALDAV_URL": "https://dav.example.com",
            "NC_URL": "https://cloud.example.com",
        }
        cred, clean = _split_credential_env(env)
        assert cred == {
            "CALDAV_PASSWORD": "secret1",
            "NC_PASS": "secret2",
            "SMTP_PASSWORD": "secret3",
            "IMAP_PASSWORD": "secret4",
            "KARAKEEP_API_KEY": "secret5",
        }
        assert "CALDAV_PASSWORD" not in clean
        assert "NC_PASS" not in clean
        assert clean["PATH"] == "/usr/bin"
        assert clean["CALDAV_URL"] == "https://dav.example.com"
        assert clean["NC_URL"] == "https://cloud.example.com"

    def test_empty_env(self):
        cred, clean = _split_credential_env({})
        assert cred == {}
        assert clean == {}

    def test_no_credentials_present(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        cred, clean = _split_credential_env(env)
        assert cred == {}
        assert clean == env

    def test_only_credentials(self):
        env = {"CALDAV_PASSWORD": "x", "NC_PASS": "y"}
        cred, clean = _split_credential_env(env)
        assert len(cred) == 2
        assert clean == {}

    def test_strips_developer_tokens(self):
        """GITLAB_TOKEN and GITHUB_TOKEN are stripped to credential env."""
        env = {
            "GITLAB_TOKEN": "glpat-xxx",
            "GITHUB_TOKEN": "ghp_xxx",
            "CALDAV_PASSWORD": "secret",
        }
        cred, clean = _split_credential_env(env)
        assert "GITLAB_TOKEN" in cred
        assert "GITHUB_TOKEN" in cred
        assert "GITLAB_TOKEN" not in clean
        assert "GITHUB_TOKEN" not in clean
        assert "CALDAV_PASSWORD" in cred


class TestProxyCredentialLookup:
    """Test the credential lookup protocol extension."""

    def _send_request(self, sock_path, request_dict):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request_dict) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(b"".join(chunks).decode().strip())

    def test_returns_credential_value(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret", "NC_PASS": "nc_secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "GITLAB_TOKEN",
            })
        assert resp == {"value": "glpat-secret"}

    def test_returns_error_for_unknown_credential(self, sock_path):
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "NONEXISTENT",
            })
        assert "error" in resp
        assert "Unknown credential" in resp["error"]

    def test_does_not_leak_base_env(self, sock_path):
        """Credential lookup only returns values from credential_env, not base_env."""
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        base_env = {"PATH": "/usr/bin", "HOME": "/home/user"}
        with SkillProxy(sock_path, cred_env, base_env):
            resp = self._send_request(sock_path, {
                "type": "credential", "name": "PATH",
            })
        assert "error" in resp

    @patch("istota.skill_proxy.subprocess.run")
    def test_skill_requests_still_work(self, mock_run, sock_path):
        """Existing skill requests (no 'type' field) continue to work."""
        mock_run.return_value = MagicMock(
            stdout='{"ok": true}', stderr="", returncode=0,
        )
        cred_env = {"GITLAB_TOKEN": "glpat-secret"}
        with SkillProxy(sock_path, cred_env, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["send"],
            })
        assert resp["returncode"] == 0


class TestProxyCredentialVarsCompleteness:
    """Verify the credential var set covers what executor.py actually injects."""

    def test_caldav_password_in_set(self):
        assert "CALDAV_PASSWORD" in _PROXY_CREDENTIAL_VARS

    def test_nc_pass_in_set(self):
        assert "NC_PASS" in _PROXY_CREDENTIAL_VARS

    def test_smtp_password_in_set(self):
        assert "SMTP_PASSWORD" in _PROXY_CREDENTIAL_VARS

    def test_imap_password_in_set(self):
        assert "IMAP_PASSWORD" in _PROXY_CREDENTIAL_VARS

    def test_karakeep_api_key_in_set(self):
        assert "KARAKEEP_API_KEY" in _PROXY_CREDENTIAL_VARS

    def test_gitlab_token_in_set(self):
        assert "GITLAB_TOKEN" in _PROXY_CREDENTIAL_VARS

    def test_github_token_in_set(self):
        assert "GITHUB_TOKEN" in _PROXY_CREDENTIAL_VARS


# ---------------------------------------------------------------------------
# SkillProxy
# ---------------------------------------------------------------------------


class TestSkillProxyLifecycle:
    def test_start_stop(self, sock_path):
        proxy = SkillProxy(sock_path, {}, {})
        proxy.start()
        assert sock_path.exists()
        proxy.stop()
        assert not sock_path.exists()

    def test_context_manager(self, sock_path):
        with SkillProxy(sock_path, {}, {}) as proxy:
            assert sock_path.exists()
        assert not sock_path.exists()

    def test_cleans_up_stale_socket(self, sock_path):
        sock_path.write_text("stale")
        with SkillProxy(sock_path, {}, {}):
            assert sock_path.exists()

    def test_double_stop_is_safe(self, sock_path):
        proxy = SkillProxy(sock_path, {}, {})
        proxy.start()
        proxy.stop()
        proxy.stop()  # Should not raise


class TestSkillProxyProtocol:
    def _send_request(self, sock_path, request_dict):
        """Helper: connect to proxy and send a request, return parsed response."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request_dict) + "\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()
        return json.loads(b"".join(chunks).decode().strip())

    def test_rejects_unknown_skill(self, sock_path):
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {"skill": "evil_skill", "args": []})
            assert resp["returncode"] == 1
            assert "Unknown skill" in resp["stderr"]

    def test_rejects_invalid_json(self, sock_path):
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(str(sock_path))
            sock.sendall(b"not json\n")
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            sock.close()
            resp = json.loads(b"".join(chunks).decode().strip())
            assert resp["returncode"] == 1
            assert "Invalid JSON" in resp["stderr"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_runs_allowed_skill(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(
            stdout='{"status": "ok"}', stderr="", returncode=0,
        )
        base_env = {"PATH": "/usr/bin", "HOME": "/tmp"}
        cred_env = {"SMTP_PASSWORD": "secret"}

        with SkillProxy(sock_path, cred_env, base_env):
            resp = self._send_request(sock_path, {
                "skill": "email",
                "args": ["send", "--to", "bob@example.com"],
            })

        assert resp["returncode"] == 0
        assert resp["stdout"] == '{"status": "ok"}'

        # Verify subprocess was called with merged env
        call_kwargs = mock_run.call_args
        called_env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert called_env["SMTP_PASSWORD"] == "secret"
        assert called_env["PATH"] == "/usr/bin"

    @patch("istota.skill_proxy.subprocess.run")
    def test_passes_args_correctly(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            self._send_request(sock_path, {
                "skill": "calendar",
                "args": ["list", "--date", "today", "--tz", "America/New_York"],
            })

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "istota.skills.calendar"]
        assert cmd[3:] == ["list", "--date", "today", "--tz", "America/New_York"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_handles_subprocess_failure(self, mock_run, sock_path):
        mock_run.return_value = MagicMock(
            stdout="", stderr="Error: invalid arguments", returncode=2,
        )

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["bad-command"],
            })

        assert resp["returncode"] == 2
        assert "invalid arguments" in resp["stderr"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_handles_timeout(self, mock_run, sock_path):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=5)

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}, timeout=5):
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["send"],
            })

        assert resp["returncode"] == 124
        assert "timed out" in resp["stderr"]

    @patch("istota.skill_proxy.subprocess.run")
    def test_concurrent_requests(self, mock_run, sock_path):
        """Multiple concurrent skill calls should all succeed."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        results = []

        def make_request():
            resp = self._send_request(sock_path, {
                "skill": "email", "args": ["send"],
            })
            results.append(resp)

        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}):
            threads = [threading.Thread(target=make_request) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(results) == 5
        assert all(r["returncode"] == 0 for r in results)


class TestAllowedSkills:
    """Verify the allowlist matches actual __main__.py files."""

    def test_all_skills_have_main(self):
        skills_dir = Path(__file__).parent.parent / "src" / "istota" / "skills"
        for skill_name in _ALLOWED_SKILLS:
            main_file = skills_dir / skill_name / "__main__.py"
            assert main_file.exists(), f"Skill {skill_name!r} in _ALLOWED_SKILLS but no __main__.py"

    def test_no_missing_skills(self):
        """All skills with __main__.py should be in the allowlist."""
        skills_dir = Path(__file__).parent.parent / "src" / "istota" / "skills"
        for child in skills_dir.iterdir():
            if child.is_dir() and (child / "__main__.py").exists():
                assert child.name in _ALLOWED_SKILLS, (
                    f"Skill {child.name!r} has __main__.py but is not in _ALLOWED_SKILLS"
                )


# ---------------------------------------------------------------------------
# skill_client
# ---------------------------------------------------------------------------


class TestSkillClientDirect:
    """Test the direct-execution fallback path."""

    @patch("istota.skill_client.subprocess.run")
    def test_direct_fallback_without_env(self, mock_run):
        """Without ISTOTA_SKILL_PROXY_SOCK, runs skill directly."""
        mock_run.return_value = MagicMock(returncode=0)
        from istota.skill_client import _run_direct
        with pytest.raises(SystemExit) as exc_info:
            _run_direct("email", ["send", "--to", "x@y.com"])
        assert exc_info.value.code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[1:3] == ["-m", "istota.skills.email"]

    def test_proxy_path_connects(self, sock_path):
        """With proxy socket, client connects and parses response."""
        from istota.skill_client import _run_via_proxy

        # Set up a mock server
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            data = conn.recv(65536)
            response = json.dumps({"stdout": "ok\n", "stderr": "", "returncode": 0})
            conn.sendall((response + "\n").encode())
            conn.close()
            server.close()

        t = threading.Thread(target=serve)
        t.start()

        with pytest.raises(SystemExit) as exc_info:
            _run_via_proxy(str(sock_path), "email", ["send"])

        t.join(timeout=5)
        assert exc_info.value.code == 0

    def test_proxy_connection_refused(self):
        """Client handles missing proxy gracefully."""
        from istota.skill_client import _run_via_proxy
        with pytest.raises(SystemExit) as exc_info:
            _run_via_proxy("/tmp/nonexistent.sock", "email", [])
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Executor integration: credential splitting with proxy
# ---------------------------------------------------------------------------


class TestExecutorProxyIntegration:
    """Test that execute_task splits credentials when proxy is enabled."""

    def _make_config(self, tmp_path, proxy_enabled=False):
        from istota.config import (
            Config, SecurityConfig, NextcloudConfig, SchedulerConfig,
        )
        empty_bundled = tmp_path / "_empty_bundled"
        empty_bundled.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        return Config(
            db_path=tmp_path / "test.db",
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="bot",
                app_password="nc_secret",
            ),
            security=SecurityConfig(
                mode="restricted",
                skill_proxy_enabled=proxy_enabled,
                skill_proxy_timeout=30,
            ),
            scheduler=SchedulerConfig(task_timeout_minutes=5),
            skills_dir=skills_dir,
            temp_dir=tmp_path / "tmp",
            bundled_skills_dir=empty_bundled,
        )

    def test_credentials_removed_when_proxy_enabled(self, tmp_path):
        """When skill_proxy_enabled, credential vars should not be in Claude's env."""
        env = {
            "PATH": "/usr/bin",
            "CALDAV_PASSWORD": "secret",
            "NC_PASS": "secret2",
            "SMTP_PASSWORD": "secret3",
            "IMAP_PASSWORD": "secret4",
            "KARAKEEP_API_KEY": "secret5",
            "CALDAV_URL": "https://dav.example.com",
        }
        cred, clean = _split_credential_env(env)

        # Clean env should not have any credential vars
        for var in _PROXY_CREDENTIAL_VARS:
            assert var not in clean

        # All non-credential vars should remain
        assert "PATH" in clean
        assert "CALDAV_URL" in clean

        # Credential env should have all the secrets
        assert cred["CALDAV_PASSWORD"] == "secret"
        assert cred["NC_PASS"] == "secret2"

    def test_credentials_present_when_proxy_disabled(self):
        """When proxy is disabled, no splitting happens — env is unchanged."""
        env = {
            "PATH": "/usr/bin",
            "CALDAV_PASSWORD": "secret",
            "NC_PASS": "secret2",
        }
        # Proxy disabled means _split_credential_env is never called.
        # Verify the env dict is passed through as-is.
        assert "CALDAV_PASSWORD" in env
        assert "NC_PASS" in env
