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
import re
import shutil
import subprocess
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
    "package-lock.json": "TestTheLockfileInstallsThePinnedLibrary",
    "Dockerfile": "TestTheImageCopiesWhatTheProgramNeeds",
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


def _js_method(name: str) -> str:
    """The body of one method of the sidecar's `Session` class, as text."""
    source = PROGRAM.read_text()
    start = source.index(f"  {name}(")
    end = source.index("\n  }\n", start)
    return source[start:end]


def _js_send_keys(message_const: str) -> set[str]:
    """The payload keys of one ``this.link.send(MSG_X, { ... })`` call.

    A brace walk rather than a regular expression: the object literals here
    hold nested calls and ternaries, and a non-greedy `{.*?}` stops at the
    first inner brace. Only top-level keys are collected, which is the level
    the daemon's normalizers read.
    """
    source = PROGRAM.read_text()
    marker = f"this.link.send({message_const}, {{"
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

    def test_an_inbound_payload_normalizes(self):
        keys = _js_send_keys("MSG_INBOUND")
        payload = self._filled(keys, {
            "message_id": "BAE5F00D",
            "jid": "15551234567@s.whatsapp.net",
            "username": "Alice",
            "message_type": "text",
            "text": "check the backup",
            "callback_data": None,
            "reply_to_message_id": None,
            "group": False,
            "timestamp": 1757000000,
        })

        event = proto.inbound_event(payload)

        assert event.from_user.jid == "15551234567@s.whatsapp.net"
        assert event.text == "check the backup"

    def test_the_group_flag_is_read_from_the_key_the_sidecar_sends(self):
        """The one inbound field with a *behavioural* reader rather than a
        stored one: it types the message before any identity lookup, so a
        rename does not merely drop a field, it admits a group message."""
        keys = _js_send_keys("MSG_INBOUND")
        assert "group" in keys

        payload = self._filled(keys, {
            "message_id": "BAE5F00D",
            "jid": "15551234567@s.whatsapp.net",
            "username": None,
            "message_type": "text",
            "text": "hello",
            "callback_data": None,
            "reply_to_message_id": None,
            "group": True,
            "timestamp": 1757000000,
        })

        assert proto.inbound_event(payload).message_type == "group"

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

    def test_a_fatal_payload_carries_the_two_fields_the_bridge_branches_on(self):
        source = PROGRAM.read_text()
        fatals = re.findall(r"MSG_FATAL, \{ ([^}]*) \}", source)
        assert fatals, "no fatal frames found"
        for body in fatals:
            keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*):", body))
            assert keys == {"reason", "permanent"}, keys

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
        thread and a write transaction that then resolves no sender."""
        assert "isForwardableJid(jid)" in _js_method("onMessages")
        assert "@s.whatsapp.net" in _js_const("USER_JID_DOMAIN")

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
        shape: systemd restarts it and its `ready` clears the latch."""
        # The *use* rather than the declaration — the constant is declared at
        # the top of the file, hundreds of lines from the branch that sends it.
        source = PROGRAM.read_text()
        index = source.index("reason: FATAL_LOGGED_OUT")

        assert "process.exit(1)" in source[index:index + 900]

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
        """They decide what the image and the role have to carry.

        `libsignal` is Baileys' cryptography and the eslint config is one that
        package declares under `dependencies` rather than `devDependencies`,
        so `--omit=dev` keeps both and `npm ci` shells out to git for both.
        That is why the Dockerfile installs git and why both install paths
        rewrite GitHub's ssh URL to https. A third one appearing, or these two
        becoming registry packages, changes what those two files need.
        """
        git_deps = sorted(
            name.removeprefix("node_modules/")
            for name, entry in self._lock()["packages"].items()
            if str(entry.get("resolved", "")).startswith("git+")
        )

        assert git_deps == ["@whiskeysockets/eslint-config", "libsignal"]

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

    def test_it_can_install_the_git_dependencies(self):
        """Two runtime dependencies come from git, so two things are needed.

        The slim base carries no git at all, so the install step fails outright
        without it. And the lockfile records those two as
        `git+ssh://git@github.com/…`, which GitHub serves only to an
        authenticated key — a build container has none, so git has to be told
        to reach the same repositories over https. Both are invisible until a
        build runs, and no tier here builds this image.
        """
        directives = self._directives()

        assert re.search(r"apt-get install[^\n]*\bgit\b", directives)
        assert "url.https://github.com/.insteadOf" in directives
        assert "ssh://git@github.com/" in directives

    def test_it_runs_the_entry_point_the_bridge_names(self):
        from istota.transport.whatsapp.baileys_bridge import SIDECAR_ENTRY

        assert f'CMD ["node", "/app/{SIDECAR_ENTRY}"]' in self._dockerfile()

    def test_it_declares_no_user(self):
        """`ensure_session_dir` refuses a session directory owned by another
        uid rather than adopting it, so the sidecar and the daemon have to run
        as the same user — and the istota image declares no USER either. A
        `USER node` here builds cleanly and then cannot read the credential
        the daemon paired."""
        assert not re.search(r"^USER\s", self._directives(), re.M)


class TestTheReadmeSaysWhatIsNotCovered:
    """A coverage gap that is written down is a decision; one that is not is
    a reader assuming the tier covers something it cannot."""

    def test_it_names_the_manual_step(self):
        text = (SIDECAR_DIR / "README.md").read_text()

        assert "istota whatsapp pair" in text
        assert "real WhatsApp account" in text
