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
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import subprocess
import termios
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INIT_SH = REPO / "docker" / "init.sh"
ENV_EXAMPLE = REPO / "docker" / ".env.example"
NGINX_TEMPLATE = REPO / "docker" / "nginx" / "default.conf.template"

#: A prompt is the only thing the script writes that ends in ": " without a
#: newline behind it — `dim()` output always ends in a newline.
_PROMPT_TAIL = re.compile(r": (?:\x1b\[[0-9;]*m)?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_wizard(tmp_path: Path, answers: dict[str, str], timeout: float = 60.0) -> dict[str, str]:
    """Run init.sh in a copy of docker/, answering prompts by substring.

    `answers` maps a substring of the prompt to the line to type. Anything
    unmatched gets an empty line, which takes the offered default.
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

    transcript = []
    buf = ""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], timeout)
            if not ready:
                raise AssertionError(
                    f"wizard produced nothing for {timeout}s; last output:\n{''.join(transcript)[-2000:]}"
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

    assert proc.returncode == 0, f"init.sh exited {proc.returncode}:\n{''.join(transcript)[-3000:]}"

    env: dict[str, str] = {}
    for raw in (work / ".env").read_text().splitlines():
        if raw.startswith("#") or "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        env[k] = v
    return env


pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="init.sh generates passwords with openssl"
)


class TestTheClaudeCredential:
    def test_an_oauth_token_is_written_and_the_key_is_not_asked_for(self, tmp_path):
        """The second prompt exists only as the fallback for a skipped token.
        `USER_NAME` proves it: if the key had been asked for anyway, the name
        below would have been eaten by that prompt."""
        env = run_wizard(
            tmp_path,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-from-the-test",
                "ANTHROPIC_API_KEY": "api-key-that-should-not-be-asked-for",
                "USER_NAME": "operator",
            },
        )
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-from-the-test"
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["USER_NAME"] == "operator"

    def test_skipping_the_token_asks_for_an_api_key_instead(self, tmp_path):
        env = run_wizard(
            tmp_path,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "ANTHROPIC_API_KEY": "api-key-from-the-test",
                "USER_NAME": "operator",
            },
        )
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert env["ANTHROPIC_API_KEY"] == "api-key-from-the-test"
        assert env["USER_NAME"] == "operator"

    def test_neither_credential_leaves_both_keys_empty(self, tmp_path):
        env = run_wizard(tmp_path, {})
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert env["ANTHROPIC_API_KEY"] == ""


class TestTheSignalingUrl:
    def test_the_public_path_is_offered_and_taken(self, tmp_path):
        """The wizard's own answer: the address Nextcloud is served from, on
        the path nginx proxies to the server."""
        env = run_wizard(
            tmp_path,
            {
                "DOMAIN": "istota.example.com",
                "served over https": "y",
                "Enable the Talk signaling server": "y",
                "/standalone-signaling/?": "y",
            },
        )
        assert env["ISTOTA_PUBLIC_PROTO"] == "https"
        assert env["ISTOTA_TALK_SIGNALING_SERVER"] == "https://istota.example.com/standalone-signaling/"
        assert env["ISTOTA_TALK_SIGNALING_ENABLED"] == "true"
        # The daemon's own route is a different address and always the
        # container one — Talk advertises the browser URL.
        assert env["ISTOTA_TALK_SIGNALING_URL"] == "http://signaling:8080"
        assert env["ISTOTA_TALK_SIGNALING_SECRET"] != ""
        assert "signaling" in env["COMPOSE_PROFILES"].split(",")

    def test_http_is_the_default_scheme(self, tmp_path):
        env = run_wizard(
            tmp_path,
            {
                "DOMAIN": "istota.example.com",
                "served over https": "n",
                "Enable the Talk signaling server": "y",
                "/standalone-signaling/?": "y",
            },
        )
        assert env["ISTOTA_PUBLIC_PROTO"] == "http"
        assert env["ISTOTA_TALK_SIGNALING_SERVER"] == "http://istota.example.com/standalone-signaling/"

    def test_declining_the_default_still_takes_a_url_of_your_own(self, tmp_path):
        env = run_wizard(
            tmp_path,
            {
                "DOMAIN": "istota.example.com",
                "Enable the Talk signaling server": "y",
                "/standalone-signaling/?": "n",
                "Signaling URL": "https://talk.example.com/hpb/",
            },
        )
        assert env["ISTOTA_TALK_SIGNALING_SERVER"] == "https://talk.example.com/hpb/"
        assert env["ISTOTA_TALK_SIGNALING_ENABLED"] == "true"

    def test_no_domain_means_no_default_and_an_empty_answer_skips(self, tmp_path):
        """A localhost-only evaluation has no address that satisfies both legs,
        so nothing is offered and saying yes to the feature is still a no."""
        env = run_wizard(
            tmp_path,
            {
                "DOMAIN": "",
                "Enable the Talk signaling server": "y",
                "Signaling URL": "",
            },
        )
        assert env["ISTOTA_TALK_SIGNALING_SERVER"] == ""
        assert env["ISTOTA_TALK_SIGNALING_ENABLED"] == "false"
        assert "signaling" not in env["COMPOSE_PROFILES"].split(",")


class TestNginxProxiesTheSignalingServer:
    """The half of the arrangement that lives outside the wizard: without this
    location block the URL the wizard offers is a 404 from Nextcloud."""

    def test_the_location_exists_and_strips_its_prefix(self):
        text = NGINX_TEMPLATE.read_text()
        assert "location /standalone-signaling/ {" in text
        # The server serves /api/v1/... and /spreed at its own root.
        assert "rewrite ^/standalone-signaling/(.*)$ /$1 break;" in text

    def test_the_upstream_is_a_variable(self):
        """With the `signaling` profile off there is no such container. A
        literal upstream fails config load, which takes down the front end for
        the whole stack rather than just this path."""
        text = NGINX_TEMPLATE.read_text()
        assert "set $upstream_signaling signaling:8080;" in text
        assert "proxy_pass http://$upstream_signaling;" in text

    def test_the_websocket_survives_the_proxy(self):
        """`/spreed` is a long-lived websocket; without the upgrade headers the
        browser half of signaling never connects."""
        block = NGINX_TEMPLATE.read_text().split("location /standalone-signaling/ {", 1)[1]
        block = block.split("\n    }", 1)[0]
        assert "proxy_set_header Upgrade $http_upgrade;" in block
        assert "proxy_set_header Connection $connection_upgrade;" in block
        assert "proxy_http_version 1.1;" in block


class TestTheLocalhostOnlyStack:
    """A stack with no `DOMAIN` still has one workable address, and it is not
    localhost: from inside the `nextcloud` container that is Nextcloud's own
    loopback. The host's address on the network is reachable from a sibling
    container and from a browser both, so the wizard offers it — off by
    default, because it is baked into Nextcloud at first install and a DHCP
    lease outlives nothing."""

    _LAN_URL = re.compile(r"^http://\d+\.\d+\.\d+\.\d+:\d+/standalone-signaling/$")

    def test_the_host_address_is_offered_and_is_not_the_default(self, tmp_path):
        env = run_wizard(
            tmp_path,
            {
                "DOMAIN": "",
                "Enable the Talk signaling server": "y",
                # Neither prompt is answered: the offer defaults to no, and the
                # URL prompt behind it defaults to empty.
            },
        )
        assert env["ISTOTA_TALK_SIGNALING_SERVER"] == ""
        assert env["ISTOTA_TALK_SIGNALING_ENABLED"] == "false"

    def test_accepting_it_registers_the_host_address(self, tmp_path):
        env = run_wizard(
            tmp_path,
            {
                "DOMAIN": "",
                "Enable the Talk signaling server": "y",
                "/standalone-signaling/?": "y",
            },
        )
        if env["ISTOTA_TALK_SIGNALING_SERVER"] == "":
            pytest.skip("no routable address on this host, so nothing was offered")
        assert self._LAN_URL.match(env["ISTOTA_TALK_SIGNALING_SERVER"]), env[
            "ISTOTA_TALK_SIGNALING_SERVER"
        ]
        assert env["ISTOTA_TALK_SIGNALING_ENABLED"] == "true"
        assert env["ISTOTA_TALK_SIGNALING_URL"] == "http://signaling:8080"
        assert "signaling" in env["COMPOSE_PROFILES"].split(",")
