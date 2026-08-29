"""Tests for the `skills` core CLI skill (Part A, Stage 2)."""

import argparse
import json

import pytest

from istota.config import Config, UserConfig


def _write_skill(bundled, name, body, *, admin_only=False, experimental=False, cli=True, dependencies=None, resource_types=None, companion_skills=None, requires_capability=None):
    d = bundled / name
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}", "description: the {0} skill".format(name), f"cli: {'true' if cli else 'false'}"]
    if requires_capability:
        fm.append("requires_capability: [" + ", ".join(requires_capability) + "]")
    if admin_only:
        fm.append("admin_only: true")
    if experimental:
        fm.append("experimental: true")
    if dependencies:
        fm.append("dependencies: [" + ", ".join(dependencies) + "]")
    if resource_types:
        fm.append("resource_types: [" + ", ".join(resource_types) + "]")
    if companion_skills:
        fm.append("companion_skills: [" + ", ".join(companion_skills) + "]")
    fm.append("---")
    (d / "skill.md").write_text("\n".join(fm) + "\n" + body)
    return d


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "developer", "# Developer\n\nUse {scripts_dir} for {user_id}'s scripts.\n")
    _write_skill(bundled, "secret_admin", "# Admin only\n", admin_only=True)
    _write_skill(bundled, "labs", "# Experimental\n", experimental=True)
    _write_skill(bundled, "needy", "# Needs deps\n", dependencies=["nonexistent_pkg_xyz"])
    _write_skill(bundled, "noteskill", "# Notes\n", resource_types=["notes_folder"])

    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=tmp_path,
        bundled_skills_dir=bundled,
        skills_dir=tmp_path / "ops_skills",
        users={"alice": UserConfig()},
        admin_users={"boss"},  # alice is NOT admin
    )
    monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
    return config


class TestShow:
    def test_show_renders_body_with_substitution(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="developer"))
        out = capsys.readouterr().out
        assert "# Developer" in out
        # {scripts_dir} and {user_id} substituted.
        assert "{scripts_dir}" not in out
        assert "{user_id}" not in out
        assert "alice" in out

    def test_show_unknown_skill_errors(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="nope"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "unknown skill" in payload["error"]

    def test_show_admin_only_refused_for_non_admin(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="secret_admin"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "admin" in payload["error"].lower()

    def test_show_experimental_refused_when_flag_off(self, ctx, capsys):
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="labs"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"

    def test_show_disabled_skill_refused(self, ctx, capsys, monkeypatch):
        ctx.users["alice"].disabled_skills = ["developer"]
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="developer"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "disabled" in payload["error"]

    def test_show_missing_deps_refused(self, ctx, capsys):
        # A skill whose Python deps aren't installed can't be selected; the
        # on-demand loader must refuse it too, not serve a body for an
        # unrunnable skill.
        from istota.skills.skills import cmd_show

        with pytest.raises(SystemExit) as e:
            cmd_show(argparse.Namespace(name="needy"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "depend" in payload["error"].lower()

    def test_show_resource_gated_skill_loadable_without_resource(self, ctx, capsys):
        # notes/spec/todos are doc-only conventions with sensible defaults, so a
        # resource_types skill is loadable even when the user declared no
        # matching resource — there is deliberately no resource gate.
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="noteskill"))
        out = capsys.readouterr().out
        assert "# Notes" in out


class TestList:
    def test_list_excludes_restricted_skills(self, ctx, capsys):
        from istota.skills.skills import cmd_list

        cmd_list(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        names = {s["name"] for s in payload["skills"]}
        assert "developer" in names
        # admin_only + experimental skills are filtered for this non-admin caller.
        assert "secret_admin" not in names
        assert "labs" not in names
        # missing-dependency skills are filtered too (mirrors the catalogue gate).
        assert "needy" not in names
        # resource_types doc skills are NOT resource-gated (sensible defaults).
        assert "noteskill" in names

    def test_admin_sees_admin_only(self, ctx, capsys, monkeypatch):
        ctx.admin_users = {"alice"}
        from istota.skills.skills import cmd_list

        cmd_list(argparse.Namespace())
        payload = json.loads(capsys.readouterr().out)
        names = {s["name"] for s in payload["skills"]}
        assert "secret_admin" in names


class TestDevboxFold:
    """A capability-gated skill (devbox→devbox) must be hidden from this CLI
    when its capability is unavailable, mirroring the executor's disabled-fold
    so `skills list`/`show` agree with the menu."""

    def _add_devbox(self, ctx, tmp_path):
        _write_skill(
            ctx.bundled_skills_dir, "devbox", "# Devbox\n\nRun things.\n",
            requires_capability=["devbox"],
        )

    def test_devbox_hidden_when_disabled(self, ctx, tmp_path, capsys):
        self._add_devbox(ctx, tmp_path)
        assert ctx.devbox.enabled is False  # default
        from istota.skills.skills import cmd_list, cmd_show

        cmd_list(argparse.Namespace())
        names = {s["name"] for s in json.loads(capsys.readouterr().out)["skills"]}
        assert "devbox" not in names

        with pytest.raises(SystemExit):
            cmd_show(argparse.Namespace(name="devbox"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"

    def test_devbox_visible_when_enabled(self, ctx, tmp_path, capsys):
        self._add_devbox(ctx, tmp_path)
        ctx.devbox.enabled = True
        from istota.skills.skills import cmd_list, cmd_show

        cmd_list(argparse.Namespace())
        names = {s["name"] for s in json.loads(capsys.readouterr().out)["skills"]}
        assert "devbox" in names

        cmd_show(argparse.Namespace(name="devbox"))
        out = capsys.readouterr().out
        assert "Run things." in out


class TestShowCompanions:
    """`skills show <name>` delivers the skill's companions in the same
    response — the safety-critical change (a menu-pulled ingest skill arrives
    with its guardrails, not at the model's discretion)."""

    def _ctx(self, tmp_path, monkeypatch, **skill_kwargs):
        bundled = tmp_path / "bundled"
        _write_skill(bundled, "browse", "# Browse\n\nFetch pages.\n",
                     companion_skills=["untrusted_input"])
        _write_skill(bundled, "untrusted_input", "# Untrusted Input\n\nGUARDRAILS HERE.\n", cli=False)
        _write_skill(bundled, "health", "# Health\n\nNo companions.\n")
        # A skill whose only companion is missing from the index.
        _write_skill(bundled, "lonely", "# Lonely\n", companion_skills=["ghost"])
        # A skill whose companion is admin-only (gated off for non-admin alice).
        _write_skill(bundled, "gated", "# Gated\n", companion_skills=["adminhelper"])
        _write_skill(bundled, "adminhelper", "# Admin Helper\n", admin_only=True, cli=False)

        config = Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "tmp",
            nextcloud_mount_path=tmp_path,
            bundled_skills_dir=bundled,
            skills_dir=tmp_path / "ops_skills",
            users={"alice": UserConfig()},
            admin_users={"boss"},  # alice not admin
        )
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        return config

    def test_show_appends_companion_body(self, tmp_path, monkeypatch, capsys):
        self._ctx(tmp_path, monkeypatch)
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="browse"))
        out = capsys.readouterr().out
        assert "# Browse" in out
        assert "GUARDRAILS HERE." in out
        assert "<!-- companion: untrusted_input -->" in out

    def test_show_no_companions_is_clean(self, tmp_path, monkeypatch, capsys):
        self._ctx(tmp_path, monkeypatch)
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="health"))
        out = capsys.readouterr().out
        assert "# Health" in out
        assert "<!-- companion" not in out

    def test_show_missing_companion_marks_unavailable(self, tmp_path, monkeypatch, capsys, caplog):
        import logging
        self._ctx(tmp_path, monkeypatch)
        from istota.skills.skills import cmd_show

        with caplog.at_level(logging.WARNING, logger="istota.skills"):
            cmd_show(argparse.Namespace(name="lonely"))
        out = capsys.readouterr().out
        assert "# Lonely" in out  # primary still rendered
        assert "<!-- companion ghost: unavailable -->" in out
        assert any("ghost" in r.message for r in caplog.records)

    def test_show_gated_companion_marked_unavailable(self, tmp_path, monkeypatch, capsys):
        self._ctx(tmp_path, monkeypatch)
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="gated"))
        out = capsys.readouterr().out
        # admin-only companion is filtered for non-admin alice → marker, no body.
        assert "# Admin Helper" not in out
        assert "<!-- companion adminhelper: unavailable -->" in out


class TestShowOverlays:
    """The `skills show` half of per-skill user overlays.

    The eager half is witnessed by `tests/test_prompt_golden.py`'s
    `skill_overlay` case. Neither path applies the overlay itself — both hand a
    directory to `load_skills`, which is where the injection lives, so the two
    cannot render it differently.
    """

    def _ctx(self, tmp_path, monkeypatch, *, mount=True):
        bundled = tmp_path / "bundled"
        _write_skill(bundled, "primary", "# Primary\n\nPRIMARY BODY.\n",
                     companion_skills=["helper"])
        _write_skill(bundled, "helper", "# Helper\n\nHELPER BODY.\n", cli=False)

        mount_root = tmp_path / "mount"
        overlays = mount_root / "Users/alice/istota/config/skills"
        overlays.mkdir(parents=True)

        config = Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "tmp",
            nextcloud_mount_path=mount_root if mount else None,
            bundled_skills_dir=bundled,
            skills_dir=tmp_path / "ops_skills",
            users={"alice": UserConfig()},
            admin_users={"boss"},
        )
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        return overlays

    def test_show_appends_the_overlay_with_the_user_id_substituted(
        self, tmp_path, monkeypatch, capsys
    ):
        overlays = self._ctx(tmp_path, monkeypatch)
        (overlays / "primary.md").write_text("- Never run the full suite here.\n")
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="primary"))
        out = capsys.readouterr().out

        assert "#### alice's configuration for this skill" in out
        assert "- Never run the full suite here." in out
        assert "{user_id}" not in out

    def test_the_overlay_never_follows_a_safety_companion(
        self, tmp_path, monkeypatch, capsys
    ):
        """Recency would let an overlay downstream of a guardrail soften it. The
        overlay is injected inside `load_skills`, so it lands with the primary
        body and every companion delimiter comes after it."""
        overlays = self._ctx(tmp_path, monkeypatch)
        (overlays / "primary.md").write_text("- An overlay rule.\n")
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="primary"))
        out = capsys.readouterr().out

        assert out.index("- An overlay rule.") < out.index("<!-- companion: helper -->")
        assert out.index("- An overlay rule.") < out.index("HELPER BODY.")

    def test_a_companion_body_carries_no_overlay(self, tmp_path, monkeypatch, capsys):
        """A companion rides as a guardrail, not as the skill being configured."""
        overlays = self._ctx(tmp_path, monkeypatch)
        (overlays / "helper.md").write_text("- HELPER OVERLAY.\n")
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="primary"))
        out = capsys.readouterr().out

        assert "HELPER BODY." in out
        assert "HELPER OVERLAY." not in out
        assert "configuration for this skill" not in out

    def test_the_same_overlay_applies_when_that_skill_is_the_primary(
        self, tmp_path, monkeypatch, capsys
    ):
        """The control for the test above: the file is live, it is the companion
        *rendering* that skips it."""
        overlays = self._ctx(tmp_path, monkeypatch)
        (overlays / "helper.md").write_text("- HELPER OVERLAY.\n")
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="helper"))
        out = capsys.readouterr().out

        assert "HELPER OVERLAY." in out

    def test_both_load_paths_derive_the_same_directory(self, tmp_path, monkeypatch):
        """The half of "both paths behave identically" that is actually
        duplicated.

        The injected block's *content* cannot differ — both paths hand a
        directory to `load_skills` and neither renders anything itself. What each
        caller computes for itself is the directory, and a wrong `bot_dir` or a
        missing `lstrip` there leaves one path silently inert while both suites
        stay green. Both now go through one helper; this is what says so.
        """
        self._ctx(tmp_path, monkeypatch)
        from istota.config import load_config
        from istota.skills.skills import _overlay_dir
        from istota.storage import resolve_user_skill_overlays_dir

        config = load_config()

        assert _overlay_dir(config, "alice") == resolve_user_skill_overlays_dir(
            config, "alice"
        )
        # And it is the documented layout, not merely two equal wrong answers.
        assert _overlay_dir(config, "alice") == (
            config.nextcloud_mount_path / "Users/alice/istota/config/skills"
        )

    def test_a_redirected_config_directory_yields_no_overlay_dir(
        self, tmp_path, monkeypatch
    ):
        """`config` and `skills` are ordinary entries under a root bound
        read-write into that user's own sandbox, and the loader's `O_NOFOLLOW`
        covers only the overlay file. The files at the far end of a redirected
        directory are ordinary regular files that pass every leaf-level guard,
        so the directory itself is where containment has to be."""
        overlays = self._ctx(tmp_path, monkeypatch)
        from istota.config import load_config
        from istota.storage import resolve_user_skill_overlays_dir

        config = load_config()
        assert resolve_user_skill_overlays_dir(config, "alice") is not None

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "primary.md").write_text("- planted\n")
        import shutil

        shutil.rmtree(overlays)
        overlays.symlink_to(elsewhere, target_is_directory=True)

        assert resolve_user_skill_overlays_dir(config, "alice") is None

    def test_a_link_that_stays_inside_the_users_own_tree_is_allowed(
        self, tmp_path, monkeypatch
    ):
        """The control. Someone who reorganised their own config directory
        leads nowhere they could not already reach, and refusing that would
        make the guard about symlinks rather than about containment."""
        overlays = self._ctx(tmp_path, monkeypatch)
        from istota.config import load_config
        from istota.storage import resolve_user_skill_overlays_dir

        config = load_config()
        inside = config.nextcloud_mount_path / "Users/alice/skills-real"
        inside.mkdir(parents=True)
        import shutil

        shutil.rmtree(overlays)
        overlays.symlink_to(inside, target_is_directory=True)

        assert resolve_user_skill_overlays_dir(config, "alice") == inside

    def test_no_mount_means_no_overlay(self, tmp_path, monkeypatch, capsys):
        """Overlays are filesystem reads, so an rclone-remote deployment skips
        them silently — the condition the per-user PERSONA.md already carries."""
        overlays = self._ctx(tmp_path, monkeypatch, mount=False)
        (overlays / "primary.md").write_text("- An overlay rule.\n")
        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="primary"))
        out = capsys.readouterr().out

        assert "PRIMARY BODY." in out
        assert "An overlay rule." not in out
        assert "configuration for this skill" not in out
