"""`istota config-request take|finish|mark`: the host applier's half of the
web config editor, and the seam between the files `take` writes and the two
writers that read them (`istota apply` and `istota-stack put-files`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from istota import db, user_profiles
from istota.apply import CheckResult
from istota.config import Config
from istota.config_editor import edits, handoff, requests
from istota.webui import auth as web_auth

REPO = Path(__file__).resolve().parents[1]
STACK_SCRIPT = REPO / "host" / "istota-stack"

OLD_HEADER = "header-old-value-3"
NEW_HEADER = "header-new-value-4"
NEW_IMAP = "imap-new-value-5"


class Install:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "db" / "istota.db"
        self.config_path = root / "config" / "config.toml"
        self.admins_path = root / "config" / "admins"
        self.out = root / "out"
        self.env = {
            **os.environ,
            "ISTOTA_SECRET_KEY": "k" * 64,
            "ISTOTA_ADMINS_FILE": str(self.admins_path),
        }
        self.env.pop("ISTOTA_CONFIG_PATH", None)

    def run(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "istota.cli", "-c", str(self.config_path), *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.root,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.config_path.read_bytes()).hexdigest()

    def submit(self, change_set: dict) -> int:
        async def run_check(document):
            return CheckResult()

        found = asyncio.run(edits.preview(
            self.config_path.read_text(), change_set,
            config=Config(admin_users={"alice"}, db_path=self.db_path),
            acting_admin="alice", run_check=run_check, environ={},
        ))
        assert not found.blocked, found.errors
        with db.get_db(self.db_path) as conn:
            return requests.create(conn, found, admin_id="alice", base_digest=self.digest())


@pytest.fixture
def install(tmp_path, monkeypatch) -> Install:
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 64)
    found = Install(tmp_path)
    found.db_path.parent.mkdir()
    found.config_path.parent.mkdir()
    found.out.mkdir()
    db.init_db(found.db_path)
    found.config_path.write_text(
        f'db_path = "{found.db_path}"\n\n'
        '[email]\nimap_host = "imap.old.example.org"\n\n'
        f'[brain.native.extra_headers]\nX-Key = "{OLD_HEADER}"\n'
    )
    found.admins_path.write_text("alice\n")
    for user_id in ("alice", "bob"):
        user_profiles.ensure_profile(found.db_path, user_id)
        web_auth.upsert_identity(found.db_path, user_id, f"{user_id}@example.org")
    return found


CHANGE = {
    "ops": [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}],
    "secrets": {
        "istota_email_imap_password": NEW_IMAP,
        "istota_caldav_password": "",
        "brain.native.extra_headers.X-New": NEW_HEADER,
    },
    "admins": ["alice", "bob"],
}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestTake:
    def test_take_writes_the_three_files_and_prints_the_id(self, install):
        request_id = install.submit(CHANGE)
        result = install.run("config-request", "take", "--out", str(install.out))
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{request_id}\n"
        for name in ("plan.json", "files.json", "claude-code"):
            assert _mode(install.out / name) == 0o400, name

        plan = json.loads((install.out / "plan.json").read_text())
        assert plan["meta"] == {"managed": False, "source": "web", "request": request_id}
        assert plan["config"]["email"] == {"imap_host": "imap.example.org"}
        assert plan["config"]["brain"]["native"]["extra_headers"] == {"X-Key": OLD_HEADER, "X-New": NEW_HEADER}

        files = json.loads((install.out / "files.json").read_text())
        assert files == {
            "secrets": {"istota_email_imap_password": NEW_IMAP, "istota_caldav_password": ""},
            "admins": "alice\nbob\n",
            "managed": {"source": "web", "secrets": False, "admins": False},
        }
        assert (install.out / "claude-code").read_text() == "1\n"

        with db.get_db(install.db_path) as conn:
            row = conn.execute("SELECT * FROM config_change_requests WHERE id = ?", (request_id,)).fetchone()
        assert row["status"] == "taken" and row["secrets_sealed"] is None
        stored = " ".join(str(row[key]) for key in row.keys())
        assert NEW_IMAP not in stored and NEW_HEADER not in stored and OLD_HEADER not in stored

        again = install.run("config-request", "take", "--out", str(install.out))
        assert again.returncode == 0 and again.stdout == ""

    def test_the_written_plan_is_one_istota_apply_accepts(self, install):
        install.submit(CHANGE)
        assert install.run("config-request", "take", "--out", str(install.out)).returncode == 0
        plan = str(install.out / "plan.json")

        checked = install.run("apply", "--check", "--json", "-f", plan)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert json.loads(checked.stdout)["error"] is None

        dry = install.run("apply", "--dry-run", "-f", plan)
        assert dry.returncode == 2, dry.stdout + dry.stderr
        assert NEW_HEADER not in dry.stdout + dry.stderr

    def test_the_written_files_are_a_request_put_files_accepts(self, install, tmp_path):
        install.submit(CHANGE)
        assert install.run("config-request", "take", "--out", str(install.out)).returncode == 0
        script = re.search(r"^PUT_FILES_PY='\n(.*?)\n'$", STACK_SCRIPT.read_text(), re.S | re.M).group(1)
        secrets_dir, config_dir = tmp_path / "secrets", tmp_path / "stack-config"
        secrets_dir.mkdir()
        config_dir.mkdir()
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(secrets_dir), str(config_dir), "1"],
            input=(install.out / "files.json").read_text(), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "would write istota_email_imap_password" in result.stdout
        assert "would write config/admins" in result.stdout
        assert NEW_IMAP not in result.stdout + result.stderr

    def test_a_request_changing_no_file_names_nothing(self, install):
        install.submit({"ops": [{"op": "set", "path": ["email", "imap_host"], "value": "imap.example.org"}]})
        assert install.run("config-request", "take", "--out", str(install.out)).returncode == 0
        assert json.loads((install.out / "files.json").read_text()) == {}

    def test_a_changed_file_refuses_the_request_and_prints_nothing(self, install):
        request_id = install.submit(CHANGE)
        install.config_path.write_text(install.config_path.read_text() + "\n[scheduler]\npoll_interval = 7\n")
        result = install.run("config-request", "take", "--out", str(install.out))
        assert result.returncode == 0 and result.stdout == ""
        assert not (install.out / "plan.json").exists()
        with db.get_db(install.db_path) as conn:
            assert requests.get(conn, request_id)["status"] == "refused"

    def test_a_credential_removed_since_the_request_refuses_it(self, install):
        request_id = install.submit({"ops": [{"op": "set", "path": ["email", "imap_host"], "value": "x.example.org"}]})
        # The stored document keeps X-Key as a sentinel. A host edit that removed
        # it and kept the digest is impossible, so stand in for the file the
        # request was made against by rewriting the digest too.
        install.config_path.write_text(f'db_path = "{install.db_path}"\n')
        with db.get_db(install.db_path) as conn:
            conn.execute(
                "UPDATE config_change_requests SET base_digest = ? WHERE id = ?", (install.digest(), request_id),
            )
        result = install.run("config-request", "take", "--out", str(install.out))
        assert result.returncode == 0 and result.stdout == ""
        assert "no stored value to keep at brain.native.extra_headers.X-Key" in result.stderr
        with db.get_db(install.db_path) as conn:
            assert requests.get(conn, request_id)["status"] == "refused"


    def test_a_plan_claiming_a_resource_since_refuses_the_request(self, install):
        request_id = install.submit(CHANGE)
        (install.config_path.parent / "managed.json").write_text(
            json.dumps({"secrets": {"managed": True, "source": "ansible", "at": "2026-10-12T00:00:00Z"}})
        )
        result = install.run("config-request", "take", "--out", str(install.out))
        assert result.returncode == 0 and result.stdout == ""
        assert not (install.out / "plan.json").exists()
        with db.get_db(install.db_path) as conn:
            row = requests.get(conn, request_id)
        assert row["status"] == "refused"
        assert row["output_text"] == "a plan now manages secrets; change it there"


class TestBuild:
    def test_a_current_file_that_does_not_parse_refuses_the_build(self, install):
        install.submit({"ops": [{"op": "set", "path": ["email", "imap_host"], "value": "x.example.org"}]})
        with db.get_db(install.db_path) as conn:
            taken = requests.take(conn, install.digest())
        with pytest.raises(handoff.HandoffError, match=r"fix it on the host with `istota-stack apply`"):
            handoff.build(taken, None)


class TestFinishAndMark:
    def test_finish_stores_the_output_without_a_credential_line(self, install):
        request_id = install.submit(CHANGE)
        assert install.run("config-request", "take", "--out", str(install.out)).returncode == 0
        assert install.run("config-request", "mark", "--id", str(request_id), "--status", "building").returncode == 0
        output = install.out / "output"
        output.write_text(f"apply: wrote config.toml\nleaked {NEW_IMAP}\nleaked {OLD_HEADER}\ndone\n")
        result = install.run(
            "config-request", "finish", "--id", str(request_id), "--status", "applied",
            "--output-file", str(output),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        with db.get_db(install.db_path) as conn:
            row = requests.get(conn, request_id)
            notified = conn.execute(
                "SELECT user_id FROM notifications WHERE source = 'admin_config_change'"
            ).fetchall()
        assert row["status"] == "applied"
        assert row["output_text"] == "\n".join([
            "apply: wrote config.toml", handoff.REMOVED_LINE, handoff.REMOVED_LINE, "done",
        ])
        assert [item["user_id"] for item in notified] == ["bob"]

    def test_finish_on_a_request_not_taken_fails(self, install):
        request_id = install.submit(CHANGE)
        result = install.run(
            "config-request", "finish", "--id", str(request_id), "--status", "applied",
            "--output-file", str(install.out / "output"),
        )
        assert result.returncode == 1


class TestClaudeCodeRule:
    """`handoff.needs_claude_code` restates `istota-stack`'s rule, which reads the
    file on the host; both are run on the same documents."""

    DOCUMENTS = [
        {},
        {"brain": {"kind": "native"}},
        {"brain": {"kind": "native", "fallback": "claude_code"}},
        {"brain": {"kind": "native", "source_type_overrides": {"cron": "tmux_claude"}}},
        {"brain": {"kind": "native", "room_selectable": ["claude_code"]}},
        {"brain": {"kind": "native", "room_selectable": ["native"]}},
    ]

    def test_the_two_copies_agree(self, tmp_path):
        from istota.lib import toml_write

        body = re.search(
            r"^needs_claude_code\(\) \{.*?<<'PY'\n(.*?)\nPY\n", STACK_SCRIPT.read_text(), re.S | re.M,
        ).group(1)
        for index, document in enumerate(self.DOCUMENTS):
            path = tmp_path / f"config-{index}.toml"
            path.write_text(toml_write.dumps(document))
            shell = subprocess.run(
                [sys.executable, "-I", "-c", body, str(path)], capture_output=True, text=True, timeout=30,
            )
            assert shell.returncode == 0, shell.stderr
            assert shell.stdout.strip() == ("1" if handoff.needs_claude_code(document) else "0"), document
