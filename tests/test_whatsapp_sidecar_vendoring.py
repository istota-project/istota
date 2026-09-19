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
**wire constants** — the protocol version, the message-type vocabulary, the
line cap, the failure-reason keys — and the **payload field names** of every
frame the sidecar sends. Both are values a disagreement shows up in with no
error message anywhere: a version mismatch is refused loudly, but a message
type spelled differently is a frame counted as malformed and dropped, a
renamed field is `BaileysProtocolError("missing jid")` counted and dropped one
layer down, and a `reason` outside the daemon's table renders as the generic
sentence for ever.

The field-name half is driven rather than compared. Pulling the object literal
out of each `link.send(MSG_X, {...})` call gives the key set the sidecar
emits; feeding that set to the matching normalizer with filler values is what
says the daemon can read it. A list of expected names here would be a third
place for the protocol to be written down, which is the thing being avoided.

It does **not** reach behaviour. Executing the sidecar needs Node and a
``node_modules`` tree, and a real Baileys connection needs a real WhatsApp
account and a phone to scan with, so neither is in the default suite and
neither can be. That is stated in ``docker/whatsapp-baileys/README.md`` and
in the module docstring of the program itself rather than left as an absence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from istota import image_sniff
from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp import identity, media

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
    "package-lock.json": "TestTheLockfileInstallsThePinnedLibrary",
    "Dockerfile": "TestTheImageCopiesWhatTheProgramNeeds",
    ".dockerignore": "TestTheImageCopiesWhatTheProgramNeeds",
    "README.md": "TestTheReadmeSaysWhatIsNotCovered",
}


#: A directory that is generated rather than vendored, and is refused on its
#: own terms below rather than classified.
_GENERATED_DIRS = {"node_modules"}


def _sidecar_entries() -> list[str]:
    """Every entry, not every file, and not every ``*.js``.

    Two widenings of the same blind spot. The devbox guard learned the first
    one level down — a Dockerfile can COPY a JSON manifest, a lockfile or a
    shell script out of here as readily as a module — and this one adds the
    second: a `lib/`, a `proto/` or a `src/` is exactly the shape the devbox
    context already has, and a guard that lists only files would never see it.
    """
    return sorted(
        entry.name for entry in SIDECAR_DIR.iterdir()
        if entry.name not in _GENERATED_DIRS
    )


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

    def test_every_entry_is_pinned(self):
        unpinned = [name for name in _sidecar_entries() if name not in _PINS]
        assert unpinned == [], (
            f"{unpinned} under docker/whatsapp-baileys/ is pinned by nothing. "
            "Add it to _PINS here with a test that holds it to whatever in "
            "src/ it has to agree with — there is no byte-copy option on this "
            "side of the protocol, because the other end is Python."
        )

    def test_no_file_is_a_symlink(self):
        """A symlink passes every content comparison and fails `docker build`
        with "COPY failed: … outside the build context"."""
        for name in _sidecar_entries():
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

    def test_the_media_error_keys_are_the_daemons_own_keys(self):
        """`SEND_REASONS`' rule one frame over. A `media_error` outside the
        daemon's table renders as the generic sentence, so a user is told the
        image could not be fetched and nobody can say why — the diagnostic
        lost rather than the message."""
        match = re.search(
            r"const MEDIA_ERRORS = new Set\(\[(.*?)\]\);",
            PROGRAM.read_text(), re.DOTALL,
        )
        assert match is not None
        js_errors = set(re.findall(r"'([^']+)'", match.group(1)))

        assert js_errors == set(proto._MEDIA_ERRORS)

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

    def test_it_reads_the_media_directory_the_bridge_sets(self):
        """Where a staged image is written, handed over the same way the
        other two are. A rename on either side is a sidecar that exits 2."""
        from istota.transport.whatsapp.baileys_bridge import ENV_MEDIA_DIR

        assert f"process.env.{ENV_MEDIA_DIR}" in PROGRAM.read_text()

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


def _js_method(name: str) -> str:
    """The body of one method of the sidecar's `Session` class, as text."""
    source = PROGRAM.read_text()
    start = source.index(f"  {name}(")
    end = source.index("\n  }\n", start)
    return source[start:end]


def _js_body(declaration: str) -> str:
    """The body of one method, named by its **declaration** rather than its
    name.

    `_js_method` takes a bare name and finds the first match, which is two
    kinds of wrong here: it cannot see an `async` method at all, and `send`
    is declared on both `Link` and `Session` — so it silently returned the
    wrong one and an assertion about the send path passed or failed against a
    method that has nothing to do with it. Naming the declaration is
    unambiguous in both cases.
    """
    source = PROGRAM.read_text()
    start = source.index(f"  {declaration}")
    end = source.index("\n  }\n", start)
    return source[start:end]


def _js_function(name: str) -> str:
    """The body of one top-level function of the sidecar, as text."""
    source = PROGRAM.read_text()
    start = source.index(f"function {name}(")
    end = source.index("\n}\n", start)
    return source[start:end]


def _js_send_keys(message_const: str) -> set[str]:
    """The payload keys of one ``this.link.send(MSG_X, { ... })`` call.

    A brace walk rather than a regular expression: the object literals here
    hold nested calls and ternaries, and a non-greedy `{.*?}` stops at the
    first inner brace. Only top-level keys are collected, which is the level
    the daemon's normalizers read.

    **It reads the first send site and asserts there is only one.** Without
    that count the helper silently covers one of several — `MSG_READY` has two
    sites today — so a second one could carry any keys at all and every caller
    would stay green, which is the shape the `MSG_FATAL` assertion was found
    in and fixed out of. A message type that grows a second site should follow
    that assertion's pattern rather than loosening this.
    """
    source = PROGRAM.read_text()
    marker = f"this.link.send({message_const}, {{"
    sites = source.count(marker)
    assert sites == 1, f"{message_const} has {sites} send sites, not 1"
    start = source.index(marker) + len(marker)
    depth = 1
    index = start
    while depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    body = source[start:index - 1]

    keys = set()
    depth = 0
    for chunk in body.split(","):
        stripped = chunk.strip()
        if depth == 0 and stripped:
            # `name: value` and ES6 shorthand `name` both count — the sidecar
            # uses both, and a scan that saw only the first missed `jid`,
            # `text` and `group`, which is three of the fields that matter
            # most.
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(:|$)", stripped)
            if match:
                keys.add(match.group(1))
        depth += stripped.count("{") + stripped.count("[")
        depth -= stripped.count("}") + stripped.count("]")
    return keys


class TestTheSidecarsPayloadsAreReadable:
    """The field names, driven through the daemon's own normalizers.

    The envelope tests above pin the constants; this pins what is inside one.
    A renamed or dropped field is the quieter of the two failures — the frame
    decodes, the normalizer raises `BaileysProtocolError`, `_dispatch` counts
    it malformed and drops it, and both ends carry on with nothing arriving.

    Each case builds a payload from the keys the sidecar actually emits, fills
    them with values of the right shape, and requires the normalizer to accept
    it. The keys come from the program; the acceptance comes from the product.
    Neither side is restated here, which is what stops this becoming a third
    copy of the protocol.
    """

    @staticmethod
    def _filled(keys: set[str], values: dict) -> dict:
        missing = keys - set(values)
        assert not missing, (
            f"index.js sends {sorted(missing)}, which this case has no filler "
            "for — add one and check the normalizer reads it"
        )
        return {key: values[key] for key in keys}

    #: A text message's filler for every key the one `inbound` send site
    #: emits. One dict rather than four copies, because the four media keys
    #: ride the same send site as the text ones — `_js_send_keys` asserts
    #: there is exactly one — so every inbound case has to fill them whether
    #: or not it is about media.
    _INBOUND_TEXT = {
        "message_id": "BAE5F00D",
        "jid": "15551234567@s.whatsapp.net",
        "username": "Alice",
        "message_type": "text",
        "text": "check the backup",
        "callback_data": None,
        "reply_to_message_id": None,
        "group": False,
        "timestamp": 1757000000,
        "media_name": None,
        "media_mime": None,
        "media_bytes": 0,
        "media_error": None,
    }

    def test_an_inbound_payload_normalizes(self):
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(self._INBOUND_TEXT))

        event = proto.inbound_event(payload)

        assert event.from_user.jid == "15551234567@s.whatsapp.net"
        assert event.text == "check the backup"
        assert event.media is None

    def test_the_group_flag_is_read_from_the_key_the_sidecar_sends(self):
        """The one inbound field with a *behavioural* reader rather than a
        stored one: it types the message before any identity lookup, so a
        rename does not merely drop a field, it admits a group message."""
        keys = _js_send_keys("MSG_INBOUND")
        assert "group" in keys

        payload = self._filled(keys, dict(
            self._INBOUND_TEXT, username=None, text="hello", group=True,
        ))

        assert proto.inbound_event(payload).message_type == "group"

    def test_an_inbound_payload_carrying_an_image_normalizes(self):
        """The caption rides `text`, so every gate in `_dispatch_inbound`
        applies to it with no new code — and the media record is what the
        narrowed `unsupported_type` gate reads."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT,
            message_type="image",
            text="what is this?",
            media_name="0123456789abcdef0123456789abcdef.jpg",
            media_mime="image/jpeg",
            media_bytes=8192,
        ))

        event = proto.inbound_event(payload)

        assert event.message_type == "image"
        assert event.text == "what is this?"
        assert event.media is not None
        assert event.media.staged_path == "0123456789abcdef0123456789abcdef.jpg"
        assert event.media.mime_type == "image/jpeg"
        assert event.media.byte_count == 8192
        assert event.media.error is None
        # The pre-check has not run at decode; the runtime fills it.
        assert event.media.attached_for_user == ""

    def test_an_uncaptioned_image_still_carries_its_media(self):
        """`text` is null and the message is not empty — the whole reason
        `_dispatch_inbound`'s empty branch has to check media first."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT,
            message_type="image",
            text=None,
            media_name="0123456789abcdef0123456789abcdef.heic",
            media_mime="image/heic",
            media_bytes=4096,
        ))

        event = proto.inbound_event(payload)

        assert event.text is None
        assert event.media is not None

    def test_a_media_error_normalizes_to_a_reason_the_daemon_wrote(self):
        """The sidecar's key, the daemon's sentence — `_SEND_REASONS`' rule,
        and what lets the reply be "that image could not be fetched" rather
        than "that message type is not supported yet"."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT,
            message_type="image",
            text=None,
            media_error="over_the_cap",
        ))

        media = proto.inbound_event(payload).media

        assert media is not None
        assert media.error == proto._MEDIA_ERRORS["over_the_cap"]
        assert media.staged_path == ""

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "a/b", "/abs", "../escape", "x\x00y", "nul\x00", 7],
    )
    def test_a_name_that_is_not_one_component_is_dropped(self, name):
        """`media.is_staged_name`, applied to a value a sidecar chose. The
        message survives and loses its image, because a frame this side
        cannot place must not be answered by guessing where it goes."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT, message_type="image", media_name=name,
        ))

        assert proto.inbound_event(payload).media is None

    @pytest.mark.parametrize("absent", ["media_mime", "media_bytes"])
    def test_an_absent_advisory_field_does_not_cost_the_image(self, absent):
        """Both are carried for the log line and neither decides anything, so
        losing a photo over a missing label would put the strictness on the
        wrong field. The two arms used to disagree: an absent `media_mime`
        read as `""` while an absent `media_bytes` dropped the media."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT,
            message_type="image",
            media_name="0123456789abcdef0123456789abcdef.jpg",
            media_mime="image/jpeg",
            media_bytes=4096,
        ))
        del payload[absent]

        assert proto.inbound_event(payload).media is not None

    def test_a_group_message_carrying_media_drops_it(self):
        """A group message is refused before any identity lookup, so nothing
        will ever consume the file; the sweep takes it."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT,
            group=True,
            message_type="image",
            media_name="0123456789abcdef0123456789abcdef.jpg",
        ))

        assert proto.inbound_event(payload).media is None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("media_mime", 7),
            ("media_mime", "x" * (proto.MAX_MEDIA_MIME_CHARS + 1)),
            # A log line is where this value goes, so a newline or an escape
            # off the wire forges one there.
            ("media_mime", "image/jpeg\nWARNING forged"),
            ("media_mime", "image/jpeg\x1b[31m"),
            ("media_bytes", "8192"),
            ("media_bytes", True),
            ("media_bytes", -1),
            ("media_error", 7),
            ("media_error", ""),
        ],
    )
    def test_a_malformed_media_field_costs_the_media_and_not_the_message(
        self, field, value
    ):
        """The batch rule one layer up, restated at the decoder: a media
        failure costs the media, never the message. Raising here would drop a
        caption somebody typed over a field nothing authoritative reads."""
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, dict(
            self._INBOUND_TEXT,
            message_type="image",
            media_name="0123456789abcdef0123456789abcdef.jpg",
            **{field: value},
        ))

        event = proto.inbound_event(payload)

        assert event.media is None
        assert event.text == "check the backup"

    def test_a_receipt_payload_normalizes(self):
        keys = _js_send_keys("MSG_RECEIPT")
        payload = self._filled(keys, {
            "message_id": "BAE5F00D",
            "status": "delivered",
            "timestamp": 1757000000,
            "error_code": None,
        })

        event = proto.delivery_event(payload)

        assert event is not None
        assert event.status == "delivered"

    def test_every_receipt_status_the_sidecar_can_send_is_one_the_daemon_maps(self):
        """The map the daemon drops an unknown value from, against the values
        the sidecar can produce. A status this side invents is a receipt that
        vanishes."""
        source = PROGRAM.read_text()
        by_number = re.search(
            r"const RECEIPT_BY_NUMBER = \{(.*?)\};", source, re.DOTALL,
        )
        by_name = re.search(r"const RECEIPT_BY_NAME = \[(.*?)\];", source, re.DOTALL)
        assert by_number and by_name
        produced = set(re.findall(r"'([^']+)'", by_number.group(1)))
        produced |= set(re.findall(r"'([^']+)'", by_name.group(1)))
        produced.add("failed")

        assert produced <= set(proto._STATUS_MAP)

    def test_the_error_status_is_mapped_rather_than_dropped(self):
        """`proto.WebMessageInfo.Status.ERROR` is **0**, so it fell through a
        `byNumber[status] || null` and then through the caller's falsy guard —
        a handset-level failure the ledger never heard about, which is the
        class the parked-status table exists for."""
        assert "0: 'failed'" in PROGRAM.read_text().replace('"', "'")

    def test_a_send_result_payload_normalizes_both_ways(self):
        """`answer` spreads `{request_id}` over its caller's fields, so the
        two shapes are its call sites rather than one literal — and both have
        to satisfy `send_outcome`, whose `definite` bit is the ledger's
        `failed`-against-`unknown` decision."""
        source = PROGRAM.read_text()
        assert "Object.assign({ request_id: requestId }, fields)" in source
        shapes = re.findall(r"this\.answer\(requestId, \{ ([^}]*) \}\)", source)
        assert shapes, "no answer() call sites found"

        for shape in shapes:
            keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*):", shape))
            fillers = {
                "ok": "ok: true" in shape,
                "message_id": "BAE5F00D",
                "reason": "not_connected",
                "definite": True,
            }
            missing = keys - set(fillers)
            assert not missing, (
                f"answer() sends {sorted(missing)}, which this case has no "
                "filler for — add one and check `send_outcome` reads it"
            )
            payload = {"request_id": "a1"}
            payload.update({key: fillers[key] for key in keys})

            outcome = proto.send_outcome(payload)

            # Both shapes have to carry which one they are. A success with no
            # id and a failure read as a success are the two ways this frame
            # settles the wrong ledger row.
            assert (outcome.message_id if fillers["ok"] else None) == (
                "BAE5F00D" if fillers["ok"] else None
            )

        ok = proto.send_outcome({"request_id": "a1", "ok": True,
                                 "message_id": "BAE5F00D"})
        bad = proto.send_outcome({"request_id": "a1", "ok": False,
                                  "reason": "not_connected", "definite": True})

        assert ok.message_id == "BAE5F00D"
        assert bad.definite is True
        assert bad.safe_reason == proto._SEND_REASONS["not_connected"]

    def test_the_send_handler_reads_every_field_the_daemon_sends(self):
        """`send_payload` is the daemon's half. A field it emits and the
        sidecar never reads is a silently ignored instruction — `kind` is the
        one that matters, since ignoring it would send a service message for a
        row the ledger records as a template."""
        from istota.transport.whatsapp._types import WhatsAppSendRequest

        emitted = set(proto.send_payload("a1", WhatsAppSendRequest(
            to="15551234567@s.whatsapp.net", text="hi", kind="service",
        )))
        source = PROGRAM.read_text()

        unread = {
            key for key in emitted
            if f"payload.{key}" not in source and f"CONFIG['{key}']" not in source
        }
        # Two are deliberately unread and both are recorded rather than
        # filtered out of the emitter. `buttons`: this adapter has no
        # interactive object, the caps say so, and the answer travels in the
        # body. `reply_to_message_id`: a quoted reply needs the whole original
        # message, which the sidecar does not keep, and the synthetic stub it
        # used to pass could be refused — which would settle `unknown` on a
        # message that never left, for a thread marker.
        assert unread == {"buttons", "reply_to_message_id"}, unread

    def test_a_qr_payload_carries_the_key_the_bridge_reads(self):
        assert _js_send_keys("MSG_QR") == {"qr"}

    def test_a_fatal_payload_carries_the_fields_the_bridge_branches_on(self):
        """`reason` and `permanent` on every frame, and `run_unrecorded` on the
        ones that report a backoff running on a guess (ISSUE-501).

        **The match is no longer line-oriented, and the count is asserted.**
        The previous version required `{ ... }` on a single line, so a frame
        wrapped across lines was invisible to it — and it kept passing while
        covering two of the four frames the program sends, which is this
        repository's recurring "a probe whose success is indistinguishable
        from a no-op". Comparing against the number of `send(MSG_FATAL` calls
        is what makes a frame the pattern cannot read a failure here rather
        than a silent omission.
        """
        source = PROGRAM.read_text()
        sends = source.count("link.send(MSG_FATAL")
        fatals = re.findall(r"MSG_FATAL, \{(.*?)\}\)", source, re.S)
        assert sends and len(fatals) == sends, (len(fatals), sends)
        for body in fatals:
            keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*):", body))
            assert keys in (
                {"reason", "permanent"},
                {"reason", "permanent", "run_unrecorded"},
            ), keys

    def test_the_hello_frame_carries_the_version_field_the_bridge_reads(self):
        source = PROGRAM.read_text()
        assert "MSG_HELLO, { protocol_version: PROTOCOL_VERSION }" in source
        # `hello_version` is what refuses a mismatch, and it reads that key.
        assert proto.hello_version({"protocol_version": 1}) == 1


class TestTheSidecarsPureFunctions:
    """The one part of the program the default suite can **execute**.

    `loadBaileys()` is lazy, so `require('./index.js')` succeeds with no
    `node_modules` in the tree — which is what makes running its exported pure
    functions possible here at all. The alternative was a source assertion,
    and a source assertion about a lookup table is a second copy of the table.

    Skipped rather than failed without `node`: this is a Python suite, and a
    developer without a Node runtime should not see a red test about a program
    they cannot run. That is the same trade `requires_dac` takes.
    """

    @staticmethod
    def _call(expression: str) -> str:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        script = (
            f"const m = require({json.dumps(str(PROGRAM))});"
            f"process.stdout.write(JSON.stringify({expression}));"
        )
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    @pytest.mark.parametrize(
        "status,expected",
        [
            # `proto.WebMessageInfo.Status`: ERROR, PENDING, SERVER_ACK,
            # DELIVERY_ACK, READ, PLAYED.
            (0, "failed"),
            (1, None),
            (2, "sent"),
            (3, "delivered"),
            (4, "read"),
            (5, "read"),
            (99, None),
        ],
    )
    def test_each_enum_member_maps_the_way_the_ledger_needs(self, status, expected):
        """**0 is ERROR**, and dropping it is a delivery that failed and was
        never reported — the row stays `accepted` with no alert behind it.

        **1 is PENDING**, which is before the server acknowledged anything, so
        it maps to nothing: calling it `sent` advances the monotonic status
        ladder ahead of the fact.
        """
        assert self._call(f"m.receiptStatus({status})") == expected

    @pytest.mark.parametrize(
        "name,expected",
        [("error", "failed"), ("delivered", "delivered"), ("played", None)],
    )
    def test_the_name_form_agrees_with_the_numeric_one(self, name, expected):
        assert self._call(f"m.receiptStatus({json.dumps(name)})") == expected

    def test_every_status_it_can_produce_is_one_the_daemon_maps(self):
        """Driven rather than read: a status this side invents is a receipt
        the daemon drops, silently."""
        produced = {
            self._call(f"m.receiptStatus({value})")
            for value in list(range(-1, 8))
        }
        produced |= {
            self._call(f"m.receiptStatus({json.dumps(name)})")
            for name in ("sent", "delivered", "read", "failed", "error", "nonsense")
        }

        assert (produced - {None}) <= set(proto._STATUS_MAP)

    def test_every_failure_reason_it_can_produce_is_one_the_daemon_knows(self):
        """`sendFailureReason` against the daemon's fixed table. A key outside
        it renders as the generic sentence for ever, which is the diagnostic
        being lost rather than the message."""
        cases = [
            "{output: {statusCode: 401}}",
            "{output: {statusCode: 403}}",
            "{output: {statusCode: 408}}",
            "{output: {statusCode: 500}}",
            "{message: 'that number is not on whatsapp'}",
            "{}",
            "null",
        ]
        produced = {self._call(f"m.sendFailureReason({case})") for case in cases}

        assert produced <= set(proto._SEND_REASONS)

    def test_a_frame_it_builds_is_one_the_daemon_decodes(self):
        """`encode` on one side, `proto.decode` on the other, over a value
        that needs escaping. `JSON.stringify` escaping an embedded newline is
        what stops a payload forging a frame boundary, and this is the only
        place that claim is executed."""
        line = self._call(
            "m.encode('inbound', {text: 'one\\ntwo', jid: 'x@s.whatsapp.net'})"
            ".toString('utf8')"
        )

        assert line.count("\n") == 1
        assert proto.decode(line) == {
            "type": "inbound", "text": "one\ntwo", "jid": "x@s.whatsapp.net",
        }

    def test_it_refuses_a_frame_past_the_cap_rather_than_writing_it(self):
        """Both ends cap. A cap only on the reader lets a writer build a line
        it can never deliver."""
        threw = self._call(
            "(() => { try { m.encode('inbound', {text: 'x'.repeat(300000)}); "
            "return false; } catch (e) { return true; } })()"
        )

        assert threw is True


class TestTheSidecarsInboundMedia:
    """The image half, executed rather than read wherever it can be.

    Everything that decides what reaches the daemon is a pure function and is
    exported for that reason: which node counts as media, what the staged
    file is called, what suffix it wears, and — the one that matters most —
    the per-file cap, which is the *fetcher's* bound because the daemon only
    ever sees a file that already exists. `downloadMediaMessage` itself needs
    Baileys and a live account, so what is driven here is the collector the
    download feeds, and `TestTheSidecarsControlFlow` carries the source
    assertion that the download actually feeds it.
    """

    @staticmethod
    def _node() -> str:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        return node

    @classmethod
    def _run(cls, script: str, media_dir=None, timeout: int = 30):
        """Run a script against the program and parse what it wrote.

        `env=` replaces the whole environment, so the copy is not optional —
        without `PATH` the child cannot resolve its own interpreter's
        neighbours. The umask is forced wide for `TestTheLoggedOutBackoff`'s
        reason: `main()` does not run under `require`, so `applyPrivateUmask`
        has not, and a mode assertion in a child that inherited this suite's
        own 077 would be asserting the ambient umask rather than the program.
        """
        env = dict(os.environ)
        if media_dir is not None:
            env["ISTOTA_BAILEYS_MEDIA_DIR"] = str(media_dir)
        result = subprocess.run(
            [cls._node(), "-e", f"process.umask(0o022);{script}"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    @classmethod
    def _call(cls, expression: str, media_dir=None):
        return cls._run(
            f"const m = require({json.dumps(str(PROGRAM))});"
            f"process.stdout.write(JSON.stringify({expression}));",
            media_dir,
        )

    # --- what counts as media ---------------------------------------------

    @pytest.mark.parametrize(
        "content,expected",
        [
            ({"imageMessage": {"mimetype": "image/jpeg"}}, True),
            ({"imageMessage": {"mimetype": "image/jpeg"}, "x": 1}, True),
            ({"conversation": "hello"}, False),
            # Every one of these is a Non-goal, and each keeps the
            # `unsupported_type` reply it has now. A sticker is WebP and would
            # pass the sniff, which is exactly why it is excluded by message
            # type up here rather than by the sniff down there.
            ({"videoMessage": {"mimetype": "video/mp4"}}, False),
            ({"documentMessage": {"mimetype": "image/jpeg"}}, False),
            ({"stickerMessage": {"mimetype": "image/webp"}}, False),
            ({"audioMessage": {"mimetype": "audio/ogg"}}, False),
            ({}, False),
        ],
    )
    def test_only_an_image_message_is_media(self, content, expected):
        found = self._call(
            f"Boolean(m.mediaPart({{message: {json.dumps(content)}}}))"
        )

        assert found is expected

    def test_a_message_that_is_not_one_yields_nothing(self):
        for expression in ("null", "{}", "{message: null}", "{message: 7}"):
            assert self._call(f"m.mediaPart({expression})") is None

    @pytest.mark.parametrize(
        "content,expected",
        [
            ({"imageMessage": {"caption": "what is this?"}}, "what is this?"),
            ({"imageMessage": {}}, None),
            ({"imageMessage": {"caption": 7}}, None),
            ({"conversation": "plain"}, "plain"),
            # Deliberately not read: those types keep the unsupported reply,
            # and a caption without the bytes is a message answered about an
            # image nobody can see.
            ({"videoMessage": {"caption": "clip"}}, None),
            ({"documentMessage": {"caption": "doc"}}, None),
        ],
    )
    def test_the_caption_is_read_for_an_image_and_for_nothing_else(
        self, content, expected
    ):
        assert self._call(
            f"m.messageText({{message: {json.dumps(content)}}})"
        ) == expected

    # --- the per-file cap --------------------------------------------------

    def test_the_cap_is_the_one_the_daemon_enforces_at_the_funnel(self):
        """Two enforcement points for one number: the sidecar aborts past it
        and `stage_to_attachment` refuses a staged file above it. A drift
        makes the second refuse what the first accepted, which is an image
        fetched, written and then silently discarded."""
        assert self._call("m.MAX_MEDIA_BYTES") == media.MAX_MEDIA_BYTES

    def test_a_download_inside_the_cap_is_collected(self):
        assert self._call(
            "(() => { const c = m.newMediaCollector();"
            " const kept = m.collectMediaChunk(c, Buffer.alloc(1024));"
            " return [kept, c.received, c.overCap]; })()"
        ) == [True, 1024, False]

    def test_a_download_past_the_cap_is_refused_and_what_it_held_dropped(self):
        """**The control for the whole fetcher-owns-the-cap rule.** Removing
        the check inside `collectMediaChunk` turns this red.

        One buffer fed repeatedly rather than a fresh one each time: the
        collector holds references, so the child's memory stays at a megabyte
        whatever the cap is.

        `overCap` and an emptied `chunks` are asserted together because
        either alone is the defect wearing a label — a collector that flags
        and keeps is a 16 MiB buffer the caller may still concatenate.
        """
        chunk_bytes = 1024 * 1024
        chunks = media.MAX_MEDIA_BYTES // chunk_bytes + 1

        assert self._call(
            f"(() => {{ const c = m.newMediaCollector();"
            f" const buf = Buffer.alloc({chunk_bytes});"
            f" let kept = true;"
            f" for (let i = 0; i < {chunks}; i++)"
            f"   kept = m.collectMediaChunk(c, buf) && kept;"
            f" return [kept, c.overCap, c.chunks.length]; }})()"
        ) == [False, True, 0]

    def test_the_boundary_byte_is_accepted_and_the_one_past_it_is_not(self):
        """A cap spelled `>=` refuses a file of exactly the permitted size,
        which is a silent refusal nobody would look for."""
        at_cap, past_cap = self._call(
            f"(() => {{ const a = m.newMediaCollector();"
            f" const b = m.newMediaCollector();"
            f" const exact = Buffer.alloc({media.MAX_MEDIA_BYTES});"
            f" const over = Buffer.alloc({media.MAX_MEDIA_BYTES + 1});"
            f" return [m.collectMediaChunk(a, exact),"
            f"         m.collectMediaChunk(b, over)]; }})()"
        )

        assert (at_cap, past_cap) == (True, False)

    # --- naming ------------------------------------------------------------

    def test_a_name_it_mints_is_one_the_daemon_will_join(self):
        """The two sides of the component rule, driven across the language
        boundary. The sidecar cannot compute `media.staged_name`'s
        fingerprint — the salt is the daemon's — so the validator is
        deliberately wider than that format, and the thing that has to hold
        is that what this mints passes it."""
        for ext in ("jpg", "png", "heic", "bin"):
            name = self._call(f"m.stagedMediaName({json.dumps(ext)})")

            assert media.is_staged_name(name), name
            assert name.endswith(f".{ext}")

    def test_two_names_for_one_message_do_not_collide(self):
        names = {self._call("m.stagedMediaName('jpg')") for _ in range(3)}

        assert len(names) == 3

    @pytest.mark.parametrize(
        "declared,expected",
        [
            ("image/jpeg", "jpg"),
            ("image/png", "png"),
            ("image/heic", "heic"),
            ("image/JPEG", "jpg"),
            ("image/jpeg; codecs=x", "jpg"),
            ("application/pdf", "bin"),
            ("../../etc/passwd", "bin"),
            ("", "bin"),
            (None, "bin"),
            (7, "bin"),
        ],
    )
    def test_the_staged_suffix_is_bounded_whatever_was_declared(
        self, declared, expected
    ):
        """The declared mimetype is attacker-influenced and the suffix is
        **advisory** — `stage_to_attachment` re-derives the inbox copy's
        suffix from its own sniff. What it still must not be is a path."""
        assert self._call(
            f"m.mediaExtension({json.dumps(declared)})"
        ) == expected

    def test_the_advisory_suffixes_are_the_ones_the_daemon_would_choose(self):
        """**Not** a claim that the sidecar's answer is trusted: nothing
        downstream reads it. What agreeing buys is that the staged stem in
        `sidecar.log` and the inbox copy's suffix match in the ordinary case,
        so the two logs can be read side by side.

        Equality rather than a subset over values, which is what this asserted
        first and which passes on an emptied table, on a deleted key, and on
        `image/jpeg` mapped to `png` — the claim is about the *mapping*, so a
        value-set check could not carry it.
        """
        assert self._call("m.MEDIA_EXTENSIONS") == image_sniff.EXTENSION_BY_MEDIA_TYPE

    # --- the staged write --------------------------------------------------

    def test_a_staged_file_is_private_to_the_account_that_wrote_it(self, tmp_path):
        """0600 under a 0700 directory the daemon made. The child's umask is
        forced to 0022 above, so this is the program's own mode argument and
        not the ambient one — the control below says the wide mode really is
        reachable in that child."""
        written = self._call(
            "(() => { const n = m.stagedMediaName('jpg');"
            " m.writeStaged(n, Buffer.from('hello'));"
            " return n; })()",
            media_dir=tmp_path,
        )

        assert (tmp_path / written).read_bytes() == b"hello"
        assert oct((tmp_path / written).stat().st_mode & 0o777) == "0o600"

    def test_the_control_says_the_wide_mode_is_reachable(self, tmp_path):
        """Without this the assertion above passes in a child whose umask
        already forbids the wide mode, which is a test of the harness."""
        self._run(
            f"require('fs').writeFileSync({json.dumps(str(tmp_path / 'wide'))},"
            " 'x');process.stdout.write('null');",
        )

        assert oct((tmp_path / "wide").stat().st_mode & 0o777) == "0o644"

    def test_a_name_already_standing_is_refused_rather_than_written_through(
        self, tmp_path
    ):
        """`O_EXCL`, which also refuses a symlink planted at the name: with
        `O_CREAT | O_EXCL` the kernel fails on an existing symlink rather
        than following it."""
        (tmp_path / "taken.jpg").write_text("mine")
        threw = self._call(
            "(() => { try { m.writeStaged('taken.jpg', Buffer.from('x'));"
            " return false; } catch (e) { return true; } })()",
            media_dir=tmp_path,
        )

        assert threw is True
        assert (tmp_path / "taken.jpg").read_text() == "mine"

    def test_a_symlink_planted_at_the_name_is_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.write_text("untouched")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "bait.jpg").symlink_to(outside)

        threw = self._call(
            "(() => { try { m.writeStaged('bait.jpg', Buffer.from('x'));"
            " return false; } catch (e) { return true; } })()",
            media_dir=staging,
        )

        assert threw is True
        assert outside.read_text() == "untouched"

    @pytest.mark.parametrize("name", ["../escape.jpg", "a/b.jpg", "", "."])
    def test_a_name_that_is_not_one_component_is_refused_at_the_write(
        self, tmp_path, name
    ):
        """The daemon validates the name it is *sent*; this is the other end
        of the same rule, so a bug here cannot put bytes outside the staging
        directory even before the frame is built."""
        threw = self._call(
            f"(() => {{ try {{ m.writeStaged({json.dumps(name)},"
            f" Buffer.from('x')); return false; }}"
            f" catch (e) {{ return true; }} }})()",
            media_dir=tmp_path,
        )

        assert threw is True


class TestTheSidecarsControlFlow:
    """Properties the static pin reaches only as source shape, and says so.

    Each of these is a guard whose absence is silent — a receipt for somebody
    else's message, a status forwarded for a chat this surface does not model,
    a `ready` that is never re-announced, a logged-out process that never
    exits, two sessions started at once. Executing them needs a live Baileys
    connection, which needs a real WhatsApp account, so what is available is
    an assertion that the guard is still written. That is a weaker claim than
    the class above and is separated from it for that reason: it catches a
    deletion and not a subtle change.
    """

    def test_receipts_are_filtered_to_our_own_sends(self):
        """The inverse of `onMessages`' filter. `messages.update` fires in
        both directions, and a receipt for an inbound message matches no
        ledger row — so the daemon parks it against whatever send is in flight
        and prunes it later as foreign traffic."""
        body = _js_method("onReceipts")

        assert "!item.key.fromMe" in body

    def test_inbound_is_filtered_to_the_chats_this_surface_models(self):
        """`status@broadcast` and `@newsletter` arrive like any other message
        and on an active account never stop, each costing a queue slot, a
        thread and a write transaction that then resolves no sender.

        The filter is `chatAddress` rather than `isForwardableJid` directly,
        because a LID chat is *translated* rather than passed through — but
        the discard is still the one gate, so it is the falsy return that is
        pinned here and `TestTheChatAddressUnderLid` that drives what it
        answers.
        """
        body = _js_body("async handleMessages(event)")

        assert "chatAddress(message.key)" in body
        assert "if (!jid)" in body
        # Ordering, not merely presence: the withhold has to sit ahead of the
        # frame, because what it is protecting is the daemon's dedup claim.
        assert (body.index("hasReadableContent(message)")
                < body.index("this.link.send(MSG_INBOUND"))
        assert "isForwardableJid" in _js_function("chatAddress")
        assert "@s.whatsapp.net" in _js_const("USER_JID_DOMAIN")
        assert "@lid" in _js_const("LID_JID_DOMAIN")

    def test_inbound_batches_are_handled_one_at_a_time(self):
        """Fetching an image is an `await`, so handling messages
        concurrently reorders a conversation at the source — and the daemon's
        inbound worker is serial precisely because order within a
        conversation is meaning.

        The `catch` is the other half and is not tidiness: without it one
        failed batch leaves a rejected promise as the chain's tail and every
        later message is dropped for the life of the process.
        """
        body = _js_method("onMessages")

        assert "this.inbound" in body
        assert ".then(() => this.handleMessages(event))" in body
        assert ".catch(" in body

    def test_the_download_is_bounded_by_the_collector_it_feeds(self):
        """The pure-function class drives the cap; this is what says the
        download path is what drives it in the program. A collector tested in
        isolation while the real loop concatenates a stream is the
        "success indistinguishable from a no-op" shape."""
        body = _js_body("async downloadMedia(message)")

        assert "newMediaCollector()" in body
        assert "collectMediaChunk(collector, chunk)" in body
        # Streamed rather than buffered, which is what makes the cap an abort
        # instead of a measurement taken after the bytes are already here.
        assert "'stream'" in body

    def test_nothing_is_staged_once_the_cap_is_passed(self):
        """Ordering: `writeStaged` must sit behind the not-over-cap branch,
        or the cap reports a refusal about a file that is on disk."""
        body = _js_body("async downloadMedia(message)")
        assert (body.index("overCap") < body.index("writeStaged("))

    def test_the_media_directory_is_required_at_startup(self):
        """Beside the socket and the session directory. A sidecar with
        nowhere to stage would type an image `image` and send a frame naming
        no file, which the daemon reads as media it cannot place — a
        deployment fault arriving as a per-message one.

        **Driven, because the obvious source assertion cannot fail.**
        `process.exit(2)` was already in `main()` before this change, for the
        socket and session-directory refusal, so `"process.exit(2)" in body`
        is pre-satisfied and stays green for a `main()` that reads the
        variable and carries on — the exact defect this is about.
        """
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        env = dict(os.environ)
        env["ISTOTA_BAILEYS_SOCKET"] = "/nonexistent/sock"
        env["ISTOTA_BAILEYS_SESSION_DIR"] = "/nonexistent/session"
        env.pop("ISTOTA_BAILEYS_MEDIA_DIR", None)

        # Short, because the expected behaviour is an immediate exit. A
        # program that does *not* refuse stays up retrying its socket for
        # ever, so the bound is what turns that into a red rather than a
        # hang — and a long one spends itself on every run of the failing
        # case for no extra confidence.
        try:
            result = subprocess.run(
                [node, str(PROGRAM)], capture_output=True, text=True,
                timeout=10, env=env,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "the sidecar started without ISTOTA_BAILEYS_MEDIA_DIR; it "
                "would type every photo `image` and name no file"
            )

        # The socket and session-directory variables are both set, so a 2
        # here is about the third one. With all three absent every version of
        # this program exits 2 and the case would say nothing at all.
        assert result.returncode == 2
        # Rule 1 holds even on the refusal path: the reason goes to the log
        # inside the session directory, never to stdio.
        assert result.stdout == "" and result.stderr == ""

    def test_the_socket_is_given_a_way_to_answer_a_retry(self):
        """The cache is inert unless Baileys is handed it. `getMessage`
        defaults to `async () => undefined`, so an unwired cache is exactly
        the defect with tests passing over it."""
        body = _js_body("async open_()")

        assert "getMessage:" in body
        assert "recallSent(" in body

    def test_the_send_path_remembers_what_it_sent(self):
        """And it caches the *generated* content rather than the `{ text }`
        handed in, because the generated content is what `relayMessage`
        re-encrypts on a retry."""
        body = _js_body("async send(payload)")

        assert "rememberSent(id, sent && sent.message)" in body

    def test_the_signal_keys_are_read_through_a_cache(self):
        """`useMultiFileAuthState` reads each key back off the disk, so a key
        written and immediately re-read can miss — and a session that reads as
        absent is re-established with a PreKey handshake it did not need,
        which is one of the ways the far side ends up unable to decrypt.

        Pinned as the absence of the raw form as well as the presence of the
        wrapper, since `auth: state` is what it silently reverts to."""
        body = _js_body("async open_()")

        assert "makeCacheableSignalKeyStore(state.keys" in body
        assert "auth: state," not in body

    def test_ready_is_re_announced_when_the_daemon_link_returns(self):
        """The daemon clears `ready` on every link drop and only a `ready`
        frame sets it back, while a WhatsApp session that never closed emits
        no second `open` — so without this the bridge reports `connected` and
        not `ready` for ever and pairing can never finish."""
        source = PROGRAM.read_text()

        assert "this.onReady();" in source
        assert "link.onReady = () => session.announceReady();" in source
        assert "if (this.open) this.link.send(MSG_READY, {});" in source

    def test_the_open_flag_is_cleared_only_by_a_real_close(self):
        """`connection.update` is a **partial** — it fires with no
        `connection` key at all for a QR rotation and for
        `receivedPendingNotifications` after the session opens. Clearing the
        flag above the `!== 'close'` guard marks a live session closed, so the
        next link reconnect announces nothing and the bridge is back in the
        state the flag exists to prevent.

        Ordering rather than presence, which is the one thing about this fix a
        source assertion can still say.
        """
        body = _js_method("onConnection")
        guard = body.index("if (connection !== 'close') return;")
        clear = body.index("this.open = false;")

        assert clear > guard

    def test_a_logged_out_session_exits(self):
        """Staying alive leaves a process holding the session directory with
        a dead socket, and after a re-pair it never re-runs `start()`. The
        bridge's supervisor docstring assumes the exit on the external-unit
        shape: systemd restarts it and its `ready` clears the latch.

        Followed through the call rather than scanned for within a window of
        the branch: the exit moved behind `scheduleLogoutExit` when the
        backoff went in, and a character count from `FATAL_LOGGED_OUT` is a
        bound on how much prose may sit between the two — which is a thing
        nobody editing this file would think to check.
        """
        branch = _js_method("onConnection")
        branch = branch[branch.index("reason: FATAL_LOGGED_OUT"):]

        assert "scheduleLogoutExit(" in branch
        assert "onExit();" in _js_function("scheduleLogoutExit")

    def test_starting_a_session_is_guarded_against_reentry(self):
        """Two `connection: close` events before the reconnect timer fires
        would schedule two `start()`s, and two live sockets both write the
        session directory through `creds.update` — the auth-state corruption
        the pair command refuses a whole running daemon to avoid, reached from
        inside one process."""
        source = PROGRAM.read_text()

        assert "if (this.starting || this.stopping) return;" in source
        assert "this.starting = true;" in source
        assert "const mine = () => this.sock === sock;" in source


class TestTheLoggedOutBackoff:
    """A permanent 401 must not become an unbounded run of failed logins.

    The `loggedOut` branch sends `fatal` and exits, and neither
    `MAX_START_FAILURES` nor `START_RETRY_MS` is upstream of it — those gate
    `reportStartFailure`, which covers a session that could not be
    *constructed*. So on both shipped deployment shapes the exit is answered
    by a supervisor that starts the program again at a fixed interval:
    `Restart=always` with `RestartSec=30` on the unit, Docker's own backoff
    capped at 60s on compose. Each cycle is a real websocket and a real
    authentication attempt against an account WhatsApp has already unlinked
    once, which `.claude/rules/whatsapp.md` records as the behaviour class
    the maintainers found accounts being banned for.

    The counter has to survive a process that exits, so it lives in a file
    beside `sidecar.log` — inside the same 0700 directory, holding a count and
    two timestamps and nothing else. The sleep goes *before* the exit rather
    than into the supervisor's interval, because the supervisor cannot tell
    "unlinked" from "crashed" and has to keep restarting promptly for the
    second.

    Executed rather than asserted against the source wherever it can be:
    `loadBaileys()` is lazy, so `require('./index.js')` reaches all of this
    with no `node_modules` in the tree.
    """

    @staticmethod
    def _node() -> str:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        return node

    @classmethod
    def _run(cls, script: str, session_dir=None, timeout: int = 30):
        """Run a script against the program and parse what it wrote.

        `env=` replaces the whole environment rather than adding to it, so the
        copy is not optional — without `PATH` the child cannot resolve its own
        interpreter's neighbours, and without the rest `node` itself behaves
        differently enough to be its own bug hunt.

        **The child's umask is deliberately wide.** `main()` does not run under
        `require`, so `applyPrivateUmask` has not, and a mode assertion in a
        child that inherited this suite's own 077 would be asserting the
        ambient umask rather than the program — which is the failure class
        `.claude/rules/testbed.md` catalogues and the class twenty lines below
        already guards against the same way.
        """
        env = dict(os.environ)
        if session_dir is not None:
            env["ISTOTA_BAILEYS_SESSION_DIR"] = str(session_dir)
            env["ISTOTA_BAILEYS_SOCKET"] = str(session_dir / "sock")
        result = subprocess.run(
            [cls._node(), "-e", f"process.umask(0o022);{script}"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    @classmethod
    def _call(cls, expression: str, session_dir=None):
        """Evaluate one expression against the program."""
        return cls._run(
            f"const m = require({json.dumps(str(PROGRAM))});"
            f"process.stdout.write(JSON.stringify({expression}));",
            session_dir,
        )

    # --- the ladder --------------------------------------------------------

    @pytest.mark.parametrize(
        "count,expected_ms",
        [
            # The first logout is unchanged: 500ms is the existing floor that
            # lets the `fatal` frame leave the socket before the exit, and a
            # single unlink must not become slower than it is today.
            (1, 500),
            (2, 30_000),
            (3, 300_000),
            (4, 900_000),
            (5, 1_800_000),
            (6, 3_600_000),
            # Clamped, not indexed past the end — `undefined` here would be
            # `setTimeout(fn, undefined)`, which fires immediately and turns
            # the whole backoff off at exactly the run length where it matters
            # most.
            (7, 3_600_000),
            (999, 3_600_000),
        ],
    )
    def test_the_wait_grows_with_the_run_and_then_stops_growing(
        self, count, expected_ms
    ):
        assert self._call(f"m.logoutExitDelayMs({count})") == expected_ms

    @pytest.mark.parametrize("garbage", ["0", "-3", "1.5", "'x'", "null", "NaN"])
    def test_a_count_it_cannot_read_degrades_to_todays_behaviour(self, garbage):
        """The direction matters more than the value. The count comes off a
        file inside the directory a full-account credential lives in, and the
        safe failure is the *short* wait: a longer one delays the start that
        would have worked after a re-pair, which is the one property the issue
        asked to keep."""
        assert self._call(f"m.logoutExitDelayMs({garbage})") == 500

    # --- the state file ----------------------------------------------------

    def test_a_run_of_logouts_accumulates_across_processes(self, tmp_path):
        """The point of a file rather than a field on `Session`: every one of
        these cycles is a *different process*, so in-memory state resets to
        zero on each and the backoff never leaves its first rung."""
        first = self._call("m.recordLogout('2026-09-13T00:00:00.000Z')", tmp_path)
        second = self._call("m.recordLogout('2026-09-13T00:01:00.000Z')", tmp_path)

        assert first["count"] == 1
        assert second["count"] == 2
        # `first_at` is carried rather than restamped, so the file says how
        # long the session has been unlinked and not merely when it last tried.
        assert second["first_at"] == "2026-09-13T00:00:00.000Z"
        assert second["at"] == "2026-09-13T00:01:00.000Z"

    def test_the_state_file_is_private(self, tmp_path):
        """It sits inside the 0700 credential directory, so it is as private
        as the credential beside it — and `harden_session_files` walks the
        whole directory, so a wide one would be reported as a session possibly
        readable by other accounts.

        `_run` puts the child under a deliberately wide umask, so a mode of
        0600 can only have come from the program's own `{ mode: 0o600 }`.
        """
        self._call("m.recordLogout('2026-09-13T00:00:00.000Z')", tmp_path)
        written = [p for p in tmp_path.iterdir() if p.is_file()]

        assert len(written) == 1, [p.name for p in written]
        assert written[0].stat().st_mode & 0o777 == 0o600

    def test_the_control_says_the_wide_mode_is_reachable(self, tmp_path):
        """Without the explicit mode the same write lands 0644 in the same
        child. A test asserting a mode has to be shown able to see the other
        one, or it is asserting the ambient umask — this class's own
        `applyPrivateUmask` neighbour is the precedent."""
        target = tmp_path / "logout-backoff.json"
        self._run(
            f"require('fs').writeFileSync({json.dumps(str(target))}, 'x');"
            "process.stdout.write('null');",
            tmp_path,
        )

        assert target.stat().st_mode & 0o777 == 0o644

    def test_a_session_that_opens_clears_the_run(self, tmp_path):
        """The existing `startFailures = 0` rule one level up: a session that
        opened is evidence the credential is usable, so the next unlink starts
        again from the first rung rather than from an hour."""
        self._call("m.recordLogout('2026-09-13T00:00:00.000Z')", tmp_path)
        after = self._call("(m.clearLogoutState(), m.readLogoutState())", tmp_path)

        assert after["count"] == 0
        assert not [p for p in tmp_path.iterdir() if p.is_file()]

    def test_clearing_a_run_that_was_never_recorded_is_not_an_error(self, tmp_path):
        """It runs on the `open` transition, which is the ordinary path on
        every healthy start — so the missing file is the common case rather
        than the exception."""
        assert self._call(
            "(m.clearLogoutState(), m.readLogoutState().count)", tmp_path
        ) == 0

    @pytest.mark.parametrize(
        "content",
        ["", "not json", "[]", "null", '{"count": "seven"}', '{"count": -2}'],
    )
    def test_a_file_it_cannot_read_reads_as_no_runs(self, tmp_path, content):
        """Same direction as the garbage counts above, one layer out. A
        truncated write — a full disk, a host killed mid-write — must degrade
        to today's prompt retry rather than to an hour of silence."""
        (tmp_path / "logout-backoff.json").write_text(content)

        assert self._call("m.readLogoutState().count", tmp_path) == 0

    def test_a_corrupt_file_still_advances_the_run(self, tmp_path):
        """Reading it as zero must not mean *staying* at zero: the write that
        follows is what stops the next cycle from being unbounded."""
        (tmp_path / "logout-backoff.json").write_text("not json")
        state = self._call("m.recordLogout('2026-09-13T00:00:00.000Z')", tmp_path)

        assert state["count"] == 1
        assert self._call("m.readLogoutState().count", tmp_path) == 1

    @pytest.mark.requires_dac
    def test_a_write_that_fails_keeps_the_rung_it_read(self, tmp_path):
        """`recordLogout` is read-modify-write, and the caller waits on what it
        returns. A write it cannot make must not turn that into `undefined`,
        which `setTimeout` fires immediately on — the backoff switched off by
        the one failure mode most likely to be permanent.

        The rung it keeps is the one it *read*, not the first: five recorded
        logouts and an unwritable file is still a run five long, and collapsing
        to 500ms there would make the ladder disappear exactly when the disk
        filled. What the failure costs is the next process's increment.

        The file is read-only rather than the directory, which is the
        difference that makes this test able to fail at all: directory write
        permission governs creating and removing a name, so `writeFileSync`
        over an existing file in a 0500 directory succeeds.
        """
        state_file = tmp_path / "logout-backoff.json"
        state_file.write_text('{"count": 5, "first_at": "2026-09-01T00:00:00.000Z"}')
        state_file.chmod(0o400)
        try:
            state = self._call("m.recordLogout('2026-09-13T00:00:00.000Z')", tmp_path)
        finally:
            state_file.chmod(0o600)

        assert state["count"] == 6
        # The write is what failed, so the file is untouched — which is how
        # this separates a failed write from a successful one. Without it the
        # assertion above is equally true of a write that worked.
        assert json.loads(state_file.read_text())["count"] == 5

    # --- the run that cannot be recorded -----------------------------------

    def test_a_planted_directory_no_longer_switches_the_backoff_off(self, tmp_path):
        """The both-fail case, and the one ISSUE-501 was filed for.

        `recordLogout` is read-modify-write and both halves swallow their
        errors, so where the file can be neither read nor written every cycle
        computes `0 + 1` and waits the 500ms frame-flush floor. The ladder
        never advances and the deployment is back to roughly 2,880 real logins
        a day against an account WhatsApp has already unlinked — the exact
        condition ISSUE-498 was filed to bound, reintroduced by the failure
        modes most likely to be permanent.

        Driven end to end through `logoutWaitMs(recordLogout())` rather than
        asserting on the count alone: the count *is* 1 here and correctly so,
        since one logout really has happened. What went wrong is the wait
        derived from it, so a test that stopped at the count would pass
        against the broken program.

        A planted directory rather than a chmod, so this carries no
        `requires_dac`: `EISDIR` is not a permission check, so it reproduces
        as root too — where the file-mode cases below silently skip.
        """
        (tmp_path / "logout-backoff.json").mkdir()
        waits = self._call(
            "Array.from({length: 4}, () => m.logoutWaitMs(m.recordLogout()))",
            tmp_path,
        )

        assert waits == [300_000] * 4

    def test_the_control_says_that_directory_is_what_breaks_both_halves(
        self, tmp_path
    ):
        """The negative control for the case above. An assertion about a
        fallback has to be shown able to see the ordinary ladder, or it is
        asserting a constant."""
        waits = self._call(
            "Array.from({length: 4}, () => m.logoutWaitMs(m.recordLogout()))",
            tmp_path,
        )

        assert waits == [500, 30_000, 300_000, 900_000]

    @pytest.mark.requires_dac
    def test_a_read_only_directory_with_no_file_still_backs_off(self, tmp_path):
        """On the issue's own repro list, and the case that decides the shape.

        The entry proposed discriminating the read errno — `ENOENT` being
        "genuinely the first logout of a run" and the unknowable ones meaning
        something else. That split does not cover this: the read here *is*
        `ENOENT`, because no file was ever created, and the run is pinned at
        the floor anyway because the write is what cannot land. The condition
        has to be that the state did not reach disk, which is why the write is
        consulted as well as the read.
        """
        tmp_path.chmod(0o500)
        try:
            waits = self._call(
                "Array.from({length: 3}, () => m.logoutWaitMs(m.recordLogout()))",
                tmp_path,
            )
        finally:
            tmp_path.chmod(0o700)

        assert waits == [300_000] * 3

    @pytest.mark.requires_dac
    def test_an_unreadable_file_that_can_be_rewritten_still_backs_off(
        self, tmp_path
    ):
        """The other half of the same correction, in the opposite direction.

        Here the write lands on every cycle and the read never does, so the
        file is rewritten to `count: 1` for ever. The entry's shape asked for
        the read to be unknowable *and* the write to fail; this satisfies only
        the first and is just as unbounded, which is why the two conditions
        are a union rather than a conjunction.

        `writeFileSync`'s `mode` applies only to a file it creates, so the
        0200 survives its own rewrite and the next cycle reads it just as
        badly — which is what makes this permanent rather than one cycle.
        """
        state_file = tmp_path / "logout-backoff.json"
        state_file.write_text('{"count": 4}')
        state_file.chmod(0o200)
        try:
            waits = self._call(
                "Array.from({length: 3}, () => m.logoutWaitMs(m.recordLogout()))",
                tmp_path,
            )
        finally:
            state_file.chmod(0o600)

        assert waits == [300_000] * 3

    def test_the_healthy_first_logout_is_still_the_short_wait(self, tmp_path):
        """The property the fallback must not cost. A missing file with a
        writable directory is the common, healthy path — the first logout of a
        run — and 500ms there is the frame-flush floor rather than a rung.
        Making that slower would delay every single unlink in order to protect
        the rare broken deployment."""
        assert self._call("m.logoutWaitMs(m.recordLogout())", tmp_path) == 500

    @pytest.mark.requires_dac
    def test_a_failed_write_never_shortens_the_rung_it_read(self, tmp_path):
        """The fallback is a floor under the ladder, not a replacement for it.

        Five recorded logouts and an unwritable file is still a run five long,
        and `test_a_write_that_fails_keeps_the_rung_it_read` above is what says
        the count survives. This says the *wait* does too: taking the fallback
        rung unconditionally whenever the write failed would cut an hour to
        five minutes at exactly the run length where the wait matters most,
        which is the direction this whole mechanism must not fail in.
        """
        state_file = tmp_path / "logout-backoff.json"
        state_file.write_text('{"count": 5, "first_at": "2026-09-01T00:00:00.000Z"}')
        state_file.chmod(0o400)
        try:
            wait = self._call("m.logoutWaitMs(m.recordLogout())", tmp_path)
        finally:
            state_file.chmod(0o600)

        assert wait == 3_600_000

    def test_a_corrupt_file_the_program_can_rewrite_keeps_the_short_wait(
        self, tmp_path
    ):
        """A truncated write — a host killed mid-write, a full disk since
        cleared — is a *content* failure on a file the program can still
        replace, so the next cycle reads the count it just wrote and the
        ladder climbs from the floor. That is ISSUE-498's stated direction and
        the fallback must not quietly take it over: the run genuinely is one
        long here."""
        (tmp_path / "logout-backoff.json").write_text("not json")
        waits = self._call(
            "Array.from({length: 3}, () => m.logoutWaitMs(m.recordLogout()))",
            tmp_path,
        )

        assert waits == [500, 30_000, 300_000]

    def test_the_fallback_rung_is_taken_from_the_ladder(self):
        """Never a literal. The rung is the ladder's own entry, so a change to
        the ladder moves the fallback with it rather than leaving a number here
        that used to be mid-ladder."""
        assert self._call(
            "m.logoutWaitMs({count: 1, unrecorded: true})"
        ) == self._call("m.logoutExitDelayMs(m.LOGOUT_UNKNOWN_RUN)")

    @pytest.mark.requires_dac
    def test_the_marker_never_reaches_the_file(self, tmp_path):
        """It is a fact about this process's filesystem rather than about the
        run, so a deployment that recovers must not read a stale one back.

        The unreadable-file case is the discriminating one: it is the only
        shape where the marker is set *and* the write lands, so it is the only
        place a marker set a statement too early could be serialized.
        """
        state_file = tmp_path / "logout-backoff.json"
        state_file.write_text('{"count": 4}')
        state_file.chmod(0o200)
        try:
            self._call("m.recordLogout('2026-09-13T00:00:00.000Z')", tmp_path)
        finally:
            state_file.chmod(0o600)
        written = json.loads(state_file.read_text())

        assert set(written) == {"count", "first_at", "at"}, written

    # --- the credential stamp ----------------------------------------------

    def test_the_stamp_changes_when_the_credential_is_replaced(self, tmp_path):
        """What ends the wait early. A re-pair replaces `creds.json`, and from
        that moment the next login is no longer the doomed one the backoff
        exists to space out — so holding a working session down for the rest
        of an hour is the opposite of what the wait is for.

        mtime and size rather than the contents: this is a full-account
        credential, and a fingerprint that never reads it cannot leak it.
        """
        creds = tmp_path / "creds.json"
        creds.write_text('{"me": 1}')
        before = self._call("m.credentialStamp()", tmp_path)
        # A distinct size, so the assertion does not rest on filesystem mtime
        # granularity — coarse enough on some filesystems that two writes in
        # one test share a timestamp.
        creds.write_text('{"me": 2, "paired": "again"}')

        assert self._call("m.credentialStamp()", tmp_path) != before

    def test_the_stamp_is_stable_while_nothing_touches_the_credential(self, tmp_path):
        """The control. A stamp that changed on its own would exit every
        process at the first poll and leave the backoff unreachable — which
        looks exactly like the bug being fixed."""
        (tmp_path / "creds.json").write_text('{"me": 1}')

        assert (self._call("m.credentialStamp()", tmp_path)
                == self._call("m.credentialStamp()", tmp_path))

    def test_a_credential_that_is_removed_ends_the_wait_too(self, tmp_path):
        """The documented remedy for this state is to move the session
        directory aside, and ISSUE-496's `--reset` is the same primitive. Both
        are evidence the next start is not the doomed one, so both end the
        wait — and an absent credential must not read the same as a present
        one, which is what a bare `try`/`catch` returning a constant would do.
        """
        creds = tmp_path / "creds.json"
        creds.write_text('{"me": 1}')
        present = self._call("m.credentialStamp()", tmp_path)
        creds.unlink()

        assert self._call("m.credentialStamp()", tmp_path) != present

    # --- the wiring --------------------------------------------------------

    def test_the_run_is_recorded_before_the_process_sleeps(self):
        """A `systemctl restart` or a SIGTERM during a half-hour wait must not
        lose the increment — otherwise the operator's own intervention resets
        the ladder to its first rung and the loop is unbounded again."""
        body = _js_method("onConnection")

        assert body.index("recordLogout(") < body.index("scheduleLogoutExit(")

    def test_the_fatal_frame_leaves_before_the_wait_begins(self):
        """The daemon latches the permanent fatal and alerts on it. Delaying
        the frame by the length of the backoff would mean a deployment learns
        its WhatsApp is down an hour after it went down."""
        body = _js_method("onConnection")

        assert (body.index("reason: FATAL_LOGGED_OUT")
                < body.index("scheduleLogoutExit("))

    def test_the_open_transition_clears_the_run(self):
        """Beside `startFailures = 0`, which is the same rule for the
        in-process counter."""
        body = _js_method("onConnection")
        branch = body[body.index("if (connection === 'open')"):]

        assert "clearLogoutState();" in branch[:branch.index("return;")]

    def test_the_wait_is_derived_from_what_could_be_established(self):
        """The call-site half of ISSUE-501, and the mutation that otherwise
        passes this whole class: `logoutExitDelayMs(run.count)` is still a
        perfectly good expression that compiles, runs, and returns 500 for
        ever on a deployment that cannot record its run. Everything else here
        exercises `logoutWaitMs` in isolation and would stay green."""
        body = _js_method("onConnection")

        assert "const delay = logoutWaitMs(run);" in body
        assert "logoutExitDelayMs(run.count)" not in body

    def test_the_reported_marker_is_the_one_record_logout_returned(self):
        """The sibling of the pin above, and the mutation it exists for is a
        one-word one: `run.unknown` is a real key on the state object — it is
        what `readLogoutState` sets — so reading it here compiles, runs, and
        silently reports the wrong half of the union. The unreadable-file shape
        then still reports (both halves are true there) while the read-only
        directory, where the read is an ordinary `ENOENT` and only the write
        failed, reports nothing at all: `doctor` calls that deployment healthy
        for as long as it lasts. Measured — swapping the word leaves every
        other test in this file green."""
        body = _js_method("onConnection")

        assert "this.runUnrecorded = run.unrecorded === true;" in body

    def test_the_unrecorded_run_is_reported_before_the_wait(self):
        """It has to reach the daemon while the daemon can still be told. The
        frame goes out before `scheduleLogoutExit`, which on this path does not
        return for up to an hour."""
        body = _js_method("onConnection")

        assert (body.index("run_unrecorded: true")
                < body.index("scheduleLogoutExit("))

    def test_the_first_fatal_still_leaves_before_any_filesystem_work(self):
        """Why the marker rides a *second* frame rather than the first. The
        first one's position is load-bearing: `recordLogout` reads and writes a
        file, which on a hung mount blocks indefinitely, and the daemon must
        not learn its WhatsApp is down only once that returns."""
        body = _js_method("onConnection")
        branch = body[body.index("if (loggedOut) {"):]

        assert branch.index("reason: FATAL_LOGGED_OUT") < branch.index(
            "recordLogout("
        )

    def test_the_re_announced_verdict_carries_the_marker(self):
        """The daemon's latch is in memory, so a scheduler that restarted
        during the wait re-learns the logout from `announceReady` — and has to
        re-learn this with it, or `doctor` reports a deployment retrying every
        thirty seconds as one backing off correctly."""
        body = _js_method("announceReady")

        assert "run_unrecorded: this.runUnrecorded" in body

    def test_the_branch_waits_for_the_delay_it_computed(self):
        """The one thing a substring pin still has to say, and it is the
        mutation that otherwise passes the whole of this class: passing
        `run.count` where `delay` belongs makes the wait 1 to 6 milliseconds,
        the deadline already past at the first tick, and the backoff off
        entirely — while every assertion above stays green, because each one
        exercises `logoutExitDelayMs` in isolation and nothing else looks at
        what reaches the wait."""
        body = _js_method("onConnection")

        assert "scheduleLogoutExit(delay, () => process.exit(1));" in body

    # --- the wait loop, executed -------------------------------------------

    def _wait_outcome(self, session_dir, delay_ms, poll_ms, change_after_ms=None):
        """Run the real wait loop and report when it decided to exit.

        A top-level function taking its own exit and interval is what makes
        this possible: as a `Session` method it was unreachable, since
        `Session` is not exported, and the loop is the one piece of this work
        whose failure modes — an inverted comparison, a deadline computed
        wrongly, a tick that never reschedules — are invisible to a substring
        pin.
        """
        creds = session_dir / "creds.json"
        creds.write_text('{"me": 1}')
        change = (
            "setTimeout(() => require('fs').writeFileSync("
            f"{json.dumps(str(creds))}, '{{\"me\": 2, \"paired\": \"again\"}}'), "
            f"{change_after_ms});"
            if change_after_ms is not None else ""
        )
        outcome = self._run(
            f"const m = require({json.dumps(str(PROGRAM))});"
            "const started = Date.now();"
            "const say = (v) => {process.stdout.write(JSON.stringify(v));"
            "process.exit(0);};"
            f"{change}"
            # The give-up timer is what separates "waited out the deadline"
            # from "never rescheduled and is still sitting there".
            f"setTimeout(() => say(null), {delay_ms + 3000});"
            f"m.scheduleLogoutExit({delay_ms}, "
            "() => say(Date.now() - started), "
            f"{poll_ms});",
            session_dir,
            timeout=60,
        )
        return outcome

    def test_a_stable_credential_waits_out_the_whole_delay(self, tmp_path):
        """The control for the two below. Without it, a loop that exited at
        its first tick would satisfy every early-exit assertion in this file
        while having no backoff in it at all."""
        elapsed = self._wait_outcome(tmp_path, delay_ms=1200, poll_ms=50)

        assert elapsed is not None, "the wait never ended"
        assert elapsed >= 1200, elapsed

    def test_a_credential_replaced_mid_wait_ends_it_early(self, tmp_path):
        """The issue's first property, driven rather than asserted. Nothing in
        the re-pair flow restarts the systemd unit, so without this a session
        re-paired two minutes into an hour-long wait sits out the other
        fifty-eight."""
        elapsed = self._wait_outcome(
            tmp_path, delay_ms=30_000, poll_ms=50, change_after_ms=200,
        )

        assert elapsed is not None, "the wait never ended"
        assert elapsed < 5_000, elapsed

    def test_a_write_landing_before_the_first_poll_does_not_end_the_wait(
        self, tmp_path
    ):
        """The regression control for why the baseline is taken at the first
        poll rather than at schedule time.

        `saveCreds` is an async write, so one started by the login that has
        just failed can land after `scheduleLogoutExit` is called. With the
        baseline taken up front, that write reads as a re-pair and ends the
        wait at the first poll — on every rung, every cycle, capping the
        effective interval at the supervisor's own while `logout-backoff.json`
        keeps climbing and looks like a working backoff. Exactly the "success
        indistinguishable from a no-op" shape `.claude/rules/testbed.md`
        catalogues.

        Seeding the baseline at schedule time instead turns this red and
        leaves the two tests above green.
        """
        elapsed = self._wait_outcome(
            tmp_path, delay_ms=1200, poll_ms=400, change_after_ms=30,
        )

        assert elapsed is not None, "the wait never ended"
        assert elapsed >= 1200, elapsed

    # --- the link through the wait -----------------------------------------

    def test_the_daemon_link_reconnects_through_the_wait(self):
        """`stopping` alone would stop it: the flag is set before the wait and
        is also what a deliberate shutdown sets. With the wait now up to an
        hour, a scheduler restart landing inside one would otherwise leave a
        sidecar that neither reconnects nor exits."""
        source = PROGRAM.read_text()

        assert "if (session.stopping && !session.loggedOut) return;" in source

    def test_the_verdict_is_re_announced_on_a_reconnected_link(self):
        """The daemon's permanent-fatal latch is in memory and only this frame
        sets it, so a scheduler that restarted during the wait has none —
        `doctor` and the admin alert would say "no sidecar connected" rather
        than "logged out, re-pair" for the rest of the rung."""
        body = _js_method("announceReady")

        assert "if (this.loggedOut)" in body
        assert body.index("FATAL_LOGGED_OUT") < body.index("MSG_READY")

    def test_a_second_logged_out_close_does_not_advance_the_ladder(self):
        """`if (this.stopping) return;` sits below this branch, and `mine()`
        still passes for the socket that just closed, so a repeated close
        carrying the same status re-enters. Harmless while the branch was one
        `setTimeout`; a rung per duplicate event now that it writes a
        persisted count."""
        body = _js_method("onConnection")
        branch = body[body.index("if (loggedOut) {"):]

        assert branch.index("if (this.loggedOut) return;") < branch.index(
            "recordLogout("
        )

    def test_the_dead_socket_is_dropped_before_the_wait(self):
        """`send`'s `if (!this.sock)` guard is the only thing answering a send
        with a definite `not_connected`; left in place, a send during the wait
        reaches `sendMessage` on a dead socket and settles the ledger
        `unknown`. It is also what makes `mine()` false, which is what stops a
        late `creds.update` moving the credential the wait fingerprints."""
        body = _js_method("onConnection")
        branch = body[body.index("if (loggedOut) {"):]

        assert branch.index("this.sock = null;") < branch.index(
            "scheduleLogoutExit("
        )

    def test_the_credential_writer_is_bound_to_the_live_socket(self):
        """Its three siblings are guarded and it was not. After a logout the
        socket is nulled, so this guard is what keeps a late save from moving
        `creds.json` under a wait that watches it."""
        body = _js_body("async open_()")

        assert "sock.ev.on('creds.update', () => {" in body
        assert "if (mine()) saveCreds();" in body


class TestTheSidecarCreatesPrivateFiles:
    """The session directory holds a full-account WhatsApp credential, and
    the program is the only place all three of its launch shapes pass through.

    The daemon's own spawn passes `umask=0o077`, which covers exactly one of
    them. On Ansible the sidecar is a systemd unit, whose default `UMask` is
    0022; on compose it is a service of its own, and a compose service cannot
    express a umask at all. So on both deployment shapes every key file
    Baileys wrote after pairing landed 0644, and `harden_session_files` — which
    runs once, when the bridge starts — could narrow what was there and not
    what a live session writes next.

    **Executed, not asserted against the source**, and it is one of the few
    things here that can be: `applyPrivateUmask` touches nothing Baileys owns,
    so `require('./index.js')` reaches it with no `node_modules` in the tree.
    A source assertion would say the call is written and not that a file
    created after it is private, which is the property.
    """

    @staticmethod
    def _file_mode_after(prelude: str, tmp_path) -> int:
        """Create a file in a child node process and report its mode.

        `prelude` runs first. The child is started under a deliberately wide
        umask, so a mode of 0600 can only have come from the program.
        """
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        target = tmp_path / "creds.json"
        script = (
            "process.umask(0o022);"
            f"const m = require({json.dumps(str(PROGRAM))});"
            f"{prelude}"
            f"require('fs').writeFileSync({json.dumps(str(target))}, 'x');"
        )
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return target.stat().st_mode & 0o777

    def test_a_file_written_after_it_is_private(self, tmp_path):
        assert self._file_mode_after("m.applyPrivateUmask();", tmp_path) == 0o600

    def test_the_control_says_the_wide_mode_is_reachable(self, tmp_path):
        """Without the call the same write lands 0644, which is what the two
        external shapes were doing. A test asserting a mode has to be shown
        able to see the other one, or it is asserting the ambient umask."""
        assert self._file_mode_after("", tmp_path) == 0o644

    def test_the_entry_point_applies_it_before_anything_can_write(self):
        """Exporting it is what makes the test above possible; calling it is
        what makes it true of the deployment. `main` is the first thing in the
        program that can create a file, so the call is its first statement —
        ahead of the exit-2 arm, which has no log destination to write to and
        no reason to be the one branch outside the invariant."""
        lines = _js_function("main").splitlines()[1:]
        statements = [
            line.strip() for line in lines
            if line.strip() and not line.strip().startswith(("//", "*", "/*"))
        ]

        assert statements[0] == "applyPrivateUmask();"


class TestThePinnedLibrary:
    def test_baileys_is_pinned_to_an_exact_version(self):
        """A range is what makes a sidecar that worked yesterday stop today:
        Baileys tracks a protocol WhatsApp changes without notice, so which
        version is installed has to be a fact about the commit."""
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())
        version = manifest["dependencies"]["@whiskeysockets/baileys"]

        # A prerelease suffix is allowed because 7.x is where WhatsApp's LID
        # addressing is implemented and it has published nothing else; what
        # stays refused is a *range*, which is the thing that makes a sidecar
        # that worked yesterday stop today. npm's own tags make that concrete
        # here: `latest` is the 7.0.0 prerelease while `legacy` is 6.7.24, and
        # a stranded 6.17.16 published before 6.7.18 outranks every real 6.7.x
        # by semver — so `^6` resolves to a build from seventeen months
        # earlier. Only an exact string is safe in either line.
        assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version), (
            f"baileys is pinned as {version!r}; an exact version is required"
        )

    def test_it_declares_the_node_floor_the_library_needs(self):
        """Baileys 7 refuses to install below Node 20 — its `preinstall`
        script exits 1 — so a manifest still claiming 18 promises an install
        that cannot happen."""
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())

        assert manifest["engines"]["node"].startswith(">=20")

    def test_the_library_is_loaded_by_dynamic_import(self):
        """Baileys 7 is ESM-only, so a CommonJS `require` of it throws
        `ERR_REQUIRE_ESM` at the first session open — a failure no test
        reaching for the program's exports would ever see, because they never
        call this.

        Both halves are pinned. The dynamic `import()` is what makes the
        library loadable at all; `module.exports` staying is what lets the
        default suite load the program with no `node_modules` present and run
        its pure functions, which converting this file to ESM would have
        cost.
        """
        source = PROGRAM.read_text()
        start = source.index("async function loadBaileys()")
        body = source[start:source.index("\n}\n", start)]

        assert "import('@whiskeysockets/baileys')" in body
        assert "require('@whiskeysockets/baileys')" not in PROGRAM.read_text()
        assert "module.exports" in PROGRAM.read_text()

    def test_the_import_stays_lazy(self):
        """The specifier is resolved when a session opens, not when the
        module loads — which is the whole reason the default suite can
        `require` this program in a tree with no dependencies installed."""
        source = PROGRAM.read_text()

        assert source.index("async open_()") > source.index("async function loadBaileys()")
        assert "await loadBaileys()" in _js_body("async open_()")

    def test_it_is_private_so_it_cannot_be_published(self):
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())

        assert manifest["private"] is True

    def test_the_entry_point_agrees_with_the_manifest(self):
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())
        from istota.transport.whatsapp.baileys_bridge import SIDECAR_ENTRY

        assert manifest["main"] == SIDECAR_ENTRY


class TestTheLockfileInstallsThePinnedLibrary:
    """`npm ci` is the install verb everywhere, and it reads only this file.

    The manifest's exact version above is a fact about the commit only if the
    tree actually installed from it: `npm ci` refuses to resolve anything and
    takes the lockfile verbatim, so a lockfile naming a different version is
    the pin silently not holding. It is also what makes the install
    reproducible — 205 integrity hashes rather than whatever the registry
    serves today, for a program that holds a full WhatsApp account.
    """

    def _lock(self) -> dict:
        return json.loads((SIDECAR_DIR / "package-lock.json").read_text())

    def test_it_installs_the_version_the_manifest_pins(self):
        manifest = json.loads((SIDECAR_DIR / "package.json").read_text())
        entry = self._lock()["packages"]["node_modules/@whiskeysockets/baileys"]

        assert entry["version"] == manifest["dependencies"]["@whiskeysockets/baileys"]

    def test_every_package_is_pinned_by_a_hash_of_some_kind(self):
        """Integrity for a registry tarball, a commit sha for a git one.

        npm records no `integrity` for a git dependency, and this tree has
        two of them — so a flat integrity assertion is a test that would have
        to be weakened rather than a rule. What pins a git dependency is the
        40-character commit its URL fragment names, a git object being
        addressed by its own content. An entry with neither is a download
        nothing verifies at all.
        """
        unpinned = sorted(
            name
            for name, entry in self._lock()["packages"].items()
            if entry.get("resolved")
            and not entry.get("integrity")
            and not re.search(r"#[0-9a-f]{40}$", entry["resolved"])
        )

        assert unpinned == []

    def test_the_git_dependencies_are_named_rather_than_discovered(self):
        """They decide whether either install path needs git at all.

        `libsignal` is Baileys' cryptography, recorded as
        `git+ssh://git@github.com/…`, which reads like a build needing git and
        a credential — and measurably is not, because npm fetches a *hosted*
        git dependency as a codeload tarball over https. The image therefore
        ships no git, verified by building it with none and finding
        `node_modules/libsignal` present.

        There were two until the 6.7.24 bump: Baileys 6.7.18 declared its own
        eslint config under `dependencies` rather than `devDependencies`, so
        `--omit=dev` kept it and every install paid for eslint. The bump drops
        it, which is most of the tree going from 207 packages to 92.

        What that rests on is the set below: a third git dependency, on a forge
        `hosted-git-info` does not know, would put git and an https rewrite back
        into the Dockerfile and the role. This is the assertion that says so.
        """
        git_deps = sorted(
            name.removeprefix("node_modules/")
            for name, entry in self._lock()["packages"].items()
            if str(entry.get("resolved", "")).startswith("git+")
        )

        assert git_deps == ["libsignal"]

    def test_it_is_a_lockfile_npm_ci_can_read(self):
        """`npm ci` needs v2 or later; v1 has no `packages` map at all."""
        lock = self._lock()

        assert lock["lockfileVersion"] >= 2
        assert lock["name"] == json.loads(
            (SIDECAR_DIR / "package.json").read_text()
        )["name"]


class TestTheImageCopiesWhatTheProgramNeeds:
    """The Dockerfile is the devbox guard's own lesson one directory over.

    A Dockerfile can COPY a manifest, a lockfile or a script out of here as
    readily as a module, so it is the file most likely to fall out of step
    with the directory: a leaf added and not copied is an image that builds
    and then fails at require time, and a copy of something no longer here is
    a build that fails outright.
    """

    def _dockerfile(self) -> str:
        return (SIDECAR_DIR / "Dockerfile").read_text()

    def _directives(self) -> str:
        """The file with its comments removed.

        The comments in there name `npm install` and `USER node` in order to
        say why neither is used, so a scan of the whole text answers the
        opposite of the question being asked.
        """
        return "\n".join(
            line for line in self._dockerfile().splitlines()
            if not line.lstrip().startswith("#")
        )

    def _copied(self) -> set[str]:
        names: set[str] = set()
        for line in self._dockerfile().splitlines():
            match = re.match(r"^COPY\s+(?!--from)(.+)$", line.strip())
            if match:
                # The last word is the destination.
                names.update(match.group(1).split()[:-1])
        return names

    def test_everything_it_copies_is_in_this_directory(self):
        for name in sorted(self._copied()):
            assert (SIDECAR_DIR / name).exists(), (
                f"the Dockerfile copies {name!r}, which is not in "
                "docker/whatsapp-baileys/ — the build fails outright"
            )

    def test_it_copies_the_program_and_both_manifest_files(self):
        """The runtime set, and it is not "every entry": README.md and the
        Dockerfile itself are deliberately not in the image."""
        assert self._copied() == {"index.js", "package.json", "package-lock.json"}

    def test_it_installs_from_the_lockfile(self):
        """`npm install` would resolve afresh and quietly install a version
        this repository never pinned."""
        assert re.search(r"\bnpm ci\b", self._directives())
        assert not re.search(r"\bnpm install\b", self._directives())

    def test_it_installs_nothing_beyond_the_node_base(self):
        """The install step is `npm ci` and nothing else.

        Recorded because this image carried an `apt-get install git` and a
        GitHub ssh-to-https rewrite for one commit, on the reading that its two
        git dependencies needed them. A build with neither, checked for
        `node_modules/libsignal`, says otherwise — npm fetches a hosted git
        dependency as a codeload tarball. Nothing here can measure that, so
        what this holds is the smaller claim: if either comes back, it comes
        back with a reason, rather than by habit.
        """
        assert not re.search(r"\bapt-get\b", self._directives())
        assert "GIT_CONFIG" not in self._directives()

    def test_it_runs_the_entry_point_the_bridge_names(self):
        from istota.transport.whatsapp.baileys_bridge import SIDECAR_ENTRY

        assert f'CMD ["node", "/app/{SIDECAR_ENTRY}"]' in self._dockerfile()

    def test_the_context_excludes_the_dependency_tree(self):
        """The role installs node_modules into this same directory.

        The compose build context *is* this directory, so on a host that has
        run the play — or on any checkout where somebody ran `npm ci` by hand
        — a build would transfer a few hundred megabytes the Dockerfile copies
        nothing out of. It installs from the lockfile instead.
        """
        ignored = {
            line.strip()
            for line in (SIDECAR_DIR / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }

        assert "node_modules" in ignored
        # Nothing the Dockerfile copies may be ignored, or the build fails on
        # a file the context no longer carries.
        assert not (ignored & self._copied())

    def test_it_declares_no_user(self):
        """`ensure_session_dir` refuses a session directory owned by another
        uid rather than adopting it, so the sidecar and the daemon have to run
        as the same user — and the istota image declares no USER either. A
        `USER node` here builds cleanly and then cannot read the credential
        the daemon paired."""
        assert not re.search(r"^USER\s", self._directives(), re.M)


class TestTheChatAddressUnderLid:
    """Which address an inbound message is attributed to.

    WhatsApp addresses an ordinary one-to-one chat by **LID** — a durable
    per-contact id in its own namespace, carrying no phone number — and puts
    the sender's phone-number JID on the message key as `senderPn`. The
    daemon's whole Baileys identity story is the phone JID: `normalize_jid`
    accepts `@s.whatsapp.net` alone, `jid_number` takes the E.164 out of it to
    compare against the operator's configured bootstrap number, and
    `address_for_binding` renders that same spelling back as a destination. A
    LID therefore resolves to nothing the daemon can act on.

    Observed on a live deployment: a paired session, a message delivered, and
    `remoteJid` ending `@lid` with `senderPn` populated — dropped by the
    sidecar's own forwardable-JID filter, silently, so neither side logged a
    thing.

    Driven through `node` rather than read off the source, because the
    substitution decides **which principal an inbound message acts as** and a
    source assertion about that is a second copy of the rule.
    """

    _call = staticmethod(TestTheSidecarsPureFunctions._call)

    @staticmethod
    def _key(**fields) -> str:
        return json.dumps(fields)

    def test_a_lid_chat_is_attributed_to_the_senders_phone_jid(self):
        """Baileys 7 spells it `remoteJidAlt` — the *other* namespace's address
        for the same correspondent, which in a LID-addressed chat is the phone
        JID."""
        key = self._key(
            remoteJid="277009032835160@lid",
            remoteJidAlt="13105551234@s.whatsapp.net",
        )
        assert self._call(f"m.chatAddress({key})") == "13105551234@s.whatsapp.net"

    def test_the_6_7_spelling_is_still_read(self):
        """`senderPn` was 6.7.x's name for it. Kept as a fallback so the
        function does not depend on which version is installed — a downgrade
        is then a version change rather than a silent return to the bug this
        was written for."""
        key = self._key(
            remoteJid="277009032835160@lid",
            senderPn="13105551234@s.whatsapp.net",
        )
        assert self._call(f"m.chatAddress({key})") == "13105551234@s.whatsapp.net"

    def test_the_v7_field_wins_when_both_are_present(self):
        key = self._key(
            remoteJid="277009032835160@lid",
            remoteJidAlt="13105551234@s.whatsapp.net",
            senderPn="19995550000@s.whatsapp.net",
        )
        assert self._call(f"m.chatAddress({key})") == "13105551234@s.whatsapp.net"

    def test_a_phone_addressed_chat_keeps_its_own_address(self):
        """The control. A chat WhatsApp still addresses by number must not
        start being attributed to a different field of the same key — the
        substitution is scoped to the namespace that needs it."""
        key = self._key(
            remoteJid="13105551234@s.whatsapp.net",
            remoteJidAlt="19995550000@lid",
        )
        assert self._call(f"m.chatAddress({key})") == "13105551234@s.whatsapp.net"

    def test_a_group_keeps_its_own_address(self):
        key = self._key(remoteJid="120363000000000000@g.us", senderPn=None)
        assert self._call(f"m.chatAddress({key})") == "120363000000000000@g.us"

    def test_a_lid_chat_with_no_phone_jid_yields_nothing(self):
        """The residual, held as a test so it stays a decision. A contact
        whose number WhatsApp withholds cannot enroll and cannot resolve on
        this adapter, and the honest answer is a named drop rather than
        forwarding a LID the daemon would refuse one layer later."""
        for pn in (None, "", "277009032835160@lid", "not-a-jid", 12345):
            key = self._key(
                remoteJid="277009032835160@lid", remoteJidAlt=pn, senderPn=pn,
            )
            assert self._call(f"m.chatAddress({key})") == "", pn

    def test_a_missing_or_malformed_key_yields_nothing(self):
        for expression in ("m.chatAddress(null)", "m.chatAddress({})",
                           "m.chatAddress({remoteJid: 42})"):
            assert self._call(expression) == "", expression

    def test_the_phone_domain_it_produces_is_the_one_the_daemon_accepts(self):
        """Driven both ways: a spelling this side invents is a message the
        daemon refuses at `normalize_jid`, which is the silent drop one layer
        down from the one being fixed."""
        key = self._key(
            remoteJid="277009032835160@lid",
            remoteJidAlt="13105551234:7@s.whatsapp.net",
        )
        produced = self._call(f"m.chatAddress({key})")

        assert identity.normalize_jid(produced) == "13105551234@s.whatsapp.net"
        assert identity.jid_number(produced) == "+13105551234"


class TestAnUndecryptedMessageIsNotForwarded:
    """A message Baileys could not decrypt **yet**, and why forwarding one
    loses the message for good.

    A linked device that cannot decrypt a stanza gets a placeholder rather
    than nothing: Baileys emits it through `messages.upsert` with
    `messageStubType = CIPHERTEXT` and no `message` content at all, then sends
    WhatsApp a retry request, and WhatsApp re-sends **the same message id**
    re-encrypted. So the placeholder is a promise of a real delivery, not a
    message.

    Forwarding it was wrong twice over, and the second is the expensive half.
    The user is told "that WhatsApp message type is not supported yet" about
    an ordinary text message, which is false. And `_claim` writes the id into
    `processed_whatsapp` — so every retry that follows, carrying the decrypted
    text, is refused as a duplicate. Observed on a live deployment: one claim
    row at `unsupported_type` and three `whatsapp.inbound.duplicate` lines
    behind it, and the message never arrived.

    Dropping it is what lets the retry through, and it is the direction that
    fails safe: the worst case is silence on a message WhatsApp never managed
    to redeliver, against a guaranteed loss the other way.
    """

    _call = staticmethod(TestTheSidecarsPureFunctions._call)

    @staticmethod
    def _msg(content) -> str:
        return json.dumps({"message": content} if content is not None else {})

    @pytest.mark.parametrize("content", [None, {}])
    def test_a_message_with_no_content_is_not_a_message(self, content):
        assert self._call(f"m.hasReadableContent({self._msg(content)})") is False

    def test_metadata_alone_is_not_content(self):
        """`messageContextInfo` and a sender-key distribution ride *alongside*
        content. Arriving on their own they are the same placeholder in a
        different spelling, and reading them as a message reintroduces the
        claim this exists to withhold."""
        for content in ({"messageContextInfo": {}},
                        {"senderKeyDistributionMessage": {}},
                        {"messageContextInfo": {}, "senderKeyDistributionMessage": {}}):
            assert self._call(f"m.hasReadableContent({self._msg(content)})") is False, content

    def test_a_real_message_is_still_a_message(self):
        """The control, and it carries the case that must not regress: an
        `imageMessage` is genuinely an unsupported *type* and has to keep
        reaching the daemon, which answers for it and claims the id. Only a
        message that was never received is withheld."""
        for content in ({"conversation": "hi"},
                        {"extendedTextMessage": {"text": "hi"}},
                        {"messageContextInfo": {}, "conversation": "hi"},
                        {"imageMessage": {}},
                        {"audioMessage": {}},
                        {"ephemeralMessage": {"message": {"conversation": "hi"}}}):
            assert self._call(f"m.hasReadableContent({self._msg(content)})") is True, content

    def test_the_shape_of_an_undecrypted_message_is_reportable(self):
        """The drop has to say something, or this is the silent discard the
        LID break already was."""
        assert self._call("m.messageShape({})") == "none"


class TestTheSentMessageCacheServesARetry:
    """Answering the recipient's "I could not decrypt that" — and why not
    answering it leaves the message unreadable for good.

    Signal encryption is per device and per session, and the first message
    after a session goes stale routinely fails to decrypt on the far side.
    WhatsApp's own remedy is a **retry receipt**: the recipient asks for the
    message again, the sender re-encrypts against a fresh session and relays
    it. Baileys implements the receiving half and delegates the one thing only
    the caller has — the message body — to a `getMessage` hook, whose default
    is `async () => undefined` and whose source carries the matching TODO
    ("implement a cache to store the last 256 sent messages"). The sidecar
    passed no hook, so `sendMessagesAgain` logged "message not available" and
    relayed nothing.

    Observed on a live deployment across three sidecar restarts in ten
    minutes: the first two replies reached `read` on sessions fresh from
    pairing, and every reply after them stuck at `accepted` with the recipient
    showing "Waiting for this message. This may take a while." for ever. The
    self-heal WhatsApp designed for exactly that state was switched off.

    **In memory and nowhere else.** The value is a person's message body, and
    the session directory holds a full-account credential rather than a
    transcript — nothing in this program writes content to disk, and a
    resend cache is not the thing to start with. The cost is the residual in
    `whatsapp.md`: a retry arriving after a restart finds an empty cache.
    """

    _call = staticmethod(TestTheSidecarsPureFunctions._call)

    def test_a_sent_message_can_be_recalled_by_its_id(self):
        assert self._call(
            '(() => { m.rememberSent("A1", {conversation: "hi"});'
            ' return m.recallSent("A1"); })()'
        ) == {"conversation": "hi"}

    def test_an_unknown_id_recalls_nothing(self):
        """`undefined` rather than a stub: Baileys branches on falsy and
        relays nothing, which is the honest answer for a message this process
        never sent or no longer holds."""
        assert self._call('m.recallSent("nope") === undefined') is True

    @pytest.mark.parametrize("bad", ["null", '""', "42", "undefined"])
    def test_an_unusable_id_is_neither_stored_nor_recalled(self, bad):
        assert self._call(
            f'(() => {{ m.rememberSent({bad}, {{conversation: "hi"}});'
            f' return m.recallSent({bad}) === undefined; }})()'
        ) is True

    def test_a_send_with_no_content_is_not_cached(self):
        """A `sendMessage` result carrying no `message` is nothing to relay,
        and storing the absence would answer a retry with a falsy value the
        cache had to hold a slot for."""
        assert self._call(
            '(() => { m.rememberSent("A2", undefined);'
            ' return m.recallSent("A2") === undefined; })()'
        ) is True

    def test_the_cache_is_bounded_and_evicts_the_oldest(self):
        """Unbounded, this is a process that never restarts holding every
        message body it ever sent. 256 is Baileys' own number, from the TODO
        this implements."""
        limit = self._call("m.SENT_CACHE_LIMIT")
        assert limit == 256

        assert self._call(
            "(() => {"
            f" for (let i = 0; i < {limit} + 1; i++)"
            '  m.rememberSent("id" + i, {conversation: String(i)});'
            ' return {'
            '  first: m.recallSent("id0") === undefined,'
            f'  second: m.recallSent("id1") !== undefined,'
            f'  last: m.recallSent("id{limit}") !== undefined,'
            ' }; })()'
        ) == {"first": True, "second": True, "last": True}

    def test_re_sending_an_id_does_not_grow_the_cache(self):
        """A repeat of a known id refreshes rather than occupying a second
        slot, or a retry loop against one message evicts everything else."""
        assert self._call(
            '(() => { for (let i = 0; i < 400; i++)'
            '  m.rememberSent("same", {conversation: String(i)});'
            ' return m.recallSent("same"); })()'
        ) == {"conversation": "399"}


class TestTheReadmeSaysWhatIsNotCovered:
    """A coverage gap that is written down is a decision; one that is not is
    a reader assuming the tier covers something it cannot."""

    def test_it_names_the_manual_step(self):
        text = (SIDECAR_DIR / "README.md").read_text()

        assert "istota whatsapp pair" in text
        assert "real WhatsApp account" in text
