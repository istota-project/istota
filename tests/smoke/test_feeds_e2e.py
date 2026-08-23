"""The feeds poller against a real HTTP server, inside the shipped image.

`tests/test_feeds_poller.py` covers what the poller does with a response, and
it does it by injecting `http_get` — so nothing in the default suite has ever
checked that the headers `_poll_rss` *builds* are the headers a server reads,
or that a 304 comes back as `not_modified` rather than as a feed that lost all
its entries. Both are properties of the pair, and a stub on one side of the
pair cannot witness them.

**Nothing points the daemon here through config.** A feed's URL is a row in the
user's own `modules/testuser/feeds.db`, so the scenario subscribes through the
shipped `feeds add` CLI — from inside a task, through the skill proxy, which is
the path a deployment uses. `services/feeds.py::config_env` says the same thing
from the other side.

Not here: `image_dedupe`. Its only caller is `feeds/routes.py`, the
authenticated web reader, and it is a *read-time display decision* computed
from rows already in the database rather than anything the poller does — so a
deployed-path scenario cannot reach it without driving the web UI, which the
spec's non-goals exclude for want of `data-testid` hooks. It is a pure function
over two entries and `tests/test_feeds_image_dedupe.py` plus
`tests/test_feeds_routes.py` already hold it down.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.smoke

#: Per class, matching `test_forge_e2e.py`; the module-level marker stays a
#: bare `pytest.mark.smoke` because `tests/test_smoke_tier.py` greps for it.
FEEDS = pytest.mark.profile("feeds")

CONTAINER_CONFIG = "/data/config/config.toml"

ETAG = '"v1-testbed"'


def _rss(*items: tuple[str, str]) -> str:
    """An RSS 2.0 document feedparser will parse, with one entry per item.

    A guid per item and nothing optional: `_persist_poll` drops an item with no
    guid, so a document without them would produce zero entries and the failure
    would read as a poller that never ran.
    """
    body = "".join(
        f"<item><title>{title}</title><link>https://example.invalid/{guid}</link>"
        f"<guid isPermaLink=\"false\">{guid}</guid></item>"
        for title, guid in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Testbed feed</title>"
        "<link>https://example.invalid/</link>"
        "<description>seeded by tests/smoke/test_feeds_e2e.py</description>"
        f"{body}"
        "</channel></rss>"
    )


def _unique_path() -> str:
    """A fresh path per scenario, and it is doing two jobs.

    The stub's recorded fetches are then this scenario's alone, and — the half
    that matters more — `feeds add` refuses a URL already subscribed, while the
    user's feeds database is a *module* database that `Stack.reset` deliberately
    does not touch (`PROTECTED_CONTAINER_PATHS` refuses anything under
    `/data/db`, which is where the tier reads its assertions from). So a second
    scenario reusing a URL would fail on the subscription rather than on
    anything it meant to assert.
    """
    return f"/feed-{uuid.uuid4().hex[:12]}.xml"


def _script(command: str) -> list[dict]:
    """One Bash turn, then an answering turn.

    The second is not optional: a turn ending in `tool_calls` asks for another
    round, and the scripted endpoint answers an unscripted round with an error
    frame rather than replaying.
    """
    return [
        {
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "Bash",
                    "arguments": {"command": command},
                }
            ]
        },
        {"text": "the feed was polled"},
    ]


#: Resolve a subscribed feed's row id, for the commands that take one.
#:
#: `feeds add` returns the url it was given and not the id, so this reads it
#: back off `feeds list`. In the sandbox, through the same `istota-skill` the
#: rest of the scenario uses, because the module database is not visible from
#: anywhere else in the tier.
_FEED_ID = (
    "istota-skill feeds list | python3 -c "
    "'import json,sys; print(next(f[\"id\"] for f in json.load(sys.stdin)"
    "[\"feeds\"] if f[\"url\"] == sys.argv[1]))' \"$URL\""
)


@FEEDS
class TestThePollerReachesARealServer:
    def test_a_seeded_feed_is_fetched_and_its_entries_written(self, stack):
        service = stack.service("feeds")
        path = _unique_path()
        url = service.add(path, _rss(("First post", "g1"), ("Second post", "g2")))

        stack.script(
            _script(
                "set -eu\n"
                f"URL={url!r}\n"
                'istota-skill feeds add --url "$URL"\n'
                "istota-skill feeds run-scheduled\n"
                f"ID=$({_FEED_ID})\n"
                'istota-skill feeds entries --feed-id "$ID" --limit 20\n'
            )
        )

        task_id = stack.submit("subscribe to a feed and read it")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        transcript = stack.endpoint.transcript()

        fetches = service.fetches(path)
        assert fetches, (
            "the feeds stub was never asked for the document, so the poller "
            "did not run or could not reach it\n"
            f"--- transcript ---\n{transcript[-4000:]}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        # The poller's own User-Agent, so a fetch by something else — a shell
        # `curl` in a scripted turn, say — could not satisfy this.
        agent = fetches[0].headers.get("User-Agent", "")
        assert agent.startswith("istota-feeds/"), (
            f"the document was fetched by {agent!r}, not by the feeds poller"
        )
        for title in ("First post", "Second post"):
            assert title in transcript, (
                f"{title!r} is not in the entries the CLI listed, so the poll "
                "reached the server and wrote nothing\n"
                f"--- transcript ---\n{transcript[-4000:]}"
            )

    def test_the_second_poll_sends_the_validator_and_takes_the_304(self, stack):
        """Conditional GET, both directions, in one task.

        `feeds refresh --id` rather than the bare `feeds refresh`, which clears
        `next_poll_at` on *every* row: a feed left behind by the scenario above
        would then be polled too, against a document `FeedsService.reset` has
        already forgotten, and the 404 would land in this poll's summary.

        The "not modified" half is asserted from the CLI's own JSON rather than
        from the absence of new rows, because those are different claims — a
        server that answered 200 with the same body also writes no new rows,
        and it is the 304 path that this exists to witness.
        """
        service = stack.service("feeds")
        path = _unique_path()
        url = service.add(path, _rss(("Only post", "g1")), etag=ETAG)

        stack.script(
            _script(
                "set -eu\n"
                f"URL={url!r}\n"
                'istota-skill feeds add --url "$URL"\n'
                "istota-skill feeds run-scheduled\n"
                f"ID=$({_FEED_ID})\n"
                'istota-skill feeds refresh --id "$ID"\n'
                "istota-skill feeds run-scheduled\n"
            )
        )

        task_id = stack.submit("poll the feed twice")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        transcript = stack.endpoint.transcript()

        fetches = service.fetches(path)
        assert len(fetches) >= 2, (
            f"the document was fetched {len(fetches)} time(s); the second poll "
            "never reached the server\n"
            f"--- transcript ---\n{transcript[-4000:]}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "If-None-Match" not in fetches[0].headers, (
            "the *first* poll sent a validator, so the feed row already carried "
            "an ETag and this scenario is not starting from where it thinks"
        )
        # `>= 2` and "some later fetch" rather than exactly `fetches[1]`: with
        # the module enabled the scheduler also runs `_module.feeds.run_scheduled`
        # on a `*/5` cron for the whole session, so an extra poll landing inside
        # this window is legal and must not fail the scenario.
        validators = [
            call.headers.get("If-None-Match") for call in fetches[1:]
        ]
        assert ETAG in validators, (
            f"no later fetch carried If-None-Match: {ETAG} — the ETag the "
            f"server sent was not stored or not sent back. Saw {validators!r}"
        )
        assert '"not_modified": true' in transcript, (
            "no poll reported `not_modified`, so the 304 was not honoured — "
            "the poller either did not send the validator or parsed the empty "
            "response as a feed that lost its entries\n"
            f"--- transcript ---\n{transcript[-4000:]}"
        )
        assert '"new_entries": 0' in transcript, (
            "the second poll wrote entries; a 304 carries no body and must "
            f"produce none\n--- transcript ---\n{transcript[-4000:]}"
        )
