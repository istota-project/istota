"""The web routes over held outbound mail.

Stage 5 of the outbound-approval spec. The store and the gate are tested in
`tests/test_outbound_drafts.py` and `tests/test_outbound_gate.py`; what is
specific here is the HTTP boundary — who may address a draft, what an edit is
allowed to change, and which failures must not be offered a retry.

The ownership rule is the load-bearing one. A draft holds a body, a recipient
list and a subject the user has not yet decided to send, so a route that answers
403 for someone else's id is an existence oracle over exactly that. Every route
answers 404 instead, indistinguishable from an id that was never issued.
"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

from istota import db, outbound_drafts
from istota.config import Config, EmailConfig, SiteConfig, UserConfig, WebConfig

ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path, db_path, *, floor="untrusted", user_setting=""):
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        email=EmailConfig(
            enabled=True,
            smtp_host="smtp.example.com",
            bot_email="bot@example.com",
            outbound_approval_floor=floor,
        ),
        users={
            "alice": UserConfig(
                display_name="Alice",
                email_addresses=["alice@example.com"],
                outbound_approval=user_setting,
            ),
            "mallory": UserConfig(display_name="Mallory"),
        },
        web=WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mock_oauth = MagicMock()
    mock_oauth.nextcloud = MagicMock()
    mod._oauth = mock_oauth
    return mod.app


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(tmp_path, db_path):
    return _make_config(tmp_path, db_path)


@pytest.fixture
def app(config):
    return _patch_app(config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username="alice"):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
        "user_id": username,
    })
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _hold(db_path, *, user_id="alice", room_token=None, task_id=None,
          to=("stranger@example.invalid",), subject="Re: Invite",
          body="Wednesday at two works.", cc=(), bcc=(), attachments=()):
    with db.get_db(db_path) as conn:
        draft_id = outbound_drafts.hold(
            conn,
            user_id=user_id,
            task_id=task_id,
            room_token=room_token,
            to_addrs=list(to),
            cc_addrs=list(cc),
            bcc_addrs=list(bcc),
            subject=subject,
            body=body,
            html=False,
            in_reply_to="<parent@example.invalid>",
            references="<parent@example.invalid>",
            attachments=list(attachments),
            origin_target="talk:rm1",
            hold_reason="untrusted_recipient",
        )
        conn.commit()
    return draft_id


def _status(db_path, draft_id):
    with db.get_db(db_path) as conn:
        draft = outbound_drafts.get(conn, draft_id)
    return draft.status if draft else None


# ---------------------------------------------------------------------------
# GET /chat/drafts
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestListDrafts:
    async def test_lists_only_the_callers_pending_drafts(
        self, client, app, db_path,
    ):
        mine = _hold(db_path, subject="Mine")
        _hold(db_path, user_id="mallory", subject="Theirs")
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        assert resp.status_code == 200
        drafts = resp.json()["drafts"]
        assert [d["id"] for d in drafts] == [mine]
        assert drafts[0]["subject"] == "Mine"

    async def test_a_roomless_draft_is_still_listed(self, client, app, db_path):
        """A cron job mailing an external address has no room to render a card
        in, and the global list is the only place it is reachable."""
        draft_id = _hold(db_path, room_token=None)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        [draft] = resp.json()["drafts"]
        assert draft["id"] == draft_id
        assert draft["room_token"] is None

    async def test_payload_carries_what_the_card_renders(
        self, client, app, db_path,
    ):
        draft_id = _hold(
            db_path,
            room_token="rm1",
            to=("stranger@example.invalid",),
            cc=("cc@example.invalid",),
            bcc=("bcc@example.invalid",),
            attachments=("/srv/mount/Users/alice/notes.pdf",),
        )
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        [draft] = resp.json()["drafts"]
        assert draft["id"] == draft_id
        assert draft["to"] == ["stranger@example.invalid"]
        assert draft["cc"] == ["cc@example.invalid"]
        # The owner's own view, unlike `!drafts`, which posts into a shared room.
        assert draft["bcc"] == ["bcc@example.invalid"]
        assert draft["body"] == "Wednesday at two works."
        assert draft["hold_reason"] == "untrusted_recipient"
        assert draft["room_token"] == "rm1"
        # Basenames only — the stored values are daemon-side host paths.
        assert draft["attachments"] == ["notes.pdf"]

    async def test_a_discarded_draft_leaves_the_list(self, client, app, db_path):
        draft_id = _hold(db_path)
        with db.get_db(db_path) as conn:
            outbound_drafts.discard(conn, draft_id)
            conn.commit()
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        assert resp.json()["drafts"] == []

    async def test_a_sent_draft_leaves_the_list(self, client, app, db_path):
        draft_id = _hold(db_path)
        cookies = await _login(client)
        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        assert resp.json()["drafts"] == []

    async def test_actions_taken_rides_along_so_discard_is_informed(
        self, client, app, db_path,
    ):
        """Calendar writes are deliberately not gated, so declining a draft can
        leave an orphan event. The card has to be able to say so."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, "alice", "book the meeting", source_type="email",
            )
            db.update_task_status(
                conn, task_id, "completed", result="done",
                actions_taken='["Created calendar event: Coffee, Wed 14:00"]',
            )
            conn.commit()
        _hold(db_path, task_id=task_id)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        [draft] = resp.json()["drafts"]
        assert draft["actions_taken"] == [
            "Created calendar event: Coffee, Wed 14:00",
        ]

    async def test_a_draft_outliving_its_task_still_lists(
        self, client, app, db_path,
    ):
        """Retention prunes tasks at seven days and a hold is designed to sit
        indefinitely, so a missing task row is ordinary rather than an error."""
        _hold(db_path, task_id=999999)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        assert resp.status_code == 200
        [draft] = resp.json()["drafts"]
        assert draft["actions_taken"] == []

    async def test_unauthenticated_is_refused(self, client, app, db_path):
        _hold(db_path)
        resp = await client.get("/istota/api/chat/drafts")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Ownership — every route answers 404, never 403
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestOwnership:
    async def test_approve_of_another_users_draft_is_404_and_sends_nothing(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path, user_id="mallory")
        cookies = await _login(client, "alice")

        with patch("istota.outbound_drafts.release") as release:
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 404
        release.assert_not_called()
        assert _status(db_path, draft_id) == "pending"

    async def test_patch_of_another_users_draft_is_404_and_does_not_edit(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path, user_id="mallory", body="theirs")
        cookies = await _login(client, "alice")

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "mine now"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 404
        with db.get_db(db_path) as conn:
            assert outbound_drafts.get(conn, draft_id).body == "theirs"

    async def test_discard_of_another_users_draft_is_404_and_leaves_it_pending(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path, user_id="mallory")
        cookies = await _login(client, "alice")

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 404
        assert _status(db_path, draft_id) == "pending"

    async def test_an_unknown_id_is_indistinguishable_from_a_foreign_one(
        self, client, app, db_path,
    ):
        foreign = _hold(db_path, user_id="mallory")
        cookies = await _login(client, "alice")

        theirs = await client.post(
            f"/istota/api/chat/drafts/{foreign}/discard",
            cookies=cookies, headers=ORIGIN,
        )
        nobodys = await client.post(
            "/istota/api/chat/drafts/424242/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert theirs.status_code == nobodys.status_code == 404
        assert theirs.json() == nobodys.json()


# ---------------------------------------------------------------------------
# PATCH — the body, and only the body
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestEdit:
    async def test_edit_replaces_the_body(self, client, app, db_path):
        draft_id = _hold(db_path, body="original")
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "edited"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 200
        assert resp.json()["draft"]["body"] == "edited"
        with db.get_db(db_path) as conn:
            assert outbound_drafts.get(conn, draft_id).body == "edited"

    async def test_recipients_are_not_editable(self, client, app, db_path):
        """An editable recipient list is a gate the user can be talked through,
        which is the failure this whole feature exists to prevent."""
        draft_id = _hold(db_path, to=("stranger@example.invalid",))
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "edited", "to": ["someone-else@example.invalid"]},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            draft = outbound_drafts.get(conn, draft_id)
        assert draft.to_addrs == ["stranger@example.invalid"]
        assert draft.body == "edited"

    async def test_edit_then_approve_sends_the_edited_body(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path, body="original")
        cookies = await _login(client)
        await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "the version the user read"},
            cookies=cookies, headers=ORIGIN,
        )

        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 200
        assert send.call_args.kwargs["body"] == "the version the user read"

    async def test_the_html_flag_moves_with_the_body(self, client, app, db_path):
        """`release` picks the content type off this flag, so a user retyping an
        HTML draft in a plain textarea must be able to say so — otherwise their
        newlines collapse and a typed `<` is parsed as markup, irreversibly."""
        draft_id = _hold(db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                "UPDATE outbound_drafts SET html = 1 WHERE id = ?", (draft_id,),
            )
            conn.commit()
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "plain text with <brackets>", "html": False},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 200
        assert resp.json()["draft"]["html"] is False
        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )
        assert send.call_args.kwargs["content_type"] == "plain"

    async def test_omitting_html_leaves_the_flag_alone(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        with db.get_db(db_path) as conn:
            conn.execute(
                "UPDATE outbound_drafts SET html = 1 WHERE id = ?", (draft_id,),
            )
            conn.commit()
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "<p>still html</p>"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.json()["draft"]["html"] is True

    async def test_a_non_boolean_html_is_rejected(self, client, app, db_path):
        draft_id = _hold(db_path, body="original")
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "edited", "html": "yes"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 400
        with db.get_db(db_path) as conn:
            assert outbound_drafts.get(conn, draft_id).body == "original"

    async def test_patch_on_a_discarded_row_is_refused(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        with db.get_db(db_path) as conn:
            outbound_drafts.discard(conn, draft_id)
            conn.commit()
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "too late"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409

    async def test_patch_on_a_sent_row_is_refused(self, client, app, db_path):
        draft_id = _hold(db_path)
        cookies = await _login(client)
        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "recall it"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409

    async def test_a_missing_body_is_rejected(self, client, app, db_path):
        draft_id = _hold(db_path, body="original")
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"subject": "nope"},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 400
        with db.get_db(db_path) as conn:
            assert outbound_drafts.get(conn, draft_id).body == "original"

    async def test_an_oversized_body_is_rejected(self, client, app, db_path):
        import istota.web_app as mod
        draft_id = _hold(db_path, body="original")
        cookies = await _login(client)

        resp = await client.patch(
            f"/istota/api/chat/drafts/{draft_id}",
            json={"body": "x" * (mod._MAX_DRAFT_BODY_CHARS + 1)},
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 400
        with db.get_db(db_path) as conn:
            assert outbound_drafts.get(conn, draft_id).body == "original"


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestApprove:
    async def test_approve_sends_and_returns_the_message_id(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        cookies = await _login(client)

        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 200
        assert resp.json()["message_id"] == "<sent@example.com>"
        assert _status(db_path, draft_id) == "sent"

    async def test_double_approve_sends_once(self, client, app, db_path):
        draft_id = _hold(db_path)
        cookies = await _login(client)

        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            first = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )
            second = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert send.call_count == 1
        assert first.status_code == second.status_code == 200
        assert first.json()["message_id"] == second.json()["message_id"]

    async def test_smtp_failure_is_502_and_leaves_the_row_pending(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        cookies = await _login(client)

        with patch("istota.skills.email.send_email") as send:
            send.side_effect = OSError("connection refused")
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 502
        assert resp.json()["retryable"] is True
        assert resp.json()["sent"] is False
        assert "connection refused" in resp.json()["error"]
        # Never marked sent optimistically — a draft wrongly marked sent is a
        # message the user believes went out and did not.
        assert _status(db_path, draft_id) == "pending"

    async def test_a_sent_but_unrecorded_draft_is_not_offered_a_retry(
        self, client, app, db_path,
    ):
        """The one failure that must never read as "still waiting, try again":
        the mail is gone and the bookkeeping after it broke."""
        draft_id = _hold(db_path)
        cookies = await _login(client)

        with patch("istota.outbound_drafts.release") as release:
            release.side_effect = outbound_drafts.DraftSentButUnrecorded(
                "<gone@example.com>", RuntimeError("disk full"),
            )
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 500
        payload = resp.json()
        assert payload["sent"] is True
        assert payload["retryable"] is False
        assert payload["message_id"] == "<gone@example.com>"

    async def test_a_permanent_refusal_is_not_offered_a_retry(
        self, client, app, db_path,
    ):
        """`.claude/rules/web-chat.md`: retry is withheld where it cannot
        succeed. Email being unconfigured, an attachment that no longer resolves
        and a corrupt column are all permanent — pressing the button again
        cannot fix any of them."""
        draft_id = _hold(db_path)
        cookies = await _login(client)

        with patch("istota.outbound_drafts.release") as release:
            release.side_effect = outbound_drafts.DraftError(
                "email sending is not configured on this instance",
            )
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 500
        assert resp.json()["retryable"] is False
        assert resp.json()["sent"] is False
        # Still answerable — the row was never claimed.
        assert _status(db_path, draft_id) == "pending"

    async def test_a_409_says_which_state_it_is_in(self, client, app, db_path):
        """`sending` and `discarded` both produce a 409 and call for opposite
        wording: one means "going out right now, do not press again", the other
        means "already binned"."""
        draft_id = _hold(db_path)
        with db.get_db(db_path) as conn:
            outbound_drafts.discard(conn, draft_id)
            conn.commit()
        cookies = await _login(client)

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/approve",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409
        assert resp.json()["state"] == "discarded"

    async def test_approving_a_discarded_draft_is_409(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        with db.get_db(db_path) as conn:
            outbound_drafts.discard(conn, draft_id)
            conn.commit()
        cookies = await _login(client)

        with patch("istota.skills.email.send_email") as send:
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 409
        send.assert_not_called()


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestDiscard:
    async def test_discard_marks_the_row_and_sends_nothing(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        cookies = await _login(client)

        with patch("istota.skills.email.send_email") as send:
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/discard",
                cookies=cookies, headers=ORIGIN,
            )

        assert resp.status_code == 200
        assert _status(db_path, draft_id) == "discarded"
        send.assert_not_called()

    async def test_discard_is_idempotent(self, client, app, db_path):
        draft_id = _hold(db_path)
        cookies = await _login(client)

        first = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )
        second = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert first.status_code == second.status_code == 200
        assert _status(db_path, draft_id) == "discarded"

    async def test_discarding_a_sent_draft_is_409(self, client, app, db_path):
        draft_id = _hold(db_path)
        cookies = await _login(client)
        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409
        assert _status(db_path, draft_id) == "sent"


# ---------------------------------------------------------------------------
# The room-event tail and /chat/config
# ---------------------------------------------------------------------------


@_needs_web_deps
class TestEventTail:
    async def test_events_snapshot_carries_held_drafts(
        self, client, app, db_path,
    ):
        """Without this the polling fallback is a downgrade — a draft would be
        invisible until the next full reload."""
        draft_id = _hold(db_path, room_token="rm1")
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/events", cookies=cookies)

        assert resp.status_code == 200
        assert [d["id"] for d in resp.json()["drafts"]] == [draft_id]

    async def test_the_snapshot_is_scoped_to_the_caller(
        self, client, app, db_path,
    ):
        """Rooms are shared; a co-member must not be handed the body and
        recipients of someone else's held mail."""
        _hold(db_path, user_id="mallory", room_token="rm1")
        cookies = await _login(client, "alice")

        resp = await client.get("/istota/api/chat/events", cookies=cookies)

        assert resp.json()["drafts"] == []

    async def test_a_failed_read_says_so_rather_than_dropping_the_key(
        self, client, app, db_path,
    ):
        """A client reading an absent key as "none held" would clear the
        approval cards on every transient lock — and the snapshot runs on the
        2s stream busy timeout, so contention is the ordinary failure."""
        import istota.web_app as mod
        _hold(db_path, room_token="rm1")
        cookies = await _login(client)

        with patch.object(mod, "_drafts_snapshot") as snap:
            snap.side_effect = sqlite3.OperationalError("database is locked")
            resp = await client.get("/istota/api/chat/events", cookies=cookies)

        assert resp.status_code == 200
        assert resp.json()["drafts"] == []
        assert resp.json()["drafts_unavailable"] is True

    async def test_a_healthy_read_carries_no_unavailable_marker(
        self, client, app, db_path,
    ):
        cookies = await _login(client)
        resp = await client.get("/istota/api/chat/events", cookies=cookies)
        assert resp.json()["drafts"] == []
        assert "drafts_unavailable" not in resp.json()

    async def test_a_resolved_draft_leaves_the_snapshot(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path, room_token="rm1")
        cookies = await _login(client)
        await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        resp = await client.get("/istota/api/chat/events", cookies=cookies)

        assert resp.json()["drafts"] == []


@_needs_web_deps
class TestStuckAndUnreadableRows:
    """The two states the card has to be able to show, and neither of which the
    Stage 5 payload could reach.

    A row stuck in `sending` was filtered out by every producer, so the one
    state that calls for "check your Sent folder" was invisible on the whole web
    surface. And one row with a malformed JSON column 500'd the list for every
    other draft the user held.
    """

    def _corrupt(self, db_path, draft_id, column="to_addrs", value="not json"):
        with db.get_db(db_path) as conn:
            conn.execute(
                f"UPDATE outbound_drafts SET {column} = ? WHERE id = ?",
                (value, draft_id),
            )
            conn.commit()

    def _mark_sending(self, db_path, draft_id):
        with db.get_db(db_path) as conn:
            conn.execute(
                "UPDATE outbound_drafts SET status = 'sending' WHERE id = ?",
                (draft_id,),
            )
            conn.commit()

    async def test_a_sending_row_is_listed_with_its_status(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path, room_token="rm1")
        self._mark_sending(db_path, draft_id)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        [draft] = resp.json()["drafts"]
        assert draft["id"] == draft_id
        assert draft["status"] == "sending"

    async def test_a_sending_row_reaches_the_stream_snapshot_too(
        self, client, app, db_path,
    ):
        """The list and the frame have to agree, or the card appears on a
        reload and not on the tick that produced it."""
        draft_id = _hold(db_path, room_token="rm1")
        self._mark_sending(db_path, draft_id)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/events", cookies=cookies)

        assert [d["status"] for d in resp.json()["drafts"]] == ["sending"]

    async def test_one_corrupt_row_does_not_500_the_list(
        self, client, app, db_path,
    ):
        good = _hold(db_path, subject="Readable")
        bad = _hold(db_path, subject="Corrupt")
        self._corrupt(db_path, bad)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        assert resp.status_code == 200
        assert [d["id"] for d in resp.json()["drafts"]] == [good, bad]

    async def test_the_corrupt_row_is_marked_and_carries_no_content(
        self, client, app, db_path,
    ):
        """Named rather than dropped, so held mail never disappears silently —
        and stripped, because the columns that would carry it are the ones we
        just failed to read."""
        good = _hold(db_path, subject="Readable")
        bad = _hold(db_path, subject="Corrupt")
        self._corrupt(db_path, bad)
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        by_id = {d["id"]: d for d in resp.json()["drafts"]}
        assert by_id[good].get("unreadable") is not True
        assert by_id[bad]["unreadable"] is True
        assert "body" not in by_id[bad]
        assert "to" not in by_id[bad]

    async def test_a_corrupt_row_can_still_be_discarded(
        self, client, app, db_path,
    ):
        """The one action that does not depend on reading the row. Without it
        the card is stuck on screen forever with nothing that works."""
        draft_id = _hold(db_path)
        self._corrupt(db_path, draft_id, column="attachments", value="[1,2]")
        cookies = await _login(client)

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            assert outbound_drafts.identity(conn, draft_id)[1] == "discarded"

    async def test_a_corrupt_row_still_refuses_to_send(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        self._corrupt(db_path, draft_id)
        cookies = await _login(client)

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/approve",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 500
        assert resp.json()["retryable"] is False

    async def test_someone_elses_corrupt_row_is_still_404(
        self, client, app, db_path,
    ):
        """The parse-free ownership read must not become a way around the rule
        every other route follows."""
        draft_id = _hold(db_path, user_id="mallory")
        self._corrupt(db_path, draft_id)
        cookies = await _login(client, "alice")

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 404


@_needs_web_deps
class TestConflictState:
    """A 409 has to say what the row is now, on every route that can raise one.

    The client reads `state` to decide whether the card goes: a settled state
    means "answered elsewhere", `sending` means "in motion, keep it". A route
    that omits the key leaves the client to guess, and the wrong guess reports a
    refused action as a completed one.
    """

    def _claim(self, db_path, draft_id):
        with db.get_db(db_path) as conn:
            conn.execute(
                "UPDATE outbound_drafts SET status = 'sending' WHERE id = ?",
                (draft_id,),
            )
            conn.commit()

    async def test_a_discard_losing_the_race_reports_sending(
        self, client, app, db_path,
    ):
        """`!drafts send` from Talk, or a second tab, puts the row in `sending`
        while this card is still on screen. Discard must not report success on
        mail that is going out."""
        draft_id = _hold(db_path)
        cookies = await _login(client)
        self._claim(db_path, draft_id)

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409
        assert resp.json()["state"] == "sending"

    async def test_a_discard_of_an_already_sent_row_names_that_state(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        cookies = await _login(client)
        with db.get_db(db_path) as conn:
            conn.execute(
                "UPDATE outbound_drafts SET status = 'sent' WHERE id = ?",
                (draft_id,),
            )
            conn.commit()

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/discard",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409
        assert resp.json()["state"] == "sent"

    async def test_an_approve_losing_the_race_reports_sending(
        self, client, app, db_path,
    ):
        draft_id = _hold(db_path)
        cookies = await _login(client)
        self._claim(db_path, draft_id)

        resp = await client.post(
            f"/istota/api/chat/drafts/{draft_id}/approve",
            cookies=cookies, headers=ORIGIN,
        )

        assert resp.status_code == 409
        assert resp.json()["state"] == "sending"


@_needs_web_deps
class TestStreamBudget:
    """The frame carries the whole set on every tick, and a body is capped only
    by the PATCH ceiling (200k each). Past a budget the extra rows ride as stubs
    and the client refetches, rather than the frame growing without bound."""

    async def test_a_small_set_rides_whole(self, client, app, db_path):
        _hold(db_path, room_token="rm1", body="short")
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/events", cookies=cookies)

        [draft] = resp.json()["drafts"]
        assert draft["body"] == "short"
        assert draft.get("truncated") is not True

    async def test_rows_past_the_budget_become_stubs(
        self, client, app, db_path,
    ):
        import istota.web_app as mod
        first = _hold(db_path, room_token="rm1", body="x" * 4000)
        second = _hold(db_path, room_token="rm1", body="y" * 4000)
        cookies = await _login(client)

        with patch.object(mod, "_DRAFT_FRAME_MAX_BYTES", 3000):
            resp = await client.get("/istota/api/chat/events", cookies=cookies)

        drafts_out = {d["id"]: d for d in resp.json()["drafts"]}
        # Every draft is still named — the card must not lose one to a budget.
        assert set(drafts_out) == {first, second}
        assert drafts_out[first].get("truncated") is not True
        assert drafts_out[second]["truncated"] is True
        assert "body" not in drafts_out[second]
        # Enough to place the card and go fetch the rest.
        assert drafts_out[second]["room_token"] == "rm1"
        assert drafts_out[second]["status"] == "pending"

    async def test_a_stub_carries_the_field_that_places_its_card(
        self, client, app, db_path,
    ):
        """`task_id` is the placement key. Without it a stub moves its own card
        out of its turn and into the fallback list, and back again when the full
        row lands — which destroys the component and any edit in progress."""
        import istota.web_app as mod
        _hold(db_path, room_token="rm1", task_id=7, body="x" * 4000)
        second = _hold(db_path, room_token="rm1", task_id=9, body="y" * 4000)
        cookies = await _login(client)

        with patch.object(mod, "_DRAFT_FRAME_MAX_BYTES", 3000):
            resp = await client.get("/istota/api/chat/events", cookies=cookies)

        stub = next(d for d in resp.json()["drafts"] if d["id"] == second)
        assert stub["truncated"] is True
        assert stub["task_id"] == 9

    async def test_the_full_list_endpoint_is_not_budgeted(
        self, client, app, db_path,
    ):
        """`GET /chat/drafts` is what a stubbed frame sends the client to, so
        capping it too would leave the body unreachable."""
        import istota.web_app as mod
        _hold(db_path, room_token="rm1", body="x" * 4000)
        _hold(db_path, room_token="rm1", body="y" * 4000)
        cookies = await _login(client)

        with patch.object(mod, "_DRAFT_FRAME_MAX_BYTES", 3000):
            resp = await client.get("/istota/api/chat/drafts", cookies=cookies)

        bodies = [d["body"] for d in resp.json()["drafts"]]
        assert bodies == ["x" * 4000, "y" * 4000]


@_needs_web_deps
class TestChatConfig:
    async def test_config_carries_the_display_and_policy_settings(
        self, client, app, db_path,
    ):
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/config", cookies=cookies)

        body = resp.json()
        assert body["external_turn_display"] == "collapsed"
        # The raw value, so a settings pane can tell "unset" from a choice.
        assert body["outbound_approval"] == ""
        assert body["outbound_approval_effective"] == "untrusted"
        assert body["outbound_approval_floor"] == "untrusted"

    async def test_a_user_setting_shows_raw_and_resolved_separately(
        self, tmp_path, client, app, db_path,
    ):
        _patch_app(_make_config(tmp_path, db_path, floor="off", user_setting="all"))
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/config", cookies=cookies)

        body = resp.json()
        assert body["outbound_approval"] == "all"
        assert body["outbound_approval_effective"] == "all"
        assert body["outbound_approval_floor"] == "off"

    async def test_a_user_may_not_loosen_below_the_operator_floor(
        self, tmp_path, client, app, db_path,
    ):
        _patch_app(
            _make_config(tmp_path, db_path, floor="untrusted", user_setting="off"),
        )
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/config", cookies=cookies)

        body = resp.json()
        assert body["outbound_approval"] == "off"
        assert body["outbound_approval_effective"] == "untrusted"

    @pytest.mark.parametrize("stored", ["", "off", "untrusted", "all"])
    async def test_a_recognized_setting_is_flagged_valid(
        self, tmp_path, client, app, db_path, stored,
    ):
        _patch_app(_make_config(tmp_path, db_path, user_setting=stored))
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/config", cookies=cookies)

        assert resp.json()["outbound_approval_valid"] is True

    async def test_an_unrecognized_setting_is_published_but_flagged(
        self, tmp_path, client, app, db_path,
    ):
        """`effective_policy` treats it as unset and resolves to the floor. The
        raw value still goes out — hiding a hand-edited row would make the pane
        show a setting nobody chose — so the flag is what stops the pane from
        rendering it as a live selection."""
        _patch_app(
            _make_config(tmp_path, db_path, floor="untrusted", user_setting="lax"),
        )
        cookies = await _login(client)

        resp = await client.get("/istota/api/chat/config", cookies=cookies)

        body = resp.json()
        assert body["outbound_approval"] == "lax"
        assert body["outbound_approval_valid"] is False
        assert body["outbound_approval_effective"] == "untrusted"
