"""Tests for ``istota user ensure --disabled-module`` flag.

The Phase 1 modules / connected services refactor added a
``disabled_modules`` JSON column to ``user_profiles``. The CLI needs a
flag so Ansible can provision per-user module opt-outs without going
through the web UI.

Mirrors the existing ``--disabled-skill`` flag: repeatable, validated
against ``modules.MODULE_NAMES``, and writing the flag at all replaces
the existing list (consistent with how ``user_profiles.update_profile``
treats list-shaped columns).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import db, user_profiles


class _FakeArgs:
    """Match the argparse defaults so omitted flags are ``None``."""

    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "name": None,
            "display_name": None,
            "tz": None,
            "email": None,
            "trusted_sender": None,
            "quiet_sender": None,
            "log_channel": None,
            "alerts_channel": None,
            "max_foreground_workers": None,
            "max_background_workers": None,
            "disabled_skill": None,
            "disabled_module": None,
            "default_destination": None,
            "route": None,
            "email_reply_routing": None,
            "outbound_approval": None,
            "external_turn_display": None,
            "default_briefings": None,
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def cfg_with_db(tmp_path: Path, monkeypatch):
    """Minimal config TOML pointing at a fresh, initialized DB."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
        "\n[users.alice]\n"
        'display_name = "Alice"\n'
    )
    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    return cfg, db_path


class TestUserEnsureQuietSender:
    def test_quiet_senders_persist(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice",
            quiet_sender=["*@stratechery.com", "news@x.com"],
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile is not None
        assert profile.quiet_email_senders == ["*@stratechery.com", "news@x.com"]


class TestUserEnsureDisabledModule:
    def test_single_module_persists(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", disabled_module=["feeds"],
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile is not None
        assert profile.disabled_modules == ["feeds"]

    def test_multiple_modules_persist(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice",
            disabled_module=["feeds", "money"],
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert sorted(profile.disabled_modules) == ["feeds", "money"]

    def test_omitted_flag_preserves_existing_list(self, cfg_with_db):
        # Ansible may run `user ensure` for the same user multiple times across
        # plays. A run that doesn't touch --disabled-module must not wipe the
        # list — same partial-update contract as --disabled-skill.
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", disabled_module=["feeds"],
        ))
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", display_name="Alice K",
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.disabled_modules == ["feeds"]
        assert profile.display_name == "Alice K"

    def test_passing_empty_list_clears(self, cfg_with_db):
        # An operator who wants to clear an opt-out re-runs the play without
        # the flag and expects the list to stay; clearing requires the
        # explicit empty form. argparse gives us [] when flag is passed zero
        # times via the action="append" default of None, so the way to clear
        # is `--disabled-module ""` — which we treat as "set to empty".
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", disabled_module=["feeds"],
        ))
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", disabled_module=[""],
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.disabled_modules == []

    def test_unknown_module_is_rejected(self, cfg_with_db, capsys):
        # Validate against MODULE_NAMES so a typo doesn't silently disable
        # nothing — Ansible should fail loud.
        from istota.cli import cmd_user_ensure

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit) as excinfo:
            cmd_user_ensure(_FakeArgs(
                config=str(cfg), name="alice",
                disabled_module=["feeeds"],
            ))
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "unknown module" in err.lower()
        assert "feeeds" in err

    def test_replaces_existing_list_when_passed(self, cfg_with_db):
        # Mirrors update_profile semantics: passing the flag replaces the
        # whole list rather than appending.
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice",
            disabled_module=["feeds", "money"],
        ))
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", disabled_module=["location"],
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.disabled_modules == ["location"]


class TestUserEnsureRouting:
    def test_route_and_default_destination_persist(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice",
            route=["alert=email", "briefing=talk:room"],
            default_destination="both",
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.routing == {"alert": "email", "briefing": "talk:room"}
        assert profile.default_destination == "both"

    def test_route_empty_descriptor_clears_purpose(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", route=["alert="],
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.routing == {}

    def test_route_without_equals_exits(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        with pytest.raises(SystemExit):
            cmd_user_ensure(_FakeArgs(
                config=str(cfg), name="alice", route=["alert"],
            ))


class TestUserEnsureDefaultBriefings:
    def test_default_is_on(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice"))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.default_briefings is True

    def test_opt_out_persists(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", default_briefings=False,
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.default_briefings is False

    def test_omitted_flag_preserves_existing(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", default_briefings=False,
        ))
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", display_name="Alice K",
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.default_briefings is False
        assert profile.display_name == "Alice K"

    def test_re_enable(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", default_briefings=False))
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice", default_briefings=True))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.default_briefings is True


class TestUserEnsureOutboundApproval:
    """`istota user ensure --outbound-approval` — the per-user half of the
    outbound email approval gate, and the flag Ansible threads.

    The column carries a three-state policy plus a fourth state that is not one
    of the three: `''` means *unset*, i.e. follow the operator's
    `[email] outbound_approval_floor`. That distinction is the whole reason the
    flag validates by hand instead of using argparse `choices` — "" has to be
    accepted and named in the error.
    """

    def test_unset_by_default(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice"))
        profile = user_profiles.get_profile(db_path, "alice")
        # Not "off": a user who never chose follows the floor, which is what
        # lets an operator raise it and reach everyone.
        assert profile.outbound_approval == ""

    @pytest.mark.parametrize("policy", ["off", "untrusted", "all"])
    def test_each_policy_persists(self, cfg_with_db, policy):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", outbound_approval=policy,
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.outbound_approval == policy

    def test_empty_value_clears_back_to_following_the_floor(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", outbound_approval="all",
        ))
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", outbound_approval="",
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.outbound_approval == ""

    def test_omitted_flag_preserves_existing(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", outbound_approval="all",
        ))
        # A redeploy that does not mention the policy must not reset it —
        # the same non-clobber rule timezone has.
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", display_name="Alice K",
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.outbound_approval == "all"

    def test_unknown_policy_exits(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit):
            cmd_user_ensure(_FakeArgs(
                config=str(cfg), name="alice", outbound_approval="none",
            ))

    def test_a_rejected_value_writes_nothing(self, cfg_with_db):
        """A typo must not half-apply: the row keeps whatever it had.

        This is a security floor, so silently landing on a *weaker* value than
        the operator typed is the failure worth pinning.
        """
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", outbound_approval="all",
        ))
        with pytest.raises(SystemExit):
            cmd_user_ensure(_FakeArgs(
                config=str(cfg), name="alice", outbound_approval="offf",
            ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.outbound_approval == "all"


class TestUserEnsureExternalTurnDisplay:
    def test_default_is_collapsed(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(config=str(cfg), name="alice"))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.external_turn_display == "collapsed"

    @pytest.mark.parametrize("value", ["full", "collapsed", "hidden"])
    def test_each_value_persists(self, cfg_with_db, value):
        from istota.cli import cmd_user_ensure

        cfg, db_path = cfg_with_db
        cmd_user_ensure(_FakeArgs(
            config=str(cfg), name="alice", external_turn_display=value,
        ))
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.external_turn_display == value

    def test_unknown_value_exits(self, cfg_with_db):
        from istota.cli import cmd_user_ensure

        cfg, _ = cfg_with_db
        with pytest.raises(SystemExit):
            cmd_user_ensure(_FakeArgs(
                config=str(cfg), name="alice", external_turn_display="collapse",
            ))

    def test_cli_and_web_validate_against_one_list(self):
        """The CLI, the web profile PUT and the column default must agree.

        Three surfaces write this column; a second hand-maintained tuple is the
        one that drifts, and the symptom would be a value the CLI accepts and
        the pane cannot render.
        """
        from istota import user_profiles as up
        from istota.web_app import _PROFILE_EDITABLE_FIELDS

        assert (
            _PROFILE_EDITABLE_FIELDS["external_turn_display"]["values"]
            is up.EXTERNAL_TURN_DISPLAY_VALUES
        )
