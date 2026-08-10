"""`messages.author_user_id` / `author_label` — the write path and the backfill.

A room bound to several surfaces is multi-human by construction, so a transcript
row with no author has to guess who wrote it, and the guess ("whoever is
reading") is wrong for every co-member and every external sender. The two
columns record it instead, decided once at ingest.

Two columns rather than one because the distinction is load-bearing: an istota
user id is an identity the system can resolve, while an external label is
attacker-supplied text that has to be sanitized before it is stored. Collapsing
them would move that decision to every reader.

The rendered result of all this is pinned in `test_web_email_turn_attribution`;
this file covers the storage.
"""

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.ingest import record_inbound, resolve_author


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        users={"alice": UserConfig(
            display_name="Alice", email_addresses=["alice@example.com"],
        )},
    )
    db.init_db(cfg.db_path)
    return cfg


def _author_of(conn, task_id):
    row = conn.execute(
        "SELECT author_user_id, author_label FROM messages "
        "WHERE task_id = ? AND role = 'user'",
        (task_id,),
    ).fetchone()
    assert row is not None, "no user row was stored"
    return row["author_user_id"], row["author_label"]


class TestResolveAuthor:
    """The pure resolution rule, before any storage."""

    def test_no_sender_is_the_istota_user(self, config):
        assert resolve_author(config, "alice", None) == ("alice", None)

    def test_external_sender_becomes_a_label_and_no_user(self, config):
        assert resolve_author(config, "alice", "contact@example.com") == (
            None, "contact@example.com",
        )

    def test_own_address_is_the_istota_user(self, config):
        assert resolve_author(config, "alice", "alice@example.com") == (
            "alice", None,
        )

    def test_display_name_never_survives_into_the_label(self, config):
        """The half of a `From:` header the sender chose is not an identity.

        A display name reaching `author_label` would be rendered verbatim as the
        speaker, which is precisely the impersonation `external_email_sender`
        exists to refuse — and storing it would move that refusal to every
        reader instead of holding it at the one write point.
        """
        _uid, label = resolve_author(
            config, "alice", '"Alice (your boss)" <contact@example.com>',
        )
        assert label == "contact@example.com"

    def test_an_unrenderable_address_becomes_the_sentinel(self, config):
        _uid, label = resolve_author(config, "alice", "<not an address>")
        assert label == db.UNATTRIBUTED_SENDER
        assert "not an address" not in (label or "")

    def test_an_unknown_user_still_resolves(self, config):
        # No config entry means no known addresses, so an email from anyone is
        # external. Fails toward naming the sender, never toward silently
        # crediting the account it was routed to.
        assert resolve_author(config, "carol", "x@example.com") == (
            None, "x@example.com",
        )


class TestWritePath:
    def test_a_web_turn_gets_the_user_id(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            _room, tid = record_inbound(
                conn, config, surface="web", surface_ref="rm",
                user_id="alice", text="hello",
            )
            assert _author_of(conn, tid) == ("alice", None)

    def test_a_talk_turn_gets_the_user_id(self, config):
        with db.get_db(config.db_path) as conn:
            _room, tid = record_inbound(
                conn, config, surface="talk", surface_ref="tk",
                user_id="alice", text="hello",
            )
            assert _author_of(conn, tid) == ("alice", None)

    def test_an_external_email_turn_gets_a_label_and_no_user_id(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            _room, tid = record_inbound(
                conn, config, surface="email", surface_ref="rm",
                user_id="alice", text="mail body",
                sender_address="contact@example.com",
            )
            assert _author_of(conn, tid) == (None, "contact@example.com")

    def test_an_email_from_the_users_own_address_gets_the_user_id(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            _room, tid = record_inbound(
                conn, config, surface="email", surface_ref="rm",
                user_id="alice", text="mail body",
                sender_address="Alice <alice@example.com>",
            )
            assert _author_of(conn, tid) == ("alice", None)

    def test_a_raw_from_header_never_reaches_the_column(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            _room, tid = record_inbound(
                conn, config, surface="email", surface_ref="rm",
                user_id="alice", text="mail body",
                sender_address='"Alice (your boss)" <contact@example.com>',
            )
            _uid, label = _author_of(conn, tid)
            assert label == "contact@example.com"

    def test_a_confirmation_answer_names_who_gave_it(self, config):
        """A `task_id IS NULL` row has no task to recover an identity from, so
        the author column is the only record of who answered. In a shared room
        that matters more than anywhere else: an authorization decision
        attributed to the wrong member is the worst row to get wrong."""
        from istota import confirmations

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            confirmations.record_exchange(
                conn, "rm", answer_text="yes", ack="Confirmed.",
                origin_surface="web", answered_by="bob",
            )
            user_row = conn.execute(
                "SELECT author_user_id, author_label FROM messages "
                "WHERE role = 'user' AND task_id IS NULL"
            ).fetchone()
            assert (user_row["author_user_id"], user_row["author_label"]) == (
                "bob", None,
            )
            # The ack is the bot's, and carries no author at all.
            ack_row = conn.execute(
                "SELECT author_user_id, author_label FROM messages "
                "WHERE role = 'system'"
            ).fetchone()
            assert (ack_row["author_user_id"], ack_row["author_label"]) == (
                None, None,
            )

    def test_attribution_failure_does_not_lose_the_message(self, config):
        """`resolve_author` runs inside the inbound transaction. A lookup that
        raises must cost the row its author, never cost the user their message.

        It must also not fail *open*: a message that arrived with a sender keeps
        the sentinel rather than falling back to nothing, because nothing renders
        as the room owner — a stranger's mail shown as the user's own words,
        which is the whole defect. A message with no sender is the user's by
        construction and stays theirs.
        """
        class Exploding:
            @property
            def users(self):
                raise RuntimeError("config blew up")

        assert resolve_author(Exploding(), "alice", "x@example.com") == (
            None, db.UNATTRIBUTED_SENDER,
        )
        assert resolve_author(Exploding(), "alice", None) == ("alice", None)


class TestApprovedGateMirror:
    def test_an_approved_email_turn_is_attributed_to_its_sender(self, config):
        """`confirmations.approve` writes the withheld user row on a path that
        never saw the inbound message, and has no `Config` — so it resolves the
        author from the DB. A row written there with no author would render the
        held mail as the room owner's own words, which is the defect the gate's
        own fix (ISSUE-136, re-reached) was about."""
        from istota import confirmations

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            tid = db.create_task(
                conn, "held mail body", "alice", source_type="email",
                conversation_token="rm",
            )
            db.mark_email_processed(
                conn, "uid-1", "contact@example.com", subject="Hi",
                user_id="alice", task_id=tid, routing_method="plus_address",
            )
            db.set_task_confirmation(conn, tid, "Process this email?")
            task = db.get_task(conn, tid)
            confirmations.approve(conn, task)
            assert _author_of(conn, tid) == (None, "contact@example.com")

    def test_a_sender_match_is_the_user_not_a_stranger(self, config):
        """`sender_match` is *defined* as the `From:` matching one of that
        user's configured addresses, so it answers the own-address question
        from a fact the router already recorded — which is what makes the
        DB-only resolver safe where config is out of reach."""
        from istota import confirmations

        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            tid = db.create_task(
                conn, "held mail body", "alice", source_type="email",
                conversation_token="rm",
            )
            db.mark_email_processed(
                conn, "uid-2", "alice@example.com", subject="Hi",
                user_id="alice", task_id=tid, routing_method="sender_match",
            )
            db.set_task_confirmation(conn, tid, "Process this email?")
            confirmations.approve(conn, db.get_task(conn, tid))
            assert _author_of(conn, tid) == ("alice", None)


class TestConfigFreeResolution:
    """`author_for_email_task` without a config — the approval mirror's fallback
    and the backfill's only option."""

    def _plus_address_self_mail(self, conn, sender="alice@example.com"):
        tid = db.create_task(
            conn, "own mail", "alice", source_type="email",
            conversation_token="rm",
        )
        db.mark_email_processed(
            conn, f"uid-{tid}", sender, subject="Hi", user_id="alice",
            task_id=tid, routing_method="plus_address",
        )
        return tid

    def test_config_addresses_win_when_supplied(self, config):
        """The authoritative list, and the reason every caller that can reach a
        config passes one."""
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            tid = self._plus_address_self_mail(conn)
            assert db.author_for_email_task(
                conn, tid, "alice", ["alice@example.com"],
            ) == ("alice", None)

    def test_a_profile_row_covers_the_plus_address_self_mail(self, config):
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_profiles (user_id, email_addresses) "
                "VALUES ('alice', '[\"alice@example.com\"]')"
            )
            db.register_room(conn, "rm", "alice", origin="web")
            tid = self._plus_address_self_mail(conn)
            assert db.author_for_email_task(conn, tid, "alice") == ("alice", None)

    def test_a_prior_sender_match_covers_it_with_no_profile_row(self, config):
        """The case that has no profile row at all: a deployment configured
        purely in TOML, which nothing seeds `user_profiles` from.

        Without this the user's own plus-address mail is labelled with their own
        address as an external speaker — and the backfill writes that
        permanently, behind a one-shot marker. A previously recorded
        `sender_match` is the router having already concluded the address is
        theirs, against the full config.
        """
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            # An earlier mail the router classified as the user's own.
            prior = db.create_task(
                conn, "earlier", "alice", source_type="email",
                conversation_token="rm",
            )
            db.mark_email_processed(
                conn, "uid-prior", "Alice <alice@example.com>", subject="x",
                user_id="alice", task_id=prior, routing_method="sender_match",
            )
            tid = self._plus_address_self_mail(conn)
            assert db.author_for_email_task(conn, tid, "alice") == ("alice", None)

    def test_a_genuine_stranger_is_still_labelled(self, config):
        """The recovery must not become a blanket "everything is the user"."""
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm", "alice", origin="web")
            prior = db.create_task(
                conn, "earlier", "alice", source_type="email",
                conversation_token="rm",
            )
            db.mark_email_processed(
                conn, "uid-prior", "alice@example.com", subject="x",
                user_id="alice", task_id=prior, routing_method="sender_match",
            )
            tid = self._plus_address_self_mail(conn, sender="evil@elsewhere.test")
            assert db.author_for_email_task(conn, tid, "alice") == (
                None, "evil@elsewhere.test",
            )


class TestBackfill:
    """`messages_author_v1` — rows written before the columns existed."""

    def _legacy_rows(self, config):
        """A transcript as it looked before this migration: user rows with both
        author columns NULL."""
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_profiles (user_id, email_addresses) "
                "VALUES ('alice', '[\"alice@example.com\"]')"
            )
            db.register_room(conn, "rm", "alice", origin="web")
            web_tid = db.create_task(
                conn, "a web turn", "alice", source_type="web",
                conversation_token="rm",
            )
            db.add_message(
                conn, "rm", role="user", body="a web turn",
                origin_surface="web", task_id=web_tid,
            )
            ext_tid = db.create_task(
                conn, "external mail", "alice", source_type="email",
                conversation_token="rm",
            )
            db.mark_email_processed(
                conn, "uid-ext", '"Boss" <contact@example.com>', subject="Hi",
                user_id="alice", task_id=ext_tid,
                routing_method="thread_match",
            )
            db.add_message(
                conn, "rm", role="user", body="external mail",
                origin_surface="email", task_id=ext_tid,
            )
            own_tid = db.create_task(
                conn, "own mail", "alice", source_type="email",
                conversation_token="rm",
            )
            db.mark_email_processed(
                conn, "uid-own", "alice@example.com", subject="Hi",
                user_id="alice", task_id=own_tid,
                routing_method="plus_address",
            )
            db.add_message(
                conn, "rm", role="user", body="own mail",
                origin_surface="email", task_id=own_tid,
            )
            # The confirmation-exchange shape: no task at all.
            db.add_message(
                conn, "rm", role="user", body="yes", origin_surface="web",
            )
            db.add_message(
                conn, "rm", role="assistant", body="an answer",
                origin_surface="web", task_id=web_tid,
            )
            # Re-arm: `init_db` already ran the migration over an empty table.
            conn.execute(
                "DELETE FROM _migration_state WHERE name = 'messages_author_v1'"
            )
        return web_tid, ext_tid, own_tid

    def _run(self, config):
        with db.get_db(config.db_path) as conn:
            db._migrate_messages_author(conn)

    def test_it_sets_both_classes_correctly(self, config):
        web_tid, ext_tid, own_tid = self._legacy_rows(config)
        self._run(config)
        with db.get_db(config.db_path) as conn:
            assert _author_of(conn, web_tid) == ("alice", None)
            assert _author_of(conn, ext_tid) == (None, "contact@example.com")
            # Routed `plus_address`, so the routing method says nothing; the
            # address matching the profile row is what identifies the user.
            assert _author_of(conn, own_tid) == ("alice", None)

    def test_a_taskless_row_keeps_both_null(self, config):
        self._legacy_rows(config)
        self._run(config)
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT author_user_id, author_label FROM messages "
                "WHERE role = 'user' AND task_id IS NULL"
            ).fetchone()
            assert (row["author_user_id"], row["author_label"]) == (None, None)

    def test_assistant_rows_are_left_alone(self, config):
        self._legacy_rows(config)
        self._run(config)
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT author_user_id, author_label FROM messages "
                "WHERE role = 'assistant'"
            ).fetchone()
            assert (row["author_user_id"], row["author_label"]) == (None, None)

    def test_it_is_idempotent(self, config):
        web_tid, ext_tid, own_tid = self._legacy_rows(config)
        self._run(config)
        with db.get_db(config.db_path) as conn:
            before = conn.execute(
                "SELECT id, author_user_id, author_label FROM messages ORDER BY id"
            ).fetchall()
            before = [tuple(r) for r in before]
        # Second run: the marker short-circuits it, and re-arming it and running
        # again still lands on the same answer — the migration is scoped to rows
        # that are still unattributed.
        self._run(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "DELETE FROM _migration_state WHERE name = 'messages_author_v1'"
            )
        self._run(config)
        with db.get_db(config.db_path) as conn:
            after = conn.execute(
                "SELECT id, author_user_id, author_label FROM messages ORDER BY id"
            ).fetchall()
            assert [tuple(r) for r in after] == before

    def test_a_pruned_task_does_not_cost_the_sender_their_attribution(
        self, config,
    ):
        """`messages` is never age-pruned; `tasks` is, at 7 days by default.

        `cleanup_old_processed_emails` deliberately keeps a ledger row a message
        still references, precisely so an email turn's attribution outlives its
        task. Joining `tasks` here would drop the label for every turn older
        than a week — and permanently, the marker being one-shot.
        """
        _web, ext_tid, _own = self._legacy_rows(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (ext_tid,))
        self._run(config)
        with db.get_db(config.db_path) as conn:
            assert _author_of(conn, ext_tid) == (None, "contact@example.com")

    def test_two_ledger_rows_for_one_task_pick_the_oldest(self, config):
        """`processed_emails.task_id` is not unique, so the join fans out.

        The oldest row is the one `EMAIL_SENDER_SUBQUERY` picks, and the two
        readers must agree about who sent a message.
        """
        _web, ext_tid, _own = self._legacy_rows(config)
        with db.get_db(config.db_path) as conn:
            db.mark_email_processed(
                conn, "uid-ext-2", "later@example.com", subject="Hi",
                user_id="alice", task_id=ext_tid, routing_method="thread_match",
            )
        self._run(config)
        with db.get_db(config.db_path) as conn:
            assert _author_of(conn, ext_tid) == (None, "contact@example.com")
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE task_id = ? "
                "AND role = 'user'",
                (ext_tid,),
            ).fetchone()
            assert rows["n"] == 1

    def test_the_marker_is_written_on_success(self, config):
        self._legacy_rows(config)
        self._run(config)
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM _migration_state WHERE name = 'messages_author_v1'"
            ).fetchone() is not None

    def test_a_partially_backfilled_table_still_renders(self, config):
        """The migration re-arms on failure, so a half-done table is a state the
        app runs in. A NULL author falls back to the room owner, which is what
        every row did before the columns existed."""
        web_tid, ext_tid, _own = self._legacy_rows(config)
        with db.get_db(config.db_path) as conn:
            # Attribute one row, leave the rest — the shape a failed pass leaves.
            conn.execute(
                "UPDATE messages SET author_user_id = 'alice' WHERE task_id = ?",
                (web_tid,),
            )
        with db.get_db(config.db_path) as conn:
            assert _author_of(conn, web_tid) == ("alice", None)
            assert _author_of(conn, ext_tid) == (None, None)
        # And finishing the job attributes the rest without disturbing the first.
        self._run(config)
        with db.get_db(config.db_path) as conn:
            assert _author_of(conn, web_tid) == ("alice", None)
            assert _author_of(conn, ext_tid) == (None, "contact@example.com")
