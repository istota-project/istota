"""A gate prompt nobody received, and the push that stands in for it, at the wire.

The email gate parks a stranger's mail and asks its user on the `alert` route
(`delivery.send_confirmation_prompt`). Every held task gets a `confirmation`
row inside the poll's transaction, and that row is delivered only where the
prompt was not (`inbound._deliver_confirmation_prompts`), as a second attempt
through `deliver_pending`. The row is written `in_room=False` (`14cec6c2`): its
question is in no room, so its push takes the user's whole alert route rather
than the room-free cut to ntfy and email (`store.ROOM_FREE_SURFACES`). Before
that commit a user routing alerts to Talk alone got no second attempt at all.

Telling those apart needs a route the cut empties and a send that fails once
and then works, which is why this case is here and not on a session stack
(a runtime routing change would leak into every later scenario). The route is
the user's `alerts_channel`, a Talk room. Talk is the one surface this tier can
reach only through a double: `tests/support/talk_double.py`, patched at the
client factory by the `fake_talk` fixture, whose `send_failures` list fails the
prompt's post and lets the next one through. The prompt's failure is a 403,
which `TalkTransport._post_part` neither retries nor reads back, so it is one
call. Everything else is real: the mail server, `poll_emails`, the database.

The other half, the prompt that did arrive and the row that is therefore not
pushed, is on the lean stack
(`tests/smoke/test_email_senders.py::TestCorrespondents::test_a_stranger_at_the_plus_address_is_held`),
and the warning an undeliverable prompt logs is
`test_email_wire.py::TestTheConfirmationGate::test_an_undeliverable_prompt_says_so`.
"""

from __future__ import annotations

import httpx
import pytest

from istota.notifications.resolvers import confirmation as confirmation_source
from istota.notifications.resolvers.task_alert import flatten_body

from ..support.email_flow import new_nonce
from .conftest import USER_ID, USER_TAG_ADDRESS

pytestmark = pytest.mark.testbed

#: The user's alerts room. Named to the double so it is a room Talk would
#: accept; nothing binds it to a registered room.
ALERTS_TOKEN = "alertsroom7"


def _refused() -> httpx.HTTPStatusError:
    """A 403 on the post: an answer, so no retry and no readback."""
    request = httpx.Request("POST", "https://nextcloud.invalid/ocs")
    return httpx.HTTPStatusError(
        "403 Forbidden", request=request, response=httpx.Response(403, request=request),
    )


class TestAnUndeliverablePrompt:
    def test_its_row_is_pushed_on_the_whole_alert_route(self, wire, fake_talk):
        fake_talk.db_path = wire.config.db_path
        fake_talk.known_channels.add(ALERTS_TOKEN)
        fake_talk.send_failures[ALERTS_TOKEN] = [_refused(), None]
        wire.config.nextcloud.url = "https://nextcloud.invalid"
        wire.config.users[USER_ID].alerts_channel = ALERTS_TOKEN

        nonce = new_nonce()
        words = f"please wire the deposit today {nonce}"
        stranger = f"stranger-{nonce}@stranger.test"
        wire.send(from_addr=stranger, to_addr=USER_TAG_ADDRESS,
                  subject=f"deposit {nonce}", body=words)

        wire.poll()

        tasks = [t for t in wire.tasks() if t["status"] == "pending_confirmation"]
        assert len(tasks) == 1, wire.tasks()
        task = tasks[0]
        rows = wire.probe.query(
            "SELECT * FROM notifications WHERE user_id = ? AND dedup_key = ?",
            [USER_ID, confirmation_source.dedup_key(task["id"])],
        )
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["source"] == confirmation_source.SOURCE, row
        # Stamped only on a send that reached somebody (`store.mark_delivered`).
        assert row["last_delivered_at"] is not None, row

        posts = [c for c in fake_talk.calls if c.method == "send_message"]
        assert [(c.token, c.refused) for c in posts] == [
            (ALERTS_TOKEN, False), (ALERTS_TOKEN, False),
        ], posts
        prompt, push = posts
        # The prompt is the composed question; it failed and minted no id.
        assert prompt.sent_id is None, prompt
        assert prompt.args["message"] == flatten_body(task["confirmation_prompt"])
        # The push is the row's own title and body, and it landed.
        assert push.sent_id is not None, push
        assert row["title"] in push.args["message"], (row["title"], push.args)
        assert row["body"] in push.args["message"], (row["body"], push.args)
        # Neither carries the mail's words: the gate's text names the sender
        # and the subject only (`inbound.poll_emails`, `confirmation_msg`).
        for call in posts:
            assert words not in call.args["message"], call.args

        with wire.outbox() as session:
            assert session.uids() == [], "a held mail sends nothing to anyone"
