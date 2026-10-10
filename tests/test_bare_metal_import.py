"""`istota migrate import-bare-metal`: a bare-metal install's state in the container layout.

The source is what `host/export-bare-metal.sh` writes, staged in a temporary
directory: the role-rendered `config.toml` (a frozen render of the role's own
template, `tests/fixtures/bare_metal/role-config.toml`), the two secrets env
files, the admins file, a `db_backup` snapshot of a real `init_db` database and
one module database, and, for the repository cases, a real bare clone with a
worktree cut at the old path.

The target is a directory standing in for `/data` (`--data-dir`), the config
file and the stack's `secrets/` directory, in the shape `istota setup` leaves
them. Every value is synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest

from istota import apply as apply_module
from istota import db
from istota.maintenance import bare_metal_import as bmi
from istota.setup_wizard import SECRET_NAMES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bare_metal" / "role-config.toml"

OLD_HOME = "/srv/app/istota"
OLD_MOUNT = "/srv/mount/nextcloud/content"
SECRET_KEY = "a" * 64
WEB_TOKEN_KEY = "b" * 64

GIT_ISOLATION = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

SECRETS_ENV = "\n".join([
    "# Istota secrets - managed by Ansible",
    "ISTOTA_NEXTCLOUD_APP_PASSWORD=nc-app-password",
    "ISTOTA_EMAIL_IMAP_PASSWORD=imap-password",
    "ISTOTA_DEVELOPER_GITLAB_TOKEN=gitlab-token",
    "ISTOTA_WEB_SESSION_SECRET_KEY=session-key",
    "CLAUDE_CODE_OAUTH_TOKEN=claude-token",
    f"ISTOTA_SECRET_KEY={SECRET_KEY}",
    "",
])


def _old_config() -> dict:
    return tomllib.loads(FIXTURE.read_text())


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def _framework_db(path: Path, *, repos_dir: str = f"{OLD_HOME}/repos") -> None:
    """A real schema with rows in every column the import reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, status, source_type, user_id, prompt, attachments) "
            "VALUES (1, 'completed', 'web', 'alice', ?, ?)",
            (
                f"look at {OLD_MOUNT}/Users/alice/inbox/a.pdf",
                json.dumps([
                    f"{OLD_MOUNT}/Users/alice/inbox/a.pdf",
                    f"{OLD_MOUNT}-old/Users/alice/b.pdf",
                    "/opt/elsewhere/c.pdf",
                ]),
            ),
        )
        conn.execute(
            "INSERT INTO tasks (id, status, source_type, user_id, prompt, attachments) "
            "VALUES (2, 'completed', 'talk', 'alice', 'hi', NULL)"
        )
        conn.execute(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, "
            "content, content_hash, metadata_json) VALUES (?, ?, ?, 0, ?, ?, ?)",
            (
                "alice", "memory_file", f"{OLD_MOUNT}/Users/alice/memories/2026-10-01.md",
                "one", "h1",
                json.dumps({"file_path": f"{OLD_MOUNT}/Users/alice/memories/2026-10-01.md"}),
            ),
        )
        conn.execute(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, "
            "content, content_hash, metadata_json) VALUES (?, ?, ?, 0, ?, ?, ?)",
            ("alice", "conversation", "12", "two", "h2", json.dumps({"task_id": "12"})),
        )
        conn.execute(
            "INSERT INTO istota_kv (user_id, namespace, key, value) VALUES (?, ?, ?, ?)",
            (
                "alice", "corpus", "history",
                json.dumps({"seen": [f"{OLD_MOUNT}/Users/alice/notes/x.md"], "count": 1}),
            ),
        )
        conn.execute(
            "INSERT INTO istota_kv (user_id, namespace, key, value) VALUES (?, ?, ?, ?)",
            (
                "alice", "system_check", "last_run",
                json.dumps({"path": f"{OLD_HOME}/.claude/.credentials.json"}),
            ),
        )
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, command) "
            "VALUES ('alice', 'nightly', '0 3 * * *', ?)",
            (f"{OLD_HOME}/istota/.venv/bin/python -m something",),
        )
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
            "VALUES ('alice', 'morning', '0 7 * * *', 'say hello')"
        )
        conn.execute(
            "INSERT INTO briefing_configs (user_id, name, cron_expression, "
            "conversation_token, components) VALUES ('alice', 'morning', '0 7 * * *', 'tok', '{}')"
        )
        conn.execute(
            "INSERT INTO user_resources (user_id, resource_type, resource_path, display_name) "
            "VALUES ('alice', 'folder', '/Users/alice/projects', 'Projects')"
        )
        # User-created tables a script keeps in the framework database.
        conn.execute("CREATE TABLE note_resurface_configs (user_id TEXT, notes_path TEXT)")
        conn.execute("CREATE TABLE note_resurface_history (user_id TEXT, note_path TEXT)")
        conn.execute(
            "INSERT INTO note_resurface_configs VALUES ('alice', ?)",
            (f"{OLD_MOUNT}/Users/alice/notes",),
        )
        conn.execute(
            "INSERT INTO note_resurface_history VALUES ('alice', ?)",
            (f"{OLD_MOUNT}/Users/alice/notes/x.md",),
        )
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


def _module_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT)")
    conn.execute("INSERT INTO feeds (url) VALUES ('https://example.test/feed.xml')")
    conn.commit()
    conn.close()


def _manifest(source: Path, **extra: str) -> None:
    lines = {
        "format": "1",
        "source_host": "bare-metal.example.test",
        "source_release": "v0.43.0",
        "exported_at": "2026-10-10T12:00:00Z",
        "mode": "stopped",
        "namespace": "istota",
        "istota_home": OLD_HOME,
        **extra,
    }
    (source / "MANIFEST").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))


def _export(tmp_path: Path, *, config_text: str | None = None, secrets: str = SECRETS_ENV) -> Path:
    source = tmp_path / "export"
    (source / "config").mkdir(parents=True)
    (source / "secrets").mkdir()
    _manifest(source)
    (source / "config" / "config.toml").write_text(config_text or FIXTURE.read_text())
    (source / "config" / "admins").write_text("alice\n")
    (source / "secrets" / "secrets.env").write_text(secrets)
    (source / "secrets" / "web-secrets.env").write_text(f"ISTOTA_WEB_TOKEN_KEY={WEB_TOKEN_KEY}\n")
    snapshot = source / "db" / "2026-10-10"
    _framework_db(snapshot / "framework" / "istota.db")
    _module_db(snapshot / "alice" / "feeds.db")
    return source


class Target:
    """`/data`, the config file and `secrets/` as `istota setup` leaves them."""

    def __init__(self, tmp_path: Path):
        self.data = tmp_path / "data"
        self.config = self.data / "config" / "config.toml"
        self.secrets = tmp_path / "secrets"
        self.config.parent.mkdir(parents=True)
        self.secrets.mkdir()
        for name in SECRET_NAMES:
            (self.secrets / name).write_text("")
        (self.data / ".secret_key").write_text("c" * 64)

    def args(self, source: Path, *extra: str) -> list[str]:
        return [
            "--source", str(source),
            "--config", str(self.config),
            "--secrets-dir", str(self.secrets),
            "--data-dir", str(self.data),
            "--ingress", "proxied",
            *extra,
        ]

    @property
    def db_path(self) -> Path:
        return self.data / "db" / "istota.db"

    def rows(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def snapshot(self) -> dict[str, bytes]:
        out = {}
        for root in (self.data, self.secrets):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    out[str(path)] = path.read_bytes()
        return out


@pytest.fixture
def target(tmp_path):
    return Target(tmp_path)


@pytest.fixture
def source(tmp_path):
    return _export(tmp_path)


# ---------------------------------------------------------------------------
# The config rewrite
# ---------------------------------------------------------------------------


class TestTheConfigRewrite:
    def test_state_paths_move_to_the_container_layout(self):
        new = bmi.rewrite_config(_old_config(), ingress="proxied").document
        assert new["db_path"] == "/data/db/istota.db"
        assert new["temp_dir"] == "/data/tmp"
        assert new["workspace_path"] == "/mnt/shared"
        assert new["nextcloud_mount_path"] == "/mnt/shared"
        dev = new["developer"]
        assert dev["repos_dir"] == "/data/repos"
        assert dev["gh_bin_path"] == "/usr/local/lib/istota_forge/gh"
        assert dev["glab_bin_path"] == "/usr/local/lib/istota_forge/glab"
        assert dev["devbox_proxy_socket_dir"] == "/data/devbox/cred"
        assert dev["container"]["exec_socket_dir"] == "/data/devbox/exec"
        assert new["browser"]["api_url"] == "http://istota-browser:9223"
        assert new["web"]["trusted_proxy_hops"] == 2

    def test_keys_the_container_derives_or_removed_are_dropped_and_reported(self):
        result = bmi.rewrite_config(_old_config(), ingress="proxied")
        new = result.document
        for key in ("module_data_dir", "skills_dir"):
            assert key not in new
        assert "file" not in new["logging"]
        assert new["logging"]["output"] == "console"
        assert "host_pressure_docker_socket" not in new["scheduler"]
        assert "allow_unsandboxed_multi_user_vaults" not in new["security"]
        report = "\n".join(result.changes)
        for key in ("module_data_dir", "skills_dir", "logging.file",
                    "scheduler.host_pressure_docker_socket",
                    "security.allow_unsandboxed_multi_user_vaults"):
            assert key in report

    def test_the_role_namespace_is_dropped_and_reported(self):
        old = _old_config()
        old["namespace"] = "example-ns"
        result = bmi.rewrite_config(old, ingress="proxied")
        assert "namespace" not in result.document
        assert any(
            line.startswith("namespace: dropped") and "example-ns" in line
            for line in result.changes
        )

    def test_devbox_docker_cli_is_dropped(self):
        old = _old_config()
        old["devbox"]["docker_cli"] = "/usr/bin/docker"
        result = bmi.rewrite_config(old, ingress="proxied")
        assert "docker_cli" not in result.document["devbox"]
        assert any("devbox.docker_cli" in line for line in result.changes)

    def test_everything_else_is_left_as_the_operator_had_it(self):
        old = _old_config()
        new = bmi.rewrite_config(old, ingress="proxied").document
        assert new["nextcloud"] == old["nextcloud"]
        assert new["site"] == old["site"]
        assert new["email"] == old["email"]
        assert new["talk"] == old["talk"]
        assert new["models"] == old["models"]

    def test_the_hop_count_follows_the_ingress_mode(self):
        old = _old_config()
        assert bmi.rewrite_config(old, ingress="direct").document["web"]["trusted_proxy_hops"] == 1
        assert bmi.rewrite_config(old, ingress="local").document["web"]["trusted_proxy_hops"] == 1

    def test_a_session_log_dir_the_role_set_is_dropped_so_it_derives(self):
        old = _old_config()
        old.setdefault("brain", {}).setdefault("native", {})["session_log"] = {
            "dir": f"{OLD_HOME}/logs",
        }
        new = bmi.rewrite_config(old, ingress="proxied").document
        assert "dir" not in new["brain"]["native"]["session_log"]

    def test_a_backup_dir_under_the_old_mount_is_rebased(self):
        old = _old_config()
        old["scheduler"]["db_backup_dir"] = f"{OLD_MOUNT}/Backups/db"
        new = bmi.rewrite_config(old, ingress="proxied").document
        assert new["scheduler"]["db_backup_dir"] == "/mnt/shared/Backups/db"

    def test_a_host_path_left_anywhere_else_is_reported(self):
        old = _old_config()
        old["security"]["sandbox_ro_paths"] = [f"{OLD_HOME}/shared-ro"]
        result = bmi.rewrite_config(old, ingress="proxied", old_home=OLD_HOME)
        assert any("security.sandbox_ro_paths" in line for line in result.changes)

    def test_the_rewritten_config_loads(self):
        new = bmi.rewrite_config(_old_config(), ingress="proxied").document
        apply_module.validate_config(new)

    def test_rehearsal_turns_every_transport_and_poller_off(self):
        old = _old_config()
        old.setdefault("sms", {})["enabled"] = True
        new = bmi.rewrite_config(old, ingress="local", rehearsal=True).document
        for section in ("talk", "email", "sms", "whatsapp", "sleep_cycle", "channel_sleep_cycle"):
            assert new[section]["enabled"] is False, section
        apply_module.validate_config(new)


class TestRebasing:
    PREFIXES = [(OLD_MOUNT, "/mnt/shared"), (f"{OLD_HOME}/tmp", "/data/tmp")]

    def test_a_path_under_a_prefix_is_rebased(self):
        assert bmi.rebase_path(f"{OLD_MOUNT}/Users/a/x.md", self.PREFIXES) == "/mnt/shared/Users/a/x.md"

    def test_the_prefix_itself_is_rebased(self):
        assert bmi.rebase_path(OLD_MOUNT, self.PREFIXES) == "/mnt/shared"

    def test_a_sibling_sharing_the_prefix_text_is_not(self):
        assert bmi.rebase_path(f"{OLD_MOUNT}-old/x.md", self.PREFIXES) is None

    def test_a_relative_path_is_not(self):
        assert bmi.rebase_path("Users/a/x.md", self.PREFIXES) is None

    def test_json_is_rebased_as_decoded_values(self):
        value = {"a": [f"{OLD_MOUNT}/x", "unrelated"], "b": {"c": f"{OLD_HOME}/tmp/y"}, "n": 3}
        rebased, changed = bmi.rebase_json(value, self.PREFIXES)
        assert rebased == {"a": ["/mnt/shared/x", "unrelated"], "b": {"c": "/data/tmp/y"}, "n": 3}
        assert changed == 2


# ---------------------------------------------------------------------------
# The import, end to end
# ---------------------------------------------------------------------------


class TestTheImport:
    def test_it_lands_config_secrets_databases_and_the_record(self, source, target, capsys):
        assert bmi.main(target.args(source)) == bmi.EXIT_OK

        written = tomllib.loads(target.config.read_text())
        assert written["db_path"] == "/data/db/istota.db"
        assert written["workspace_path"] == "/mnt/shared"
        assert (target.config.parent / "admins").read_text() == "alice\n"

        assert (target.secrets / "istota_nextcloud_app_password").read_text() == "nc-app-password"
        assert (target.secrets / "claude_code_oauth_token").read_text() == "claude-token"
        assert (target.secrets / "istota_web_session_secret_key").read_text() == "session-key"
        assert (target.secrets / "istota_caldav_password").read_text() == ""
        # Carried byte for byte: every stored credential is encrypted under it.
        assert (target.data / ".secret_key").read_text() == SECRET_KEY
        assert (target.data / ".web_token_key").read_text() == WEB_TOKEN_KEY
        assert (target.data / ".secret_key").stat().st_mode & 0o777 == 0o600

        assert target.db_path.exists()
        assert (target.data / "db" / "modules" / "alice" / "feeds.db").exists()

        record = target.rows("SELECT value FROM shared_kv WHERE namespace = '_migration'")
        assert len(record) == 1
        meta = json.loads(record[0][0])
        assert meta["source_host"] == "bare-metal.example.test"
        assert meta["source_release"] == "v0.43.0"
        assert meta["rehearsal"] is False

    def test_the_measured_columns_are_rebased(self, source, target):
        assert bmi.main(target.args(source)) == bmi.EXIT_OK

        attachments = json.loads(target.rows("SELECT attachments FROM tasks WHERE id = 1")[0][0])
        assert attachments == [
            "/mnt/shared/Users/alice/inbox/a.pdf",
            f"{OLD_MOUNT}-old/Users/alice/b.pdf",
            "/opt/elsewhere/c.pdf",
        ]
        chunk = target.rows(
            "SELECT source_id, metadata_json FROM memory_chunks WHERE source_type = 'memory_file'"
        )[0]
        assert chunk[0] == "/mnt/shared/Users/alice/memories/2026-10-01.md"
        assert json.loads(chunk[1])["file_path"] == chunk[0]
        assert target.rows(
            "SELECT source_id FROM memory_chunks WHERE source_type = 'conversation'"
        ) == [("12",)]
        assert target.rows("SELECT notes_path FROM note_resurface_configs") == [
            ("/mnt/shared/Users/alice/notes",)
        ]
        assert target.rows("SELECT note_path FROM note_resurface_history") == [
            ("/mnt/shared/Users/alice/notes/x.md",)
        ]
        history = json.loads(target.rows(
            "SELECT value FROM istota_kv WHERE namespace = 'corpus' AND key = 'history'"
        )[0][0])
        assert history == {"seen": ["/mnt/shared/Users/alice/notes/x.md"], "count": 1}

    def test_history_and_report_only_columns_are_left_alone(self, source, target, capsys):
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        out = capsys.readouterr().out

        assert target.rows("SELECT prompt FROM tasks WHERE id = 1") == [
            (f"look at {OLD_MOUNT}/Users/alice/inbox/a.pdf",)
        ]
        assert target.rows("SELECT command FROM scheduled_jobs WHERE name = 'nightly'") == [
            (f"{OLD_HOME}/istota/.venv/bin/python -m something",)
        ]
        system_check = json.loads(target.rows(
            "SELECT value FROM istota_kv WHERE namespace = 'system_check'"
        )[0][0])
        assert system_check["path"] == f"{OLD_HOME}/.claude/.credentials.json"

        assert "scheduled_jobs.command" in out
        assert "alice/nightly" in out
        assert "istota_kv system_check/last_run" in out
        assert "/opt/elsewhere/c.pdf" in out

    def test_a_second_run_is_a_no_op(self, source, target, capsys):
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        before = target.snapshot()
        capsys.readouterr()
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        assert "already imported" in capsys.readouterr().out
        assert target.snapshot() == before

    def test_dry_run_touches_nothing(self, source, target, capsys):
        before = target.snapshot()
        assert bmi.main(target.args(source, "--dry-run")) == bmi.EXIT_OK
        assert target.snapshot() == before
        out = capsys.readouterr().out
        assert "tasks.attachments: 1" in out
        assert "+db_path" in out or "db_path" in out

    def test_list_touches_nothing(self, source, target, capsys):
        before = target.snapshot()
        assert bmi.main(target.args(source, "--list")) == bmi.EXIT_OK
        assert target.snapshot() == before
        out = capsys.readouterr().out
        assert "bare-metal.example.test" in out
        assert "framework/istota.db" in out
        assert "alice/feeds.db" in out

    def test_emit_plan_writes_an_apply_plan_without_credentials(self, source, target, tmp_path):
        plan = tmp_path / "plan.json"
        assert bmi.main(target.args(source, "--dry-run", "--emit-plan", str(plan))) == bmi.EXIT_OK
        document = apply_module.load_plan(plan)
        assert document["meta"]["version"] == apply_module.PLAN_VERSION
        assert document["config"] == bmi.rewrite_config(_old_config(), ingress="proxied").document
        text = plan.read_text()
        for value in ("nc-app-password", "imap-password", "claude-token", SECRET_KEY):
            assert value not in text
        assert plan.stat().st_mode & 0o777 == 0o600


#: Every name a role-rendered install of the surveyed shape carries across its
#: two env files, with synthetic values.
SURVEYED_SECRETS_ENV = "\n".join([
    "ISTOTA_NEXTCLOUD_APP_PASSWORD=nc-app-password",
    "ISTOTA_EMAIL_IMAP_PASSWORD=imap-password",
    "ISTOTA_EMAIL_SMTP_PASSWORD=smtp-password",
    "ISTOTA_SMS_TELNYX_API_KEY=telnyx-api-key",
    "ISTOTA_SMS_TELNYX_PUBLIC_KEY=telnyx-public-key",
    "ISTOTA_SMS_TELNYX_MESSAGING_PROFILE_ID=telnyx-profile-id",
    "ISTOTA_DEVELOPER_GITLAB_TOKEN=gitlab-token",
    "ISTOTA_DEVELOPER_GITHUB_TOKEN=github-token",
    "ISTOTA_WEB_OAUTH2_CLIENT_SECRET=oauth-secret",
    "ISTOTA_WEB_SESSION_SECRET_KEY=session-key",
    "ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET=gws-secret",
    "CLAUDE_CODE_OAUTH_TOKEN=claude-token",
    f"ISTOTA_SECRET_KEY={SECRET_KEY}",
    "ISTOTA_BRAIN_NATIVE_API_KEY=native-key",
    "",
])


class TestTheSurveyedSecretSet:
    """The five names that had no file in this shape now have one each."""

    NEW_FILES = {
        "istota_email_smtp_password": "smtp-password",
        "istota_sms_telnyx_api_key": "telnyx-api-key",
        "istota_sms_telnyx_public_key": "telnyx-public-key",
        "istota_sms_telnyx_messaging_profile_id": "telnyx-profile-id",
        "istota_google_workspace_client_secret": "gws-secret",
    }

    def test_a_live_import_of_every_surveyed_name_succeeds(self, tmp_path, target):
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        for name, value in self.NEW_FILES.items():
            assert (target.secrets / name).read_text() == value, name
        assert (target.secrets / "istota_brain_native_api_key").read_text() == "native-key"

    def test_a_rehearsal_withholds_all_five(self, tmp_path, target):
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        assert bmi.main(target.args(source, "--rehearsal")) == bmi.EXIT_OK
        for name in self.NEW_FILES:
            assert (target.secrets / name).read_text() == "", name


class TestRefusals:
    def test_an_unmapped_secret_refuses_and_touches_nothing(self, tmp_path, target, capsys):
        source = _export(
            tmp_path, secrets=SECRETS_ENV + "ISTOTA_SMS_TWILIO_AUTH_TOKEN=twilio-token\n",
        )
        before = target.snapshot()
        assert bmi.main(target.args(source)) == bmi.EXIT_REFUSED
        assert target.snapshot() == before
        assert not target.config.exists()
        err = capsys.readouterr().err
        assert "ISTOTA_SMS_TWILIO_AUTH_TOKEN" in err
        assert "twilio-token" not in err

    def test_a_target_that_has_done_work_refuses(self, source, target):
        target.db_path.parent.mkdir(parents=True)
        db.init_db(target.db_path)
        conn = sqlite3.connect(target.db_path)
        conn.execute(
            "INSERT INTO tasks (status, source_type, user_id, prompt) "
            "VALUES ('completed', 'web', 'bob', 'live work')"
        )
        conn.commit()
        conn.close()
        before = target.snapshot()
        assert bmi.main(target.args(source)) == bmi.EXIT_REFUSED
        assert target.snapshot() == before

    def test_a_source_without_a_manifest_refuses(self, source, target):
        (source / "MANIFEST").unlink()
        assert bmi.main(target.args(source)) == bmi.EXIT_REFUSED

    def test_a_missing_secrets_directory_refuses_before_anything_is_written(
        self, source, target, tmp_path, capsys,
    ):
        before = target.snapshot()
        args = target.args(source)
        args[args.index("--secrets-dir") + 1] = str(tmp_path / "no-such-secrets")
        assert bmi.main(args) == bmi.EXIT_REFUSED
        assert target.snapshot() == before
        assert not target.config.exists()
        assert "no-such-secrets" in capsys.readouterr().err

    def test_the_default_secrets_directory_is_where_istota_stack_mounts_it(self):
        assert bmi.build_parser().get_default("secrets_dir") == "/secrets"

    def test_a_secrets_line_without_an_equals_sign_refuses(self, tmp_path, target):
        source = _export(tmp_path, secrets=SECRETS_ENV + "NOT A LINE\n")
        assert bmi.main(target.args(source)) == bmi.EXIT_REFUSED


class TestRehearsal:
    def test_transport_credentials_are_withheld_and_schedules_suspended(self, tmp_path, target, capsys):
        source = _export(
            tmp_path, secrets=SECRETS_ENV + "ISTOTA_SMS_TELNYX_API_KEY=telnyx-key\n",
        )
        assert bmi.main(target.args(source, "--rehearsal")) == bmi.EXIT_OK

        assert (target.secrets / "istota_nextcloud_app_password").read_text() == ""
        assert (target.secrets / "istota_email_imap_password").read_text() == ""
        assert (target.secrets / "claude_code_oauth_token").read_text() == "claude-token"
        assert (target.data / ".secret_key").read_text() == SECRET_KEY

        written = tomllib.loads(target.config.read_text())
        assert written["talk"]["enabled"] is False
        assert written["email"]["enabled"] is False

        assert target.rows(
            "SELECT count(*) FROM scheduled_jobs WHERE auto_disabled_at IS NULL"
        ) == [(0,)]
        assert target.rows("SELECT enabled FROM briefing_configs") == [(0,)]
        meta = json.loads(target.rows(
            "SELECT value FROM shared_kv WHERE namespace = '_migration'"
        )[0][0])
        assert meta["rehearsal"] is True
        assert "ISTOTA_SMS_TELNYX_API_KEY" in capsys.readouterr().out


class TestARehearsalReachesNobody:
    """A rehearsal holds production state; nothing in it may reach a person or
    a service the production install is still talking to."""

    def _with_session(self, tmp_path: Path, **manifest: str) -> Path:
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        session = source / "whatsapp-baileys-session"
        session.mkdir()
        (session / "creds.json").write_text('{"me": "synthetic"}')
        if manifest:
            _manifest(source, **manifest)
        return source

    def test_the_baileys_session_is_withheld_from_a_rehearsal(self, tmp_path, target, capsys):
        """The sidecar connects with whatever session it finds, `whatsapp.enabled`
        or not, and two connections on one session replace each other forever."""
        source = self._with_session(tmp_path, mode="online")
        assert bmi.main(target.args(source, "--rehearsal")) == bmi.EXIT_OK
        assert not (target.data / "db" / "whatsapp-baileys-session").exists()
        assert "whatsapp-baileys-session/: withheld for the rehearsal" in capsys.readouterr().out

    def test_a_live_import_carries_the_baileys_session(self, tmp_path, target):
        source = self._with_session(tmp_path)
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        assert (target.data / "db" / "whatsapp-baileys-session" / "creds.json").exists()

    def test_an_online_export_is_refused_without_rehearsal(self, tmp_path, target, capsys):
        source = self._with_session(tmp_path, mode="online")
        before = target.snapshot()
        assert bmi.main(target.args(source)) == bmi.EXIT_REFUSED
        assert target.snapshot() == before
        assert not target.config.exists()
        assert "--rehearsal" in capsys.readouterr().err

    def test_a_withheld_secret_the_operator_already_filled_is_emptied(self, tmp_path, target):
        """`istota-stack setup` may have written production values already; a
        withheld credential has to be blank, not merely left unwritten."""
        source = _export(tmp_path)  # carries no CalDAV or Telnyx variable at all
        for name in ("istota_nextcloud_app_password", "istota_caldav_password",
                     "istota_sms_telnyx_api_key", "istota_web_oauth2_client_secret"):
            (target.secrets / name).write_text("value-from-setup")
        assert bmi.main(target.args(source, "--rehearsal")) == bmi.EXIT_OK
        for name in ("istota_nextcloud_app_password", "istota_caldav_password",
                     "istota_sms_telnyx_api_key", "istota_web_oauth2_client_secret"):
            assert (target.secrets / name).read_text() == "", name
        assert (target.secrets / "claude_code_oauth_token").read_text() == "claude-token"

    def test_the_forge_tokens_are_withheld(self, tmp_path, target, capsys):
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        (target.secrets / "istota_developer_github_token").write_text("value-from-setup")
        assert bmi.main(target.args(source, "--rehearsal")) == bmi.EXIT_OK
        assert (target.secrets / "istota_developer_gitlab_token").read_text() == ""
        assert (target.secrets / "istota_developer_github_token").read_text() == ""
        out = capsys.readouterr().out
        assert "withheld for the rehearsal: ISTOTA_DEVELOPER_GITLAB_TOKEN" in out
        assert "withheld for the rehearsal: ISTOTA_DEVELOPER_GITHUB_TOKEN" in out


def _add_live_paths(db_path: Path) -> None:
    """Rows a running install holds that would reach a person or a service."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO secrets (user_id, service, key, encrypted_value) "
            "VALUES ('alice', 'ntfy', 'topic', x'00'), ('alice', 'ntfy', 'token', x'01'), "
            "('alice', 'karakeep', 'api_key', x'02')"
        )
        # A module job the daemon had already failed on once and suspended
        # (a stamp older than the rescue arm's cooldown), and one it had not.
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, skill, "
            "skill_args, skip_log_channel, consecutive_failures, last_run_at, "
            "auto_disabled_at) VALUES ('alice', '_module.feeds.run_scheduled', "
            "'*/5 * * * *', 'feeds', 'run-scheduled', 1, 1, "
            "datetime('now', '-3 hours'), datetime('now', '-2 hours'))"
        )
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, skill, "
            "skill_args, skip_log_channel) VALUES ('alice', '_module.health.garmin_sync', "
            "'0 * * * *', 'health', 'garmin-sync', 1)"
        )
        conn.execute(
            "INSERT INTO shared_block_configs (name, cron, title, enabled, sources, "
            "created_at, updated_at) VALUES ('world-headlines', '45 5 * * *', 'Headlines', "
            "1, '[]', datetime('now'), datetime('now'))"
        )
        for task_id, status in (
            (10, "pending"), (11, "locked"), (12, "running"),
            (13, "pending_confirmation"), (14, "failed"),
        ):
            conn.execute(
                "INSERT INTO tasks (id, status, source_type, user_id, prompt) "
                "VALUES (?, ?, 'talk', 'alice', 'queued on the old host')",
                (task_id, status),
            )
        conn.commit()
    finally:
        conn.close()


def _loaded(document: dict):
    """The written config as the daemon loads it, against no real database."""
    from istota.config import load_config
    from istota.lib import toml_write

    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        probe = dict(document)
        probe["db_path"] = str(Path(scratch) / "absent" / "istota.db")
        path = Path(scratch) / "config.toml"
        path.write_text(toml_write.dumps(probe), encoding="utf-8")
        return load_config(path)


class TestARehearsalIsNotASecondLiveBot:
    """Every loop and delivery path that reaches a person or another service is
    off in the imported copy, through a switch the daemon already reads."""

    @pytest.fixture
    def imported(self, tmp_path, target):
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        _add_live_paths(source / "db" / "2026-10-10" / "framework" / "istota.db")
        assert bmi.main(target.args(source, "--rehearsal")) == bmi.EXIT_OK
        return target

    def test_the_written_config_switches_each_poller_off(self, imported):
        written = tomllib.loads(imported.config.read_text())
        scheduler = written["scheduler"]
        assert scheduler["heartbeat_check_interval"] == 0
        assert scheduler["tasks_file_poll_interval"] == 0
        assert scheduler["shared_file_check_interval"] == 0
        assert scheduler["push_notification_sources"] == []
        assert written["developer"]["worktree_reap_enabled"] is False
        assert written["web"]["avatar_import_from_nextcloud"] is False
        assert "default_briefings" not in written
        assert [b["enabled"] for b in written["briefing_shared_blocks"]] == [False, False]

    def test_the_daemon_builds_no_gate_for_any_of_them(self, imported):
        from istota.scheduler import build_interval_gates

        config = _loaded(tomllib.loads(imported.config.read_text()))
        enabled = {g.name for g in build_interval_gates(config) if g.enabled(config)}
        for name in (
            "heartbeats", "tasks-file-poll", "briefing-triggers", "shared-files",
            "email-poll", "worktree-reap", "avatar-import", "whatsapp-pairing",
        ):
            assert name not in enabled, name
        assert config.talk.enabled is False
        assert config.scheduler.push_notification_sources == []
        assert all(not b.enabled for b in config.briefing_shared_blocks)
        assert config.default_briefings == []

    def test_ntfy_credentials_are_removed_and_others_kept(self, imported):
        assert imported.rows("SELECT count(*) FROM secrets WHERE service = 'ntfy'") == [(0,)]
        assert imported.rows("SELECT service FROM secrets") == [("karakeep",)]

    def test_no_scheduled_job_can_fire(self, imported):
        assert imported.rows(
            "SELECT count(*) FROM scheduled_jobs "
            "WHERE enabled = 1 AND auto_disabled_at IS NULL"
        ) == [(0,)]
        assert imported.rows(
            "SELECT name FROM scheduled_jobs WHERE name LIKE '\\_module.%' ESCAPE '\\' "
            "AND (enabled != 0 OR disabled_at IS NULL) ORDER BY name"
        ) == []

    def test_the_module_sync_does_not_bring_a_module_job_back(self, imported):
        """The module sync rescues a `_module.*` row the daemon suspended once
        its stamp is an hour old, so a suspension alone would not hold."""
        from istota import scheduler
        from istota.config import Config, UserConfig

        conn = sqlite3.connect(imported.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # As if the rehearsal had run for hours.
            conn.execute(
                "UPDATE scheduled_jobs SET auto_disabled_at = datetime('now', '-5 hours') "
                "WHERE auto_disabled_at IS NOT NULL"
            )
            conn.commit()
            config = Config(db_path=imported.db_path, users={"alice": UserConfig()})
            scheduler._sync_module_jobs(
                conn, config, module_name="feeds", module_prefix="_module.feeds.",
                resolve_for_user=lambda user_id, cfg, conn=None: object(),
                jobs_for_user=lambda ctx, user_id: [{
                    "name": "_module.feeds.run_scheduled", "cron": "*/5 * * * *",
                    "skill": "feeds", "skill_args": "run-scheduled",
                }],
                user_not_found_exc=LookupError,
            )
            live = conn.execute(
                "SELECT name FROM scheduled_jobs WHERE enabled = 1 AND auto_disabled_at IS NULL"
            ).fetchall()
        finally:
            conn.close()
        assert live == []

    def test_shared_blocks_and_briefings_are_off(self, imported):
        assert imported.rows("SELECT enabled FROM shared_block_configs") == [(0,)]
        assert imported.rows("SELECT enabled FROM briefing_configs") == [(0,)]

    def test_the_old_hosts_queue_is_cancelled_and_history_kept(self, imported):
        assert imported.rows(
            "SELECT id, status FROM tasks WHERE id >= 10 ORDER BY id"
        ) == [
            (10, "cancelled"), (11, "cancelled"), (12, "cancelled"),
            (13, "cancelled"), (14, "failed"),
        ]
        assert imported.rows("SELECT status FROM tasks WHERE id = 1") == [("completed",)]

    def test_the_report_names_what_was_switched_off(self, tmp_path, target, capsys):
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        _add_live_paths(source / "db" / "2026-10-10" / "framework" / "istota.db")
        assert bmi.main(target.args(source, "--rehearsal", "--dry-run")) == bmi.EXIT_OK
        out = capsys.readouterr().out
        assert "scheduler.heartbeat_check_interval" in out
        assert "2 ntfy credential(s) removed" in out
        assert "4 queued task(s) cancelled" in out

    def test_a_live_import_switches_none_of_it_off(self, tmp_path, target):
        source = _export(tmp_path, secrets=SURVEYED_SECRETS_ENV)
        _add_live_paths(source / "db" / "2026-10-10" / "framework" / "istota.db")
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        written = tomllib.loads(target.config.read_text())
        assert "heartbeat_check_interval" not in written["scheduler"] or (
            written["scheduler"]["heartbeat_check_interval"] != 0
        )
        assert target.rows("SELECT count(*) FROM secrets WHERE service = 'ntfy'") == [(2,)]
        assert target.rows("SELECT status FROM tasks WHERE id = 10") == [("pending",)]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
class TestRepositories:
    def test_worktrees_point_at_the_new_root_after_the_copy(self, tmp_path, target):
        old_repos = tmp_path / "old-host" / "repos"
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q", "-b", "main")
        (upstream / "f.txt").write_text("x")
        _git(upstream, "add", "f.txt")
        _git(upstream, "commit", "-q", "-m", "one")
        clone = old_repos / "alice" / "acme" / "proj.git"
        clone.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(clone))
        worktree = old_repos / "alice" / "acme" / "proj-feature"
        _git(clone, "worktree", "add", "-q", "-b", "feature", str(worktree), "main")
        (old_repos / ".istota-layout").write_text("2\n")

        config = _old_config()
        config["developer"]["repos_dir"] = str(old_repos)
        from istota.lib import toml_write
        source = _export(tmp_path, config_text=toml_write.dumps(config))
        shutil.copytree(old_repos, source / "repos", symlinks=True)
        shutil.rmtree(old_repos)

        assert bmi.main(target.args(source)) == bmi.EXIT_OK

        new_worktree = target.data / "repos" / "alice" / "acme" / "proj-feature"
        new_clone = target.data / "repos" / "alice" / "acme" / "proj.git"
        assert (target.data / "repos" / ".istota-layout").read_text() == "2\n"
        gitdir = (new_worktree / ".git").read_text()
        assert str(new_clone.resolve()) in gitdir or str(new_clone) in gitdir
        assert _git(new_worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature"


class TestPartialAndResume:
    def test_a_failure_mid_run_is_partial_and_a_rerun_finishes(self, source, target, monkeypatch, capsys):
        def boom(*_args, **_kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(bmi, "_import_side_trees", boom)
        assert bmi.main(target.args(source)) == bmi.EXIT_PARTIAL
        assert (target.data / bmi.PROGRESS_NAME).exists()
        assert target.rows("SELECT count(*) FROM shared_kv WHERE namespace = '_migration'") == [(0,)]
        assert "disk full" in capsys.readouterr().err

        monkeypatch.undo()
        assert bmi.main(target.args(source)) == bmi.EXIT_OK
        assert not (target.data / bmi.PROGRESS_NAME).exists()
        assert target.rows("SELECT count(*) FROM shared_kv WHERE namespace = '_migration'") == [(1,)]
        # Rebased once, from the restored copy: not twice over.
        assert json.loads(target.rows("SELECT attachments FROM tasks WHERE id = 1")[0][0])[0] == (
            "/mnt/shared/Users/alice/inbox/a.pdf"
        )

    def test_a_progress_file_for_another_export_refuses(self, source, target):
        target.data.mkdir(exist_ok=True)
        (target.data / bmi.PROGRESS_NAME).write_text("other.example.test 2026-01-01T00:00:00Z\n")
        assert bmi.main(target.args(source)) == bmi.EXIT_REFUSED


class TestTheCli:
    def test_istota_migrate_import_bare_metal_reaches_the_module(self, source, target, monkeypatch):
        from istota import cli

        monkeypatch.setattr(
            "sys.argv", ["istota", "migrate", "import-bare-metal", *target.args(source, "--list")],
        )
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code in (0, None)

    def test_a_refusal_is_the_process_exit_code(self, source, target, monkeypatch):
        from istota import cli

        (source / "MANIFEST").unlink()
        monkeypatch.setattr("sys.argv", ["istota", "migrate", "import-bare-metal", *target.args(source)])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == bmi.EXIT_REFUSED
