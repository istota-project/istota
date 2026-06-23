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

    def test_removes_bullet_under_subsection(self, tmp_path, monkeypatch, capsys):
        # Removal now reaches into `### subsections` so stale bullets there
        # can be pruned (previously rejected as match_in_subsection).
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "remove", "--heading", "Communication style",
            "--match", "sign off",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "Always sign off with name" not in body
        # The subheading and the other section content survive.
        assert "### Email" in body
        assert "Prefers short replies" in body


class TestReplaceCli:
    def test_replace_unique_bullet(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "replace", "--heading", "Notes",
            "--match", "note 1", "--line", "Reworded note one",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "- Reworded note one" in body
        assert "Existing note 1" not in body

    def test_replace_in_subsection(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "replace", "--heading", "Communication style",
            "--match", "sign off", "--line", "Sign off with first name only",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "Sign off with first name only" in body
        assert "Always sign off with name" not in body


class TestRemoveHeadingCli:
    def test_remove_heading_drops_section(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["remove-heading", "--heading", "Notes"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "## Notes" not in body
        assert "## Communication style" in body

    def test_remove_heading_missing(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["remove-heading", "--heading", "Nope"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_missing"


class TestAppendSubheadingCli:
    def test_append_under_subheading(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "append", "--heading", "Communication style",
            "--subheading", "Email", "--line", "Use plain text, no HTML",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "Use plain text, no HTML" in body
        # Lands under the Email subsection, after its existing bullet.
        assert body.index("Always sign off") < body.index("Use plain text")


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


class TestLockAnchorPlacement:
    def test_anchor_goes_under_deferred_dir_not_tmp(
        self, tmp_path, monkeypatch, capsys
    ):
        # With ISTOTA_DEFERRED_DIR set (the daemon path), the flock anchor must
        # live under that per-user dir — it's bind-mounted into the sandbox, so
        # a sandboxed CLI and the host curator share the same inode.
        user_md = _setup_user(tmp_path, monkeypatch)
        deferred = tmp_path / "deferred" / "alice"
        deferred.mkdir(parents=True)
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
        memory_main(["append", "--heading", "Notes", "--line", "Anchored"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "- Anchored" in user_md.read_text()
        # Anchor created under the deferred dir, not as a mount sibling.
        anchors = list((deferred / ".md-locks").glob("USER.md.*.lock"))
        assert anchors, "expected a flock anchor under ISTOTA_DEFERRED_DIR/.md-locks"
        assert not (user_md.parent / "USER.md.lock").exists()


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

    def test_channel_refused_when_env_token_unset(self, tmp_path, monkeypatch, capsys):
        # ISSUE-075: empty/unset ISTOTA_CONVERSATION_TOKEN must refuse --channel,
        # not pass through. Otherwise prompt-injected non-Talk tasks (email,
        # briefing, scheduled, cron, subtask) can write into any channel's
        # CHANNEL.md by passing --channel <victim_token>.
        self._setup(tmp_path, monkeypatch, token="room-1")
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--heading", "Decisions", "--line", "X",
                "--channel", "room-1",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel write requires ISTOTA_CONVERSATION_TOKEN"

    def test_channel_refused_when_env_token_empty(self, tmp_path, monkeypatch, capsys):
        # Same gap: env var present but empty string also short-circuited the
        # original guard. Treat empty as unset.
        self._setup(tmp_path, monkeypatch, token="room-1")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "")
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--heading", "Decisions", "--line", "X",
                "--channel", "room-1",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel write requires ISTOTA_CONVERSATION_TOKEN"

    def test_channel_write_skips_audit_log(self, tmp_path, monkeypatch, capsys):
        # ISSUE-076: channel writes intentionally skip the audit log + the
        # USER.md last_seen sidecar (no per-channel audit infrastructure).
        # Lock in the asymmetry so a future change can't quietly start
        # writing channel-write entries to the user-scoped audit file.
        ch_md = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room-1")
        # Materialize a USER.md so the audit path is resolvable and the
        # absence of a write is meaningful (not just a missing parent dir).
        user_md_dir = tmp_path / "mount" / "Users" / "alice" / "istota" / "config"
        user_md_dir.mkdir(parents=True, exist_ok=True)
        (user_md_dir / "USER.md").write_text("# User Memory\n\n")

        memory_main([
            "append", "--heading", "Decisions",
            "--line", "Use Redis for queues", "--channel", "room-1",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "Use Redis for queues" in ch_md.read_text()

        # No audit JSONL, no last_seen sidecar.
        assert not (user_md_dir / "USER.md.audit.jsonl").exists()
        assert not (user_md_dir / "USER.md.last_seen.json").exists()


class TestAtomicWrite:
    def test_no_partial_write_on_reject(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        # multiple_matches: "note" appears in both bullets
        with pytest.raises(SystemExit):
            memory_main(["remove", "--heading", "Notes", "--match", "note"])
        capsys.readouterr()
        assert user_md.read_text() == before


class TestBotDirFallback:
    """ISSUE-077: when ISTOTA_BOT_DIR_NAME is unset, refuse to guess between
    multiple candidate bot dirs. Single-candidate fallback still works."""

    def _setup_two_bots(self, tmp_path, monkeypatch, user_id="alice"):
        mount = tmp_path / "mount"
        for bot_dir in ("istota", "zorg"):
            d = mount / "Users" / user_id / bot_dir / "config"
            d.mkdir(parents=True)
            (d / "USER.md").write_text(SEED_USER_MD)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        return mount

    def test_multiple_candidates_refused(self, tmp_path, monkeypatch, capsys):
        self._setup_two_bots(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "X"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "multiple bot dirs" in out["error"]
        assert sorted(out["candidates"]) == ["istota", "zorg"]

    def test_single_candidate_used(self, tmp_path, monkeypatch, capsys):
        mount = tmp_path / "mount"
        d = mount / "Users" / "alice" / "istota" / "config"
        d.mkdir(parents=True)
        user_md = d / "USER.md"
        user_md.write_text(SEED_USER_MD)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        memory_main(["append", "--heading", "Notes", "--line", "Inferred"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert "- Inferred" in user_md.read_text()

    def test_zero_candidates_refused(self, tmp_path, monkeypatch, capsys):
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "X"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "could not infer" in out["error"]
