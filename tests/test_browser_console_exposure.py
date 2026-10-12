"""Where the browser stack's noVNC console listens, and whether it has a password.

The browser container holds a persistent, logged-in Chrome profile. noVNC
serves an *interactive* view of that Chrome — not an API, a console. So two
properties decide whether that console is a hole: which address the port is
published on, and whether x11vnc was given a password.

Both were wrong by default (ISSUE-297). The port was published on ``0.0.0.0``
while the browser's own API port on the line directly above was correctly bound
to ``127.0.0.1``, and ``docker/browser/entrypoint.sh`` passes ``-passwd`` only
when ``VNC_PASSWORD`` is non-empty. A default deployment therefore published an
unauthenticated interactive console of a logged-in browser on every interface.

**The fix is not "bind to localhost".** Reaching that console over a VPN is the
reason it exists — it is how you see what the browser is doing when something
goes wrong. Hardcoding ``0.0.0.0`` is wrong; hardcoding ``127.0.0.1`` is wrong
in the other direction and would quietly remove a working troubleshooting path.
So the address is a variable that defaults to loopback.

What this file cannot see: whether the host firewall independently blocks the
port, and whether Docker's own DNAT rules bypass that firewall. Both are real
and neither is knowable from a compose file. Binding the publish address is
what makes the answer not depend on them.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "docker" / "browser" / "entrypoint.sh"

# Every compose source that publishes this console. It was three once (the
# Ansible template and a standalone browser compose file beside this one), and
# fixing only one left the others publishing an unauthenticated console on
# every interface — the shape of defect this file exists for.
COMPOSE_SOURCES = {
    "docker-full": REPO / "docker" / "docker-compose.yml",
}

# A quoted list entry under `ports:`. The spec inside is split separately,
# because a naive split on ":" is wrong in both directions here: a compose
# `${BROWSER_VNC_BIND:-127.0.0.1}` contains a colon of its own, and an IPv6
# host address contains several. Getting this wrong fails open — the port
# simply is not found, and a check that cannot see a port reports no problem
# with it.
_PORT_LINE = re.compile(r'^\s*-\s*"([^"]+)"', re.M)
_ATOM = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}|\[[^\]]*\]")


def _split_port_spec(spec: str) -> list[str]:
    """Split a compose port spec on its structural colons only."""
    atoms: list[str] = []

    def stash(match: re.Match) -> str:
        atoms.append(match.group(0))
        return f"\x00{len(atoms) - 1}\x00"

    masked = _ATOM.sub(stash, spec)
    return [
        re.sub(r"\x00(\d+)\x00", lambda m: atoms[int(m.group(1))], part)
        for part in masked.split(":")
    ]


def published_ports(text: str) -> dict[str, str]:
    """container port -> host bind address, as written in the source.

    A two-part spec (``"6080:6080"``) names no host address, which compose reads
    as every interface — reported as ``""`` so it is caught rather than skipped.
    """
    found: dict[str, str] = {}
    for spec in _PORT_LINE.findall(text):
        parts = _split_port_spec(spec)
        container = parts[-1].split("/")[0]
        if not container.isdigit():
            continue
        found[container] = parts[0] if len(parts) >= 3 else ""
    return found


# What compose treats as "every interface": the wildcard, and an omitted
# address. Both have to be rejected, or a spec written the other way slips past.
WIDE_OPEN = {"0.0.0.0", "", "::", "[::]"}

# The browser's own two ports. Port 80 on the full stack is the reverse proxy
# and is public on purpose, so it is not in scope here.
BROWSER_PORTS = {"6080": "noVNC console", "9223": "browser API"}


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class TestTheConsoleIsNotPublishedOnEveryInterface:
    @pytest.mark.parametrize("name", sorted(COMPOSE_SOURCES))
    def test_no_browser_port_publishes_on_all_interfaces(self, name):
        """Scoped to the browser's two ports, deliberately.

        The full stack also publishes 80 with no host address, which is the
        reverse proxy and is meant to be public — asserting over *every* port
        would make this test a running argument about which services are
        allowed to face the internet, and the first response to that argument
        would be to weaken the test. The browser's console and API are not
        public under any deployment, so they are what this pins.
        """
        text = COMPOSE_SOURCES[name].read_text()
        published = published_ports(text)
        assert published, f"{name} publishes no ports — did the file move?"
        for port, what in BROWSER_PORTS.items():
            host_ip = published.get(port)
            if host_ip is None:
                continue
            assert host_ip not in WIDE_OPEN, (
                f"{name}: the {what} (port {port}) publishes on "
                f"{host_ip or 'every interface (no host address given)'}"
            )

    def test_the_console_is_published_by_every_source(self):
        """Guards the vacuous pass in the check above, which skips a port it
        cannot find — and a port it cannot find is exactly what a parser bug
        looks like."""
        for name, path in COMPOSE_SOURCES.items():
            assert "6080" in published_ports(path.read_text()), (
                f"{name} publishes no noVNC port, so every check of it above "
                "passed by skipping"
            )

    @pytest.mark.parametrize("name", sorted(COMPOSE_SOURCES))
    def test_every_source_defaults_the_console_to_loopback(self, name):
        """What has to hold is that an operator who sets nothing gets
        loopback, and one who sets an address gets that one."""
        text = COMPOSE_SOURCES[name].read_text()
        bind = published_ports(text).get("6080")
        assert bind is not None, f"{name} no longer publishes the noVNC port"
        match = re.fullmatch(r"\$\{[A-Z_]+:-([^}]+)\}", bind)
        assert match, (
            f"{name} publishes noVNC on {bind!r}, which is neither a "
            "variable with a default nor something an operator can change"
        )
        assert _is_loopback(match.group(1)), (
            f"{name} defaults the noVNC bind to {match.group(1)!r}"
        )


class TestTheEntrypointStillHonoursThePassword:
    """A password the operator sets has to reach x11vnc."""

    def test_a_password_is_passed_to_x11vnc(self, monkeypatch):
        monkeypatch.syspath_prepend(str(ENTRYPOINT.parent))
        import importlib.util
        import sys
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            "browser_pool_password_test", ENTRYPOINT.parent / "pool.py",
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"chrome": mock.Mock(), spec.name: module}):
            spec.loader.exec_module(module)
        for password in ("", "test-password"):
            inst = module.BrowserInstance("alice", "/unused", ":100", 9300, 5900, 0)
            with mock.patch.dict("os.environ", {"VNC_PASSWORD": password}), \
                 mock.patch.object(module, "_wait_for_display"), \
                 mock.patch.object(module, "_wait_for_vnc"), \
                 mock.patch.object(module.subprocess, "Popen") as popen:
                module._start_display(inst)
            args = popen.call_args_list[-1].args[0]
            assert ("-passwd" in args) == bool(password)
            if password:
                assert args[args.index("-passwd") + 1] == password


class TestTheseAssertionsCanFail:
    """Each check above asserts over template text, and reading one tells you
    little about whether it can go red."""

    @pytest.mark.parametrize(
        "source, mutate, why",
        [
            ("docker-full",
             lambda t: t.replace("${BROWSER_VNC_BIND:-127.0.0.1}", "0.0.0.0"),
             "the Docker stack back to every interface"),
            ("docker-full",
             lambda t: t.replace("${BROWSER_VNC_BIND:-127.0.0.1}",
                                 "${BROWSER_VNC_BIND:-0.0.0.0}"),
             "a variable whose default is still wide open"),
        ],
        ids=["docker_all_interfaces", "docker_default_wide_open"],
    )
    def test_a_broken_compose_is_rejected(self, monkeypatch, tmp_path, source,
                                          mutate, why):
        """Feed the mutation to the *production* checks rather than to a copy of
        their logic.

        The first cut of this control re-implemented the assertions inline,
        which meant it proved the mutation anchors were live and nothing about
        whether the real checks work — weaken one of them and the control stays
        green. Writing the mutated text to a file and repointing
        `COMPOSE_SOURCES` is what makes it exercise the same code path a
        regression would.
        """
        original = COMPOSE_SOURCES[source]
        broken_text = mutate(original.read_text())
        assert broken_text != original.read_text(), (
            f"the mutation changed nothing — anchor stale ({why})"
        )
        broken = tmp_path / original.name
        broken.write_text(broken_text)
        monkeypatch.setitem(COMPOSE_SOURCES, source, broken)

        checks = TestTheConsoleIsNotPublishedOnEveryInterface()
        with pytest.raises(AssertionError):
            checks.test_no_browser_port_publishes_on_all_interfaces(source)
            checks.test_every_source_defaults_the_console_to_loopback(source)

    def test_a_loopback_default_check_rejects_a_wildcard(self):
        assert not _is_loopback("0.0.0.0")
        assert not _is_loopback("10.0.0.5")
        assert _is_loopback("127.0.0.1")
