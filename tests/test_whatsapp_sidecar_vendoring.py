"""Every file under ``docker/whatsapp-baileys/`` is pinned to something.

The shape ``tests/test_devbox_vendored_lib.py`` established one directory
over, and the reason is the same: Docker cannot ``COPY`` from outside its
build context, so an image's leaves live in the context as second copies, and
a guard that names its own subjects only covers the ones its author thought of.
The list here comes from ``iterdir``, so a file added later is red until
somebody classifies it.

**Nothing here can be a byte copy.** The other end of this protocol is
TypeScript, so there is no Python file to compare against — the situation
``.claude/rules/devbox.md`` already records for ``istota_devbox_client.py``,
which is a rewrite pinned behaviourally rather than by bytes.
``baileys_protocol.py``'s own docstring says so and names this stage as the
owner of the pin.

What the pin can and cannot reach is worth being exact about, because the
gap is where a reader will assume coverage that is not there. It reaches the
**wire constants**: the protocol version, the message-type vocabulary, the
line cap, and the failure-reason keys. Those are the values a disagreement
shows up in with no error message anywhere — a version mismatch is refused
loudly, but a message type spelled differently is a frame counted as
malformed and dropped, and a `reason` outside the daemon's table renders as
the generic sentence for ever.

It does **not** reach behaviour. Executing the sidecar needs Node and a
``node_modules`` tree, and a real Baileys connection needs a real WhatsApp
account and a phone to scan with, so neither is in the default suite and
neither can be. That is stated in ``docker/whatsapp-baileys/README.md`` and
in the module docstring of the program itself rather than left as an absence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from istota.transport.whatsapp import baileys_protocol as proto

REPO = Path(__file__).resolve().parents[1]
SIDECAR_DIR = REPO / "docker" / "whatsapp-baileys"
PROGRAM = SIDECAR_DIR / "index.js"

#: Every file in the directory, mapped to the class in this module that pins
#: it. The value is checked — an entry naming a class that does not exist is
#: red — so silencing the coverage guard with a one-line dict entry is not a
#: thing this map lets you do. That mattered in the devbox version of this
#: file, whose first draft carried a prose reason instead and admitted
#: membership was unenforced.
_PINS = {
    "index.js": "TestTheSidecarSpeaksTheSameProtocol",
    "package.json": "TestThePinnedLibrary",
    "README.md": "TestTheReadmeSaysWhatIsNotCovered",
}


def _sidecar_files() -> list[str]:
    """Every file, not every ``*.js``.

    The devbox guard learned this one level down: a Dockerfile can COPY a JSON
    manifest, a lockfile or a shell script out of here just as readily as a
    module, and a guard that only sees programs is the same blind spot one
    file type over.
    """
    return sorted(p.name for p in SIDECAR_DIR.iterdir() if p.is_file())


def _js_const(name: str) -> str:
    """One ``const NAME = <literal>;`` from the program, as source text.

    Parsed rather than executed. Running the file needs Node and its
    dependencies; reading it needs neither, and what is being pinned is a
    literal.
    """
    match = re.search(
        rf"^const {re.escape(name)} = (.+?);$", PROGRAM.read_text(), re.MULTILINE,
    )
    assert match is not None, f"{name} is not declared in index.js"
    return match.group(1).strip()


def _js_string_const(name: str) -> str:
    raw = _js_const(name)
    assert raw.startswith("'") and raw.endswith("'"), f"{name} is not a string literal"
    return raw[1:-1]


class TestTheDirectoryIsFullyAccountedFor:
    def test_every_pin_names_a_class_that_exists(self):
        for name, class_name in _PINS.items():
            assert class_name in globals(), (
                f"_PINS claims {name} is pinned by {class_name}, which is not "
                "defined in this module"
            )

    def test_every_file_is_pinned(self):
        unpinned = [name for name in _sidecar_files() if name not in _PINS]
        assert unpinned == [], (
            f"{unpinned} under docker/whatsapp-baileys/ is pinned by nothing. "
            "Add it to _PINS here with a test that holds it to whatever in "
            "src/ it has to agree with — there is no byte-copy option on this "
            "side of the protocol, because the other end is Python."
        )

    def test_no_file_is_a_symlink(self):
        """A symlink passes every content comparison and fails `docker build`
        with "COPY failed: … outside the build context"."""
        for name in _sidecar_files():
            assert not (SIDECAR_DIR / name).is_symlink(), name

    def test_no_dependency_tree_is_committed(self):
        """`npm ci` writes `node_modules` here, and committing it would put a
        few hundred megabytes and somebody else's code into a public repo."""
        assert not (SIDECAR_DIR / "node_modules").exists()


class TestTheSidecarSpeaksTheSameProtocol:
    """The wire constants, against `baileys_protocol.py`.

    A disagreement here is the failure mode with no error message, which is
    the argument `test_devbox_exec_protocol.py` makes for its byte comparison
    and the one available to a pin that cannot compare bytes.
    """

    def test_the_protocol_version_matches(self):
        """The one constant that fails *loudly* when it drifts — the bridge
        refuses a `hello` whose version it does not know — and therefore the
        one worth catching before a deployment does."""
        assert _js_const("PROTOCOL_VERSION") == str(proto.PROTOCOL_VERSION)

    def test_the_line_cap_matches(self):
        """Both ends cap, which is the rule the protocol module states: a cap
        only on the reader lets a writer build a frame it can never deliver."""
        assert _js_const("MAX_LINE_BYTES") == "256 * 1024"
        assert proto.MAX_LINE_BYTES == 256 * 1024

    @pytest.mark.parametrize(
        "js_name,py_name",
        [
            ("MSG_HELLO", "MSG_HELLO"),
            ("MSG_READY", "MSG_READY"),
            ("MSG_QR", "MSG_QR"),
            ("MSG_INBOUND", "MSG_INBOUND"),
            ("MSG_RECEIPT", "MSG_RECEIPT"),
            ("MSG_SEND_RESULT", "MSG_SEND_RESULT"),
            ("MSG_FATAL", "MSG_FATAL"),
            ("MSG_SEND", "MSG_SEND"),
            ("MSG_SHUTDOWN", "MSG_SHUTDOWN"),
        ],
    )
    def test_each_message_type_is_spelled_the_same(self, js_name, py_name):
        """A misspelled type is not refused: the bridge counts it malformed
        and drops the line, and the sidecar logs it as unexpected. Both sides
        keep running and nothing arrives."""
        assert _js_string_const(js_name) == getattr(proto, py_name)

    def test_the_message_type_vocabulary_is_complete_on_both_sides(self):
        """Membership rather than a count, so a type added to the protocol is
        red here until the sidecar knows it."""
        js_types = {
            _js_string_const(name)
            for name in re.findall(r"^const (MSG_\w+) = ", PROGRAM.read_text(),
                                   re.MULTILINE)
        }

        assert js_types == set(proto.UP_MESSAGES) | set(proto.DOWN_MESSAGES)

    def test_the_failure_reasons_are_the_daemons_own_keys(self):
        """The sidecar's own words never cross, so a `reason` is a key into
        the daemon's fixed table. One outside it renders as the generic
        sentence for ever, with nothing saying so."""
        match = re.search(
            r"const SEND_REASONS = new Set\(\[(.*?)\]\);",
            PROGRAM.read_text(), re.DOTALL,
        )
        assert match is not None
        js_reasons = set(re.findall(r"'([^']+)'", match.group(1)))

        assert js_reasons == set(proto._SEND_REASONS)

    def test_the_permanent_fatals_are_ones_the_bridge_latches(self):
        """A `fatal` reason the bridge does not recognise is only permanent if
        the sidecar also sets `permanent: true`; these are the two it sends,
        and both are in the bridge's own set, so the latch does not depend on
        that flag surviving a refactor."""
        from istota.transport.whatsapp.baileys_bridge import _PERMANENT_FATALS

        for name in ("FATAL_LOGGED_OUT", "FATAL_BAD_SESSION"):
            assert _js_string_const(name) in _PERMANENT_FATALS

    def test_it_reads_the_two_environment_variables_the_bridge_sets(self):
        """The bridge hands the socket and the session directory through the
        environment rather than argv, so neither shows up in `ps` and the argv
        stays the operator's. A rename on either side is a sidecar that exits
        2 with no log destination to say why."""
        source = PROGRAM.read_text()
        from istota.transport.whatsapp.baileys_bridge import (
            ENV_SESSION_DIR, ENV_SOCKET,
        )

        assert f"process.env.{ENV_SOCKET}" in source
        assert f"process.env.{ENV_SESSION_DIR}" in source

    def test_the_entry_point_is_the_one_the_bridge_resolves(self):
        from istota.transport.whatsapp.baileys_bridge import SIDECAR_ENTRY

        assert (SIDECAR_DIR / SIDECAR_ENTRY).is_file()

    def test_it_writes_to_no_standard_stream(self):
        """The daemon spawns it with both on /dev/null, so anything printed is
        lost — but the rule is stronger than that and is why the log goes into
        the session directory instead: a future caller that *did* inherit
        stdio would be handed JIDs and message bodies straight into the
        journal and the admin Logs pane.

        `process.exit` is exempt and is the one channel left when there is no
        session directory to log into.
        """
        source = PROGRAM.read_text()
        offenders = re.findall(r"(?:console\.\w+|process\.std(?:out|err)\.write)",
                               source)

        assert offenders == [], offenders


class TestThePinnedLibrary:
    def test_baileys_is_pinned_to_an_exact_version(self):
        """A range is what makes a sidecar that worked yesterday stop today:
        Baileys tracks a protocol WhatsApp changes without notice, so which
        version is installed has to be a fact about the commit."""
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())
        version = manifest["dependencies"]["@whiskeysockets/baileys"]

        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
            f"baileys is pinned as {version!r}; an exact version is required"
        )

    def test_it_declares_the_node_floor_the_library_needs(self):
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())

        assert manifest["engines"]["node"].startswith(">=18")

    def test_it_is_private_so_it_cannot_be_published(self):
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())

        assert manifest["private"] is True

    def test_the_entry_point_agrees_with_the_manifest(self):
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())
        from istota.transport.whatsapp.baileys_bridge import SIDECAR_ENTRY

        assert manifest["main"] == SIDECAR_ENTRY


class TestTheReadmeSaysWhatIsNotCovered:
    """A coverage gap that is written down is a decision; one that is not is
    a reader assuming the tier covers something it cannot."""

    def test_it_names_the_manual_step(self):
        text = (SIDECAR_DIR / "README.md").read_text()

        assert "istota whatsapp pair" in text
        assert "real WhatsApp account" in text
