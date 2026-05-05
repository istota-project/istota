"""Tests for istota.skills.memory — runtime memory CLI."""

from __future__ import annotations

import json
import os

import pytest

from istota.skills.memory import main as memory_main


SEED_USER_MD = (
    "# User Memory\n"
    "\n"
    "## Notes\n"
    "\n"
    "- Existing note 1\n"
    "- Existing note 2\n"
    "\n"
    "## Communication style\n"
    "\n"
    "- Prefers short replies\n"
    "\n"
    "### Email\n"
    "\n"
    "- Always sign off with name\n"
    "\n"
)


def _setup_user(tmp_path, monkeypatch, user_id="alice", bot_dir="istota"):
    mount = tmp_path / "mount"
    user_md_dir = mount / "Users" / user_id / bot_dir / "config"
    user_md_dir.mkdir(parents=True)
    user_md = user_md_dir / "USER.md"
    user_md.write_text(SEED_USER_MD)
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
    monkeypatch.setenv("ISTOTA_USER_ID", user_id)
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", bot_dir)
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    return user_md


class TestAppend:
    def test_append_to_existing_heading(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Brand new bullet"])
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "status": "ok",
            "outcome": "applied",
            "heading": "Notes",
            "line": "Brand new bullet",
        }
        body = user_md.read_text()
        assert "- Brand new bullet" in body
        assert "## Notes" in body

    def test_append_to_missing_heading_returns_error(
        self, tmp_path, monkeypatch, capsys
    ):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "DoesNotExist", "--line", "Hi"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert out["error"] == "heading_missing"
        assert "available_headings" in out
        assert "Notes" in out["available_headings"]
        # File untouched.
        assert user_md.read_text() == before

    def test_append_duplicate_is_noop(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        memory_main(["append", "--heading", "Notes", "--line", "Existing note 1"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["outcome"] == "noop_dup"
        assert user_md.read_text() == before

    def test_append_writes_audit_log(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Audit me"])
        capsys.readouterr()
        audit = user_md.parent / "USER.md.audit.jsonl"
        assert audit.exists()
        entry = json.loads(audit.read_text().strip().splitlines()[-1])
        assert entry["source"] == "runtime"
        assert entry["entry_kind"] == "batch"
        assert entry["applied"][0]["outcome"] == "applied"


class TestAddHeading:
    def test_creates_new_heading(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "add-heading", "--heading", "Travel",
            "--line", "Default vehicle is motorcycle",
            "--line", "Prefer overnight trains",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "## Travel" in body
        assert "Default vehicle is motorcycle" in body
        assert "Prefer overnight trains" in body

    def test_duplicate_heading_rejected(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["add-heading", "--heading", "Notes", "--line", "Already"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_exists"


class TestRemove:
    def test_removes_unique_match(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["remove", "--heading", "Notes", "--match", "note 1"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "Existing note 1" not in user_md.read_text()
        assert "Existing note 2" in user_md.read_text()

    def test_match_in_subsection_rejected(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        with pytest.raises(SystemExit):
            memory_main([
                "remove", "--heading", "Communication style",
                "--match", "sign off",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "match_in_subsection"
        assert user_md.read_text() == before


class TestShowHeadings:
    def test_headings_list(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        memory_main(["headings"])
        out = json.loads(capsys.readouterr().out)
        assert out["headings"] == ["Notes", "Communication style"]

    def test_show_one_heading(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        memory_main(["show", "--heading", "Notes"])
        out = capsys.readouterr().out
        assert "## Notes" in out
        assert "Existing note 1" in out

    def test_show_missing_heading(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["show", "--heading", "Nope"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_missing"
        assert "Notes" in out["available_headings"]


class TestChannel:
    def _setup(self, tmp_path, monkeypatch, token="room-1"):
        mount = tmp_path / "mount"
        ch_dir = mount / "Channels" / token
        ch_dir.mkdir(parents=True)
        ch_md = ch_dir / "CHANNEL.md"
        ch_md.write_text("# Channel Memory\n\n## Decisions\n\n- Use Postgres\n\n")
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        return ch_md

    def test_channel_append(self, tmp_path, monkeypatch, capsys):
        ch_md = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room-1")
        memory_main([
            "append", "--heading", "Decisions",
            "--line", "Use Redis for queues", "--channel", "room-1",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "Use Redis for queues" in ch_md.read_text()

    def test_channel_token_mismatch_refused(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, token="room-1")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room-1")
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--heading", "Decisions", "--line", "X",
                "--channel", "room-OTHER",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel token mismatch — refusing cross-channel write"


class TestAtomicWrite:
    def test_no_partial_write_on_reject(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        # multiple_matches: "note" appears in both bullets
        with pytest.raises(SystemExit):
            memory_main(["remove", "--heading", "Notes", "--match", "note"])
        capsys.readouterr()
        assert user_md.read_text() == before
