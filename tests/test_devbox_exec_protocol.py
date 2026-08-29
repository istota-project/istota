"""The devbox exec wire format, with no I/O at all.

The point of splitting serialization out of the client and the server is that
the frames, the caps and the request shapes can be pinned here, and the two
files that speak them can be tested against real sockets without also re-testing
the format. Same split ``devbox_proxy_protocol`` already uses.

The other job of this file is the vendored copy. ``istota-exec-serve`` runs in a
container with no istota package and imports
``docker/devbox/lib/istota_devbox_exec_protocol.py``; that copy is what the
image ships, and a drift between it and ``src/`` is a wire-format disagreement
between the two ends of the transport, which is the failure mode with no error
message.
"""

from __future__ import annotations

import inspect
import json
import struct
from pathlib import Path

import pytest

from istota import devbox_exec_protocol as p

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "src/istota/devbox_exec_protocol.py"
VENDORED = ROOT / "docker/devbox/lib/istota_devbox_exec_protocol.py"
SYNC_SCRIPT = ROOT / "scripts/sync-devbox-lib.sh"


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #


class TestFrames:
    def test_the_header_is_the_stdcopy_shape(self):
        """Eight bytes, `>BxxxI` — the same header Docker's stdcopy uses, so
        anyone who has read one has read the other."""
        assert p.FRAME_HEADER_BYTES == 8
        assert p.pack_header(p.STREAM_STDERR, 5) == struct.pack(">BxxxI", 2, 5)

    @pytest.mark.parametrize(
        "stream",
        [p.STREAM_STDIN, p.STREAM_STDOUT, p.STREAM_STDERR, p.STREAM_CONTROL],
    )
    def test_every_stream_id_round_trips(self, stream):
        frame = p.pack_frame(stream, b"payload")
        assert p.unpack_header(frame[:8]) == (stream, 7)
        assert frame[8:] == b"payload"

    def test_a_zero_length_frame_is_legal(self):
        frame = p.pack_frame(p.STREAM_STDOUT, b"")
        assert len(frame) == 8
        assert p.unpack_header(frame) == (p.STREAM_STDOUT, 0)
        assert p.FrameDecoder().feed(frame) == [(p.STREAM_STDOUT, b"")]

    def test_a_64_kib_frame_round_trips(self):
        body = bytes(range(256)) * 256
        assert len(body) == 64 * 1024
        frames = p.FrameDecoder().feed(p.pack_frame(p.STREAM_STDOUT, body))
        assert frames == [(p.STREAM_STDOUT, body)]

    def test_binary_payloads_are_not_touched(self):
        body = bytes(range(256))
        assert p.FrameDecoder().feed(p.pack_frame(p.STREAM_STDIN, body)) == [
            (p.STREAM_STDIN, body)
        ]

    def test_an_unknown_stream_id_is_refused_in_both_directions(self):
        with pytest.raises(p.ProtocolError):
            p.pack_header(7, 1)
        with pytest.raises(p.ProtocolError):
            p.unpack_header(struct.pack(">BxxxI", 7, 1))

    def test_a_frame_over_the_cap_is_refused_before_it_is_allocated(self):
        """A garbled or hostile four-byte length must not ask either side for a
        4 GiB buffer."""
        with pytest.raises(p.ProtocolError) as e:
            p.unpack_header(struct.pack(">BxxxI", p.STREAM_STDOUT, 2**31))
        assert e.value.code == p.ERR_TOO_LARGE
        with pytest.raises(p.ProtocolError):
            p.pack_frame(p.STREAM_STDOUT, b"x" * (p.MAX_FRAME_BYTES + 1))

    def test_a_short_header_is_refused_rather_than_unpacked(self):
        with pytest.raises(p.ProtocolError):
            p.unpack_header(b"\x01\x00\x00")


class TestTheFrameDecoder:
    def test_a_split_header_is_reassembled(self):
        """A socket read hands back an arbitrary slice. Half a header is the
        case a hand-rolled reader gets wrong."""
        frame = p.pack_frame(p.STREAM_STDOUT, b"hello")
        decoder = p.FrameDecoder()
        assert decoder.feed(frame[:3]) == []
        assert decoder.feed(frame[3:6]) == []
        assert decoder.feed(frame[6:]) == [(p.STREAM_STDOUT, b"hello")]
        assert decoder.pending == 0

    def test_a_split_payload_is_reassembled(self):
        frame = p.pack_frame(p.STREAM_STDOUT, b"x" * 5000)
        decoder = p.FrameDecoder()
        assert decoder.feed(frame[:1000]) == []
        assert decoder.pending == 1000
        assert decoder.feed(frame[1000:]) == [(p.STREAM_STDOUT, b"x" * 5000)]

    def test_several_frames_in_one_read_all_come_back(self):
        blob = (
            p.pack_frame(p.STREAM_STDOUT, b"a")
            + p.pack_frame(p.STREAM_STDERR, b"bb")
            + p.encode_control({"exit_code": 0})
        )
        frames = p.FrameDecoder().feed(blob)
        assert [f[0] for f in frames] == [
            p.STREAM_STDOUT,
            p.STREAM_STDERR,
            p.STREAM_CONTROL,
        ]
        assert p.decode_control(frames[2][1]) == {"exit_code": 0}

    def test_one_byte_at_a_time_still_works(self):
        frame = p.pack_frame(p.STREAM_STDERR, b"drip")
        decoder = p.FrameDecoder()
        got = []
        for i in range(len(frame)):
            got += decoder.feed(frame[i : i + 1])
        assert got == [(p.STREAM_STDERR, b"drip")]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class TestRequestsRoundTrip:
    def test_an_argv_exec(self):
        line = p.encode_exec_request(argv=["npm", "ci"], cwd="/srv/repos/u/proj")
        assert p.decode_request(line) == {
            "action": "exec",
            "argv": ["npm", "ci"],
            "cwd": "/srv/repos/u/proj",
            "stdin": False,
            "timeout": 0,
        }

    def test_a_shell_exec_with_stdin_and_a_timeout(self):
        line = p.encode_exec_request(
            shell="npm ci 2>&1 | tail -5", cwd="/srv/repos/u/proj", stdin=True, timeout=300
        )
        assert p.decode_request(line) == {
            "action": "exec",
            "shell": "npm ci 2>&1 | tail -5",
            "cwd": "/srv/repos/u/proj",
            "stdin": True,
            "timeout": 300,
        }

    def test_an_exec_that_declines_to_name_a_directory(self):
        """`null` is a value, not an omission: it selects the server's own
        default. The pair with the test below is what keeps "declined to name
        one" distinguishable from "forgot the field"."""
        line = p.encode_exec_request(argv=["pip", "install", "httpx"], cwd=None)
        assert p.decode_request(line) == {
            "action": "exec",
            "argv": ["pip", "install", "httpx"],
            "cwd": None,
            "stdin": False,
            "timeout": 0,
        }

    def test_a_missing_cwd_key_is_a_bad_request_rather_than_the_default(self):
        """A client whose `getcwd` broke and dropped the field must not quietly
        land in the server's home directory."""
        with pytest.raises(p.ProtocolError) as caught:
            p.decode_request(
                p.encode_line({"action": "exec", "argv": ["pwd"], "stdin": False})
            )
        assert caught.value.code == p.ERR_BAD_REQUEST
        assert "cwd" in caught.value.message

    def test_a_write_file(self):
        line = p.encode_write_file_request(path="/home/dev/x.py", size=1886, mode=0o644)
        assert p.decode_request(line) == {
            "action": "write_file",
            "path": "/home/dev/x.py",
            "mode": 0o644,
            "size": 1886,
        }

    def test_a_read_file(self):
        line = p.encode_read_file_request(path="/home/dev/out.json")
        assert p.decode_request(line) == {
            "action": "read_file",
            "path": "/home/dev/out.json",
        }

    def test_stat_and_ping(self):
        assert p.decode_request(p.encode_stat_request()) == {"action": "stat"}
        assert p.decode_request(p.encode_ping_request()) == {"action": "ping"}

    def test_every_line_is_newline_terminated(self):
        for line in (
            p.encode_ping_request(),
            p.encode_stat_request(),
            p.encode_read_file_request(path="/home/dev/a"),
            p.encode_exec_request(argv=["true"], cwd="/srv/repos/u"),
        ):
            assert line.endswith(b"\n")
            assert line.count(b"\n") == 1

    def test_an_argument_containing_a_newline_survives(self):
        """JSON escapes it, so a value cannot forge the end of the request
        line — the framing bug the credential proxy's `-z` reading avoids in
        the other direction."""
        line = p.encode_exec_request(
            argv=["sh", "-c", 'echo "a\nb"'], cwd="/srv/repos/u"
        )
        assert line.count(b"\n") == 1
        assert p.decode_request(line)["argv"][2] == 'echo "a\nb"'


class TestTheRequestEncoderHasNoEnvField:
    """Design 4 deletes it, twice over: forwarding a filtered copy of the
    model's environment is not enforcement, since a hand-written client sends
    whatever it likes, and the executor's own cache variables point at sandbox
    paths that do not exist in the container. These two tests exist so the
    deletion cannot come back by accident."""

    def test_the_encoder_takes_no_env_parameter(self):
        assert "env" not in inspect.signature(p.encode_exec_request).parameters

    def test_a_fully_populated_request_carries_no_env_key(self):
        line = p.encode_exec_request(
            argv=["npm", "ci"], cwd="/srv/repos/u/proj", stdin=True, timeout=60
        )
        assert "env" not in json.loads(line)


class TestMalformedRequests:
    def test_an_unknown_action_has_its_own_code(self):
        with pytest.raises(p.ProtocolError) as e:
            p.decode_request(p.encode_line({"action": "rm_rf"}))
        assert e.value.code == p.ERR_UNKNOWN_ACTION

    def test_a_missing_action_is_a_bad_request(self):
        with pytest.raises(p.ProtocolError) as e:
            p.decode_request(p.encode_line({"argv": ["true"]}))
        assert e.value.code == p.ERR_BAD_REQUEST

    def test_malformed_json_is_a_bad_request(self):
        with pytest.raises(p.ProtocolError) as e:
            p.decode_request(b"{not json\n")
        assert e.value.code == p.ERR_BAD_REQUEST

    def test_a_json_array_is_not_a_request(self):
        with pytest.raises(p.ProtocolError):
            p.decode_request(b'["exec"]\n')

    def test_an_empty_line_is_a_bad_request(self):
        with pytest.raises(p.ProtocolError):
            p.decode_request(b"\n")

    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "exec", "cwd": "/srv/repos/u"},
            {"action": "exec", "argv": ["a"], "shell": "a", "cwd": "/srv/repos/u"},
            {"action": "exec", "argv": [], "cwd": "/srv/repos/u"},
            {"action": "exec", "argv": ["a", 3], "cwd": "/srv/repos/u"},
            {"action": "exec", "argv": ["a"]},
            {"action": "exec", "argv": ["a"], "cwd": "/x", "stdin": "yes"},
            {"action": "exec", "argv": ["a"], "cwd": "/x", "timeout": -1},
            {"action": "exec", "argv": ["a"], "cwd": "/x", "timeout": "300"},
            {"action": "exec", "argv": ["a"], "cwd": ""},
            {"action": "exec", "argv": ["a"], "cwd": 7},
            {"action": "exec", "argv": ["a"], "cwd": True},
            {"action": "read_file"},
            {"action": "write_file", "path": "/x", "size": -1},
            {"action": "write_file", "path": "/x", "size": 1, "mode": "0644"},
            {"action": "write_file", "path": "/x", "size": 1, "mode": 0o4755},
            {"action": "write_file", "path": "/x", "size": 1, "mode": 0o2755},
            {"action": "write_file", "path": "/x", "size": 1, "mode": -1},
        ],
    )
    def test_a_malformed_shape_is_refused(self, payload):
        with pytest.raises(p.ProtocolError):
            p.decode_request(p.encode_line(payload))

    def test_argv_and_shell_are_mutually_exclusive_in_the_encoder_too(self):
        with pytest.raises(p.ProtocolError):
            p.encode_exec_request(argv=["a"], shell="a", cwd="/srv/repos/u")


class TestTheCaps:
    def test_a_request_line_over_the_cap_is_refused_by_size_not_by_shape(self):
        line = p.encode_line(
            {
                "action": "exec",
                "argv": ["true"],
                "cwd": "/srv/repos/u",
                "pad": "p" * p.MAX_REQUEST_BYTES,
            }
        )
        with pytest.raises(p.ProtocolError) as e:
            p.decode_request(line)
        assert e.value.code == p.ERR_TOO_LARGE

    def test_a_write_file_body_over_the_cap_is_refused(self):
        with pytest.raises(p.ProtocolError) as e:
            p.encode_write_file_request(path="/home/dev/big", size=p.MAX_WRITE_FILE_BYTES + 1)
        assert e.value.code == p.ERR_TOO_LARGE

    def test_a_mode_is_permission_bits_and_nothing_else(self):
        """The server applies this mode with an explicit chmod that defeats the
        umask, so a setuid bit asked for here would arrive under the repos root
        exactly as asked for."""
        assert 0o777 == json.loads(
            p.encode_write_file_request(path="/home/dev/x", size=1, mode=0o777)
        )["mode"]
        with pytest.raises(p.ProtocolError):
            p.encode_write_file_request(path="/home/dev/x", size=1, mode=0o4755)

    def test_the_caps_are_the_ones_the_design_names(self):
        assert p.MAX_REQUEST_BYTES == 1024 * 1024
        assert p.MAX_WRITE_FILE_BYTES == 64 * 1024 * 1024
        assert p.MAX_READ_FILE_BYTES == 64 * 1024 * 1024
        assert p.CHUNK_BYTES <= p.MAX_FRAME_BYTES


# --------------------------------------------------------------------------- #
# Acknowledgements and control frames
# --------------------------------------------------------------------------- #


class TestAcknowledgements:
    def test_the_ok_ack_carries_the_protocol_version(self):
        assert p.decode_ack(p.encode_ack_ok()) == {
            "status": "ok",
            "protocol": p.PROTOCOL_VERSION,
        }

    def test_an_error_ack_carries_a_code_and_a_message(self):
        ack = p.decode_ack(p.encode_ack_error(p.ERR_PATH_REFUSED, "nope"))
        assert ack == {"status": "error", "code": p.ERR_PATH_REFUSED, "message": "nope"}

    def test_an_ack_without_a_status_is_refused(self):
        with pytest.raises(p.ProtocolError):
            p.decode_ack(p.encode_line({"protocol": 1}))

    def test_every_error_code_the_design_names_exists(self):
        assert p.ALL_ERROR_CODES == {
            "bad_request",
            "unknown_action",
            "path_refused",
            "no_such_cwd",
            "spawn_failed",
            "command_not_found",
            "too_large",
            "internal",
        }

    def test_every_action_the_design_names_exists(self):
        assert p.ALL_ACTIONS == {"exec", "write_file", "read_file", "stat", "ping"}


class TestTheProtocolVersion:
    def test_this_build_speaks_its_own_version(self):
        assert p.supported_protocol(p.PROTOCOL_VERSION)

    @pytest.mark.parametrize("value", [0, 2, 99, -1, None, "1", 1.0, True])
    def test_an_unknown_version_is_not_accepted(self, value):
        """The client exits 121 on one of these. `True` is in the list because
        `True == 1` in Python, and an ack carrying `"protocol": true` is a
        malformed one rather than protocol 1."""
        assert not p.supported_protocol(value)


class TestControlFrames:
    def test_a_control_object_round_trips_through_a_frame(self):
        body = {"exit_code": 137, "signal": "SIGKILL", "reason": "timeout"}
        stream, payload = p.FrameDecoder().feed(p.encode_control(body))[0]
        assert stream == p.STREAM_CONTROL
        assert p.decode_control(payload) == body

    def test_the_one_client_to_server_control_object(self):
        stream, payload = p.FrameDecoder().feed(p.encode_stdin_eof())[0]
        assert stream == p.STREAM_CONTROL
        assert p.is_stdin_eof(p.decode_control(payload))
        assert not p.is_stdin_eof({"exit_code": 0})

    def test_stdin_eof_must_be_true_rather_than_merely_present(self):
        assert not p.is_stdin_eof({"stdin_eof": False})
        assert not p.is_stdin_eof({"stdin_eof": "yes"})

    def test_a_terminal_frame_is_the_one_carrying_an_exit_code(self):
        """One rule for every action, `ping` and `stat` included, so a client
        does not need a table of which reply ends which conversation."""
        assert p.is_terminal({"exit_code": 0, "signal": None})
        assert not p.is_terminal({"pong": True, "protocol": 1})

    def test_a_malformed_control_frame_is_refused(self):
        with pytest.raises(p.ProtocolError):
            p.decode_control(b"{not json")
        with pytest.raises(p.ProtocolError):
            p.decode_control(b'"a string"')


class TestTheSigpipeNote:
    def test_an_unsignalled_141_gets_the_note(self):
        note = p.sigpipe_note(141, None)
        assert note and "SIGPIPE" in note

    def test_a_signalled_child_gets_no_note(self):
        """It already reports its signal; there is nothing to guess about."""
        assert p.sigpipe_note(141, "SIGPIPE") is None

    def test_no_other_status_gets_it(self):
        assert p.sigpipe_note(1, None) is None
        assert p.sigpipe_note(0, None) is None
        assert p.sigpipe_note(None, None) is None


# --------------------------------------------------------------------------- #
# The vendored copy
# --------------------------------------------------------------------------- #


class TestVendoredCopy:
    """The server imports the copy, the daemon imports the original, and the two
    ends of a wire format that disagree produce no error message at all."""

    def test_the_copy_exists_and_is_byte_identical(self):
        assert VENDORED.exists(), f"{VENDORED} missing — run scripts/sync-devbox-lib.sh"
        assert CANONICAL.read_bytes() == VENDORED.read_bytes(), (
            "src/istota/devbox_exec_protocol.py and "
            "docker/devbox/lib/istota_devbox_exec_protocol.py have drifted — "
            "run scripts/sync-devbox-lib.sh"
        )

    def test_the_copy_is_a_real_file(self):
        """A symlink passes a byte comparison and fails `docker build` with
        "COPY failed: … outside the build context"."""
        assert not VENDORED.is_symlink()

    def test_the_sync_script_lists_it(self):
        assert (
            "src/istota/devbox_exec_protocol.py:istota_devbox_exec_protocol.py"
            in SYNC_SCRIPT.read_text()
        )

    def test_the_module_imports_nothing_from_istota(self):
        """It is imported by a process in a container that has no such package,
        so an import added here breaks the server and nothing else."""
        source = CANONICAL.read_text()
        assert "\nfrom istota" not in source
        assert "\nimport istota" not in source
