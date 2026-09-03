"""The table is authoritative at the sites the equivalence pins cannot reach.

`tests/test_surface_model_equivalence.py` proves the conversion changed no
answer, and it is the stronger instrument for that. What it cannot prove is
that the **product** reads the table at all: by its own docstring, both sides of
every comparison there are callables written in the test file, and its tie to
the code is a source anchor — a substring check, a strong one, but a substring
check. The other half was Stage 6's negative control, run by deleting `talk`'s
row from `surfaces.SURFACES` and requiring the converted sites to go red.

That control turned four of the seven sites red through existing suites. Three
were unmoved by it, not because they ignore the table but because nothing in
the tree exercises them in a way that distinguishes the answer:

- `routing._room_for_destination` — no suite resolves a *promoted* room's Talk
  ref through this branch, which is the only shape where resolving and not
  resolving differ.
- `web_app._user_row_display` — no suite renders a talk-origin row that also
  carries an `author_label`, which is the second condition the marker needs.
- `scheduler`'s `store_turn_message` gate — the assistant ROW is not a
  discriminator, because `_store_room_turn` is a second producer of the same row
  and writes it either way. That is why `test_scheduler_assistant_stamp.py`
  stayed green under the control. The gate's own observable output is
  `stored_assistant_msg_id`, which rides the terminal `done` event as `msg_id`.

So this file makes those three durable. Each case asserts today's answer AND
the answer with `talk` removed, in the same class: a probe that only asserts
today's answer would pass against a site still reading a stale literal, which is
the entire failure mode being guarded. Removing a row rather than adding one is
deliberate — it is the shape of the spec's own negative control, and a site
reading a literal cannot notice a removal any more than an addition.

The two constant-agreement guards at the bottom are a different concern: they
protect the shared `db.TRANSCRIPT_SURFACES` tuple from being widened for a
reason that belongs to one of its two consumers.
"""

import json
from unittest.mock import patch

import pytest

from istota import db, surfaces, web_app
from istota.transport import routing
from istota.transport.routing import Destination


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "rooms.db"
    db.init_db(path)
    with db.get_db(path) as c:
        yield c


def _promoted(conn, user_id="testuser"):
    """A room created on web and later bound to Talk — the ISSUE-400 shape,
    and the only one where a canonical token and a Talk ref differ."""
    db.register_room(conn, "web-abc", origin="web", user_id=user_id, name="R")
    db.add_room_binding(conn, "web-abc", "web", "web-abc")
    db.add_room_binding(conn, "web-abc", "talk", "tlk123")
    return "web-abc", "tlk123"


class TestRoutingResolvesAMemberSurfaceThroughItsBinding:
    """`_room_for_destination`'s binding branch reads `is_room_member`."""

    def test_a_talk_ref_resolves_to_the_canonical_token(self, conn):
        canonical, talk_ref = _promoted(conn)
        got = routing._room_for_destination(
            conn, object(), "testuser", Destination("talk", talk_ref, talk_ref),
        )
        assert got == canonical

    def test_and_stops_resolving_when_talk_is_not_a_member(self, conn, monkeypatch):
        _, talk_ref = _promoted(conn)
        monkeypatch.delitem(surfaces.SURFACES, "talk")
        # Unresolved, the raw ref names no registered room, so the destination
        # names none — which is what proves the branch consulted the table.
        got = routing._room_for_destination(
            conn, object(), "testuser", Destination("talk", talk_ref, talk_ref),
        )
        assert got is None


class TestTheForeignMarkerReadsTheTable:
    """`web_app._user_row_display` reads `is_room_member` **negated**, so it is
    the one site where an unrecognised surface is the unsafe answer."""

    ROW = {
        "body": "hello",
        "author_user_id": None,
        "author_label": "stranger@example.com",
        "origin_surface": "talk",
    }

    def test_a_talk_row_is_not_marked_foreign(self):
        assert "origin" not in web_app._user_row_display(self.ROW)

    def test_and_is_marked_foreign_when_talk_is_not_a_member(self, monkeypatch):
        monkeypatch.delitem(surfaces.SURFACES, "talk")
        assert web_app._user_row_display(self.ROW)["origin"] == "talk"


class TestTheConversationalStoreGateReadsTheTable:
    """The `store_turn_message` gate, end to end through `process_one_task`.

    Asserted on `msg_id` of the terminal `done` event rather than on the
    assistant row, because `_store_room_turn` writes that row either way.
    """

    def _done_payload(self, db_path, tmp_path, *, drop_talk, monkeypatch):
        from istota.scheduler import process_one_task
        from tests.test_scheduler_assistant_stamp import _make_config

        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpzpcfx2", "testuser", origin="talk")
            db.add_room_binding(conn, "cpzpcfx2", "talk", "cpzpcfx2")
            task_id = db.create_task(
                conn, prompt="q", user_id="testuser",
                source_type="talk", conversation_token="cpzpcfx2",
            )
        if drop_talk:
            monkeypatch.delitem(surfaces.SURFACES, "talk")
        with patch("istota.scheduler.post_result_to_talk", return_value=93602), \
                patch("istota.scheduler.run_coro", return_value=93602), \
                patch(
                    "istota.scheduler.execute_task",
                    return_value=(True, "an answer.", None, None),
                ):
            process_one_task(config)
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'done' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        assert row is not None, "the task produced no terminal event"
        return json.loads(row["payload"])

    def test_a_talk_task_stamps_its_stored_row_on_the_done_event(
        self, db_path, tmp_path, monkeypatch,
    ):
        payload = self._done_payload(
            db_path, tmp_path, drop_talk=False, monkeypatch=monkeypatch,
        )
        assert "msg_id" in payload

    def test_and_does_not_when_talk_is_not_a_member(
        self, db_path, tmp_path, monkeypatch,
    ):
        payload = self._done_payload(
            db_path, tmp_path, drop_talk=True, monkeypatch=monkeypatch,
        )
        assert "msg_id" not in payload


class TestTheSharedTranscriptTupleIsNotWidenedForOneConsumer:
    """`db.TRANSCRIPT_SURFACES` now has two consumers reading two domains.

    `db`'s own SQL filters `messages.origin_surface`, whose domain is
    `source_type` values; `commands._record_confirm_exchange` tests it against
    `ctx.surface`, a registry surface name. They coincide today, and the
    declaration comment names only the first — so a maintainer widening the
    tuple for a source-type reason (the obvious one being to close the gap with
    the `scheduled` value in the migration DELETE) would, in the same edit,
    authorize a `role='user'` transcript write from a surface nobody looked at.
    Before the tuple was shared, that edit was local to one file.
    """

    def test_every_member_is_a_real_surface_name(self):
        assert set(db.TRANSCRIPT_SURFACES) <= set(surfaces.SURFACES), (
            "a value that is not a surface name would reach "
            "`commands._record_confirm_exchange`'s gate, which tests it "
            "against `ctx.surface`"
        )

    def test_every_member_may_deposit_a_user_row(self):
        # The question the tuple asks: member plus guest, never `None`.
        for name in db.TRANSCRIPT_SURFACES:
            assert surfaces.room_role(name) in ("member", "guest"), name

    def test_it_is_a_superset_relation_with_the_migration_delete(self):
        # `_migrate_nonconversational_transcript_cleanup`'s DELETE spares this
        # set plus `scheduled`. The relation is asserted in prose at both ends
        # and by nothing executable, and the two disagreeing is how live email
        # turns get silently swept (the comment on the DELETE says so).
        #
        # **Read off the statements, never off the raw source**, and the
        # control is what forced that. The first cut split the function text on
        # the first `origin_surface NOT IN (` and matched the *docstring*, which
        # restates the same four values — so removing `'email'` from the real
        # DELETE left this green. `ast.unparse` over the body with the docstring
        # dropped also folds the implicit string concatenation into the one SQL
        # string the database actually receives.
        import ast
        import inspect
        import textwrap

        fn = db._migrate_nonconversational_transcript_cleanup
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        body = node.body
        if ast.get_docstring(node) is not None:
            body = body[1:]
        statements = "\n".join(ast.unparse(n) for n in body)
        assert "origin_surface NOT IN (" in statements
        spared = statements.split("origin_surface NOT IN (")[1].split(")")[0]
        for name in db.TRANSCRIPT_SURFACES:
            assert f"'{name}'" in spared, (
                f"{name} is rendered by TRANSCRIPT_SURFACE_FILTER but the "
                f"migration DELETE would sweep it; spared = ({spared})"
            )

    def test_commands_reads_the_declaration_rather_than_a_copy(self):
        from istota import commands

        assert commands._TRANSCRIPT_SURFACES is db.TRANSCRIPT_SURFACES
