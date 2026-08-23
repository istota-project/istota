"""A push leaves the container, and its headers survive the wire.

`ntfy_headers.py` exists because RFC 2047 encoding of a header value is easy to
get wrong: httpx serializes headers as ASCII, so a title with an em dash or a
CJK character raised `UnicodeEncodeError` inside the POST and took the whole
notification with it (ISSUE-213). Every test of that today asserts on what
`encode_header_value` *returned*, which cannot tell you whether the value it
returned is the value that went out — the skill CLI, the proxy that runs it and
httpx all sit between the two.

So the assertion here is on the header the server read, and it is written
against the *decoded* value rather than against the encoder's output. A test
comparing the wire bytes to `encode_header_value(title)` would pass on any
encoder consistent with itself, including one that dropped the title; a test
that decodes the header and gets the original glyphs back can only pass if
something encoded them correctly and something else transmitted them.

**The skill runs host-side through the proxy, and that is the point of running
it from a task at all.** `istota-skill ntfy send` is spawned by the skill proxy
outside the sandbox with `NTFY_TOKEN` injected server-side; the model never
holds it. `test_secret_isolation.py` asserts the other half of that.
"""

from __future__ import annotations

import shlex
from email.header import decode_header

import pytest

from testbed.services import ntfy

pytestmark = pytest.mark.smoke

#: Applied per class, matching `test_forge_e2e.py`. The module-level marker
#: stays a bare `pytest.mark.smoke` because `tests/test_smoke_tier.py` greps
#: for that exact line — a guard against a scenario file that would otherwise
#: run in the default suite and hang on a Docker build.
NOTIFY = pytest.mark.profile("notify")

USER_ID = "testuser"

#: Every class of character the encoder exists for, in one title.
#:
#: An em dash (Latin-1 but not ASCII), a diaeresis (a composed letter), a CJK
#: pair (three UTF-8 bytes each) and an emoji (outside the BMP). One title
#: rather than four scenarios: the failure is in the *encoding step*, which is
#: either there or not, and four stack round trips to prove that costs a minute
#: for nothing.
TITLE = "Grüße — 完了 🎉"

BODY = "the scripted push"


def _seed_ntfy_secret(stack) -> None:
    """Point the stack's one user at the stub, through the shipped CLI.

    Not `config_env()`, because there is no such variable: ntfy is a per-user
    connected service in the encrypted `secrets` table, so this is how an
    operator would configure it and therefore how the tier does.

    `secret ensure` is idempotent by contract — it prints `STATE: noop` on a
    second identical call — so every test calls it rather than a fixture
    holding state the session-scoped stack would outlive.
    """
    service = stack.service("ntfy")
    for key, value in (
        ("server_url", service.container_url),
        ("topic", service.topic),
        ("token", ntfy.NTFY_TOKEN),
    ):
        result = stack.exec(
            [
                "uv", "run", "istota", "-c", "/data/config/config.toml",
                "secret", "ensure", "-u", USER_ID,
                "--service", "ntfy", "--key", key, "--value", value,
            ],
            timeout=120,
        )
        assert result.returncode == 0, (
            f"seeding the ntfy {key} secret exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def _push_script(title: str, body: str) -> list[dict]:
    """One Bash turn that sends a push, then one that answers.

    `shlex.quote` on both, because the title is the point of the scenario and
    it contains a space, an em dash and an emoji. A shell-quoting bug here
    would present as a lost or truncated title, which is exactly the defect
    under test — so the harness must not be able to produce it.
    """
    command = (
        f"istota-skill ntfy send {shlex.quote(body)} --title {shlex.quote(title)}"
    )
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
        {"text": "the push was sent"},
    ]


def _decoded(raw: str) -> str:
    """An RFC 2047 header value, back to the string it encodes.

    `decode_header` returns a list of `(bytes|str, charset)` pairs, because one
    header may mix encoded words and plain runs — which is exactly what a title
    with an ASCII prefix produces.
    """
    parts = []
    for chunk, charset in decode_header(raw):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "ascii", "replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


@NOTIFY
class TestAPushLeavesTheContainer:
    def test_the_push_arrives_on_the_configured_topic(self, stack):
        _seed_ntfy_secret(stack)
        stack.script(_push_script(TITLE, BODY))
        service = stack.service("ntfy")

        task_id = stack.submit("notify me on my phone that the build finished")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        pushes = service.pushes()
        assert pushes, (
            "nothing was POSTed to the ntfy stub. The skill runs host-side "
            "through the proxy, so this is a task that never reached it — read "
            "the transcript for what the CLI printed.\n"
            f"--- transcript ---\n{stack.endpoint.transcript()[-3000:]}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert pushes[-1].body.decode() == BODY, (
            f"the body was not the message: {pushes[-1].body!r}"
        )

    def test_a_non_ascii_title_arrives_decodable_rather_than_lost(self, stack):
        """The assertion ISSUE-213 was reopened by, stated at the wire.

        Three things at once, and each rules out a different way of passing.
        The header has to be there (a dropped title is the ISSUE-213 symptom,
        since httpx raises rather than sends). It has to be pure ASCII (raw
        UTF-8 in a header is what httpx refuses, so a header that is not ASCII
        did not go through httpx and cannot have reached a real server). And it
        has to decode back to the exact glyphs — a lossy `?`-substituting
        fallback satisfies the first two and loses the user's title.
        """
        _seed_ntfy_secret(stack)
        stack.script(_push_script(TITLE, BODY))
        service = stack.service("ntfy")

        task_id = stack.submit("push a notification to my phone")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        raw = service.header("Title")
        assert raw, (
            "the push carried no Title header at all\n"
            f"--- transcript ---\n{stack.endpoint.transcript()[-3000:]}"
        )
        assert raw.isascii(), (
            f"the Title header is not ASCII ({raw!r}); httpx cannot serialize "
            "that, so this did not travel the path a deployment uses"
        )
        assert _decoded(raw) == TITLE, (
            f"the Title decoded to {_decoded(raw)!r}, not {TITLE!r} — the "
            f"encoding is lossy. Raw header: {raw!r}"
        )

    def test_the_token_was_injected_without_the_model_ever_holding_it(self, stack):
        """The credential arrived, and it arrived from the proxy.

        Both halves matter and neither is enough alone. A push with no
        `Authorization` would be answered 401 by the stub — so "a push arrived"
        already implies a token — but the assertion says so by name, because
        the 401 would otherwise present as `pushes()` being non-empty and the
        task failing for an unexplained reason.

        The shape, not the value: `ServiceCall.auth` is scheme-and-length on
        every service in this package, because a fixture that keeps real-looking
        tokens in a list that gets printed into failure output is a liability on
        a public repo.
        """
        _seed_ntfy_secret(stack)
        stack.script(_push_script(TITLE, BODY))

        task_id = stack.submit("beep me when it is done")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)

        pushes = stack.service("ntfy").pushes()
        assert pushes, "nothing was POSTed to the ntfy stub"
        assert pushes[-1].auth == f"Bearer len={len(ntfy.NTFY_TOKEN)}", (
            f"the push carried {pushes[-1].auth!r} rather than a bearer token "
            "of the seeded length"
        )
        assert ntfy.NTFY_TOKEN not in stack.endpoint.transcript(), (
            "the ntfy token reached the model's context; the skill proxy is "
            "supposed to inject it into the CLI's environment host-side"
        )
