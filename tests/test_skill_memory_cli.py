"""Tests for istota.skills.memory — runtime memory CLI."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import db
from istota.memory.curation.audit import (
    AUDIT_NAMESPACE,
    CURATION_NAMESPACE,
    LAST_SEEN_KEY,
)
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
    "- Use plain text\n"
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
    # The audit trail and the USER.md fingerprint are rows in the framework
    # KV store, so the CLI needs the DB the skill proxy hands every skill CLI.
    db_path = tmp_path / "istota.db"
    if not db_path.exists():
        db.init_db(db_path)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    return user_md


def _audit_entries(tmp_path, user_id="alice"):
    """The audit rows the CLI wrote, oldest first."""
    with db.get_db(tmp_path / "istota.db") as conn:
        rows = db.kv_list(conn, user_id, AUDIT_NAMESPACE)
    return [json.loads(r["value"]) for r in rows]


def _last_seen(tmp_path, user_id="alice"):
    with db.get_db(tmp_path / "istota.db") as conn:
        return db.kv_get(conn, user_id, CURATION_NAMESPACE, LAST_SEEN_KEY)


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
        _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Audit me"])
        capsys.readouterr()
        entries = _audit_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["source"] == "runtime"
        assert entries[0]["entry_kind"] == "batch"
        assert entries[0]["applied"][0]["outcome"] == "applied"

    def test_append_writes_no_sidecar_files(self, tmp_path, monkeypatch, capsys):
        """The whole point of the move: the user's config folder holds the
        documents they wrote and nothing the machine keeps for itself."""
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Audit me"])
        capsys.readouterr()
        assert sorted(p.name for p in user_md.parent.iterdir()) == ["USER.md"]


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
        # The subheading and the other section content survive. `### Email`
        # keeps a second bullet, so this stays a test about scoping rather than
        # about the delete-on-empty rule (tests/test_curation_subheading_removal.py).
        assert "### Email" in body
        assert "Use plain text" in body
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
        # A reachable store that stays empty is the assertion; an absent one
        # would make "channel writes are not audited" true for the wrong
        # reason.
        db_path = tmp_path / "istota.db"
        if not db_path.exists():
            db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
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

        # Channel writes are not audited and do not move the USER.md
        # fingerprint. Asserted against the store rather than against absent
        # sidecar files, which nothing writes any more and which would
        # therefore be a check that cannot fail.
        assert _audit_entries(tmp_path) == []
        assert _last_seen(tmp_path) is None


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
        for bot_dir in ("istota", "helper"):
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
        assert sorted(out["candidates"]) == ["helper", "istota"]

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


# --------------------------------------------------------- per-skill overlays

OVERLAY_SKILLS = ("developer", "notes", "sensitive_actions", "untrusted_input", "browse")


@pytest.fixture
def overlay_env(tmp_path, monkeypatch):
    """USER.md set up as usual, plus a Config whose skill index is ours.

    The `--skill` paths load a Config where the rest of this CLI reads env
    vars, so the index has to be pinned or the tests would assert against
    whatever skills happen to be bundled.
    """
    from istota.config import Config, UserConfig

    user_md = _setup_user(tmp_path, monkeypatch)
    bundled = tmp_path / "bundled"
    for name in OVERLAY_SKILLS:
        d = bundled / name
        d.mkdir(parents=True)
        (d / "skill.md").write_text(
            f"---\nname: {name}\ndescription: the {name} skill\n---\n\n# {name}\n"
        )
    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=tmp_path / "mount",
        bundled_skills_dir=bundled,
        skills_dir=tmp_path / "ops_skills",
        users={"alice": UserConfig()},
    )
    monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
    overlays = user_md.parent / "skills"
    return SimpleNamespace(user_md=user_md, overlays=overlays, config=config)


class TestOverlayAppend:
    def test_append_creates_the_directory_and_the_file(self, overlay_env, capsys):
        assert not overlay_env.overlays.exists()
        memory_main([
            "append", "--skill", "developer",
            "--line", "Never run the full suite here",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["outcome"] == "applied"
        assert out["skill"] == "developer"
        f = overlay_env.overlays / "developer.md"
        assert f.read_text() == "- Never run the full suite here\n"
        assert oct(f.stat().st_mode)[-3:] == "644"
        assert oct(overlay_env.overlays.stat().st_mode)[-3:] == "755"

    def test_append_to_an_existing_overlay(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- first rule\n")
        memory_main(["append", "--skill", "developer", "--line", "second rule"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert f.read_text() == "- first rule\n- second rule\n"

    def test_append_duplicate_is_a_noop(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- first rule\n")
        memory_main(["append", "--skill", "developer", "--line", "First Rule"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "noop_dup"
        assert f.read_text() == "- first rule\n"

    def test_append_under_a_subsection(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- top rule\n\n### Testing\n\n- existing\n")
        memory_main([
            "append", "--skill", "developer", "--heading", "Testing",
            "--line", "added",
        ])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        body = f.read_text()
        assert body.index("- existing") < body.index("- added")

    def test_append_under_a_missing_subsection_lists_what_there_is(
        self, overlay_env, capsys
    ):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text("### Testing\n\n- a\n")
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--skill", "developer", "--heading", "Nope", "--line", "x",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "subheading_missing"
        assert out["available_subheadings"] == ["Testing"]

    def test_a_level_two_heading_cannot_be_written(self, overlay_env, capsys):
        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", "developer", "--line", "## Rules"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "line_starts_with_hash"
        assert not (overlay_env.overlays / "developer.md").exists()

    def test_subheading_flag_is_refused_with_skill(self, overlay_env, capsys):
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--skill", "developer",
                "--subheading", "Testing", "--line", "x",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "subheading_not_valid_with_skill"

    def test_append_writes_an_audit_entry_naming_the_target(
        self, overlay_env, capsys, tmp_path
    ):
        memory_main(["append", "--skill", "developer", "--line", "Audit me"])
        capsys.readouterr()
        entry = _audit_entries(tmp_path)[-1]
        assert entry["source"] == "runtime"
        assert entry["skill"] == "developer"
        assert entry["target_path"] == "Users/alice/istota/config/skills/developer.md"
        assert entry["applied"][0]["outcome"] == "applied"
        # Not USER.md's size — that key is read as USER.md growth.
        assert "user_md_size_bytes" not in entry

    def test_overlay_write_does_not_move_the_user_md_fingerprint(
        self, overlay_env, capsys, tmp_path
    ):
        # last_seen is what the nightly bypass detector compares against.
        # Stamping it here would mask an out-of-band USER.md edit.
        memory_main(["append", "--skill", "developer", "--line", "x"])
        capsys.readouterr()
        assert _last_seen(tmp_path) is None

    def test_a_user_md_write_does_move_the_fingerprint(
        self, overlay_env, capsys, tmp_path
    ):
        """The control for the assertion above: it has to be the overlay
        target that withholds the stamp, not the store being unreachable."""
        memory_main(["append", "--heading", "Notes", "--line", "x"])
        capsys.readouterr()
        assert _last_seen(tmp_path) is not None


class TestOverlayRemoveAndReplace:
    def test_remove_takes_one_bullet(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- alpha rule\n- beta rule\n")
        memory_main(["remove", "--skill", "developer", "--match", "alpha"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert f.read_text() == "- beta rule\n"

    def test_removing_the_last_bullet_deletes_the_file(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- only rule\n")
        memory_main(["remove", "--skill", "developer", "--match", "only"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert out["removed_file"] is True
        assert not f.exists()
        # The directory stays — an empty one is an honest "nothing customized".
        assert overlay_env.overlays.is_dir()

    def test_multiple_matches_leaves_the_file_alone(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- alpha one\n- alpha two\n")
        before = f.read_text()
        with pytest.raises(SystemExit):
            memory_main(["remove", "--skill", "developer", "--match", "alpha"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "multiple_matches"
        assert out["skill"] == "developer"
        assert f.read_text() == before

    def test_no_match_is_a_noop_not_an_error(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- alpha\n")
        memory_main(["remove", "--skill", "developer", "--match", "gamma"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "noop_no_match"
        assert f.read_text() == "- alpha\n"

    def test_replace_rewrites_in_place(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "notes.md"
        f.write_text("- author: legacy_name\n- other rule\n")
        memory_main([
            "replace", "--skill", "notes",
            "--match", "author: legacy_name", "--line", "author: current_name",
        ])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert f.read_text() == "- author: current_name\n- other rule\n"


class TestOverlayTargetRefusals:
    def test_unknown_skill_is_refused_with_the_known_names(self, overlay_env, capsys):
        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", "develper", "--line", "x"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "unknown_skill"
        assert out["skill"] == "develper"
        assert "developer" in out["available_skills"]
        assert not overlay_env.overlays.exists()

    def test_a_traversal_name_is_not_a_known_skill(self, overlay_env, capsys):
        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", "../../USER", "--line", "x"])
        assert json.loads(capsys.readouterr().out)["error"] == "unknown_skill"

    @pytest.mark.parametrize("skill", ["sensitive_actions", "untrusted_input"])
    def test_append_to_a_denylisted_skill_is_refused(self, overlay_env, capsys, skill):
        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", skill, "--line", "be nicer"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "denylisted_skill"
        assert out["skill"] == skill
        assert not (overlay_env.overlays / f"{skill}.md").exists()

    def test_replace_on_a_denylisted_skill_is_refused(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "untrusted_input.md"
        f.write_text("- planted by hand\n")
        with pytest.raises(SystemExit):
            memory_main([
                "replace", "--skill", "untrusted_input",
                "--match", "planted", "--line", "softened",
            ])
        assert json.loads(capsys.readouterr().out)["error"] == "denylisted_skill"
        assert f.read_text() == "- planted by hand\n"

    def test_remove_on_a_denylisted_skill_is_the_escape_hatch(
        self, overlay_env, capsys
    ):
        # `remove` and `show` cannot put text into the safety layer, and
        # refusing them would leave a hand-planted file unreadable and
        # undeletable through the only sanctioned write path.
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "untrusted_input.md"
        f.write_text("- planted by hand\n")
        memory_main(["remove", "--skill", "untrusted_input", "--match", "planted"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert not f.exists()

    def test_skill_and_channel_together_are_refused(self, overlay_env, capsys):
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--skill", "developer", "--channel", "room-1", "--line", "x",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "skill_and_channel_are_exclusive"
        assert not overlay_env.overlays.exists()

    @pytest.mark.parametrize("verb", ["add-heading", "remove-heading"])
    def test_heading_ops_are_refused_with_skill(self, overlay_env, capsys, verb):
        argv = [verb, "--skill", "developer", "--heading", "Rules"]
        if verb == "add-heading":
            argv += ["--line", "x"]
        with pytest.raises(SystemExit):
            memory_main(argv)
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_ops_not_valid_with_skill"
        assert out["verb"] == verb

    def test_user_md_ops_still_require_a_heading(self, overlay_env, capsys):
        with pytest.raises(SystemExit):
            memory_main(["append", "--line", "x"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_required"


class TestOverlayPlantedPaths:
    """The overlay directory is bound read-write into the user's sandbox and
    this CLI runs host-side with the daemon's filesystem view, so every entry
    in it is model-plantable."""

    def test_a_symlink_at_the_overlay_path_is_refused_not_followed(
        self, tmp_path, overlay_env, capsys
    ):
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN\n")
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").symlink_to(secret)

        with pytest.raises(SystemExit):
            memory_main(["show", "--skill", "developer"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_is_a_symlink"

    def test_a_symlink_is_refused_on_the_write_path_too(
        self, tmp_path, overlay_env, capsys
    ):
        victim = tmp_path / "victim.md"
        victim.write_text("- untouched\n")
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").symlink_to(victim)

        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", "developer", "--line", "planted"])
        assert json.loads(capsys.readouterr().out)["error"] == "overlay_is_a_symlink"
        assert victim.read_text() == "- untouched\n"

    def test_a_fifo_at_the_overlay_path_is_refused_without_blocking(
        self, overlay_env, capsys
    ):
        # A blocking `open(2)` here hangs host-side work until the proxy
        # timeout, so the refusal has to come from O_NONBLOCK + S_ISREG rather
        # than from a read that never returns.
        overlay_env.overlays.mkdir(parents=True)
        os.mkfifo(overlay_env.overlays / "developer.md")
        with pytest.raises(SystemExit):
            memory_main(["show", "--skill", "developer"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "overlay_not_a_regular_file"

    def test_a_symlinked_overlay_directory_is_refused(
        self, tmp_path, overlay_env, capsys
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        overlay_env.overlays.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", "developer", "--line", "x"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "overlay_dir_not_a_directory"
        assert list(elsewhere.iterdir()) == []

    @pytest.mark.parametrize("argv", [["show"], ["skills"]])
    def test_the_read_paths_refuse_a_symlinked_directory_too(
        self, tmp_path, overlay_env, capsys, argv
    ):
        # O_NOFOLLOW covers the last path component only, so a redirected
        # directory would otherwise be read straight through on every verb
        # that does not write.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "developer.md").write_text("- TOP SECRET TOKEN\n")
        overlay_env.overlays.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(SystemExit):
            memory_main(argv + (["--skill", "developer"] if argv == ["show"] else []))
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_dir_not_a_directory"

    def test_a_symlinked_ancestor_cannot_redirect_the_write(
        self, tmp_path, overlay_env, capsys
    ):
        # `config/` is an ordinary entry in the read-write-bound user tree, so
        # `mv config config.real && ln -s /anywhere config` is two commands
        # from inside the sandbox — and the leaf `lstat` never sees it. This
        # wrote model-chosen content to a model-chosen directory, and
        # `mkdir(parents=True)` obligingly created `skills/` at the far end.
        victim = tmp_path / "victim"
        victim.mkdir()
        config_dir = overlay_env.overlays.parent
        config_dir.rename(tmp_path / "config.real")
        config_dir.symlink_to(victim, target_is_directory=True)

        with pytest.raises(SystemExit):
            memory_main(["append", "--skill", "developer", "--line", "pwned"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "overlay_dir_outside_user_tree"
        assert list(victim.iterdir()) == []

    def test_a_symlinked_ancestor_cannot_redirect_the_read(
        self, tmp_path, overlay_env, capsys
    ):
        victim = tmp_path / "victim"
        (victim / "skills").mkdir(parents=True)
        (victim / "skills" / "developer.md").write_text("- TOP SECRET TOKEN\n")
        config_dir = overlay_env.overlays.parent
        config_dir.rename(tmp_path / "config.real")
        config_dir.symlink_to(victim, target_is_directory=True)

        with pytest.raises(SystemExit):
            memory_main(["show", "--skill", "developer"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_dir_outside_user_tree"

    def test_an_ancestor_symlink_inside_the_users_own_tree_is_allowed(
        self, overlay_env, capsys
    ):
        # Containment is the user's own root, not the literal path. A link
        # that stays inside the tree the sandbox binds read-write leads
        # nowhere the user could not already reach, so refusing it would be a
        # stricter rule than the threat calls for — and would break a user who
        # had reorganised their own config directory.
        config_dir = overlay_env.overlays.parent
        real = config_dir.parent / "config.real"
        config_dir.rename(real)
        config_dir.symlink_to(real, target_is_directory=True)

        memory_main(["append", "--skill", "developer", "--line", "a rule"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert (real / "skills" / "developer.md").read_text() == "- a rule\n"


class TestOverlayUnreadableFiles:
    """The three refusals `_read_overlay_bytes` can return that no planted
    inode produces. Each must reach the caller as an error — reporting one as
    "file absent" is what would let `append` clobber it."""

    def test_a_file_over_the_read_cap_is_refused_on_both_paths(
        self, overlay_env, capsys
    ):
        from istota.skills.memory import _MAX_OVERLAY_READ_BYTES

        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- padding\n" * (_MAX_OVERLAY_READ_BYTES // 10 + 10))
        with pytest.raises(SystemExit):
            memory_main(["show", "--skill", "developer"])
        assert json.loads(capsys.readouterr().out)["error"] == (
            "overlay_unreadably_large"
        )
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "overlay_unreadably_large"

    def test_a_non_utf8_file_is_refused_on_both_paths(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_bytes(b"- caf\xff\xfe\n")
        with pytest.raises(SystemExit):
            memory_main(["show", "--skill", "developer"])
        assert json.loads(capsys.readouterr().out)["error"] == "overlay_not_utf8"
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "overlay_not_utf8"

    @pytest.mark.requires_dac
    def test_an_unreadable_file_is_refused_not_treated_as_absent(
        self, overlay_env, capsys
    ):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- a rule\n")
        f.chmod(0o000)
        try:
            with pytest.raises(SystemExit):
                memory_main(["append", "--skill", "developer", "--line", "new"])
            assert json.loads(capsys.readouterr().out)["error"] == (
                "overlay_unreadable"
            )
        finally:
            f.chmod(0o644)
        # Not clobbered by an append that read it as absent.
        assert f.read_text() == "- a rule\n"

    def test_a_write_past_the_read_cap_is_refused_before_it_lands(
        self, overlay_env, capsys
    ):
        # Otherwise one oversized append produces a file the loader ignores
        # and this CLI can no longer show, edit or shrink — recoverable only
        # from a host shell.
        from istota.skills.memory import _MAX_OVERLAY_READ_BYTES

        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- seed\n")
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--skill", "developer",
                "--line", "x" * (_MAX_OVERLAY_READ_BYTES + 10),
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "overlay_would_exceed_read_cap"
        assert f.read_text() == "- seed\n"
        # Still editable, which is the whole point of refusing.
        memory_main(["show", "--skill", "developer"])
        assert capsys.readouterr().out == "- seed\n"


class TestOverlayFrontmatterOnly:
    """A file holding nothing but frontmatter has bytes and lines and loads as
    nothing. `binds` and delete-on-empty both have to be the loader's answer,
    not a second derivation of it."""

    FM = "---\ncreated: 2026-08-28\nauthor: someone\n---\n\n"

    def test_the_inventory_reports_it_as_not_binding(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text(self.FM)
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "empty"

    def test_removing_the_last_bullet_deletes_it_despite_the_frontmatter(
        self, overlay_env, capsys
    ):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text(self.FM + "- only rule\n")
        memory_main(["remove", "--skill", "developer", "--match", "only"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert out["removed_file"] is True
        assert not f.exists()

    def test_the_loader_agrees_with_the_inventory(self, overlay_env):
        # The property both of the above rest on, asserted directly against
        # the loader's own reduction rather than inferred from the CLI.
        from istota.skills._loader import overlay_effective_body

        assert overlay_effective_body(self.FM) == ""
        assert overlay_effective_body(self.FM + "- a rule") == "- a rule"


class TestOverlayScopedEdits:
    def test_heading_scopes_a_remove_to_its_subsection(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- top rule\n\n### Testing\n\n- nested rule\n")
        memory_main([
            "remove", "--skill", "developer", "--heading", "Testing",
            "--match", "top",
        ])
        assert json.loads(capsys.readouterr().out)["outcome"] == "noop_no_match"
        assert "- top rule" in f.read_text()

    def test_a_misspelled_heading_does_not_mutate_the_file(
        self, overlay_env, capsys
    ):
        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- top rule\n\n### Testing\n\n- nested rule\n")
        before = f.read_text()
        with pytest.raises(SystemExit):
            memory_main([
                "replace", "--skill", "developer", "--heading", "Nope",
                "--match", "top", "--line", "rewritten",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "subheading_missing"
        assert out["available_subheadings"] == ["Testing"]
        assert f.read_text() == before


class TestOverlayShow:
    def test_show_prints_the_whole_overlay(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text("- one\n- two\n")
        memory_main(["show", "--skill", "developer"])
        assert capsys.readouterr().out == "- one\n- two\n"

    def test_show_filters_to_a_subsection(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text(
            "- top\n\n### Testing\n\n- nested\n\n### Other\n\n- elsewhere\n"
        )
        memory_main(["show", "--skill", "developer", "--heading", "Testing"])
        out = capsys.readouterr().out
        assert "### Testing" in out
        assert "- nested" in out
        assert "elsewhere" not in out
        assert "- top" not in out

    def test_show_of_an_absent_overlay_is_empty_not_an_error(self, overlay_env, capsys):
        memory_main(["show", "--skill", "developer"])
        assert capsys.readouterr().out.strip() == ""


class TestOverlayInventory:
    def test_empty_directory_reports_nothing_customized(self, overlay_env, capsys):
        memory_main(["skills"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["skills"] == []

    def test_rows_carry_size_line_count_and_first_line(self, overlay_env, capsys):
        body = "- Never run the full suite here\n- Use scripts/qt\n"
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text(body)
        memory_main(["skills"])
        rows = json.loads(capsys.readouterr().out)["skills"]
        assert len(rows) == 1
        row = rows[0]
        assert row["skill"] == "developer"
        assert row["binds"] is True
        assert row["lines"] == 2
        assert row["first_line"] == "- Never run the full suite here"
        assert row["bytes"] == len(body)

    def test_a_misspelled_file_is_reported_as_not_binding(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "develper.md").write_text("- a rule\n")
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["skill"] == "develper"
        assert row["binds"] is False
        assert row["reason"] == "unknown_skill"

    def test_a_denylisted_file_is_reported_as_not_binding(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "untrusted_input.md").write_text("- planted\n")
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "denylisted"

    def test_a_disabled_skill_is_reported_as_not_binding(self, overlay_env, capsys):
        overlay_env.config.disabled_skills = ["browse"]
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "browse.md").write_text("- a rule\n")
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "skill_disabled"

    def test_a_planted_symlink_is_reported_rather_than_read(
        self, tmp_path, overlay_env, capsys
    ):
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN\n")
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").symlink_to(secret)
        memory_main(["skills"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        row = json.loads(captured)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "overlay_is_a_symlink"
        assert "first_line" not in row

    def test_a_refused_read_reports_a_null_size_rather_than_the_links_own(
        self, tmp_path, overlay_env, capsys
    ):
        """`bytes` is the size taken off the fd, so a refused read has none.
        An `lstat` size would describe the symlink, not the overlay."""
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN\n")
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").symlink_to(secret)
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["bytes"] is None
        assert row["reason"] == "overlay_is_a_symlink"

    def test_an_over_cap_file_is_reported_as_not_binding(self, overlay_env, capsys):
        from istota.skills._loader import OVERLAY_MAX_BYTES

        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text(
            "- padding\n" * (OVERLAY_MAX_BYTES // 10 + 10)
        )
        memory_main(["skills"])
        row = json.loads(capsys.readouterr().out)["skills"][0]
        assert row["binds"] is False
        assert row["reason"] == "over_cap"

    def test_non_markdown_entries_are_not_listed(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md.bak").write_text("- a rule\n")
        (overlay_env.overlays / "README.txt").write_text("hi\n")
        memory_main(["skills"])
        assert json.loads(capsys.readouterr().out)["skills"] == []


class TestOverlayCapWarning:
    def test_crossing_the_hard_cap_warns_that_it_will_not_load(
        self, overlay_env, capsys
    ):
        from istota.skills._loader import OVERLAY_MAX_BYTES

        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- padding\n" * (OVERLAY_MAX_BYTES // 10))
        memory_main(["append", "--skill", "developer", "--line", "one more rule"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "will not be loaded" in out["warning"]

    def test_crossing_the_guidance_warns_without_claiming_it_is_inert(
        self, overlay_env, capsys
    ):
        from istota.skills._loader import OVERLAY_WARN_BYTES

        overlay_env.overlays.mkdir(parents=True)
        f = overlay_env.overlays / "developer.md"
        f.write_text("- padding\n" * (OVERLAY_WARN_BYTES // 10))
        memory_main(["append", "--skill", "developer", "--line", "one more rule"])
        out = json.loads(capsys.readouterr().out)
        assert "over the" in out["warning"]
        assert "will not be loaded" not in out["warning"]



class TestOverlayIndexOnWrite:
    """`reindex_all` covers the nightly sweep; this covers the minutes after a
    write, which is exactly when a user asks whether the rule took."""

    @staticmethod
    def _init_index_db(config):
        schema = Path(__file__).parent.parent / "schema.sql"
        conn = sqlite3.connect(str(config.db_path))
        conn.executescript(schema.read_text())
        conn.commit()
        conn.close()

    @staticmethod
    def _rows(config):
        conn = sqlite3.connect(str(config.db_path))
        try:
            return conn.execute(
                "SELECT source_id, content FROM memory_chunks "
                "WHERE source_type = 'skill_overlay'"
            ).fetchall()
        finally:
            conn.close()

    def test_an_append_is_searchable_immediately(self, overlay_env, capsys):
        self._init_index_db(overlay_env.config)
        memory_main([
            "append", "--skill", "developer",
            "--line", "Never run the full suite in a foreground task",
        ])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"

        rows = self._rows(overlay_env.config)
        assert len(rows) == 1
        assert rows[0][0].endswith("skills/developer.md")
        assert "foreground task" in rows[0][1]

    def test_a_second_append_replaces_rather_than_duplicates(
        self, overlay_env, capsys
    ):
        self._init_index_db(overlay_env.config)
        memory_main(["append", "--skill", "developer", "--line", "first rule"])
        memory_main(["append", "--skill", "developer", "--line", "second rule"])
        capsys.readouterr()

        rows = self._rows(overlay_env.config)
        assert len(rows) == 1
        assert "first rule" in rows[0][1] and "second rule" in rows[0][1]

    def test_removing_the_last_bullet_drops_the_rows_with_the_file(
        self, overlay_env, capsys
    ):
        """The file is deleted when its last bullet goes, so the index has to go
        with it — search returning a rule the prompt no longer carries is worse
        than not indexing at all."""
        self._init_index_db(overlay_env.config)
        memory_main(["append", "--skill", "developer", "--line", "only rule"])
        assert len(self._rows(overlay_env.config)) == 1

        memory_main(["remove", "--skill", "developer", "--match", "only rule"])
        assert json.loads(capsys.readouterr().out.strip().split("\n")[-1])[
            "removed_file"
        ] is True
        assert self._rows(overlay_env.config) == []

    def test_a_removal_that_leaves_content_reindexes_what_is_left(
        self, overlay_env, capsys
    ):
        self._init_index_db(overlay_env.config)
        memory_main(["append", "--skill", "developer", "--line", "keep this rule"])
        memory_main(["append", "--skill", "developer", "--line", "drop this rule"])
        memory_main(["remove", "--skill", "developer", "--match", "drop this"])
        capsys.readouterr()

        rows = self._rows(overlay_env.config)
        assert len(rows) == 1
        assert "keep this rule" in rows[0][1]
        assert "drop this rule" not in rows[0][1]

    def test_indexing_is_off_when_the_operator_turned_it_off(
        self, overlay_env, capsys
    ):
        self._init_index_db(overlay_env.config)
        overlay_env.config.memory_search.auto_index_memory_files = False
        memory_main(["append", "--skill", "developer", "--line", "a rule"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert self._rows(overlay_env.config) == []

    def test_a_write_still_succeeds_when_the_index_cannot_be_opened(
        self, overlay_env, capsys
    ):
        """No schema at `db_path`. The bytes are already on disk by then, so an
        indexing failure must not turn a landed write into an error."""
        memory_main(["append", "--skill", "developer", "--line", "a rule"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert (overlay_env.overlays / "developer.md").read_text() == "- a rule\n"

    def test_a_usermd_write_is_not_filed_as_an_overlay(self, overlay_env, capsys):
        """The overlay source type is scoped to the overlay path — a USER.md
        write must not land rows under it."""
        self._init_index_db(overlay_env.config)
        memory_main(["append", "--heading", "Notes", "--line", "a note"])
        capsys.readouterr()
        assert self._rows(overlay_env.config) == []
