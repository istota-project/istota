"""`istota whatsapp pair`: the only way a Baileys deployment gets a credential.

The Cloud adapter is configured by hand from a Meta console; this one is
paired by scanning a code, so this command is the whole of its setup — and it
is also the recovery path the bridge's supervisor records as owed to it, since
a permanent fatal stops the respawn loop and nothing else can spawn a sidecar
to clear the latch.

The happy path is driven against a **real socket and a real subprocess**: a
throwaway Python sidecar that dials in, says hello, offers a code and reports
ready. A double that answered method calls would assert nothing about the two
things that have to work here — that the bridge is listening before the child
dials, and that a scan is *observed* rather than reported.

The fake is written as a file with its behaviour baked in rather than read
from the environment, and that is not a style choice: the bridge hands the
child an allowlist (`_CHILD_ENV_PASSTHROUGH` plus the socket and the session
directory), so a test variable would never arrive — the property that keeps a
credential the daemon holds out of the sidecar's environment, met from the
other side.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import socket
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from istota import cli, db
from istota.transport.whatsapp import baileys_bridge, pairing_relay

from .support.baileys_sidecar import SocketDir

_SIDECAR_TEMPLATE = """\
import json, os, socket, time, traceback

CONFIG = {config}

try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(CONFIG["socket"])

    def say(**fields):
        sock.sendall((json.dumps(fields) + "\\n").encode())

    say(type="hello", protocol_version=1)
    # The spawn sets `cwd` to the session directory, so the marker is how this
    # fake models the state ISSUE-496 is about: a credential on disk naming a
    # device WhatsApp has unlinked, which `useMultiFileAuthState` reads as a
    # registered account and logs in with rather than emitting a code. Moving
    # the directory aside is what makes the next run offer one.
    if CONFIG["fatal_marker"] and os.path.exists(CONFIG["fatal_marker"]):
        say(type="fatal", reason="logged_out", permanent=True)
        # Then exit, as the shipped sidecar does 500ms after that frame.
        # `os._exit` rather than a raise: the handler below writes a traceback
        # for every `BaseException`, and a deliberate exit is not a fault.
        time.sleep(0.2)
        os._exit(1)
    if CONFIG["qr"]:
        say(type="qr", qr=CONFIG["qr"])
    if CONFIG["fatal"]:
        say(type="fatal", reason="bad_session", permanent=True)
    else:
        say(type="ready")
    time.sleep(30)
except BaseException:
    with open(CONFIG["log"], "a") as handle:
        traceback.print_exc(file=handle)
    raise
"""


@pytest.fixture
def sockets():
    """A short directory under /tmp.

    `AF_UNIX` caps a path at about 100 bytes and pytest's `tmp_path` is longer
    than that on a macOS runner, so the bridge's own `MAX_SOCKET_PATH_BYTES`
    guard fires before anything under test does.
    """
    directory = SocketDir()
    yield directory
    directory.cleanup()


def _write_sidecar(sockets, *, qr=None, fatal=False, fatal_marker=None):
    path = sockets.path / "fake_sidecar.py"
    # `repr`, not `json.dumps`: a JSON `null` or `false` is a Python
    # SyntaxError, and the child then dies at compile time — before its own
    # `except` can write the traceback, and with its stderr on /dev/null,
    # which is exactly the diagnostic-free failure this log file exists for.
    path.write_text(_SIDECAR_TEMPLATE.format(config=repr({
        "socket": str(sockets.path / "whatsapp-baileys.sock"),
        "qr": qr,
        "fatal": fatal,
        "fatal_marker": fatal_marker,
        "log": str(sockets.path / "sidecar-errors.log"),
    })))
    return (sys.executable, str(path))


def _config_file(
    tmp_path, sockets, *, provider="baileys", sidecar_command="", baileys=(),
):
    path = tmp_path / "config.toml"
    body = [
        f'db_path = "{sockets.path / "istota.db"}"',
        f'temp_dir = "{tmp_path / "tmp"}"',
        "",
        "[site]",
        'hostname = "assistant.example.com"',
        "",
        "[whatsapp]",
        "enabled = true",
        f'provider = "{provider}"',
        'business_phone_number = "+15551230000"',
    ]
    if provider == "whatsapp_cloud":
        body += [
            "",
            "[whatsapp.cloud]",
            'waba_id = "123456789012345"',
            'phone_number_id = "223456789012345"',
            'access_token = "wa-access-token"',
            'app_secret = "wa-app-secret"',
            'verify_token = "wa-verify-token"',
        ]
    body += ["", "[whatsapp.baileys]"]
    # Attach mode's patience is the *request row's* own deadline, which comes
    # from this key rather than from `WHATSAPP_PAIR_TIMEOUT_SECONDS` — so
    # without it here a case that reaches the follow loop and never gets a
    # terminal row waits the shipped five minutes. Found by a negative control
    # rather than by a test: collapsing the mode selector to attach sent the
    # own-sidecar cases into the loop and the run stopped being bounded.
    body.append("pairing_window_seconds = 8")
    if sidecar_command:
        body.append(f'sidecar_command = "{sidecar_command}"')
    body += list(baileys)
    path.write_text("\n".join(body) + "\n")
    return path


def _args(config_path, *, reset=False):
    return argparse.Namespace(config=str(config_path), reset=reset)


def _sidecar_error(sockets) -> str:
    log = sockets.path / "sidecar-errors.log"
    return log.read_text() if log.exists() else "the fake sidecar left no traceback"


def _use_sidecar(monkeypatch, argv):
    """Both resolvers, because `pair` asks both.

    The configured command first and the in-tree program second — and that
    second one resolves for real in this checkout, so patching only the first
    spawns the shipped sidecar against a tree with no `node_modules`.
    """
    monkeypatch.setattr(
        baileys_bridge, "resolve_sidecar_argv", lambda config: argv,
    )
    monkeypatch.setattr(baileys_bridge, "in_tree_sidecar_argv", lambda: ())


class _Tty(io.StringIO):
    """A stdin that claims to be a terminal.

    The unlink confirmation refuses a non-terminal stdin, so a test driving it
    has to supply one — and it reads through `sys.stdin.readline` rather than
    `input()` for exactly this reason: `input()` reaches for the process's real
    stdin when `sys.stdin` has been replaced, which under pytest is closed.
    """

    def isatty(self) -> bool:
        return True


@contextlib.contextmanager
def _live_bridge_socket(sockets):
    """A listener on the bridge socket: what "a daemon is running" looks like.

    Bound and listening rather than merely present, because the selector is a
    connect — an inode outliving its process is exactly what the bridge's own
    `_unlink_stale` cleans up.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sockets.path / "whatsapp-baileys.sock"))
    listener.listen(1)
    try:
        yield listener
    finally:
        listener.close()


def _init_pairing_db(sockets):
    """The framework database attach mode writes its request row into."""
    path = sockets.path / "istota.db"
    db.init_db(path)
    return path


def _pairing_row(sockets):
    with db.get_db(sockets.path / "istota.db") as conn:
        return db.read_whatsapp_pairing(conn)


def _relay_path(sockets):
    """Where the bridge would publish, given these tests' `db_path`.

    Derived rather than spelled: the config files here put `db_path` in the
    socket directory and nothing sets a workspace, so the relay resolves to
    `{db_path.parent}/whatsapp-pairing.json`. Asking the product means a change
    to that rule shows up as a failure rather than as a test quietly reading a
    file nothing writes.
    """
    return sockets.path / baileys_bridge.PAIRING_RELAY_NAME


def _publish(sockets, window_id, state, *, qr=None, qr_seq=0, message=""):
    pairing_relay.write_relay(
        _relay_path(sockets),
        pairing_relay.build_payload(
            window_id=window_id,
            state=state,
            expires_at=time.time() + 300.0,
            qr=qr,
            qr_seq=qr_seq,
            message=message,
        ),
    )


@pytest.fixture(autouse=True)
def _fast_pair(monkeypatch):
    """Six seconds rather than five minutes. The shipped value is fifteen QR
    rotations' worth of patience for a human finding their phone; a test
    asserting a timeout must not wait it out."""
    monkeypatch.setattr(cli, "WHATSAPP_PAIR_TIMEOUT_SECONDS", 6.0)


class TestTheRefusals:
    def test_a_cloud_deployment_is_told_where_its_setup_lives(
        self, tmp_path, sockets, capsys,
    ):
        path = _config_file(tmp_path, sockets, provider="whatsapp_cloud")

        assert cli.cmd_whatsapp_pair(_args(path)) == 1
        assert "business setup" in capsys.readouterr().err

    def test_a_stale_socket_file_does_not_refuse(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """The inode outliving its process is exactly what the bridge's own
        `_unlink_stale` exists to clean up, so presence says nothing — the
        test has to be a connect.

        Driven to the *next* refusal rather than to a pair, so what is
        asserted is having got past this gate rather than a status two gates
        could produce.
        """
        path = _config_file(tmp_path, sockets)
        socket_path = sockets.path / "whatsapp-baileys.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.close()
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        _use_sidecar(monkeypatch, ())

        assert cli.cmd_whatsapp_pair(_args(path)) == 1

        err = capsys.readouterr().err
        assert "sidecar_command" in err
        assert "Stop the istota scheduler" not in err

    def test_a_bridge_that_cannot_start_reports_it_rather_than_raising(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """`start()` opens the socket before it creates its worker, so a
        failure past that point leaves the inode behind — and the next attempt
        then meets the live-socket refusal its own predecessor left. An
        uncaught one is also a traceback where an operator wants a sentence.
        """
        _use_sidecar(monkeypatch, ("/bin/true",))
        path = _config_file(tmp_path, sockets)

        async def _explode(self):
            raise OSError("address already in use")

        monkeypatch.setattr(baileys_bridge.BaileysBridge, "start", _explode)

        assert cli.cmd_whatsapp_pair(_args(path)) == 1
        assert "could not start" in capsys.readouterr().err

    def test_no_resolvable_sidecar_says_which_setting_names_one(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        path = _config_file(tmp_path, sockets)
        _use_sidecar(monkeypatch, ())

        assert cli.cmd_whatsapp_pair(_args(path)) == 1
        assert "sidecar_command" in capsys.readouterr().err

    def test_it_falls_back_to_the_program_in_this_checkout(
        self, tmp_path, sockets, monkeypatch,
    ):
        """The fallback the daemon deliberately does not have. Pairing on a
        developer machine and on a clone with no deployment wiring is what it
        is for, and it is also what gets a logged-out deployment out of the
        one-way door a permanent fatal closes."""
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(
            baileys_bridge, "resolve_sidecar_argv", lambda config: (),
        )
        monkeypatch.setattr(
            baileys_bridge, "in_tree_sidecar_argv", lambda: ("/bin/true",),
        )

        # Reaches the wait rather than the refusal, which is the discriminating
        # answer: `/bin/true` exits at once and nobody scans.
        assert cli.cmd_whatsapp_pair(_args(path)) == 1
        assert not (sockets.path / "whatsapp-baileys.sock").exists()


class TestPairingAgainstARealSidecar:
    def test_it_pairs_and_reports_where_the_session_lives(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        _use_sidecar(monkeypatch, _write_sidecar(sockets))
        path = _config_file(tmp_path, sockets)

        # The child's own traceback, where it has one. Its stdio is on
        # /dev/null by design, so without this a fake that raised was
        # indistinguishable from one nobody scanned — which cost real time
        # once already.
        assert cli.cmd_whatsapp_pair(_args(path)) == 0, _sidecar_error(sockets)

        out = capsys.readouterr().out
        assert "Paired" in out
        assert "whatsapp-baileys-session" in out

    def test_the_session_directory_is_left_private(
        self, tmp_path, sockets, monkeypatch,
    ):
        """Pairing is what creates the credential, so it is also what has to
        create it 0700: a directory made world-readable and narrowed later has
        a window in which the account is somebody else's."""
        _use_sidecar(monkeypatch, _write_sidecar(sockets))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path)) == 0

        session = sockets.path / "whatsapp-baileys-session"
        assert stat.S_IMODE(session.stat().st_mode) == 0o700

    def test_a_code_reaches_the_terminal(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        _use_sidecar(monkeypatch, _write_sidecar(sockets, qr="2@PAIRINGPAYLOAD=="))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path)) == 0

        out = capsys.readouterr().out
        # Drawn, not dictated: the operator sees a code to scan and the
        # payload never reaches the terminal at all.
        assert "\u2588" in out
        assert "2@PAIRINGPAYLOAD==" not in out
        assert "Linked Devices" in out

    def test_a_session_that_cannot_be_used_fails_rather_than_waiting(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """A permanent fatal during pairing is a session directory that has to
        be removed, and waiting out the timeout would tell the operator
        nothing about why."""
        _use_sidecar(monkeypatch, _write_sidecar(sockets, fatal=True))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path)) == 1
        assert "cannot be used" in capsys.readouterr().err

    def test_a_sidecar_nobody_scans_times_out_and_leaves_nothing_running(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """The socket is unlinked and the child reaped, so a second attempt
        does not meet the live-socket refusal its own predecessor left."""
        silent = sockets.path / "silent.py"
        silent.write_text("import time; time.sleep(30)\n")
        _use_sidecar(monkeypatch, (sys.executable, str(silent)))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path)) == 1

        assert "Timed out" in capsys.readouterr().err
        assert not (sockets.path / "whatsapp-baileys.sock").exists()


class TestTheCodeIsNeverWrittenDown:
    def test_the_drawn_code_does_not_carry_the_payload_with_it(self, capsys):
        """A QR is the pairing credential for the whole account. Drawing it is
        what the operator asked for; printing the string beside it hands the
        same credential to anything scraping the terminal, and to the operator
        pasting an error report."""
        cli._render_qr("2@SECRET==")

        out = capsys.readouterr().out
        assert "\u2588" in out
        assert "2@SECRET==" not in out

    def test_it_reaches_no_other_process(self, monkeypatch, capsys):
        """The renderer used to pipe the payload to `qrencode`. Nothing is
        spawned now, so the credential reaches no argv, no pipe and no
        process table."""
        def _refuse(*a, **k):
            raise AssertionError("_render_qr spawned a subprocess")

        monkeypatch.setattr("subprocess.run", _refuse)
        monkeypatch.setattr("subprocess.Popen", _refuse)

        cli._render_qr("2@SECRET==")

        assert "\u2588" in capsys.readouterr().out

    def test_a_draw_that_fails_falls_back_rather_than_raising(
        self, monkeypatch, capsys,
    ):
        """`_render_qr` runs on the bridge's read loop through its callback,
        where a raise is swallowed with no `exc_info` — so a failure here
        would lose the code with nothing anywhere saying why."""
        import segno

        monkeypatch.setattr(
            segno, "make",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("too long")),
        )

        cli._render_qr("2@SECRET==")

        assert "2@SECRET==" in capsys.readouterr().out

    def test_it_writes_the_payload_to_no_log(self, caplog):
        with caplog.at_level(0):
            cli._render_qr("2@SECRET==")

        assert "2@SECRET==" not in caplog.text


class TestTheSocketLivenessTest:
    def test_a_missing_path_is_not_live(self, sockets):
        assert cli._whatsapp_socket_is_live(sockets.path / "nothing.sock") is False

    def test_a_listening_socket_is_live(self, sockets):
        path = sockets.path / "live.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        try:
            assert cli._whatsapp_socket_is_live(path) is True
        finally:
            listener.close()
            os.unlink(path)

    def test_a_regular_file_at_the_path_is_not_live(self, sockets):
        path = sockets.path / "not-a-socket"
        path.write_text("")

        assert cli._whatsapp_socket_is_live(path) is False


class TestTheResetFlag:
    """`--reset`: the recovery ISSUE-496 found there was no path to.

    A logged-out session is the one state nothing in the deployment could get
    out of. The credential on disk names a device WhatsApp has unlinked, so
    every reconnect is refused and no code is ever offered — and `pair` itself
    met the same wall, printing the remedy rather than performing it. What the
    operator was left with was composing an `rm -rf` against a full-account
    credential directory, as root, on a host where WhatsApp was already down.

    Two properties the issue names explicitly are asserted here rather than
    left to the bridge's own cases, because both are about the order this
    command does things in: the live-socket refusal still runs first, and a
    bare `pair` still refuses.
    """

    @staticmethod
    def _session(sockets):
        """Where `pair` will actually look.

        Derived from `db_path`, not from `SocketDir.session` — the config file
        these cases write names the socket directory as the database's parent,
        so the bridge's own `default_session_dir` puts the credential at
        `SESSION_DIR_NAME` beside it. Seeding `sockets.session` instead leaves
        the sidecar finding no marker, pairing on its first run, and the case
        passing while asserting nothing about a reset.
        """
        return sockets.path / baileys_bridge.SESSION_DIR_NAME

    def _seed_dead_session(self, sockets):
        session = self._session(sockets)
        baileys_bridge.ensure_session_dir(session)
        (session / "creds.json").write_text('{"me":"unlinked"}')
        return session

    def test_a_bare_pair_still_refuses_and_names_the_flag(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """The issue's second property. Without the flag in the message the
        operator is back to reading the source or the docs to find out that
        anything short of `rm -rf` exists."""
        self._seed_dead_session(sockets)
        _use_sidecar(monkeypatch, _write_sidecar(sockets, fatal_marker="creds.json"))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path)) == 1

        err = capsys.readouterr().err
        assert "--reset" in err
        assert (self._session(sockets) / "creds.json").exists(), _sidecar_error(sockets)

    def test_reset_moves_the_dead_session_aside_and_pairs(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """End to end, against a real socket and a real subprocess: the fatal
        arrives, the directory moves, the respawned sidecar finds no
        credential and offers a code, and the pairing completes."""
        self._seed_dead_session(sockets)
        _use_sidecar(monkeypatch, _write_sidecar(
            sockets, qr="2@PAIRINGPAYLOAD==", fatal_marker="creds.json",
        ))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 0, _sidecar_error(sockets)

        session = self._session(sockets)
        moved = [
            entry for entry in sockets.path.iterdir()
            if entry.name.startswith(session.name + ".")
        ]
        assert len(moved) == 1
        assert (moved[0] / "creds.json").read_text() == '{"me":"unlinked"}'
        assert not (session / "creds.json").exists()
        assert stat.S_IMODE(session.lstat().st_mode) == 0o700

        out = capsys.readouterr().out
        assert str(moved[0]) in out
        assert "Paired" in out

    def test_a_live_socket_moves_nothing_from_this_process(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """The issue's first property, in the form it survives in.

        This case used to assert that `--reset` was *refused* while a bridge
        held the socket. The refusal became the mode selector, so what has to
        hold now is the property the refusal existed for rather than the
        refusal itself: with a bridge live, **this process renames nothing**.
        The daemon performs the move, on evidence only it has — a `shutdown`
        frame and the link drop that frame caused — and moving a session
        directory a second Baileys client is holding is the auth-state
        corruption that ordering exists to prevent.

        **A sidecar is configured deliberately, though none may be spawned.**
        Without one the argv resolution refuses a line later, so a selector
        that fell through to own-sidecar mode would leave the directory
        untouched for an unrelated reason and this case would pass on it. With
        one configured, the only thing between `--reset` and a rename in *this*
        process is the selector under test.
        """
        self._seed_dead_session(sockets)
        _use_sidecar(monkeypatch, _write_sidecar(sockets, fatal_marker="creds.json"))
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(cli.sys, "stdin", _Tty("unlink\n"))
        monkeypatch.setattr(cli, "_whatsapp_follow_pairing", lambda config: 0)

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 0

        assert "listening" in capsys.readouterr().out
        session = self._session(sockets)
        assert (session / "creds.json").read_text() == '{"me":"unlinked"}'
        assert not [
            entry for entry in sockets.path.iterdir()
            if entry.name.startswith(session.name + ".")
        ]
        # The request is what it did instead, and it carries the confirmation.
        row = _pairing_row(sockets)
        assert row["state"] == "requested"
        assert row["force"] is True


class TestTheModeSelector:
    """`_whatsapp_socket_is_live` decides which pairing this is.

    It used to decide whether pairing happened at all: a live socket was a
    refusal, and an operator's remedy was to stop the scheduler and the sidecar
    unit, pair from a terminal, and start them again. It is now the selector
    between two modes that both pair, and the property that has to hold on each
    side is different.

    **Attach mode spawns nothing**, because the bridge on the other end of that
    socket already holds the session directory and two Baileys clients on one
    auth state corrupt it. **Own-sidecar mode writes no request row**, because
    there is no daemon to service one — which is also the case a developer
    checkout and a first-ever compose pair are.

    Both halves are asserted, so the selector cannot be removed in either
    direction without something going red: collapsed to own-sidecar mode the
    attach cases fail, and collapsed to attach mode the own-sidecar one does.
    """

    @staticmethod
    def _refuse_every_bridge(monkeypatch):
        """Make constructing a bridge a test failure.

        The strongest available statement of "spawns nothing": a mode that
        reached for a sidecar would have to build a `BaileysBridge` to do it,
        and every construction site in this command goes through this name.
        """
        class _Refused:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "attach mode constructed a bridge, so it may have spawned "
                    "a second Baileys client against the session directory"
                )

        monkeypatch.setattr(baileys_bridge, "BaileysBridge", _Refused)

    def test_a_live_socket_writes_a_request_instead_of_refusing(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        _init_pairing_db(sockets)
        self._refuse_every_bridge(monkeypatch)
        _use_sidecar(monkeypatch, ("/bin/true",))
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(cli, "_whatsapp_follow_pairing", lambda config: 0)

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path)) == 0

        row = _pairing_row(sockets)
        assert row["state"] == "requested"
        # Unforced, which is the whole of the difference `--reset` makes: the
        # daemon refuses to disconnect a session that has not reported itself
        # unlinked unless this column says an operator confirmed it.
        assert row["force"] is False
        assert row["requested_by"].startswith("cli:")
        out = capsys.readouterr().out
        assert "listening" in out
        assert "Requested" in out
        # Nothing in this process touched the credential's directory.
        assert not (sockets.path / baileys_bridge.SESSION_DIR_NAME).exists()

    def test_a_dead_socket_pairs_itself_and_writes_no_request(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """The other half, and the one that keeps the developer checkout
        working. There is no daemon to service a request here, so a row written
        instead of a sidecar spawned would be a pairing that waits out its
        deadline for a code nobody is generating."""
        _init_pairing_db(sockets)
        _use_sidecar(monkeypatch, _write_sidecar(sockets, qr="2@OWNSIDECAR=="))
        path = _config_file(tmp_path, sockets)

        assert cli.cmd_whatsapp_pair(_args(path)) == 0, _sidecar_error(sockets)

        assert _pairing_row(sockets) is None
        assert "Paired" in capsys.readouterr().out


class TestAttachModeRefusals:
    """What attach mode declines to write a request for.

    Both path arms are about where the QR lands rather than about pairing, and
    the bridge applies neither — it deliberately does not check its own relay
    path, so `web_app.admin_whatsapp_pairing_start` and this are the only two
    gates in front of a window. A CLI with no gate of its own would be a second
    door onto the window the route refuses.
    """

    def test_a_relative_relay_path_is_refused_rather_than_resolved(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """This process reads the relay and the daemon writes it, each against
        its own working directory — so a relative path means watching a file
        nothing writes while a live credential is published somewhere else."""
        _init_pairing_db(sockets)
        path = _config_file(
            tmp_path, sockets, baileys=['pairing_relay_path = "relay.json"'],
        )

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path)) == 1

        err = capsys.readouterr().err
        assert "pairing_relay_path" in err
        assert "relative" in err
        assert _pairing_row(sockets) is None

    def test_a_relay_inside_a_sandbox_bind_is_refused(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """A path inside a root some task's bubblewrap namespace binds would
        hand the pairing code to the model, which links itself as a device.
        `{temp_dir}/{user}` is bound read-write into that user's namespace, so
        anything below the temp root is inside one."""
        _init_pairing_db(sockets)
        relay = tmp_path / "tmp" / "alice" / "whatsapp-pairing.json"
        path = _config_file(
            tmp_path, sockets, baileys=[f'pairing_relay_path = "{relay}"'],
        )

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path)) == 1

        err = capsys.readouterr().err
        assert "sandbox" in err
        assert "pairing_relay_path" in err
        assert _pairing_row(sockets) is None

    def test_a_request_already_in_progress_is_not_written_over(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """`request_whatsapp_pairing`'s guard is in SQL and answers `None`;
        the route turns that into a 409 and this into a sentence. Joining the
        open window instead is deliberately not a mode — it would mean two
        watchers and one of them drawing a code it did not ask for."""
        _init_pairing_db(sockets)
        with db.get_db(sockets.path / "istota.db") as conn:
            assert db.request_whatsapp_pairing(conn, "admin") is not None
        path = _config_file(tmp_path, sockets)

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path)) == 1

        assert "already in progress" in capsys.readouterr().err
        assert _pairing_row(sockets)["requested_by"] == "admin"

    def test_a_database_that_cannot_be_written_is_a_sentence(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """Attach mode is reachable from any host whose socket path happens to
        be live, so "this is not the host the daemon runs on" is an ordinary
        mistake rather than a fault."""
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)

        def _explode(*args, **kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(db, "request_whatsapp_pairing", _explode)

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path)) == 1

        assert "could not be written" in capsys.readouterr().err


class TestTheUnlinkConfirmation:
    """`--reset` in attach mode, which is the destructive half.

    Own-sidecar mode's `--reset` acts only on a session that has already
    reported a permanent fault — `reset_session` refuses anything else — so
    there is nothing working to disconnect and the flag is the whole
    confirmation. Attach mode's writes `pairing_force`, which is what lets the
    daemon disconnect a session that *is* working, and the web's control
    collects a typed phrase for exactly that. Same phrase here, and no flag to
    skip it.
    """

    def test_a_non_terminal_stdin_is_refused_with_no_flag_to_skip_it(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """There is no `--yes`, and the reason is not strictness: the command
        draws a QR and waits for a human to scan it off a phone, so it can do
        nothing useful unattended anyway — a flag would exist only to let the
        destructive half run without one."""
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("unlink\n"))

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 1

        assert "typed confirmation" in capsys.readouterr().err
        assert _pairing_row(sockets) is None

    def test_the_wrong_phrase_writes_nothing(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(cli.sys, "stdin", _Tty("yes\n"))

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 1

        assert "Not confirmed" in capsys.readouterr().err
        assert _pairing_row(sockets) is None

    def test_ctrl_c_at_the_prompt_reads_as_no(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """Nothing has been written at that point, so there is nothing for a
        traceback to explain — and a `KeyboardInterrupt` escaping here would be
        one, since no caller of this command catches it."""
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)

        class _Interrupted(_Tty):
            def readline(self, *args):
                raise KeyboardInterrupt

        monkeypatch.setattr(cli.sys, "stdin", _Interrupted())

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 1

        assert "Not confirmed" in capsys.readouterr().err
        assert _pairing_row(sockets) is None

    def test_the_phrase_matches_the_panes_own(self):
        """One spelling on both surfaces, so an operator who has used the
        destructive control in Admin, Connections recognises this prompt.

        Read out of the Svelte source rather than restated, since a constant
        copied into a test is a constant that agrees with itself.
        """
        pane = (
            Path(__file__).resolve().parents[1]
            / "web/src/routes/admin/connections/+page.svelte"
        ).read_text()
        match = re.search(r"UNLINK_CHALLENGE\s*=\s*'([^']+)'", pane)
        assert match is not None, "the pane's challenge constant moved"
        assert match.group(1) == cli.WHATSAPP_UNLINK_CHALLENGE

    def test_the_prompt_names_the_shorter_route(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """Every remedy in the tree — doctor's FAIL arm, the unlink alert, the
        sidecar's own log lines — names `--reset` about a session that is
        already unlinked, where a bare `pair` needs no confirmation at all. An
        operator arriving from one of those should be told so rather than
        typing a phrase they did not need."""
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(cli.sys, "stdin", _Tty("no\n"))

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 1

        out = capsys.readouterr().out
        assert "istota whatsapp pair" in out
        assert "without the confirmation" in out

    def test_the_confirmed_request_carries_the_force_column(
        self, tmp_path, sockets, monkeypatch,
    ):
        """`pairing_force` is the whole carrier of the confirmation across the
        two processes: the poll passes `repair_session(force=True)` only where
        this column reads 1, so a request written without it is refused
        `session_live` against a working session."""
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)
        monkeypatch.setattr(cli.sys, "stdin", _Tty("unlink\n"))
        monkeypatch.setattr(cli, "_whatsapp_follow_pairing", lambda config: 0)

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 0

        assert _pairing_row(sockets)["force"] is True

    def test_the_path_refusals_run_before_the_prompt(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """Asking somebody to type a phrase and then refusing on a
        configuration key is the wrong order. Driven by supplying a stdin that
        would answer correctly: if the prompt ran, the refusal would be the
        path one either way, so the assertion is that nothing was *read*."""
        _init_pairing_db(sockets)
        stdin = _Tty("unlink\n")
        path = _config_file(
            tmp_path, sockets, baileys=['pairing_relay_path = "relay.json"'],
        )
        monkeypatch.setattr(cli.sys, "stdin", stdin)

        with _live_bridge_socket(sockets):
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 1

        assert stdin.tell() == 0, "the confirmation was read before the refusal"
        assert "relative" in capsys.readouterr().err

    def test_a_user_with_no_passwd_entry_still_gets_a_name(self, monkeypatch):
        """`getpass.getuser` raises where no environment variable names a user
        and the uid has no passwd entry — a container, most often. The row's
        `requested_by` is prose, so the fallback is a name rather than a
        refusal."""
        import getpass

        def _explode():
            raise OSError("no such user")

        monkeypatch.setattr(getpass, "getuser", _explode)
        assert cli._whatsapp_cli_actor() == "cli"


class TestTheViewTheAttachLoopReads:
    """`_whatsapp_pairing_view`: the request row joined to the relay file.

    The sibling of `web_app._pairing_state_payload`, and the rule they share is
    what these cases are about: the row first with its terminal state as a
    veto, then the relay matched on the row's **current** window id.
    """

    @staticmethod
    def _config(tmp_path, sockets):
        _init_pairing_db(sockets)
        return cli.load_config(_config_file(tmp_path, sockets))

    def test_a_relay_from_another_window_is_ignored(self, tmp_path, sockets):
        """A window's own file is unlinked when it closes, so a file carrying
        another id is a skipped unlink or a previous attempt — and rendering
        its code would be drawing a credential nothing can scan into."""
        config = self._config(tmp_path, sockets)
        with db.get_db(sockets.path / "istota.db") as conn:
            request_id = db.request_whatsapp_pairing(conn, "admin")
        _publish(
            sockets, "a-window-nobody-owns", "awaiting_scan",
            qr="2@STALEPAYLOAD==", qr_seq=4,
        )

        view = cli._whatsapp_pairing_view(config)

        assert view["window_id"] == request_id
        assert view["qr"] == ""
        assert view["qr_seq"] == 0

    def test_the_relay_is_read_against_the_adopted_window_id(
        self, tmp_path, sockets,
    ):
        """The id the request returned is *not* the id the relay carries: the
        row adopts the bridge's own window id when a window opens. A reader
        pinned to the request id stops seeing the relay at that transition,
        which is the first thing that happens on every real pairing."""
        config = self._config(tmp_path, sockets)
        with db.get_db(sockets.path / "istota.db") as conn:
            request_id = db.request_whatsapp_pairing(conn, "admin")
            assert db.record_whatsapp_pairing_state(
                conn, request_id, "awaiting_sidecar",
                adopt_window_id="bridge-window", expires_at=time.time() + 300,
            )
        _publish(
            sockets, "bridge-window", "awaiting_scan",
            qr="2@LIVEPAYLOAD==", qr_seq=2, message="scan it",
        )

        view = cli._whatsapp_pairing_view(config)

        assert view["window_id"] == "bridge-window"
        assert view["state"] == "awaiting_scan"
        assert view["qr"] == "2@LIVEPAYLOAD=="
        assert view["qr_seq"] == 2
        # The live prose while a window is publishing.
        assert view["message"] == "scan it"

    def test_a_terminal_row_vetoes_a_relay_that_is_still_publishing(
        self, tmp_path, sockets,
    ):
        """The relay carries the window's own deadline, which is later than the
        request row's — so a window republishing after its row was closed
        passes `read_relay`'s own deadline check. The row has to be asked."""
        config = self._config(tmp_path, sockets)
        with db.get_db(sockets.path / "istota.db") as conn:
            request_id = db.request_whatsapp_pairing(conn, "admin")
            assert db.record_whatsapp_pairing_state(
                conn, request_id, "failed", "the sidecar never came back",
            )
        _publish(
            sockets, request_id, "awaiting_scan",
            qr="2@ORPHANPAYLOAD==", qr_seq=7,
        )

        view = cli._whatsapp_pairing_view(config)

        assert view["terminal"] is True
        assert view["state"] == "failed"
        assert view["qr"] == ""
        # The row's message, which is where the archived path lives.
        assert view["message"] == "the sidecar never came back"

    def test_no_row_at_all_reads_as_nothing(self, tmp_path, sockets):
        assert cli._whatsapp_pairing_view(self._config(tmp_path, sockets)) is None

    def test_the_request_stamp_is_carried_for_the_follower_to_pin_on(
        self, tmp_path, sockets,
    ):
        """The follower cannot pin on the window id, since the row rotates onto
        the bridge's own at the first transition — so these two are what tell
        one request from the next on a singleton row."""
        config = self._config(tmp_path, sockets)
        with db.get_db(sockets.path / "istota.db") as conn:
            assert db.request_whatsapp_pairing(conn, "cli:operator") is not None

        view = cli._whatsapp_pairing_view(config)

        assert view["requested_by"] == "cli:operator"
        assert view["requested_at"]

    def test_an_unresolvable_sandbox_answer_says_which_question_failed(
        self, tmp_path, sockets, monkeypatch,
    ):
        """A refusal saying "the sandbox binds (unresolvable)" names a
        collision nobody observed, which an operator cannot act on."""
        config = self._config(tmp_path, sockets)
        from istota import sandbox_plan

        def _explode(cfg, path):
            raise RuntimeError("no")

        monkeypatch.setattr(sandbox_plan, "sandbox_bound_reason", _explode)

        refusal = cli._whatsapp_attach_refusal(config)

        assert refusal is not None
        assert "could not be settled" in refusal
        assert "unresolvable" not in refusal


class TestFollowingTheWindow:
    """The attach loop, driven over scripted views.

    Scripted rather than driven through a daemon: what is under test here is
    what the loop does with each state it can be handed, and a real bridge
    would make the *sequence* of states a race rather than an assertion. The
    join those views come from has its own cases above, and one end-to-end run
    through both is at the bottom of this file.
    """

    @staticmethod
    def _view(state, **over):
        row_state = over.pop("row_state", state)
        view = {
            "state": state,
            "row_state": row_state,
            "terminal": row_state in db.WHATSAPP_PAIRING_TERMINAL_STATES,
            "window_id": "w",
            "message": "",
            "expires_at": time.time() + 300.0,
            "qr": "",
            "qr_seq": 0,
            "requested_at": "2026-09-17 10:00:00",
            "requested_by": "cli:operator",
        }
        view.update(over)
        return view

    @staticmethod
    def _drive(monkeypatch, views):
        """Hand the loop one view per tick, repeating the last for ever.

        Repeating rather than exhausting: several cases are about what the loop
        does *while* a state persists, and a `StopIteration` would end them at
        the tick the property starts holding.
        """
        remaining = list(views)
        drawn = []

        def _next(config):
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        monkeypatch.setattr(cli, "_whatsapp_pairing_view", _next)
        monkeypatch.setattr(cli, "_render_qr", drawn.append)
        monkeypatch.setattr(cli, "WHATSAPP_PAIR_POLL_SECONDS", 0.0)
        return drawn

    def test_a_code_is_drawn_once_per_rotation_not_once_per_tick(
        self, monkeypatch,
    ):
        """WhatsApp rotates the payload about every twenty seconds and this
        reads every second, so a redraw per read would scroll the operator's
        terminal past the code they were about to scan.

        The control is dropping the `qr_seq` comparison so every tick draws:
        that turns this red on the count while leaving every other case here
        green, since none of the others asserts one.
        """
        scan = self._view("awaiting_scan", qr="2@FIRST==", qr_seq=1)
        drawn = self._drive(monkeypatch, [
            self._view("servicing"),
            scan, scan, scan, scan, scan,
            self._view("awaiting_scan", qr="2@SECOND==", qr_seq=2),
            self._view("paired"),
        ])

        assert cli._whatsapp_follow_pairing(None) == 0
        assert drawn == ["2@FIRST==", "2@SECOND=="]

    def test_a_code_whose_counter_is_unusable_is_still_drawn(self, monkeypatch):
        """A rotation is compared on the payload rather than on `qr_seq`.

        `read_relay` hands that counter back exactly as the file held it —
        `web_app` coerces it with `_as_int` for the same reason — so a relay
        whose counter is missing or unparseable reads as 0, which is what the
        loop starts at. Compared on the counter, the code would never be drawn
        at all: an operator watching a window that has a live code and shows
        none. The control is comparing counters, which turns this red while
        leaving the once-per-rotation case above green.
        """
        drawn = self._drive(monkeypatch, [
            self._view("awaiting_scan", qr="2@ONLYCODE==", qr_seq=0),
            self._view("awaiting_scan", qr="2@ONLYCODE==", qr_seq=0),
            self._view("paired"),
        ])

        assert cli._whatsapp_follow_pairing(None) == 0
        assert drawn == ["2@ONLYCODE=="]

    def test_a_paired_row_is_the_only_zero(self, monkeypatch):
        for state in sorted(db.WHATSAPP_PAIRING_TERMINAL_STATES):
            self._drive(monkeypatch, [self._view(state)])
            expected = 0 if state == "paired" else 1
            assert cli._whatsapp_follow_pairing(None) == expected, state

    def test_a_closed_window_prints_the_daemons_own_prose(
        self, monkeypatch, capsys,
    ):
        """The archived path is in that message and nowhere else this process
        can reach — it is the only durable record of where an operator's
        credential went."""
        self._drive(monkeypatch, [self._view(
            "failed", message="the old session is at /srv/.baileys.old-2026",
        )])

        assert cli._whatsapp_follow_pairing(None) == 1
        assert ".baileys.old-2026" in capsys.readouterr().out

    def test_a_request_cleared_from_under_it_says_so(self, monkeypatch, capsys):
        """A web admin's cancel landing between two reads, or the poll's own
        row-without-id escape. Neither is this process's to recover."""
        self._drive(monkeypatch, [None])

        assert cli._whatsapp_follow_pairing(None) == 1
        assert "gone" in capsys.readouterr().err

    def test_a_read_that_raises_is_a_sentence_rather_than_a_traceback(
        self, monkeypatch, capsys,
    ):
        def _explode(config):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(cli, "_whatsapp_pairing_view", _explode)

        assert cli._whatsapp_follow_pairing(None) == 1
        assert "could not be read" in capsys.readouterr().err

    def test_a_row_nothing_picks_up_names_the_scheduler(
        self, monkeypatch, capsys,
    ):
        """A live socket is not proof a scheduler is running: `istota whatsapp
        pair` in own-sidecar mode on another terminal holds one too. So the
        wait says which unit to look at — and it keeps waiting, because a late
        scheduler still services the row."""
        self._drive(monkeypatch, [
            self._view("requested"), self._view("requested"),
            self._view("paired"),
        ])
        monkeypatch.setattr(cli, "WHATSAPP_PAIR_PICKUP_SECONDS", 0.0)

        assert cli._whatsapp_follow_pairing(None) == 0
        err = capsys.readouterr().err
        assert "No bridge has picked this request up" in err
        assert err.count("No bridge has picked") == 1

    def test_the_pickup_warning_is_not_printed_for_a_serviced_row(
        self, monkeypatch, capsys,
    ):
        self._drive(monkeypatch, [
            self._view("servicing"), self._view("servicing"),
            self._view("paired"),
        ])
        monkeypatch.setattr(cli, "WHATSAPP_PAIR_PICKUP_SECONDS", 0.0)

        assert cli._whatsapp_follow_pairing(None) == 0
        assert "No bridge has picked" not in capsys.readouterr().err

    def test_a_deadline_the_daemon_never_closes_is_not_waited_out_for_ever(
        self, monkeypatch, capsys,
    ):
        """The arm that closes a wedged row runs in the daemon's own poll, so a
        scheduler that died mid-window leaves nothing to stamp the row
        terminal. Without a ceiling of its own this loop waits for a code that
        cannot come, for ever."""
        self._drive(monkeypatch, [
            self._view("awaiting_scan", expires_at=time.time() - 60.0)
        ])

        assert cli._whatsapp_follow_pairing(None) == 1
        assert "poll is not running" in capsys.readouterr().err

    def test_a_row_with_no_readable_deadline_is_not_abandoned(
        self, monkeypatch, capsys,
    ):
        """`sql_epoch_from_datetime` answers `None` for a column it cannot
        parse, and reading that as "the deadline has passed" would abandon a
        live window over an unparseable timestamp."""
        self._drive(monkeypatch, [
            self._view("awaiting_sidecar", expires_at=None),
            self._view("paired"),
        ])

        assert cli._whatsapp_follow_pairing(None) == 0
        assert "poll is not running" not in capsys.readouterr().err

    def test_another_operators_request_is_not_reported_as_this_one(
        self, monkeypatch, capsys,
    ):
        """`whatsapp_runtime` is a singleton, so the row can become somebody
        else's: a web admin may write a fresh request in the second between
        this one going terminal and the next read.

        The wrong answer that follows is not cosmetic — a `paired` row from
        their attempt would be reported as this operator's, exit 0, over a
        re-pair that had just failed. Pinned on the request stamp rather than
        the window id, which the row rotates onto the bridge's own at the first
        transition; the control is dropping the pin, which turns this red with
        an exit 0 and "Paired" on stdout.
        """
        self._drive(monkeypatch, [
            self._view("servicing"),
            self._view(
                "paired", requested_at="2026-09-17 11:00:00",
                requested_by="admin",
            ),
        ])

        assert cli._whatsapp_follow_pairing(None) == 1
        assert "replaced this one" in capsys.readouterr().err

    def test_every_state_the_row_can_hold_has_something_to_say(self):
        """The note table is keyed by literals, so a renamed state would
        silently print nothing — `.get` answers `None` and the loop carries on.
        Built from the product's own constants, so a rename is a failure here
        rather than a state an operator watches in silence.

        `awaiting_scan` is the deliberate absence: the code is what that state
        has to say, and a sentence over it would push it up the terminal.
        """
        expected = {
            db.WHATSAPP_PAIRING_REQUESTED,
            db.WHATSAPP_PAIRING_SERVICING,
            *db.WHATSAPP_PAIRING_WINDOW_STATES,
            *db.WHATSAPP_PAIRING_TERMINAL_STATES,
        } - {db.WHATSAPP_PAIRING_AWAITING_SCAN}

        assert set(cli._WHATSAPP_PAIRING_NOTES) == expected

    def test_ctrl_c_leaves_the_window_open(self, monkeypatch, capsys):
        """The window lives in the daemon and its session directory has already
        moved aside, so a scan that lands with nobody watching still pairs.
        Closing it from here would abandon that."""
        def _interrupt(config):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_whatsapp_pairing_view", _interrupt)

        assert cli._whatsapp_follow_pairing(None) == 1
        err = capsys.readouterr().err
        assert "still open" in err
        assert "cancel it" in err

    def test_the_payload_reaches_no_log_record(self, monkeypatch, caplog):
        """The rule the whole relay design rests on: a QR is the full-account
        credential, so it never reaches `logging` on any path at any level —
        `db_backup` would carry a log file onto the Nextcloud mount, and a
        traceback frame holding one is why `_call_back` logs without
        `exc_info`.

        Scanned rather than trusted: the control is a `logger.debug` of the
        payload anywhere in this loop, which turns this red and nothing else.
        """
        payload = "2@NEVERLOGTHIS=="
        self._drive(monkeypatch, [
            self._view("awaiting_scan", qr=payload, qr_seq=1),
            self._view("paired"),
        ])

        with caplog.at_level(0):
            assert cli._whatsapp_follow_pairing(None) == 0

        assert payload not in caplog.text
        assert not [r for r in caplog.records if payload in str(r.args or "")]


class TestAttachModeEndToEnd:
    """One run through the real join, the real row and the real relay file.

    Every other case here drives one seam. This one is the integration: the
    command writes a request, a stand-in for the daemon services it exactly as
    the poll would — adopting a window id, publishing a code, reporting paired
    — and the operator's terminal shows the code and exits 0.
    """

    def test_a_request_is_serviced_and_the_code_reaches_the_terminal(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        _init_pairing_db(sockets)
        path = _config_file(tmp_path, sockets)
        payload = "2@ATTACHEDPAYLOAD=="
        monkeypatch.setattr(cli, "WHATSAPP_PAIR_POLL_SECONDS", 0.01)
        failure: list[BaseException] = []

        def _daemon():
            """What `poll_pairing_request` and the bridge do, in order."""
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    row = _pairing_row(sockets)
                    if row is not None and row["state"] == "requested":
                        break
                    time.sleep(0.005)
                else:
                    raise AssertionError("no pairing request was ever written")
                request_id = row["window_id"]
                with db.get_db(sockets.path / "istota.db") as conn:
                    assert db.record_whatsapp_pairing_state(
                        conn, request_id, "awaiting_sidecar",
                        adopt_window_id="bridge-window",
                        expires_at=time.time() + 300.0,
                    )
                _publish(
                    sockets, "bridge-window", "awaiting_scan",
                    qr=payload, qr_seq=1,
                )
                # Long enough for several of the loop's ticks to pass over one
                # unchanged code, which is what makes the single draw below an
                # assertion rather than a coincidence of timing.
                time.sleep(0.2)
                with db.get_db(sockets.path / "istota.db") as conn:
                    assert db.record_whatsapp_pairing_state(
                        conn, "bridge-window", "paired", "linked",
                    )
                pairing_relay.clear_relay(_relay_path(sockets))
            except BaseException as exc:  # noqa: BLE001 — reported, not raised
                failure.append(exc)

        driver = threading.Thread(target=_daemon, daemon=True)
        driver.start()
        try:
            with _live_bridge_socket(sockets):
                code = cli.cmd_whatsapp_pair(_args(path))
        finally:
            driver.join(timeout=10.0)

        assert not failure, failure
        assert code == 0

        out = capsys.readouterr().out
        assert "Paired" in out
        # Drawn, and the payload itself never printed — the same property
        # `TestTheCodeIsNeverWrittenDown` asserts of `_render_qr` directly.
        assert "█" in out or "▀" in out
        assert payload not in out
        assert _pairing_row(sockets)["state"] == "paired"
