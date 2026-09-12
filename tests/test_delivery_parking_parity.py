"""What holds the two delivery-parking implementations in step.

Both metered push surfaces settle a ledger row with a provider-minted message
id written one statement after the send returns, and both therefore have the
same race: a status callback arriving in that window matches no row. WhatsApp
was fixed first (ISSUE-490) and **is the authoritative copy**; the SMS version
in `transport/sms/outbound.py` is a port of it.

They are not consolidated, and that is a decision rather than an omission.
Four things genuinely differ — the key (a single id against a
`(provider, id)` pair), the in-flight gate (deployment-wide against
provider-scoped, which only SMS can do because its event names its provider),
the alert shape (a tuple against a single alert), and whether the stored id is
fingerprinted. A shared implementation would have to be parameterised over all
four, which is how a consolidation ends up harder to read than the two copies
it replaced. `transport/_alerts.py` is the counter-example that was worth
sharing: there the two copies differed only in a surface name.

What this file buys is that the *set* of moving parts cannot drift. Each
surface's behaviour is pinned by its own tests — the park gate, the prune, the
replay through the monotonic ladder, the terminal guard and the two-thread race
all have per-surface cases in `test_sms_core.py` and `test_whatsapp_delivery.py`.
What those cannot see is one surface quietly losing a piece the other keeps, so
that is what is asserted here.
"""

from __future__ import annotations

import sqlite3

import pytest

from istota import db
from istota.transport.sms import outbound as sms_outbound
from istota.transport.whatsapp import outbound as whatsapp_outbound

#: The parts each surface's parking is built from. A rename or a deletion on
#: one side alone fails here by name rather than going unnoticed until the
#: next status callback is dropped.
PARKING_PARTS = (
    "_send_in_flight",
    "_prune_parked_statuses",
    "_park_status",
    "_parked_event",
    "_drain_parked_statuses",
)

#: What every parked row has to carry whatever the surface: which status, why
#: it failed, and when it was held so the window can expire it.
SHARED_PARKED_COLUMNS = frozenset({"id", "status", "error_code", "parked_at"})


@pytest.mark.parametrize("module", [sms_outbound, whatsapp_outbound])
@pytest.mark.parametrize("part", PARKING_PARTS)
def test_both_surfaces_carry_every_parking_part(module, part):
    assert hasattr(module, part), (
        f"{module.__name__} has no {part}; the two delivery-parking "
        f"implementations have drifted. WhatsApp is the authoritative copy."
    )


@pytest.mark.parametrize(
    "table", ["sms_parked_status", "whatsapp_parked_status"],
)
def test_both_parking_tables_exist_and_share_their_core_columns(table, tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        }
    assert columns, f"{table} was not created by init_db"
    missing = SHARED_PARKED_COLUMNS - columns
    assert not missing, f"{table} is missing {sorted(missing)}"


def test_neither_surface_drains_inside_the_transaction_that_settles():
    """The one ordering rule both copies rest on, stated where both can see it.

    Draining inside the settle's own transaction reads better and is wrong on
    both surfaces for the same reason: `db.get_db` commits only on a clean
    exit, so a raise out of a replay unwinds the message-id write with it, and
    a message the provider accepted whose id was never recorded can never be
    matched by a later status. Each drain therefore opens its own connection,
    which is what taking a config rather than a connection encodes.

    Asserted on the signature because that is the property's visible form: a
    drain handed a live connection would necessarily share its transaction.
    """
    import inspect

    for module, first in (
        (sms_outbound, "config"),
        (whatsapp_outbound, "config"),
    ):
        parameters = list(
            inspect.signature(module._drain_parked_statuses).parameters
        )
        assert parameters[0] == first, (
            f"{module.__name__}._drain_parked_statuses takes "
            f"{parameters[0]!r} first; it must take the config and open its "
            f"own transaction rather than share the settle's."
        )
