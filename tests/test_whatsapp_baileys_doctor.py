"""What `doctor` says about a paired WhatsApp session.

Three checks, and the first is a subtraction. `whatsapp.billing` reported
"circuit closed; N service attempts of at most 900" on a deployment where
nothing is metered and there is no cap — four Meta facts stated about an
adapter none of them governs. Both reviewers of the config stage flagged it
and every stage since carried it forward.

The other two are the spec's, and one of them is the second half of an edge
case that shipped one-third implemented: the unlink case asks for a task alert
*and* a doctor failure, and until now a device WhatsApp had unlinked was an
ERROR in the log, a definite refusal on every send, and nobody told.
"""

from __future__ import annotations

import os
import pathlib
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from istota import doctor
from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_bridge, pairing_relay

from .support.drift import source_of
from .support.whatsapp_config import build_whatsapp_config


def _config(tmp_path, *, provider="baileys", enabled=True, **extra) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        users={"alice": UserConfig()},
        whatsapp=build_whatsapp_config(
            enabled=enabled,
            provider=provider,
            **extra,
            business_phone_number="+15551230000",
            waba_id="123456789012345",
            phone_number_id="223456789012345",
            access_token="wa-access-token",
            app_secret="wa-app-secret",
            verify_token="wa-verify-token",
            business_timezone="UTC",
        ),
    )


def _run(cfg, name):
    results = {
        result.name: result
        for result in doctor.run_checks(cfg, only=(name,), probe=False)
    }
    assert name in results, sorted(results)
    return results[name]


@pytest.fixture(autouse=True)
def _no_leaked_bridge():
    yield
    baileys_bridge.clear_active_bridge()


class _Status:
    """A bridge stand-in whose only job is to carry a `BridgeStatus`."""

    def __init__(self, **fields):
        self.status = baileys_bridge.BridgeStatus(**fields)


class TestTheBillingCheckUnderAnotherAdapter:
    def test_it_skips_rather_than_reporting_metas_rules(self, tmp_path):
        result = _run(_config(tmp_path), "whatsapp.billing")

        assert result.status == doctor.SKIP
        assert "baileys" in result.detail

    def test_it_states_no_meta_fact_about_this_deployment(self, tmp_path):
        """The measured string was "circuit closed; N service attempts of at
        most 900" — a circuit state, a spend, a cap and a calendar, four
        claims about rules no send here obeys.

        Asserted as the absence of a *state* rather than of the word
        "circuit": saying there is no billable circuit is the correct thing to
        say, and a naive substring test would have forbidden it. Digits are
        the discriminating half — every one of the four carried a number.
        """
        detail = _run(_config(tmp_path), "whatsapp.billing").detail.lower()

        assert not any(ch.isdigit() for ch in detail), detail
        for claim in ("closed", "open", "free_guard", "quota month", "spent"):
            assert claim not in detail

    def test_a_cloud_deployment_is_still_answered(self, tmp_path):
        """The control. The check has to keep working for the adapter it is
        about, or the skip is a way of never running it."""
        cfg = _config(tmp_path, provider="whatsapp_cloud")

        result = _run(cfg, "whatsapp.billing")

        assert result.status != doctor.SKIP or "does not exist" in result.detail


class TestTheBridgeCheck:
    def test_a_cloud_deployment_skips(self, tmp_path):
        cfg = _config(tmp_path, provider="whatsapp_cloud")

        assert _run(cfg, "whatsapp.baileys_bridge").status == doctor.SKIP

    def test_a_disabled_surface_skips(self, tmp_path):
        cfg = _config(tmp_path, enabled=False)

        assert _run(cfg, "whatsapp.baileys_bridge").status == doctor.SKIP

    def test_a_process_with_no_bridge_skips_rather_than_reporting_it_down(
        self, tmp_path,
    ):
        """`talk.signaling_watchers`' rule. On the Ansible shape the web app
        and the scheduler are separate units and the bridge lives in one of
        them; reporting it down from the other pages an operator about a
        process that was never meant to have one."""
        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.SKIP
        assert "this process" in result.detail

    def test_it_opens_no_connection_to_the_socket(self, tmp_path, monkeypatch):
        """The bridge holds one negotiated connection at a time, so a probe
        that dialled and sent no `hello` would be counted as a rejected
        connection against the thing being diagnosed."""
        import socket as socket_module

        def _explode(*args, **kwargs):
            raise AssertionError("the check opened a socket")

        monkeypatch.setattr(socket_module, "create_connection", _explode)
        monkeypatch.setattr(socket_module.socket, "connect", _explode)
        baileys_bridge.set_active_bridge(
            _Status(listening=True, connected=True, ready=True),
        )

        assert _run(_config(tmp_path), "whatsapp.baileys_bridge").status == doctor.OK

    def test_an_open_session_reports_ok_with_its_counters(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=True, inbound_applied=7,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.OK
        assert "7 inbound applied" in result.detail
        assert "refused connections" in result.detail

    def test_an_unlinked_device_fails_with_the_re_pair_remedy(self, tmp_path):
        """The doctor half of the unlink edge case, which was implemented by
        nothing."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="logged_out", fatal_is_permanent=True,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "logged_out" in result.detail
        # **The flag, not just the command** (ISSUE-496). This arm fires on
        # exactly the state a bare `pair` cannot get out of: the credential on
        # disk names an unlinked device, `useMultiFileAuthState` reads it as a
        # registered account, and every start logs in rather than offering a
        # code. The remedy named the one command that provably does nothing.
        assert "istota whatsapp pair --reset" in result.remedy

    def test_an_unrecorded_backoff_is_named_on_the_same_arm(self, tmp_path):
        """ISSUE-501. Where the sidecar can neither read nor write its backoff
        file the ladder cannot advance, so an unlinked session is retried at
        the supervisor's own interval — roughly 2,880 real logins a day
        against an account WhatsApp has already unlinked. From here that looks
        exactly like a deployment backing off correctly, and the sidecar's own
        warning about it is appended to a log file *inside* the directory it
        cannot write, so this is the only surface the condition has.

        On the existing arm rather than a new check: the action is the same
        re-pair, with one more thing to fix before it — and a re-pair into a
        directory still unwritable leaves the same retry rate behind it.
        """
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="logged_out", fatal_is_permanent=True,
            fatal_run_unrecorded=True,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "retried far more often" in result.detail
        assert "session directory" in result.remedy
        # The re-pair is still the fix, so the arm it sits on must not have
        # lost it.
        assert "istota whatsapp pair --reset" in result.remedy

    def test_the_control_says_an_ordinary_unlink_does_not_claim_it(self, tmp_path):
        """The negative control. A deployment whose backoff *is* recording
        must not be told its session directory cannot be written — that would
        send an operator looking for a permissions fault that is not there,
        on the one arm they already read during an outage."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="logged_out", fatal_is_permanent=True,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "retried far more often" not in result.detail
        assert "session directory" not in result.remedy

    def test_a_fatal_reason_from_the_sidecar_is_bounded_before_it_renders(
        self, tmp_path,
    ):
        """A `CheckResult` reaches the boot log and the admin Health pane, and
        a Baileys error string is one of the places a number turns up."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="logged_out: conflict for +15551234567 (401)",
            fatal_is_permanent=True,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert "15551234567" not in result.detail
        assert len(result.detail) < 200

    def test_a_socket_that_never_opened_fails(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(listening=False))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "not listening" in result.detail

    def test_no_sidecar_connected_warns_rather_than_failing(self, tmp_path):
        """A sidecar this deployment runs as its own unit may be restarting,
        and a diagnostic that pages somebody for a blip is a failure wearing
        the wrong label."""
        baileys_bridge.set_active_bridge(_Status(listening=True, connected=False))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "sidecar_command" in result.remedy

    def test_an_unpaired_session_warns_and_says_how_to_pair(self, tmp_path):
        """The state every install starts in, so not a failure.

        `session_unpaired` is what makes it that state rather than the
        reconnecting one below (ISSUE-506): both reach the same arm with
        `ready` false, and before the field existed the remedy had to offer
        both and let the operator pick.
        """
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False, session_unpaired=True,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "no paired WhatsApp session" in result.detail
        assert "every send is refused" in result.detail
        assert "istota whatsapp pair" in result.remedy

    def test_a_reconnecting_session_does_not_tell_them_to_re_pair(
        self, tmp_path,
    ):
        """The other half of the arm, and the reason the field is read rather
        than the arm reporting both possibilities.

        A session that is merely reconnecting has a credential and needs
        nothing from the operator; telling them to pair would have them
        archive a working one. Sends are refused here too, by the sidecar
        rather than by the latch, so the detail does not claim otherwise.
        """
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False, session_unpaired=False,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "session is not open" in result.detail
        assert "pair" not in result.remedy
        assert "reconnecting" in result.remedy

    def test_a_refused_connection_warns_with_its_own_remedy(self, tmp_path):
        """The counter behind the corruption this whole stage refuses a
        running daemon to avoid: the bridge accepts one sidecar, so a refused
        connection means a second one dialled. Its own arm rather than a row
        in the lost-message one, because its remedy is "leave exactly one
        sidecar running" and not "read the log"."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=True, rejected_connections=2,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "second WhatsApp sidecar" in result.detail
        assert "sidecar_command" in result.remedy

    def test_a_configured_library_version_is_checked_against_the_shipped_pin(
        self, tmp_path,
    ):
        """`[whatsapp.baileys] library_version` had no reader at all: loaded,
        documented in config.example.toml, settable, and acted on by nothing.

        The arm runs **before** the in-process skip, because it is a config
        string against a file and therefore answerable from an operator's own
        shell."""
        cfg = _config(tmp_path)
        cfg.whatsapp.baileys.library_version = "0.0.1"

        result = _run(cfg, "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "0.0.1" in result.detail
        assert baileys_bridge.shipped_library_version() in result.detail

    def test_a_matching_library_version_says_nothing(self, tmp_path):
        cfg = _config(tmp_path)
        cfg.whatsapp.baileys.library_version = (
            baileys_bridge.shipped_library_version()
        )

        # Falls through to the no-bridge skip, which is the next arm.
        assert _run(cfg, "whatsapp.baileys_bridge").status == doctor.SKIP

    @pytest.mark.parametrize(
        "field", ["malformed_lines", "dropped_events", "failed_events"],
    )
    def test_lost_messages_warn_even_on_an_open_session(self, tmp_path, field):
        """The spec's "a run of them trips a doctor warning". Each is a
        message that reached nobody and nothing else reports any of them."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=True, **{field: 3},
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "lost" in result.detail


class TestTheSessionCheck:
    def _paired(self, tmp_path, mode=0o700):
        path = tmp_path / "whatsapp-baileys-session"
        path.mkdir(mode=mode)
        (path / "creds.json").write_text("{}")
        os.chmod(path / "creds.json", 0o600)
        return path

    def test_a_cloud_deployment_skips(self, tmp_path):
        cfg = _config(tmp_path, provider="whatsapp_cloud")

        assert _run(cfg, "whatsapp.baileys_session").status == doctor.SKIP

    def test_a_private_directory_reports_ok(self, tmp_path):
        self._paired(tmp_path)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.OK

    def test_an_unpaired_deployment_warns_rather_than_failing(self, tmp_path):
        """Pairing is what creates it, so its absence is the state of every
        install that has not paired — not a broken one."""
        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.WARN
        assert "istota whatsapp pair" in result.remedy

    def test_a_world_readable_directory_fails(self, tmp_path):
        self._paired(tmp_path, mode=0o755)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "0755" in result.detail

    def test_a_symlink_at_the_name_fails(self, tmp_path):
        """`lstat`, not `stat`: a symlink points the credential somewhere this
        check never looked, and following it would report the target's mode
        while the link itself is what the bridge opens."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (tmp_path / "whatsapp-baileys-session").symlink_to(elsewhere)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "not a directory" in result.detail

    def test_a_directory_owned_by_another_account_fails(self, tmp_path,
                                                        monkeypatch):
        """No `requires_dac`: the mismatch is produced by moving *this*
        process's idea of its own uid, so it does not depend on the kernel
        refusing anything and is as true under root as under anyone."""
        self._paired(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1000)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "another account" in result.detail

    def test_a_wide_file_inside_it_fails_and_is_left_alone(self, tmp_path):
        """**Reported, not repaired.** The check used to call the bridge's
        narrowing pass, which made a diagnostic the second writer of a
        full-account credential's permissions — and then self-cleared, so the
        hourly sweep's transition alerting could see the exposure once and an
        operator rerunning the command to confirm saw a clean tree.

        The predicate is still the bridge's own (`survey_session_files` and
        `harden_session_files` walk one implementation), so this cannot pass
        while the bridge disagrees about what private means.
        """
        path = self._paired(tmp_path)
        exposed = path / "app-state-sync-key.json"
        exposed.write_text("{}")
        os.chmod(exposed, 0o644)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert stat.S_IMODE(exposed.stat().st_mode) == 0o644

    def test_it_keeps_reporting_a_wide_file_on_a_second_run(self, tmp_path):
        """The half a self-clearing check could not do. A condition that is
        still true has to still be reported, or the only surface that can act
        on it is whichever process happened to run first."""
        path = self._paired(tmp_path)
        exposed = path / "app-state-sync-key.json"
        exposed.write_text("{}")
        os.chmod(exposed, 0o644)

        first = _run(_config(tmp_path), "whatsapp.baileys_session")
        second = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert first.status == doctor.FAIL
        assert second.status == doctor.FAIL

    def test_it_creates_nothing(self, tmp_path):
        """A diagnostic must not make the thing it reports on.
        `ensure_session_dir` creates and tightens and is deliberately not
        called from here — a check that created a 0700 directory would report
        `OK` about a deployment that has never paired."""
        _run(_config(tmp_path), "whatsapp.baileys_session")

        assert not (tmp_path / "whatsapp-baileys-session").exists()

    def test_running_as_root_does_not_fail_a_daemon_owned_directory(
        self, tmp_path, monkeypatch,
    ):
        """`sudo istota doctor` is an ordinary invocation, and there the
        directory belongs to the daemon's account while `geteuid()` is 0 — so
        the ownership arm failed a correct install, and the remedy it printed
        (`chown -R 0 …`) makes the credential unreadable by the daemon and
        `ensure_session_dir` refuse it on the next start."""
        self._paired(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.OK

    def test_the_ownership_remedy_never_names_this_processs_uid(
        self, tmp_path, monkeypatch,
    ):
        self._paired(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1000)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "chown" not in result.remedy
        assert "the account the istota daemon runs as" in result.remedy


class TestThePairingArm:
    """What `whatsapp.baileys_bridge` says while a pairing window is open.

    Every arm is a `WARN` because `_send` refuses definitely for the whole
    span: a deployment mid-window has no WhatsApp surface, briefly and on
    purpose, and that is the fact an operator is owed when a message did not
    arrive.
    """

    def test_a_sidecar_that_never_returns_names_the_unit_and_the_update_log(
        self, tmp_path,
    ):
        """The ISSUE-497 residual from the other side: a failing `npm ci` in
        the update cron stops the unit and aborts before either restart arm,
        so the reason lives only in the update log."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=False, ready=False,
            pairing_state="sidecar_absent",
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "no sidecar has connected" in result.detail
        assert "update log" in result.remedy

    def test_a_code_waiting_to_be_scanned_says_so_and_says_sends_are_refused(
        self, tmp_path,
    ):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            pairing_state="awaiting_scan",
            pairing_expires_at=time.time() + 120,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "waiting to be scanned" in result.detail
        assert "every send is refused" in result.detail
        assert "Linked Devices" in result.remedy

    def test_the_deadline_is_reported_as_the_time_left(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            pairing_state="awaiting_scan",
            pairing_expires_at=time.time() + 125,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert "2m left" in result.detail

    def test_an_unreadable_deadline_costs_the_countdown_and_nothing_else(
        self, tmp_path,
    ):
        """The deadline arrives as a value on a status dict, and this arm
        coerces rather than trusting it — a countdown that raised would take
        the whole check with it."""
        bridge = _Status(
            listening=True, connected=True, ready=False,
            pairing_state="awaiting_scan",
        )
        bridge.status.pairing_expires_at = "soon"
        baileys_bridge.set_active_bridge(bridge)

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "waiting to be scanned" in result.detail
        assert "left" not in result.detail

    def test_the_window_outranks_the_no_sidecar_warning_it_would_otherwise_hit(
        self, tmp_path,
    ):
        """**The position of the arm, which is its main job.** Mid-window a
        disconnected, unpaired sidecar is what the sequence asked for — it
        sent the frame that stopped it — and the `connected` arm's remedy
        ("check that the sidecar process is running") is wrong for it.

        The control is moving the arm below the `connected` and `ready`
        checks, which turns this red on the remedy: `awaiting_sidecar` is
        exactly the state where `connected` reads false.
        """
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=False, ready=False,
            pairing_state="awaiting_sidecar",
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "pairing window is open" in result.detail
        assert "sidecar_command" not in result.remedy

    def test_a_socket_that_never_opened_still_outranks_the_window(self, tmp_path):
        """The other half of the ordering. A bridge with no socket can pair
        nothing, so that failure is not shadowed by a window."""
        baileys_bridge.set_active_bridge(_Status(
            listening=False, connected=False, ready=False,
            pairing_state="awaiting_sidecar",
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "not listening" in result.detail

    def test_a_latched_fatal_still_outranks_a_window(self, tmp_path):
        """A *new* permanent fatal arriving inside an open window, which is
        the only way the two states coexist: `repair_session` clears the latch
        on both its arms before the window opens, so an ordinary re-pair in
        flight carries no latched fault. The unlink arm holds the reason and
        the `--reset` remedy, so it keeps precedence."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=False, ready=False,
            fatal_reason="logged_out", fatal_is_permanent=True,
            pairing_state="awaiting_sidecar",
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "logged_out" in result.detail

    def test_a_closed_window_changes_nothing(self, tmp_path):
        """`pairing_state` is derived from the live window, so `None` is what
        a deployment that is not pairing reads — and it has to fall straight
        through to the arms that were there before."""
        baileys_bridge.set_active_bridge(
            _Status(listening=True, connected=True, ready=True),
        )

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        # The OK arm's own words, so the case still discriminates if the
        # pairing arm is deleted outright rather than merely not firing.
        assert result.status == doctor.OK
        assert "the WhatsApp session is open" in result.detail
        assert "pairing" not in result.detail


class TestThePairingRelayCheck:
    """`whatsapp.pairing_relay`: the mode, the owner and the age of the file a
    pairing code crosses processes in.

    A QR is a full-account WhatsApp credential — whoever scans it becomes a
    linked device — so the file's permissions are worth a check of their own,
    and so is one that has outlived every window it could belong to.
    """

    def _relay(self, cfg, *, mode=0o600, body='{"qr": "2@SECRET"}'):
        path = baileys_bridge.default_pairing_relay_path(cfg)
        path.write_text(body)
        os.chmod(path, mode)
        return path

    def test_a_cloud_deployment_skips(self, tmp_path):
        cfg = _config(tmp_path, provider="whatsapp_cloud")

        assert _run(cfg, "whatsapp.pairing_relay").status == doctor.SKIP

    def test_a_disabled_surface_skips(self, tmp_path):
        cfg = _config(tmp_path, enabled=False)

        assert _run(cfg, "whatsapp.pairing_relay").status == doctor.SKIP

    def test_no_file_is_the_healthy_state(self, tmp_path):
        """The relay lives only inside a window and is unlinked when one
        closes by any route, so its absence is what a deployment that is not
        pairing reads — not a fault."""
        result = _run(_config(tmp_path), "whatsapp.pairing_relay")

        assert result.status == doctor.OK
        assert "does not exist" in result.detail

    def test_a_world_readable_relay_fails(self, tmp_path):
        cfg = _config(tmp_path)
        self._relay(cfg, mode=0o644)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.FAIL
        assert "0644" in result.detail

    def test_it_repairs_nothing(self, tmp_path):
        """The survey-not-repair posture `whatsapp.baileys_session`
        established. A check that narrowed the mode could not report it: the
        second run says the tree is clean, so the hourly sweep's transition
        alerting sees the exposure once and an operator rerunning the command
        to confirm sees nothing."""
        cfg = _config(tmp_path)
        path = self._relay(cfg, mode=0o644)

        first = _run(cfg, "whatsapp.pairing_relay")
        second = _run(cfg, "whatsapp.pairing_relay")

        assert first.status == doctor.FAIL
        assert second.status == doctor.FAIL
        # **The mode, in both details.** `run_checks` reports a raising check
        # as a `FAIL` carrying the exception text, and a check that crashed
        # before touching the file would leave the mode alone too — so status
        # plus an unchanged mode is satisfied by a `NameError` on the first
        # line. The substring is what separates the two.
        assert "0644" in first.detail
        assert "0644" in second.detail
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    def test_a_relay_owned_by_another_account_fails(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        self._relay(cfg)
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1000)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.FAIL
        assert "another account" in result.detail

    def test_running_as_root_does_not_fail_a_daemon_owned_relay(
        self, tmp_path, monkeypatch,
    ):
        """`sudo istota doctor` is an ordinary invocation, and there the file
        belongs to the daemon's account while `geteuid()` is 0."""
        cfg = _config(tmp_path)
        self._relay(cfg)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.OK

    def test_a_symlink_at_the_name_fails(self, tmp_path):
        """The bridge publishes through `os.replace`, which does not follow
        the final component — so a link standing here is not what it writes
        to, it is something else wearing the relay's name."""
        cfg = _config(tmp_path)
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("{}")
        baileys_bridge.default_pairing_relay_path(cfg).symlink_to(elsewhere)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.FAIL
        assert "not a regular file" in result.detail

    def test_a_relay_older_than_its_window_warns(self, tmp_path):
        """`stop()` can skip the unlink when its cleanup task is cancelled
        mid-flight, and nothing sweeps the result until the next window
        closes. A file older than one window plus a margin cannot be serving a
        live window in any process, which is what makes this answerable with
        no bridge in hand."""
        cfg = _config(tmp_path, pairing_window_seconds=300)
        path = self._relay(cfg)
        old = time.time() - 3600
        os.utime(path, (old, old))

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.WARN
        assert "longer than one pairing window" in result.detail

    def test_a_fresh_relay_inside_its_window_is_ok(self, tmp_path):
        """The control for the arm above: a live window's relay must not read
        as residue, or the check warns through every pairing there is."""
        cfg = _config(tmp_path, pairing_window_seconds=300)
        self._relay(cfg)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.OK
        assert "0600 and private" in result.detail

    def test_a_long_window_is_honoured_rather_than_a_fixed_number(self, tmp_path):
        """The staleness bound is the operator's `pairing_window_seconds`, so
        a deployment that widened the window is not warned at the default's
        deadline. Asserted over two values, since one passes against a
        constant."""
        aged = time.time() - 3600
        narrow = _config(tmp_path, pairing_window_seconds=300)
        path = self._relay(narrow)
        os.utime(path, (aged, aged))
        wide = _config(tmp_path, pairing_window_seconds=7200)

        assert _run(narrow, "whatsapp.pairing_relay").status == doctor.WARN
        assert _run(wide, "whatsapp.pairing_relay").status == doctor.OK

    def test_the_payload_reaches_neither_the_detail_nor_the_remedy(self, tmp_path):
        """It `lstat`s and never opens. A `CheckResult` is rendered into the
        boot log and the admin Health pane, so a check that read the file
        would be one rendering decision away from publishing a credential."""
        cfg = _config(tmp_path)
        self._relay(cfg, mode=0o644, body='{"qr": "2@PAIRINGSECRET"}')

        result = _run(cfg, "whatsapp.pairing_relay")

        # Positive first: two `not in` assertions alone are satisfied by a
        # check that crashed, skipped, or never reached the arm.
        assert result.status == doctor.FAIL
        assert "0644" in result.detail
        assert "2@PAIRINGSECRET" not in result.detail
        assert "2@PAIRINGSECRET" not in result.remedy

    def test_an_operator_set_relay_path_is_the_one_checked(self, tmp_path):
        """`[whatsapp.baileys] pairing_relay_path` wins, read through the
        bridge's own resolver rather than re-derived — a check looking at the
        default path on a deployment that moved the file reports `OK` about a
        file nobody writes."""
        elsewhere = tmp_path / "somewhere-else" / "pairing.json"
        elsewhere.parent.mkdir()
        cfg = _config(tmp_path, pairing_relay_path=str(elsewhere))
        elsewhere.write_text("{}")
        os.chmod(elsewhere, 0o644)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.FAIL
        assert str(elsewhere) in result.detail


class TestTheSessionArchives:
    """The second pass, which is a coverage question rather than a formality.

    Before the pairing flow there was exactly one full-account credential on
    disk and `whatsapp.baileys_session` covered it. A re-pair moves the old
    session to a timestamped sibling and nothing sweeps those, so a host that
    has re-paired N times holds N+1 — and a check that walked the live
    directory alone would report one of them while reading as coverage.
    """

    def _paired(self, tmp_path, mode=0o700):
        path = tmp_path / "whatsapp-baileys-session"
        path.mkdir(mode=mode)
        (path / "creds.json").write_text("{}")
        os.chmod(path / "creds.json", 0o600)
        return path

    def _archive(self, tmp_path, cfg, *, mode=0o600):
        """An archive at the name **the writer would have chosen**.

        Not a hand-spelled one. A test that invents the shape and a predicate
        that matches the invention agree with each other and can both be wrong
        about production — so the name comes from `_reset_destination`, which
        is the only thing that names an archive on a real host, and the
        directory is moved there by the same `rename` the reset performs.
        """
        session = self._paired(tmp_path)
        exposed = session / "app-state-sync-key-1.json"
        exposed.write_text("{}")
        os.chmod(exposed, mode)
        bridge = baileys_bridge.BaileysBridge(
            cfg,
            socket_path=tmp_path / "whatsapp-baileys.sock",
            session_dir=session,
        )
        destination = bridge._reset_destination()
        session.rename(destination)
        return destination

    def test_the_predicate_recognises_what_the_writer_names(self, tmp_path):
        """The pin that keeps the pair honest. `_reset_destination` stamps the
        name and `session_archives` reads it back; they share one format
        constant, and this is what says the sharing works end to end rather
        than that two spellings happen to agree today."""
        cfg = _config(tmp_path)
        archive = self._archive(tmp_path, cfg)
        session = tmp_path / "whatsapp-baileys-session"

        found = baileys_bridge.session_archives(session)

        assert found == [archive]
        assert baileys_bridge.session_archive_stamp(archive) is not None

    def test_a_wide_file_in_an_archive_fails(self, tmp_path):
        """**The negative control the stage names.** Without the second pass
        this reports `OK`: the live directory is 0700 with one 0600 file in
        it, and the exposed key sits in a sibling `survey_session_files` is
        never pointed at.
        """
        cfg = _config(tmp_path)
        self._archive(tmp_path, cfg, mode=0o644)
        self._paired(tmp_path)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "archived WhatsApp session" in result.detail

    def test_an_exposed_archive_is_reported_with_no_live_session_at_all(
        self, tmp_path,
    ):
        """The case that decides where the pass runs. A host that re-paired
        and then had its session removed holds its only WhatsApp credential in
        an archive, so returning early on the unpaired arm would report that
        host as merely unpaired."""
        cfg = _config(tmp_path)
        self._archive(tmp_path, cfg, mode=0o644)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "archived WhatsApp session" in result.detail

    def test_an_archive_is_surveyed_and_never_narrowed(self, tmp_path):
        """Survey only, and with more force than on the live directory: there
        is no start path that would narrow an archive again, so a check that
        repaired here would be the only writer and would then self-clear."""
        cfg = _config(tmp_path)
        archive = self._archive(tmp_path, cfg, mode=0o644)
        exposed = archive / "app-state-sync-key-1.json"

        first = _run(cfg, "whatsapp.baileys_session")
        second = _run(cfg, "whatsapp.baileys_session")

        assert first.status == doctor.FAIL
        assert second.status == doctor.FAIL
        assert stat.S_IMODE(exposed.stat().st_mode) == 0o644

    def test_a_private_archive_is_counted_and_aged_without_failing(self, tmp_path):
        """The count is the other half of the decision not to sweep them:
        nothing deletes an archive, so the number of full-account credentials
        on the host has to be visible somewhere."""
        cfg = _config(tmp_path)
        self._archive(tmp_path, cfg)
        self._paired(tmp_path)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.OK
        assert "1 archived session(s)" in result.detail
        assert "oldest" in result.detail
        assert "0 file(s) wider" in result.detail

    def test_no_archives_says_nothing_at_all(self, tmp_path):
        """The control for the line above: a host that has never re-paired
        must read exactly as it did before, or every deployment gains a
        sentence about a thing it does not have."""
        cfg = _config(tmp_path)
        self._paired(tmp_path)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.OK
        assert "archived" not in result.detail

    def test_a_live_failure_keeps_its_status_and_carries_the_archive_finding(
        self, tmp_path,
    ):
        """Precedence is live-first, because the live directory is the
        credential in use — and the archive finding rides along rather than
        being lost behind it."""
        cfg = _config(tmp_path)
        self._archive(tmp_path, cfg, mode=0o644)
        self._paired(tmp_path, mode=0o755)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "0755" in result.detail
        assert "1 archived session(s)" in result.detail
        assert "nothing deletes one" in result.remedy

    def test_a_sibling_that_is_not_an_archive_is_not_counted(self, tmp_path):
        """The name is the whole predicate, so it has to be the writer's own
        shape and not a prefix test: a `.json` beside the directory and a
        symlink wearing an archive's name are both things this must walk
        past."""
        cfg = _config(tmp_path)
        archive = self._archive(tmp_path, cfg)
        self._paired(tmp_path)
        (tmp_path / "whatsapp-baileys-session.json").write_text("{}")
        (tmp_path / "whatsapp-baileys-session.notastamp").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (tmp_path / f"{archive.name}-9").symlink_to(elsewhere)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.OK
        assert "1 archived session(s)" in result.detail

    def test_two_archives_are_both_surveyed(self, tmp_path):
        """The wide file is in the archive that sorts **second**, so a
        predicate stopping at the first match reports the tree clean.

        The two stamps are built explicitly rather than by calling the writer
        twice, and that is this case's own correction: `_reset_destination`
        stamps to one-second resolution and appends `-1` on a collision, so
        two calls inside one second give the *later* archive the bare name,
        which sorts first — and the case would then pass against exactly the
        predicate it is written to catch. `.claude/rules/whatsapp.md` records
        the same trap on `TestTheSessionReset`. What keeps the hand-built name
        honest is the writer's own format constant plus the agreement pin
        above.
        """
        cfg = _config(tmp_path)
        stamps = [
            datetime(2026, 1, day, tzinfo=timezone.utc).strftime(
                baileys_bridge.ARCHIVE_STAMP_FORMAT,
            )
            for day in (1, 2)
        ]
        earlier, later = (
            tmp_path / f"whatsapp-baileys-session.{stamp}" for stamp in stamps
        )
        for archive in (earlier, later):
            archive.mkdir(mode=0o700)
            (archive / "creds.json").write_text("{}")
            os.chmod(archive / "creds.json", 0o600)
        exposed = later / "app-state-sync-key-1.json"
        exposed.write_text("{}")
        os.chmod(exposed, 0o644)
        self._paired(tmp_path)

        result = _run(cfg, "whatsapp.baileys_session")

        assert baileys_bridge.session_archives(
            tmp_path / "whatsapp-baileys-session",
        ) == [earlier, later]
        assert result.status == doctor.FAIL
        assert "2 archived" in result.detail


class TestTheNeverRaisesContract:
    """Both new arms run on the daemon's boot path, so neither may raise.

    `run_checks` does catch — it reports a raising check as a `FAIL` with the
    exception text — but that is the backstop rather than the contract, and a
    diagnostic that fails on its own instrument reports the wrong subsystem.
    The case that gets past an `OSError` handler is an embedded null byte in a
    configured path, which the filesystem calls answer with `ValueError`;
    `du.py` states the same rule for the same reason.
    """

    def test_a_session_dir_whose_parent_cannot_be_listed_yields_no_archives(self):
        """The enumerator's own contract, driven rather than read.

        The null byte has to be in the **parent**, which is what this walks.
        The first draft put it in the leaf name and passed with the
        `ValueError` handler removed — `iterdir` on `/tmp` succeeds and the
        prefix filter then matches nothing, so the case could not fail. Its
        control is what said so.
        """
        found = baileys_bridge.session_archives(
            pathlib.Path("/tmp/sessions\x00/whatsapp-baileys-session"),
        )

        assert found == []

    def test_a_relay_path_with_a_null_byte_is_reported_rather_than_raised(
        self, tmp_path,
    ):
        cfg = _config(tmp_path, pairing_relay_path="/tmp/pair\x00ing.json")

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.WARN
        assert "could not be read" in result.detail

    def test_a_session_dir_with_a_null_byte_is_reported_rather_than_raised(
        self, tmp_path,
    ):
        cfg = _config(tmp_path, session_dir="/tmp/session\x00dir")

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.WARN
        assert "could not be read" in result.detail

    def test_a_stamp_in_the_future_reads_as_no_age_rather_than_a_negative_one(
        self, tmp_path,
    ):
        """Clock skew, and the reason the age goes through `_duration`, which
        clamps. A host whose archive is stamped ahead of `now` must not render
        a negative duration into the boot log."""
        session = tmp_path / "whatsapp-baileys-session"
        session.mkdir(mode=0o700)
        (session / "creds.json").write_text("{}")
        os.chmod(session / "creds.json", 0o600)
        ahead = datetime.now(timezone.utc) + timedelta(days=2)
        archive = tmp_path / (
            "whatsapp-baileys-session."
            + ahead.strftime(baileys_bridge.ARCHIVE_STAMP_FORMAT)
        )
        archive.mkdir(mode=0o700)

        result = _run(_config(tmp_path), "whatsapp.baileys_session")

        assert result.status == doctor.OK
        assert "the oldest 0s old" in result.detail


class TestWhatTheReviewFound:
    """Cases for the review fixes, each with the control that made it a fix.

    Grouped rather than filed beside their siblings, because what they have in
    common is how they were found: every one is a case the suite could not
    have failed on before, and each names the control that says so.
    """

    def _paired(self, tmp_path, mode=0o700):
        path = tmp_path / "whatsapp-baileys-session"
        path.mkdir(mode=mode)
        (path / "creds.json").write_text("{}")
        os.chmod(path / "creds.json", 0o600)
        return path

    def _archive(self, tmp_path, *, dir_mode=0o700, file_mode=0o600):
        archive = tmp_path / (
            "whatsapp-baileys-session."
            + datetime(2026, 1, 1, tzinfo=timezone.utc).strftime(
                baileys_bridge.ARCHIVE_STAMP_FORMAT,
            )
        )
        archive.mkdir(mode=0o700)
        (archive / "creds.json").write_text("{}")
        os.chmod(archive / "creds.json", file_mode)
        os.chmod(archive, dir_mode)
        return archive

    def test_an_undeclared_window_does_not_shorten_the_relay_deadline(
        self, tmp_path,
    ):
        """**Both reviewers, independently.** The staleness bound read the
        config field with `_setting_float` while the bridge reads it through
        `configured_pairing_window_seconds`, which clamps a non-positive value
        to the shipped 300s — so `pairing_window_seconds = 0` ran 300s windows
        against a 120s bound, and every real pairing past two minutes was
        reported as a code nobody can scan while somebody was mid-scan.

        The two-value case above it stays green either way, which is why this
        one exists: it exercises 300 and 7200, where the two readings agree.
        """
        cfg = _config(tmp_path, pairing_window_seconds=0)
        path = baileys_bridge.default_pairing_relay_path(cfg)
        path.write_text("{}")
        os.chmod(path, 0o600)
        recent = time.time() - 200
        os.utime(path, (recent, recent))

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.OK

    def test_an_archive_directory_wider_than_0700_fails(self, tmp_path):
        """The archive pass asked one of the live pass's three questions while
        its remedy told the operator to fix all three. `rename(2)` preserves a
        mode, so a wide archive directory means somebody has since changed it
        — and an archive is no less a full-account credential for being the
        previous one."""
        cfg = _config(tmp_path)
        self._archive(tmp_path, dir_mode=0o755)
        self._paired(tmp_path)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "1 directory/ies" in result.detail

    def test_an_archive_owned_by_another_account_fails(self, tmp_path,
                                                       monkeypatch):
        cfg = _config(tmp_path)
        self._archive(tmp_path)
        self._paired(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1000)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "1 directory/ies" in result.detail

    def test_a_private_archive_directory_is_not_counted_as_loose(self, tmp_path):
        """The control for the two above: a 0700 archive owned by this account
        must not fail, or the pass warns on every host that has re-paired."""
        cfg = _config(tmp_path)
        self._archive(tmp_path)
        self._paired(tmp_path)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.OK
        assert "0 directory/ies" in result.detail

    def test_an_exposed_archive_still_reports_that_there_is_no_live_session(
        self, tmp_path,
    ):
        """The docstring said an archive finding is never dropped, and the
        `if wide` branch dropped the *live* one: a host that is both unpaired
        and holding an exposed archive was told only about the archive."""
        cfg = _config(tmp_path)
        self._archive(tmp_path, file_mode=0o644)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.FAIL
        assert "does not exist" in result.detail
        assert "archived WhatsApp session" in result.detail

    def test_a_second_sidecar_is_named_inside_an_open_window(self, tmp_path):
        """The pairing arm returns ahead of the refused-connection arm, whose
        remedy is the only surface for two Baileys clients against one session
        directory — and mid-window is when that matters most, since the
        sequence renames the directory on the strength of a drop it caused on
        the peer it adopted. The fact and the remedy come along."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            pairing_state="awaiting_scan", rejected_connections=1,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "a second WhatsApp sidecar has tried to connect" in result.detail
        assert "leave exactly one" in result.remedy

    def test_no_second_sidecar_leaves_the_window_message_alone(self, tmp_path):
        """The control. Naming it unconditionally would report the corruption
        hazard on every pairing there is."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            pairing_state="awaiting_scan",
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert "second WhatsApp sidecar" not in result.detail
        assert "leave exactly one" not in result.remedy

    def test_the_pairing_states_are_the_relay_modules_own_constants(self):
        """A drift guard rather than a driven case, because the driven ones
        cannot see this: the doctor arm compared string literals and the tests
        set the same literals, so a rename of either constant would break
        production while every test stayed green — losing the `sidecar_absent`
        remedy, which is the one arm the spec asks for by name."""
        source = source_of(doctor._baileys_pairing_window)

        assert "STATE_SIDECAR_ABSENT" in source
        assert "STATE_AWAITING_SCAN" in source
        assert '== "sidecar_absent"' not in source
        assert '== "awaiting_scan"' not in source
        assert pairing_relay.STATE_SIDECAR_ABSENT == "sidecar_absent"
        assert pairing_relay.STATE_AWAITING_SCAN == "awaiting_scan"

    def test_an_unresolvable_session_dir_is_reported_rather_than_raised(
        self, tmp_path,
    ):
        """`expanduser` answers `RuntimeError` for a `~unknownuser` value,
        which is neither an `OSError` nor a `ValueError`. It reached
        `run_checks`' catch-all and was reported as a `FAIL` naming the
        exception class."""
        cfg = _config(tmp_path, session_dir="~nosuchuser12345/session")

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.WARN
        assert "could not be resolved" in result.detail
        assert "session_dir" in result.remedy

    def test_an_unresolvable_relay_path_is_reported_rather_than_raised(
        self, tmp_path,
    ):
        cfg = _config(tmp_path, pairing_relay_path="~nosuchuser12345/pair.json")

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.WARN
        assert "could not be resolved" in result.detail
        assert "pairing_relay_path" in result.remedy

    def test_the_age_comes_from_the_oldest_parseable_stamp(self, tmp_path):
        """The name filter accepts any `\\d{8}T\\d{6}Z`, including an
        impossible date, and such a name sorts ahead of every real one — so
        reading `archives[0]`'s stamp dropped the age clause while still
        counting the archive."""
        cfg = _config(tmp_path)
        self._paired(tmp_path)
        (tmp_path / "whatsapp-baileys-session.00000000T000000Z").mkdir(mode=0o700)
        real = datetime.now(timezone.utc) - timedelta(days=3)
        (tmp_path / (
            "whatsapp-baileys-session."
            + real.strftime(baileys_bridge.ARCHIVE_STAMP_FORMAT)
        )).mkdir(mode=0o700)

        result = _run(cfg, "whatsapp.baileys_session")

        assert result.status == doctor.OK
        assert "2 archived session(s)" in result.detail
        assert "the oldest 3d 0h old" in result.detail

    def test_the_relay_mode_is_the_relay_modules_own(self):
        """Same drift shape as the states above, one constant over."""
        source = source_of(doctor.check_whatsapp_pairing_relay)

        assert "RELAY_MODE" in source
        assert "0o600" not in source
        assert pairing_relay.RELAY_MODE == 0o600

    def test_under_root_the_relay_detail_states_the_owner_it_read(
        self, tmp_path, monkeypatch,
    ):
        """The ownership arm is skipped under root by design, so the OK detail
        may not claim the file is private to this account — a check must not
        assert what it did not compare."""
        cfg = _config(tmp_path)
        path = baileys_bridge.default_pairing_relay_path(cfg)
        path.write_text("{}")
        os.chmod(path, 0o600)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        result = _run(cfg, "whatsapp.pairing_relay")

        assert result.status == doctor.OK
        assert "private to this account" not in result.detail
        assert f"owned by uid {path.stat().st_uid}" in result.detail
