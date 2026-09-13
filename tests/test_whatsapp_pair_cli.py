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
import os
import socket
import stat
import sys

import pytest

from istota import cli
from istota.transport.whatsapp import baileys_bridge

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


def _config_file(tmp_path, sockets, *, provider="baileys", sidecar_command=""):
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
    if sidecar_command:
        body += ["", "[whatsapp.baileys]", f'sidecar_command = "{sidecar_command}"']
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

    def test_a_live_socket_refuses_rather_than_pairing_beside_it(
        self, tmp_path, sockets, capsys,
    ):
        """Two Baileys clients on one auth state corrupt it — each rotates
        keys the other then fails to decrypt with — so the session directory
        is a single-writer resource and a running daemon's bridge is already
        its writer."""
        path = _config_file(tmp_path, sockets, sidecar_command="/bin/true")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sockets.path / "whatsapp-baileys.sock"))
        listener.listen(1)
        try:
            assert cli.cmd_whatsapp_pair(_args(path)) == 1
        finally:
            listener.close()

        assert "Stop the istota scheduler" in capsys.readouterr().err

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

    def test_the_live_socket_refusal_runs_before_the_reset(
        self, tmp_path, sockets, capsys, monkeypatch,
    ):
        """The issue's first property, and the one that matters most.

        Moving a session directory a second Baileys client is holding is the
        auth-state corruption the refusal exists to prevent, so `--reset` has
        to sit behind it rather than beside it — and the credential has to
        still be there afterwards, which is what separates a refusal from a
        reset that happened and then reported a problem.

        **A sidecar is configured deliberately, though none is ever spawned.**
        Without one the argv resolution refuses a line later, so removing the
        live-socket check still leaves the directory untouched and this case
        goes red on the message alone — measured. With one, the only thing
        between `--reset` and the rename is the refusal under test.
        """
        self._seed_dead_session(sockets)
        _use_sidecar(monkeypatch, _write_sidecar(sockets, fatal_marker="creds.json"))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sockets.path / "whatsapp-baileys.sock"))
        listener.listen(1)
        path = _config_file(tmp_path, sockets)
        try:
            assert cli.cmd_whatsapp_pair(_args(path, reset=True)) == 1
        finally:
            listener.close()

        assert "already listening" in capsys.readouterr().err
        session = self._session(sockets)
        assert (session / "creds.json").read_text() == '{"me":"unlinked"}'
        assert not [
            entry for entry in sockets.path.iterdir()
            if entry.name.startswith(session.name + ".")
        ]
