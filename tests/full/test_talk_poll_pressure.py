"""What a Talk long-poll answers with when the Nextcloud behind it is in trouble.

ISSUE-399: production logs a burst of `Error polling conversation <token>` every
hour or so, several rooms at once. The diagnostic added in b88ca973 named the
shape — `HTTP 200, content-type text/html; charset=UTF-8, 0 chars` — and the
issue reasoned from it that a PHP worker was dying after its headers were sent,
with istota's own long-polling as the suspected cause: `talk_poll_timeout = 30`
across six or seven rooms holds most of a small FPM pool around the clock.

The deployment then set `talk_poll_timeout = 1`, a thirtyfold cut in how long
any worker is held, and the rate did not fall. That is the experiment the
hypothesis predicted would work, so the hypothesis needs re-testing somewhere it
can be controlled rather than observed. This file is that place: a real
Nextcloud, a real `spreed`, real rooms, and the knobs on the server side of the
connection.

**The probe runs inside the istota container, not here.** It loads
`/data/config/config.toml` and talks to `http://nextcloud` over the compose
network, so the credentials never leave the container and the client, the config
and the network path are the deployment's own rather than a re-creation of them.
What comes back to pytest is one JSON line per response: status, content-type,
body length and whether it parsed. The daemon's log can only say that a poll
failed; this says what every poll answered, including the good ones, which is
what makes a negative result mean anything.

**The cursors are collected in a separate pass, before anything is broken.** A
long-poll needs a `lastKnownMessageId`, and fetching one is an ordinary
Nextcloud request — so a probe that collected them itself would have to make
them against the same crippled server it is about to measure, and would fail in
setup with none of the responses under investigation ever sent.

Restores whatever it changes on the Nextcloud container, because the stack is
session-scoped and the next file to run inherits it.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
import uuid

import pytest

pytestmark = pytest.mark.full

FULL = pytest.mark.profile("full")

CONTAINER_CONFIG = "/data/config/config.toml"

#: Production's `talk_poll_state` holds 41 rows against 6 or 7 live rooms. The
#: point of the number is that it is larger than any plausible worker pool, so
#: a cycle cannot be served without queueing.
ROOM_COUNT = 40

#: php.ini fragments in `nextcloud:30-apache` — `conf.d` is read after the main
#: file, so a `zz-` name wins.
PHP_OVERRIDE = "/usr/local/etc/php/conf.d/zz-testbed-pressure.ini"

MPM_CONF = "/etc/apache2/mods-available/mpm_prefork.conf"
MPM_BACKUP = "/etc/apache2/mods-available/mpm_prefork.conf.testbed-orig"

_PRELUDE = """
    import asyncio, json, sys
    from pathlib import Path
    import httpx
    from istota.config import load_config

    cfg = load_config(Path("{config}"))
    base = cfg.nextcloud.url.rstrip("/")
    auth = (cfg.nextcloud.username, cfg.nextcloud.app_password)
    headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
    chat = base + "/ocs/v2.php/apps/spreed/api/v1/chat/"
"""

#: Pass one: every room the bot is in, and the newest message id in each.
PROBE_CURSORS = textwrap.dedent(
    _PRELUDE
    + """
    async def main():
        cursors = {}
        async with httpx.AsyncClient(timeout=60) as client:
            listing = await client.get(
                base + "/ocs/v2.php/apps/spreed/api/v4/room",
                auth=auth, headers=headers,
            )
            for room in listing.json()["ocs"]["data"]:
                token = room["token"]
                head = await client.get(
                    chat + token, auth=auth, headers=headers,
                    params={"lookIntoFuture": 0, "limit": 1},
                )
                if head.status_code == 200:
                    rows = head.json()["ocs"]["data"]
                    if rows:
                        cursors[token] = rows[0]["id"]
        print(json.dumps(cursors))

    asyncio.run(main())
    """
).replace("{config}", CONTAINER_CONFIG)

#: Pass two: one concurrent long-poll per cursor, reporting every answer.
#: Mirrors `TalkClient.poll_messages` — same URL, params, headers and the
#: `timeout + 10` request bound — but reports the response instead of raising on
#: the ones that do not parse. Reproducing the call rather than importing it is
#: deliberate: `poll_messages` turns exactly the answer under investigation into
#: a `TalkResponseError` and discards the response object.
PROBE_POLL = textwrap.dedent(
    _PRELUDE
    + """
    poll_timeout = int(sys.argv[1])
    cursors = json.loads(sys.argv[2])

    async def main():
        async with httpx.AsyncClient(timeout=60) as client:
            async def poll(token, cursor):
                try:
                    r = await client.get(
                        chat + token, auth=auth, headers=headers,
                        timeout=poll_timeout + 10,
                        params={
                            "lookIntoFuture": 1, "timeout": poll_timeout,
                            "limit": 50, "lastKnownMessageId": cursor,
                        },
                    )
                except Exception as exc:
                    return {"token": token, "error": type(exc).__name__,
                            "detail": str(exc)[:200]}
                # A 304 carries no body by definition and is how Talk says
                # "nothing new"; it is a good answer, not an unparseable one.
                if r.status_code == 304:
                    return {"token": token, "status": 304, "ctype": "",
                            "length": 0, "parsed": True}
                parsed = True
                try:
                    r.json()
                except Exception:
                    parsed = False
                return {
                    "token": token,
                    "status": r.status_code,
                    "ctype": r.headers.get("content-type", ""),
                    "length": len(r.content),
                    "parsed": parsed,
                }

            out = await asyncio.gather(
                *(poll(t, c) for t, c in cursors.items())
            )
        print(json.dumps(out))

    asyncio.run(main())
    """
).replace("{config}", CONTAINER_CONFIG)


def _run_probe(stack, source: str, *argv: str) -> str:
    result = stack.exec(
        ["uv", "run", "python", "-c", source, *argv],
        service="istota",
        timeout=300,
    )
    assert result.returncode == 0, (
        f"the probe itself failed ({result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout.strip().splitlines()[-1]


def _cursors(stack) -> dict[str, int]:
    return json.loads(_run_probe(stack, PROBE_CURSORS))


def _poll_all(
    stack, cursors: dict[str, int], *, poll_timeout: int, label: str = "",
) -> list[dict]:
    records = json.loads(
        _run_probe(stack, PROBE_POLL, str(poll_timeout), json.dumps(cursors))
    )
    # To stderr, so the shape distribution is on the record for a passing run
    # too. A tier whose only output is "3 passed" tells the next reader nothing
    # about what the server actually answered, which here is the whole finding.
    print(f"\n[{label or 'poll'}] {len(records)} rooms:\n{_describe(records)}",
          file=sys.stderr)
    return records


def _unparsed(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("error") or not r.get("parsed")]


def _describe(records: list[dict]) -> str:
    shapes: dict[str, int] = {}
    for r in records:
        if r.get("error"):
            key = f"{r['error']}: {r.get('detail', '')[:60]}"
        else:
            key = (
                f"HTTP {r['status']} {r['ctype'] or '-'} {r['length']}B "
                f"{'json' if r['parsed'] else 'UNPARSED'}"
            )
        shapes[key] = shapes.get(key, 0) + 1
    return "\n".join(f"  {n:3d} x {k}" for k, n in sorted(shapes.items()))


def _nc(stack, argv: list[str], *, timeout: int = 60):
    result = stack.exec(argv, service="nextcloud", timeout=timeout)
    assert result.returncode == 0, (
        f"`{' '.join(argv)}` in the nextcloud container exited "
        f"{result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _restart_nextcloud(stack, *, expect_healthy: bool = True) -> str:
    """Restart the whole nextcloud container, and wait for apache to answer.

    Not `apachectl -k restart`, and not `graceful` either. In `nextcloud:30-apache`
    apache *is* pid 1 (`apache2-foreground`), so `-k restart` sends it SIGHUP,
    apache2-foreground exits, docker's restart policy brings the container back,
    and the exec that sent the signal reports 129 — a mechanism failure that
    looks nothing like one and that cost a whole tier run to attribute. `-k
    graceful` survives, but it only replaces children as they finish, so it
    applies a new php.ini and does *not* apply an `mpm_prefork` limit that lowers
    `ServerLimit`. One mechanism that does both, rather than two that differ in a
    way the caller has to remember.

    `expect_healthy=False` is for a restart that deliberately breaks PHP. The
    readiness condition is then *any* HTTP status rather than 200, because
    waiting for 200 on a server crippled on purpose times out after three
    minutes and reports the harness as the finding — which is what the
    `memory_limit` case did on its first run, and what taught this file that a
    broken PHP still answers, just not with a 200.

    Returns the status `status.php` settled on, so a caller can assert on it.
    """
    stack.restart("nextcloud")
    deadline = time.monotonic() + 180
    last = ""
    while time.monotonic() < deadline:
        probe = stack.exec(
            ["sh", "-c", "curl -s -o /dev/null -w '%{http_code}' "
                         "http://localhost/status.php"],
            service="nextcloud", timeout=30,
        )
        code = probe.stdout.strip()
        if probe.returncode == 0 and code.isdigit() and code != "000":
            if not expect_healthy or code == "200":
                return code
        last = f"rc={probe.returncode} status={code!r}"
        time.sleep(2)
    raise AssertionError(
        f"nextcloud did not answer status.php within 180s after a restart ({last})"
    )


@pytest.fixture
def many_rooms(stack):
    """`ROOM_COUNT` group rooms the bot participates in, each with a message.

    A long-poll needs a cursor and a cursor needs a message, so the seed post is
    part of the fixture rather than an afterthought.

    **The seed is posted as the bot, and that is what keeps this fixture from
    costing forty tasks** (ISSUE-415). Every room here is new, so the room pass
    seeds its cursor at `latest_id - 1` — deliberately one behind the newest
    message, so the next poll still returns it — and the ingest path then reads
    the seed as an ordinary message from a configured user in a room with no
    @mention gate to stop it. Forty rooms, forty tasks, arriving faster than the
    worker pool drains them, and every later scenario in this file then failed
    its reset quiesce with the backlog still in flight. A message from the bot's
    own account is dropped by `actor_id == config.talk.bot_username` *after* the
    cursor has advanced, so the room still has a `lastMessage`, still has a
    cursor, and creates no work.

    That this only became visible when the `full` profile gained signaling is a
    fact about throughput, not about the two drivers disagreeing: both ingest
    the seed, and the event stream simply discovers all forty rooms in one
    reconciliation instead of over several poll cycles. Nothing in this file
    asserts on a task, so producing none is the honest fixture either way.
    """
    nextcloud = stack.service("nextcloud")
    prefix = f"pressure-{uuid.uuid4().hex[:8]}"
    tokens = [
        nextcloud.create_room(
            name=f"{prefix}-{i:02d}", participants=[nextcloud.bot_user],
        )
        for i in range(ROOM_COUNT)
    ]
    for token in tokens:
        nextcloud.post_message(token, actor=nextcloud.bot_user, message="seed")
    return tokens


@FULL
class TestWhatAHealthyServerAnswers:
    def test_every_room_answers_json_or_304(self, stack, many_rooms):
        """The control. Without it a later negative result means nothing.

        Forty rooms long-polled at once against an unconstrained Nextcloud: if
        the shape under investigation showed up here, it would be istota's
        concurrency alone producing it and the rest of the file would be
        measuring something else.
        """
        cursors = _cursors(stack)
        assert len(cursors) >= ROOM_COUNT, (
            f"only {len(cursors)} rooms had a cursor; the fixture makes "
            f"{ROOM_COUNT}"
        )

        records = _poll_all(stack, cursors, poll_timeout=2, label="healthy")

        assert _unparsed(records) == [], (
            "a healthy Nextcloud answered something that does not parse:\n"
            + _describe(records)
        )


@FULL
class TestWhatADyingPhpWorkerAnswers:
    """A PHP fatal is **not** the production signature, which narrows the field.

    This class was written to reproduce `HTTP 200 / text/html / 0 bytes` on
    purpose, on the reasoning in the report: a worker dying after its headers
    were committed, with `display_errors` off. It does not reproduce it. A
    `memory_limit` PHP cannot bootstrap in gives a clean **HTTP 500** — PHP's
    own fatal handler runs, sets the status and returns a real response.

    That is a result rather than a failed attempt, and it is the useful half.
    The production signature needs the worker to die *without* PHP's shutdown
    handler running at all: SIGKILL from the OOM killer or from
    `request_terminate_timeout`, a segfault, or the FastCGI upstream dropping
    after headers went out. A clean fatal is excluded.

    **What this tier cannot settle, stated rather than implied.** The testbed
    runs `nextcloud:30-apache` — apache with mod_php — and production is nginx
    in front of PHP-FPM. A zero-byte 200 carrying PHP's `default_mimetype` is a
    FastCGI artifact: nginx has committed a status line from headers the
    upstream sent and then gets nothing, so it forwards 200 with an empty body.
    mod_php has no equivalent path, so this shape cannot reproduce the signature
    however the worker is killed. It can rule causes out, which is what it does
    here, and it can test the concurrency hypothesis, which is the class below.
    """

    def test_a_clean_php_fatal_is_a_500_not_the_signature(self, stack, many_rooms):
        cursors = _cursors(stack)
        try:
            _nc(stack, ["sh", "-c",
                        f"printf 'memory_limit = 2M\\ndisplay_errors = Off\\n' "
                        f"> {PHP_OVERRIDE}"])
            status = _restart_nextcloud(stack, expect_healthy=False)
            records = _poll_all(
                stack, cursors, poll_timeout=2, label="memory_limit=2M",
            )
        finally:
            _nc(stack, ["rm", "-f", PHP_OVERRIDE])
            _restart_nextcloud(stack)

        # The control: without this the rest is a claim about a server that was
        # never actually broken.
        assert status == "500", (
            f"crippling memory_limit left status.php answering {status}, so "
            f"whatever this measured, it was not a PHP fatal"
        )

        signature = [
            r for r in records
            if r.get("status") == 200
            and r.get("length") == 0
            and "text/html" in r.get("ctype", "")
        ]
        assert signature == [], (
            "a clean PHP fatal produced the production signature after all, "
            "which would make this the cause rather than exclude it:\n"
            + _describe(records)
        )


@FULL
class TestWhetherHoldingWorkersProducesIt:
    def test_a_saturated_worker_pool_does_not_answer_an_empty_200(
        self, stack, many_rooms,
    ):
        """ISSUE-399's hypothesis, tested rather than inferred.

        Forty concurrent long-polls against a pool of four workers is a far
        harder squeeze than production's six or seven rooms against a typical
        FPM pool, and it is the state the issue blames. If saturation alone
        produced the signature, it would appear here.

        A failure of this test is the interesting outcome: it would mean the
        issue was right and the `talk_poll_timeout = 1` experiment failed for
        some other reason. Queueing itself is not a failure — a request that
        waits its turn and then answers correctly is the pool working — so a
        connect timeout is reported separately from an unparseable answer.
        """
        cursors = _cursors(stack)
        try:
            _nc(stack, ["cp", MPM_CONF, MPM_BACKUP])
            _nc(stack, ["sh", "-c",
                        "printf '<IfModule mpm_prefork_module>\\n"
                        "StartServers 2\\nMinSpareServers 2\\n"
                        "MaxSpareServers 4\\nMaxRequestWorkers 4\\n"
                        "ServerLimit 4\\n</IfModule>\\n' > " + MPM_CONF])
            _restart_nextcloud(stack)
            records = _poll_all(
                stack, cursors, poll_timeout=3, label="4 workers",
            )
        finally:
            _nc(stack, ["sh", "-c",
                        f"[ -f {MPM_BACKUP} ] && mv {MPM_BACKUP} {MPM_CONF} "
                        f"|| true"])
            _restart_nextcloud(stack)

        broken = [r for r in _unparsed(records) if not r.get("error")]
        assert broken == [], (
            "saturating the worker pool produced an unparseable answer, so "
            "ISSUE-399's reading may be right after all:\n" + _describe(records)
        )


#: Pass three: what `/api/v4/room` says the newest message in each room is.
#:
#: This is the one fact the ISSUE-399 gate rests on and the one thing no unit
#: test can establish, because every unit test builds both sides of the
#: comparison by hand. `_has_news` compares `lastMessage.id` from this listing
#: against `talk_poll_state.last_known_message_id`, which is a *chat* message id
#: — the value `POST .../chat/{token}` returns. If those are not the same id
#: space, or if `lastMessage` does not advance for a message a long-poll would
#: return, the gate holds a room shut until the next full sweep.
#:
#: Reports `lastMessage` verbatim rather than only its id, so a room whose
#: `lastMessage` Talk elides (documented for oversized messages, and returned as
#: an empty array rather than an object) is distinguishable from one carrying an
#: id that simply does not match.
PROBE_LAST_MESSAGE = textwrap.dedent(
    _PRELUDE
    + """
    wanted = set(json.loads(sys.argv[1]))

    async def main():
        out = {}
        async with httpx.AsyncClient(timeout=60) as client:
            listing = await client.get(
                base + "/ocs/v2.php/apps/spreed/api/v4/room",
                auth=auth, headers=headers,
            )
            for room in listing.json()["ocs"]["data"]:
                token = room["token"]
                if token not in wanted:
                    continue
                last = room.get("lastMessage")
                out[token] = {
                    "shape": type(last).__name__,
                    "id": last.get("id") if isinstance(last, dict) else None,
                }
        print(json.dumps(out))

    asyncio.run(main())
    """
).replace("{config}", CONTAINER_CONFIG)


def _last_messages(stack, tokens: list[str]) -> dict[str, dict]:
    return json.loads(_run_probe(stack, PROBE_LAST_MESSAGE, json.dumps(tokens)))


@FULL
class TestTheGateReadsTheSameIdSpaceAsTheCursor:
    """The premise of the ISSUE-399 `lastMessage` gate, against a real Talk.

    `poll_talk_conversations` skips a room whose `lastMessage.id` in the
    `/api/v4/room` listing is not greater than its `talk_poll_state` cursor. The
    cursor is a chat message id — `set_talk_poll_state` stores the id of a
    message a poll returned, which is the id `POST .../chat/{token}` hands back.
    Nothing anywhere in the tree had ever compared the two against a running
    Nextcloud; the gate tests build `{"lastMessage": {"id": N}}` themselves and
    mock the poll, so they would pass identically if the real field were a
    per-room sequence number, a timestamp, or absent.

    Deliberately not driven through the daemon's poll loop. Doing that would
    make the assertion wait on a poll interval and turn a fact about an id space
    into a timing test; the link that actually needs proving is
    `lastMessage.id == the id the chat endpoint issued`, and that is
    deterministic the moment the POST returns.
    """

    def test_last_message_id_is_the_posted_chat_message_id(self, stack):
        nextcloud = stack.service("nextcloud")
        token = nextcloud.create_room(
            name=f"gate-{uuid.uuid4().hex[:8]}",
            participants=[nextcloud.bot_user],
        )

        posted = nextcloud.post_message(token, message="first")
        first = _last_messages(stack, [token])[token]

        assert first["shape"] == "dict", (
            f"the room listing carried lastMessage as {first['shape']}, not an "
            f"object — `_has_news` reads that as 'unfamiliar' and fails toward "
            f"fetching, so the gate would never hold this room"
        )
        assert first["id"] == posted, (
            f"lastMessage.id is {first['id']} but the chat endpoint issued "
            f"{posted}. The gate compares this field against a cursor built "
            f"from chat message ids, so they must be one id space"
        )

        # The control. Without it the assertion above passes for a field that
        # never changes — including one frozen at room creation — and a frozen
        # lastMessage is exactly the state that gates a room shut for ever.
        second = nextcloud.post_message(token, message="second")
        after = _last_messages(stack, [token])[token]

        assert second > posted, "Talk issued a non-increasing chat message id"
        assert after["id"] == second, (
            f"lastMessage.id stayed at {after['id']} after a second message "
            f"({second}) was posted; it does not track new messages, so a room "
            f"would be gated shut with something waiting in it"
        )

    def test_a_freshly_created_room_already_carries_a_last_message(self, stack):
        """There is no such thing as a room with no `lastMessage`.

        Written first to assert the opposite — that a room nobody has posted in
        reports no `lastMessage`, exercising `_has_news`'s fail-open arm — and
        the tier said otherwise: a room created a moment ago reports a
        `lastMessage` object with a real chat id, because Talk records the
        creation and the joins as *system* messages in the same id sequence as
        chat messages.

        That is worth pinning because it narrows a residual two reviewers
        independently raised. The concern was that a room which never acquires a
        `talk_poll_state` cursor bypasses the gate for ever, since the cursor is
        only written for a message a poll returned. If new rooms were genuinely
        empty that would be a standing cost on every such room. They are not:
        `get_latest_message_id` finds the system message, the initialisation
        branch stores `latest_id - 1`, and the gate applies from the next cycle.
        A poll returns system messages too — `poll_talk_conversations` advances
        the cursor *before* it filters them — so the two sides stay in step.

        The fail-open arm is still right, and is still reachable for a shape
        this deployment does not produce. It is simply not the common case.
        """
        nextcloud = stack.service("nextcloud")
        token = nextcloud.create_room(
            name=f"gate-fresh-{uuid.uuid4().hex[:8]}",
            participants=[nextcloud.bot_user],
        )

        fresh = _last_messages(stack, [token])[token]
        assert fresh["shape"] == "dict" and isinstance(fresh["id"], int), (
            f"a freshly created room reported lastMessage as "
            f"{fresh['shape']}/{fresh['id']!r}. If Talk ever stops recording "
            f"room creation as a message, such a room takes _has_news's "
            f"fail-open arm and is long-polled on every cycle until something "
            f"is posted in it"
        )

        # It is the *system* message, not a chat message: nothing has been
        # posted. The control for the claim above is that a chat message then
        # supersedes it in the same sequence.
        posted = nextcloud.post_message(token, message="first real message")
        after = _last_messages(stack, [token])[token]
        assert after["id"] == posted > fresh["id"], (
            f"a chat message ({posted}) did not supersede the creation system "
            f"message ({fresh['id']}) in lastMessage — the two are not one "
            f"sequence, and a cursor built from one cannot gate on the other"
        )
