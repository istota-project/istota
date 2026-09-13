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
import stat

import pytest

from istota import doctor
from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_bridge

from .support.whatsapp_config import build_whatsapp_config


def _config(tmp_path, *, provider="baileys", enabled=True) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        users={"alice": UserConfig()},
        whatsapp=build_whatsapp_config(
            enabled=enabled,
            provider=provider,
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
        """The state every install starts in, so not a failure."""
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
        ))

        result = _run(_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "istota whatsapp pair" in result.remedy

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
