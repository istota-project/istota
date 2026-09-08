"""The Docker first-run wizard, driven the way an operator drives it.

``docker/init.sh`` is 900 lines of prompts whose only output is a ``.env``, and
nothing in the suite could reach it before. Two of its answers are worth
holding still:

**The Claude credential.** The CLI takes either a Claude Code OAuth token or an
Anthropic API key, and the entrypoint checks for both — but the wizard used to
prompt for the token alone and then mention `ANTHROPIC_API_KEY` in passing, as
a thing to set later by hand in a file it was in the middle of writing. Skipping
the token now asks for the key instead.

**The signaling URL.** Talk stores one URL for the standalone signaling server
and two different clients resolve it: a browser, and Nextcloud's own PHP from
inside the ``nextcloud`` container. nginx proxies ``/standalone-signaling/``
through to the server so the public address Nextcloud is already served from
answers for both, which is what makes it a default the wizard can offer.

The driver below runs the real script under a pty, because ``read -rp`` prints
its prompt only when input is a terminal. It answers by matching the prompt
text rather than by position, so a wizard that grows a question somewhere else
does not silently shift every answer in this file by one.

**A needle that matches nothing is an error**, and that is the guard that keeps
these tests from going vacuous. An unmatched needle is indistinguishable from a
wizard that never asked: the prompt it was aimed at is simply absent, every
other answer still lands, and the assertions at the end pass for the wrong
reason. Two of these tests were written that way and passed against the
pre-change sources. `never_asked` is the deliberate other half — a prompt whose
*absence* is the thing being asserted.
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import socket
import subprocess
import termios
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parent.parent
INIT_SH = REPO / "docker" / "init.sh"
ENV_EXAMPLE = REPO / "docker" / ".env.example"
NGINX_TEMPLATE = REPO / "docker" / "nginx" / "default.conf.template"

#: A prompt is the only thing the script writes that ends in ": " without a
#: newline behind it — `dim()` output always ends in a newline.
_PROMPT_TAIL = re.compile(r": (?:\x1b\[[0-9;]*m)?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Run(NamedTuple):
    env: dict[str, str]
    transcript: str
    """Everything the wizard printed, ANSI stripped. What proves a prompt was
    *offered*, which the resulting `.env` cannot distinguish from never asked."""


def host_has_routable_address() -> bool:
    """Whether `detect_host_ip` in the wizard has an address to find.

    The same question by the same means: the source address the kernel picks
    for a route off this machine. A UDP `connect` sends nothing, so this makes
    no network access — it reads the routing table.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return not s.getsockname()[0].startswith("127.")
    except OSError:
        return False
    finally:
        s.close()


def run_wizard(
    tmp_path: Path,
    answers: dict[str, str],
    never_asked: tuple[str, ...] = (),
    timeout: float = 60.0,
) -> Run:
    """Run init.sh in a copy of docker/, answering prompts by substring.

    `answers` maps a substring of the prompt to the line to type; every one of
    them must match at least one prompt. Anything unmatched gets an empty line,
    which takes the offered default. `never_asked` is the inverse: substrings
    that must appear in no prompt at all.
    """
    work = tmp_path / "docker"
    work.mkdir()
    shutil.copy(INIT_SH, work / "init.sh")
    shutil.copy(ENV_EXAMPLE, work / ".env.example")

    master, slave = pty.openpty()
    # Echo off: the answers we type would otherwise come back and land in the
    # middle of the next prompt we are trying to match.
    mode = termios.tcgetattr(slave)
    mode[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, mode)

    proc = subprocess.Popen(
        ["bash", "init.sh", "--no-start", "--force"],
        cwd=work,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    transcript: list[str] = []
    prompts: list[str] = []
    buf = ""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], timeout)
            if not ready:
                raise AssertionError(
                    f"wizard produced nothing for {timeout}s; last output:\n"
                    f"{''.join(transcript)[-2000:]}"
                )
            try:
                chunk = os.read(master, 4096)
            except OSError:  # pty closed — the script exited
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            transcript.append(text)
            buf += text
            if _PROMPT_TAIL.search(buf):
                line = _ANSI.sub("", buf).splitlines()[-1]
                prompts.append(line)
                reply = ""
                for needle, value in answers.items():
                    if needle in line:
                        reply = value
                        break
                os.write(master, (reply + "\n").encode())
                buf = ""
    finally:
        os.close(master)
        proc.wait(timeout=30)

    whole = _ANSI.sub("", "".join(transcript))
    assert proc.returncode == 0, f"init.sh exited {proc.returncode}:\n{whole[-3000:]}"

    asked = "\n".join(prompts)
    unmatched = [n for n in answers if n not in asked]
    assert not unmatched, (
        f"answered prompts that were never asked: {unmatched}\nprompts were:\n{asked}"
    )
    appeared = [n for n in never_asked if n in asked]
    assert not appeared, f"prompted for what it should not have: {appeared}\n{asked}"

    env: dict[str, str] = {}
    for raw in (work / ".env").read_text().splitlines():
        if raw.startswith("#") or "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        env[k] = v
    return Run(env, whole)


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("python3") is None,
    reason="init.sh generates passwords with openssl and derives the bot login with python3",
)


class TestTheClaudeCredential:
    def test_an_oauth_token_is_written_and_the_key_is_not_asked_for(self, tmp_path):
        """The second prompt exists only as the fallback for a skipped token."""
        run = run_wizard(
            tmp_path,
            {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-from-the-test"},
            never_asked=("ANTHROPIC_API_KEY",),
        )
        assert run.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-from-the-test"
        assert run.env["ANTHROPIC_API_KEY"] == ""

    def test_skipping_the_token_asks_for_an_api_key_instead(self, tmp_path):
        run = run_wizard(
            tmp_path,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "ANTHROPIC_API_KEY": "api-key-from-the-test",
                "USER_NAME": "operator",
            },
        )
        assert run.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert run.env["ANTHROPIC_API_KEY"] == "api-key-from-the-test"
        assert run.env["USER_NAME"] == "operator"

    def test_neither_credential_leaves_both_keys_empty(self, tmp_path):
        run = run_wizard(tmp_path, {})
        assert run.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert run.env["ANTHROPIC_API_KEY"] == ""

    def test_a_key_pasted_into_the_token_box_is_taken_as_a_key(self, tmp_path):
        """Both credentials begin `sk-ant-` and they are now consecutive
        prompts under one heading, so this paste is the likely one. The
        entrypoint writes whatever is here into `.credentials.json` as an OAuth
        access token and never falls back, so left alone it fails at
        authentication naming nothing."""
        # Fabricated; the `sk-ant-api` prefix is the thing under test, which is
        # also why the scanner reads it as a key and why the marker is here.
        key = "sk-ant-api03-pasted-in-the-wrong-box"  # private-data-ok
        run = run_wizard(
            tmp_path,
            {"CLAUDE_CODE_OAUTH_TOKEN": key},
            never_asked=("ANTHROPIC_API_KEY",),
        )
        assert run.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert run.env["ANTHROPIC_API_KEY"] == key
        assert "not an OAuth token" in run.transcript

    def test_an_oauth_token_is_left_where_it_was_typed(self, tmp_path):
        """The negative half of the rerouting: a real token starts `sk-ant-oat`
        and must not be moved."""
        token = "sk-ant-oat01-typed-in-the-right-box"  # private-data-ok
        run = run_wizard(
            tmp_path,
            {"CLAUDE_CODE_OAUTH_TOKEN": token},
            never_asked=("ANTHROPIC_API_KEY",),
        )
        assert run.env["CLAUDE_CODE_OAUTH_TOKEN"] == token
        assert run.env["ANTHROPIC_API_KEY"] == ""


class TestTheSignalingUrl:
    def test_the_public_path_is_offered_and_taken(self, tmp_path):
        """The wizard's own answer: the address Nextcloud is served from, on
        the path nginx proxies to the server."""
        run = run_wizard(
            tmp_path,
            {
                "DOMAIN": "istota.example.com",
                "served over https": "y",
                "Enable the Talk signaling server": "y",
                "Use https://istota.example.com/standalone-signaling/?": "y",
            },
        )
        assert run.env["ISTOTA_PUBLIC_PROTO"] == "https"
        assert (
            run.env["ISTOTA_TALK_SIGNALING_SERVER"]
            == "https://istota.example.com/standalone-signaling/"
        )
        assert run.env["ISTOTA_TALK_SIGNALING_ENABLED"] == "true"
        # The daemon's own route is a different address and always the
        # container one — Talk advertises the browser URL.
        assert run.env["ISTOTA_TALK_SIGNALING_URL"] == "http://signaling:8080"
        assert run.env["ISTOTA_TALK_SIGNALING_SECRET"] != ""
        assert "signaling" in run.env["COMPOSE_PROFILES"].split(",")

    def test_http_is_the_default_scheme(self, tmp_path):
        run = run_wizard(
            tmp_path,
            {
                "DOMAIN": "istota.example.com",
                "served over https": "n",
                "Enable the Talk signaling server": "y",
                "Use http://istota.example.com/standalone-signaling/?": "y",
            },
        )
        assert run.env["ISTOTA_PUBLIC_PROTO"] == "http"
        assert (
            run.env["ISTOTA_TALK_SIGNALING_SERVER"]
            == "http://istota.example.com/standalone-signaling/"
        )

    def test_declining_the_default_still_takes_a_url_of_your_own(self, tmp_path):
        """The offer needle is spelled out in full, so this fails rather than
        passes on a wizard that offers nothing to decline."""
        run = run_wizard(
            tmp_path,
            {
                "DOMAIN": "istota.example.com",
                "Enable the Talk signaling server": "y",
                "Use http://istota.example.com/standalone-signaling/?": "n",
                "Signaling URL": "https://talk.example.com/hpb/",
            },
        )
        assert run.env["ISTOTA_TALK_SIGNALING_SERVER"] == "https://talk.example.com/hpb/"
        assert run.env["ISTOTA_TALK_SIGNALING_ENABLED"] == "true"


class TestTheLocalhostOnlyStack:
    """A stack with no `DOMAIN` still has one workable address, and it is not
    localhost: from inside the `nextcloud` container that is Nextcloud's own
    loopback. The host's address on the network is reachable from a sibling
    container and from a browser both, so the wizard offers it — off by
    default, because it is baked into Nextcloud at first install and a DHCP
    lease outlives nothing."""

    _LAN_OFFER = re.compile(r"Use http://\d+\.\d+\.\d+\.\d+:\d+/standalone-signaling/\?")

    @pytest.mark.skipif(
        not host_has_routable_address(), reason="no routable address for the wizard to find"
    )
    def test_the_offer_is_made_and_declined_at_the_default(self, tmp_path):
        run = run_wizard(
            tmp_path,
            {"DOMAIN": "", "Enable the Talk signaling server": "y", "Signaling URL": ""},
        )
        assert self._LAN_OFFER.search(run.transcript), run.transcript[-1500:]
        # Offered is not taken: the prompt above defaults to no, and the URL
        # prompt behind it defaults to empty.
        assert run.env["ISTOTA_TALK_SIGNALING_SERVER"] == ""
        assert run.env["ISTOTA_TALK_SIGNALING_ENABLED"] == "false"
        assert "signaling" not in run.env["COMPOSE_PROFILES"].split(",")

    @pytest.mark.skipif(
        not host_has_routable_address(), reason="no routable address for the wizard to find"
    )
    def test_accepting_it_registers_the_host_address(self, tmp_path):
        run = run_wizard(
            tmp_path,
            {"DOMAIN": "", "Enable the Talk signaling server": "y", "standalone-signaling/?": "y"},
        )
        assert re.fullmatch(
            r"http://\d+\.\d+\.\d+\.\d+:\d+/standalone-signaling/",
            run.env["ISTOTA_TALK_SIGNALING_SERVER"],
        ), run.env["ISTOTA_TALK_SIGNALING_SERVER"]
        assert run.env["ISTOTA_TALK_SIGNALING_ENABLED"] == "true"
        assert run.env["ISTOTA_TALK_SIGNALING_URL"] == "http://signaling:8080"
        assert "signaling" in run.env["COMPOSE_PROFILES"].split(",")

    @pytest.mark.skipif(
        host_has_routable_address(), reason="this host has an address, so one is offered"
    )
    def test_without_a_routable_address_nothing_is_offered(self, tmp_path):
        run = run_wizard(
            tmp_path,
            {"DOMAIN": "", "Enable the Talk signaling server": "y", "Signaling URL": ""},
        )
        assert not self._LAN_OFFER.search(run.transcript)
        assert run.env["ISTOTA_TALK_SIGNALING_SERVER"] == ""
        assert run.env["ISTOTA_TALK_SIGNALING_ENABLED"] == "false"


class TestNginxProxiesTheSignalingServer:
    """The half of the arrangement that lives outside the wizard: without this
    location block the URL the wizard offers is a 404 from Nextcloud."""

    def _block(self) -> str:
        text = NGINX_TEMPLATE.read_text()
        assert "location /standalone-signaling/ {" in text
        return text.split("location /standalone-signaling/ {", 1)[1].split("\n    }", 1)[0]

    def test_the_location_exists_and_strips_its_prefix(self):
        # The server serves /api/v1/... and /spreed at its own root.
        assert "rewrite ^/standalone-signaling/(.*)$ /$1 break;" in self._block()

    def test_the_upstream_is_a_variable(self):
        """With the `signaling` profile off there is no such container. A
        literal upstream fails config load, which takes down the front end for
        the whole stack rather than just this path."""
        text = NGINX_TEMPLATE.read_text()
        assert "set $upstream_signaling signaling:8080;" in text
        assert "proxy_pass http://$upstream_signaling;" in self._block()

    def test_the_websocket_survives_the_proxy(self):
        """`/spreed` is a long-lived websocket; without the upgrade headers the
        browser half of signaling never connects."""
        block = self._block()
        assert "proxy_set_header Upgrade $http_upgrade;" in block
        assert "proxy_set_header Connection $connection_upgrade;" in block
        assert "proxy_http_version 1.1;" in block

    def test_the_prefix_redirect_carries_no_authority(self):
        """A request for the prefix without its trailing slash gets nginx's own
        301, built from `$host` — port stripped — and `$scheme`, which is http
        even behind TLS termination. Relative, it cannot be wrong about
        either."""
        assert "absolute_redirect off;" in self._block()
