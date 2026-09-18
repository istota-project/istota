"""The two readers of the pairing request row, held in step.

`web_app._pairing_state_payload` and `cli._whatsapp_pairing_view` join the same
singleton row to the same relay file, by the same rule, in two processes. The
rule is six lines and the copies are deliberate; `AGENTS.md` asks a deliberate
duplicate to say which copy is authoritative and what holds the two in step, so
this file is the second half of that.

**The web copy is authoritative.** It landed with the routes, and the
reader-side terminal check was specified against it — Stage 3's review routed
that check to Stage 4 as defence in depth behind the poll's cancel, and the
CLI's follower inherited it a stage later.

**What they share**, asserted below against one row and one relay file:

- the row is read first, and its terminal state is a veto on the relay. The
  relay carries the *window's* deadline, which is later than the request row's,
  so a window republishing after its row closed passes `read_relay`'s own
  deadline check — only the row can settle it.
- the relay is matched on the row's **current** `window_id`, which is the
  bridge's own after the `awaiting_sidecar` adoption rather than the id
  `request_whatsapp_pairing` returned.
- a live window's `state` and `message` win over the row's mirrored ones.

**What differs, and why the copies are not one function:**

- **The return shape.** The web payload reduces the code to `qr_available`, a
  boolean; the CLI returns the payload itself, because a terminal is where an
  operator asked for it. This is *not* "the payload never enters the web
  process" — `_pairing_state_payload` reads `live.get("qr")` to compute that
  boolean — it is that the payload never leaves it: a shared function returning
  the code would put a full-account credential in the frame that builds a
  route's response body.
- **The deadline.** The web prefers the relay's `expires_at` where a window is
  publishing; the CLI always takes the row's. Post-adoption the two agree,
  since the bridge's outcome write stamps the window's own deadline onto the
  row.
- **Coercion.** The web runs `qr_seq` and `expires_at` through `_as_int` /
  `_as_float` because its callers are routes with no exception wrapper; the CLI
  narrows only `qr`, since it is the one field it draws.
- **Extra fields.** The CLI carries `requested_at` / `requested_by` for its
  follower's identity pin; the web carries `row_state`, `force` and
  `qr_available` for the card.

A parity test rather than a shared function is the project's own instrument for
this — `tests/test_delivery_parking_parity.py` on the two delivery ledgers,
`usageFormat.parity.test.ts` on the two cost renderers.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from istota import cli, db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import pairing_relay
from istota.transport.whatsapp.baileys_bridge import (
    PAIRING_RELAY_NAME, default_pairing_relay_path,
)

from .support.whatsapp_config import build_whatsapp_config

BAILEYS = "baileys"


@pytest.fixture
def config(tmp_path) -> Config:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    built = Config(
        db_path=state / "istota.db",
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(enabled=True, provider=BAILEYS),
        users={"alice": UserConfig()},
    )
    db.init_db(built.db_path)
    # The relay both readers resolve. Asked of the product rather than spelled,
    # so a change to that rule fails here rather than leaving each reader
    # looking at a different file.
    assert default_pairing_relay_path(built) == state / PAIRING_RELAY_NAME
    return built


@pytest.fixture
def web(config, monkeypatch):
    """`web_app` with its module-global config pointed at ours.

    The route module reads `_config` rather than taking one, which is why this
    is a patch rather than an argument — `tests/test_whatsapp_pairing_web.py`
    does the same thing for the same reason.
    """
    import istota.web_app as mod

    monkeypatch.setattr(mod, "_config", config)
    return mod


def _request(config, *, user="admin") -> str:
    with db.get_db(config.db_path) as conn:
        request_id = db.request_whatsapp_pairing(conn, user)
    assert request_id is not None
    return request_id


def _move_to(config, window_id, state, message=None, **kw) -> None:
    with db.get_db(config.db_path) as conn:
        assert db.record_whatsapp_pairing_state(
            conn, window_id, state, message, **kw
        )


def _publish(config, window_id, state, *, qr=None, qr_seq=0, message="") -> None:
    pairing_relay.write_relay(
        default_pairing_relay_path(config),
        pairing_relay.build_payload(
            window_id=window_id,
            state=state,
            expires_at=time.time() + 300.0,
            qr=qr,
            qr_seq=qr_seq,
            message=message,
        ),
    )


def _both(config, web):
    return cli._whatsapp_pairing_view(config), web._pairing_state_payload()


class TestTheSharedRule:
    def test_no_row_reads_as_nothing_on_both_sides(self, config, web):
        assert _both(config, web) == (None, None)

    def test_a_relay_from_another_window_is_ignored_by_both(self, config, web):
        request_id = _request(config)
        _publish(
            config, "a-window-nobody-owns", "awaiting_scan",
            qr="2@STALE==", qr_seq=4,
        )

        view, payload = _both(config, web)

        assert view["window_id"] == payload["window_id"] == request_id
        assert view["state"] == payload["state"] == "requested"
        assert view["qr"] == ""
        assert payload["qr_available"] is False

    def test_both_read_the_relay_against_the_adopted_window_id(
        self, config, web,
    ):
        """The id the request returned is not the id the relay carries. A
        reader pinned to the first stops seeing the relay at the transition
        that happens on every real pairing."""
        request_id = _request(config)
        _move_to(
            config, request_id, "awaiting_sidecar",
            adopt_window_id="bridge-window", expires_at=time.time() + 300.0,
        )
        _publish(
            config, "bridge-window", "awaiting_scan",
            qr="2@LIVE==", qr_seq=3, message="scan it",
        )

        view, payload = _both(config, web)

        assert view["window_id"] == payload["window_id"] == "bridge-window"
        # The live window's state and message win over the row's mirrored ones
        # on both sides.
        assert view["state"] == payload["state"] == "awaiting_scan"
        assert view["message"] == payload["message"] == "scan it"
        # The one deliberate divergence in what each returns.
        assert view["qr"] == "2@LIVE=="
        assert payload["qr_available"] is True
        assert "qr" not in payload

    def test_a_terminal_row_vetoes_a_publishing_relay_on_both_sides(
        self, config, web,
    ):
        """The relay carries the window's own deadline, which outlives the
        row's, so a window republishing after its row closed passes
        `read_relay`'s deadline check. Only the row settles it."""
        request_id = _request(config)
        _move_to(config, request_id, "failed", "moved aside to .old-2026")
        _publish(
            config, request_id, "awaiting_scan", qr="2@ORPHAN==", qr_seq=9,
        )

        view, payload = _both(config, web)

        assert view["terminal"] is payload["terminal"] is True
        assert view["state"] == payload["state"] == "failed"
        # The row's message on both, which is where the archived path lives.
        assert view["message"] == payload["message"] == "moved aside to .old-2026"
        assert view["qr"] == ""
        assert payload["qr_available"] is False


class TestTheStatedDivergences:
    """Each asserted, so a later change that collapses one is a visible
    decision rather than a silent drift from this file's prose."""

    def test_only_the_cli_returns_the_payload(self, config, web):
        request_id = _request(config)
        _publish(config, request_id, "awaiting_scan", qr="2@CODE==", qr_seq=1)

        view, payload = _both(config, web)

        assert view["qr"] == "2@CODE=="
        assert "2@CODE==" not in repr(payload)

    def test_only_the_web_prefers_the_relays_deadline(self, config, web):
        """Reachable only before the adoption write stamps the window's own
        deadline onto the row, which is the one span where the two differ."""
        request_id = _request(config)
        row_deadline = cli._whatsapp_pairing_view(config)["expires_at"]
        relay_deadline = row_deadline + 600.0
        pairing_relay.write_relay(
            default_pairing_relay_path(config),
            pairing_relay.build_payload(
                window_id=request_id,
                state="awaiting_sidecar",
                expires_at=relay_deadline,
            ),
        )

        view, payload = _both(config, web)

        assert view["expires_at"] == pytest.approx(row_deadline)
        assert payload["expires_at_epoch"] == pytest.approx(relay_deadline)

    def test_only_the_cli_carries_the_request_stamp(self, config, web):
        _request(config, user="cli:operator")

        view, payload = _both(config, web)

        assert view["requested_by"] == "cli:operator"
        assert view["requested_at"]
        # The web's own extras, which the card reads and the follower does not.
        assert payload["force"] is False
        assert "qr_available" in payload

    def test_only_the_cli_narrows_the_payload_to_a_string(self, config, web):
        """`read_relay` type-checks `window_id`, `state` and `expires_at` and
        passes the rest through as the file held it. The CLI draws `qr`, so it
        narrows that one; the web reduces it to a boolean and narrows the two
        numbers instead."""
        request_id = _request(config)
        path = default_pairing_relay_path(config)
        path.write_bytes(
            b'{"window_id": "' + request_id.encode() + b'", '
            b'"state": "awaiting_scan", "expires_at": '
            + str(time.time() + 300.0).encode()
            + b', "qr": {"not": "a string"}, "qr_seq": "3", "message": ""}'
        )

        view, payload = _both(config, web)

        assert view["qr"] == ""
        assert isinstance(payload["qr_seq"], int)
        assert payload["qr_seq"] == 3


def test_the_two_readers_live_where_this_file_says_they_do():
    """A drift guard on the subjects themselves.

    Both are private module functions, so a rename or a move makes every
    assertion above vacuous by import error rather than by silence — but the
    docstring's claim about *which* copy is authoritative is prose, and this at
    least keeps the pair of names honest.
    """
    import istota.web_app as mod

    assert callable(mod._pairing_state_payload)
    assert callable(cli._whatsapp_pairing_view)
    assert Path(cli.__file__).name == "cli.py"
