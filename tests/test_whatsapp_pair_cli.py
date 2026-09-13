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
import json, socket, time, traceback

CONFIG = {config}

try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(CONFIG["socket"])

    def say(**fields):
        sock.sendall((json.dumps(fields) + "\\n").encode())

    say(type="hello", protocol_version=1)
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


def _write_sidecar(sockets, *, qr=None, fatal=False):
    path = sockets.path / "fake_sidecar.py"
    # `repr`, not `json.dumps`: a JSON `null` or `false` is a Python
    # SyntaxError, and the child then dies at compile time — before its own
    # `except` can write the traceback, and with its stderr on /dev/null,
    # which is exactly the diagnostic-free failure this log file exists for.
    path.write_text(_SIDECAR_TEMPLATE.format(config=repr({
        "socket": str(sockets.path / "whatsapp-baileys.sock"),
        "qr": qr,
        "fatal": fatal,
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


def _args(config_path):
    return argparse.Namespace(config=str(config_path))


def _sidecar_error(sockets) -> str:
    log = sockets.path / "sidecar-errors.log"
    return log.read_text() if log.exists() else "the fake sidecar left no traceback"


def _use_sidecar(monkeypatch, argv):
    monkeypatch.setattr(
        baileys_bridge, "resolve_sidecar_argv", lambda config: argv,
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
        # `shutil.which` is asked for `qrencode`; without it the payload and
        # the command that renders it are printed instead, which is the branch
        # a host without that package takes.
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert cli.cmd_whatsapp_pair(_args(path)) == 0

        out = capsys.readouterr().out
        assert "2@PAIRINGPAYLOAD==" in out
        assert "qrencode" in out

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
    def test_rendering_reaches_qrencode_on_stdin_rather_than_argv(
        self, monkeypatch,
    ):
        """A QR is the pairing credential for the whole account. On the
        operator's own terminal at their own request it is theirs to see; in
        anybody's `ps` output it is not."""
        seen = {}

        class _Result:
            returncode = 0
            stdout = b"[drawn]"

        def _run(argv, **kwargs):
            seen["argv"] = argv
            seen["input"] = kwargs.get("input")
            return _Result()

        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/qrencode")
        monkeypatch.setattr("subprocess.run", _run)

        cli._render_qr("2@SECRET==")

        assert "2@SECRET==" not in " ".join(seen["argv"])
        assert seen["input"] == b"2@SECRET=="

    def test_a_qrencode_that_fails_falls_back_rather_than_raising(
        self, monkeypatch, capsys,
    ):
        """`_render_qr` runs on the bridge's read loop through its callback,
        where a raise is swallowed with no `exc_info` — so a failure here
        would lose the code with nothing anywhere saying why."""
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/qrencode")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no such file")),
        )

        cli._render_qr("2@SECRET==")

        assert "2@SECRET==" in capsys.readouterr().out

    def test_it_writes_the_payload_to_no_log(self, caplog, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)

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
