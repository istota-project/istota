"""Where the browser stack's noVNC console listens, and whether it has a password.

The browser container holds a persistent, logged-in Chrome profile at
``{{ istota_home }}/data/browser-profile``. noVNC serves an *interactive* view
of that Chrome — not an API, a console. So two properties decide whether that
console is a hole: which address the port is published on, and whether x11vnc
was given a password.

Both were wrong by default (ISSUE-297). The port was published on ``0.0.0.0``
while the browser's own API port on the line directly above was correctly bound
to ``127.0.0.1``, and ``istota_browser_vnc_password`` defaulted to ``""`` with
``docker/browser/entrypoint.sh`` passing ``-passwd`` only when it is non-empty.
A default deployment therefore published an unauthenticated interactive console
of a logged-in browser on every interface.

**The fix is not "bind to localhost".** Reaching that console over a VPN is the
reason it exists — it is how you see what the browser is doing when something
goes wrong, and ``istota_browser_vnc_external_url`` in the same defaults block
exists precisely because external access is expected. Hardcoding ``0.0.0.0`` is
wrong; hardcoding ``127.0.0.1`` is wrong in the other direction and would
quietly remove a working troubleshooting path on upgrade. So the address is a
variable that defaults to loopback, and the password becomes mandatory exactly
when the address is not loopback — the deployment refuses rather than silently
serving an open console.

What this file cannot see: whether the host firewall independently blocks the
port, and whether Docker's own DNAT rules bypass that firewall. Both are real
and neither is knowable from a template. Binding the publish address is what
makes the answer not depend on them.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
COMPOSE_TEMPLATE = ANSIBLE / "templates" / "docker-compose.browser.yml.j2"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"
ENTRYPOINT = REPO / "docker" / "browser" / "entrypoint.sh"

# Every compose source that publishes this console. The Ansible template is one
# of three, and fixing only it left the two Docker ones publishing an
# unauthenticated console on every interface — the shape of defect this file
# exists for, surviving in the deployment shape nobody was looking at.
# `docs/deployment/docker.md` and AGENTS.md both name the Docker stack as a
# first-class target, so "the default is safe" has to be true of all three.
COMPOSE_SOURCES = {
    "ansible": COMPOSE_TEMPLATE,
    "docker-full": REPO / "docker" / "docker-compose.yml",
    "docker-browser": REPO / "docker" / "docker-compose.browser.yml",
}

BIND_VAR = "istota_browser_vnc_bind_address"
PASSWORD_VAR = "istota_browser_vnc_password"

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


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS_FILE.read_text())


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS_FILE.read_text())


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


@pytest.fixture(scope="module")
def compose() -> str:
    return COMPOSE_TEMPLATE.read_text()


class TestTheConsoleIsNotPublishedOnEveryInterface:
    def test_the_vnc_port_binds_a_variable_not_a_literal(self, compose):
        """`0.0.0.0` hardcoded is the defect; `127.0.0.1` hardcoded would remove
        the VPN troubleshooting path the console exists for. Neither belongs in
        the template — the operator's topology decides."""
        bind = published_ports(compose).get("6080")
        assert bind is not None, "the noVNC port is no longer published at all"
        assert bind.startswith("{{") and BIND_VAR in bind, (
            f"noVNC publishes on {bind!r}; it should take the address from "
            f"{BIND_VAR} so a deployment can choose"
        )

    def test_the_default_bind_address_is_loopback(self):
        value = _defaults()[BIND_VAR]
        assert _is_loopback(value), (
            f"{BIND_VAR} defaults to {value!r}. A default deployment would "
            "publish an interactive console of a logged-in browser there"
        )

    def test_the_api_port_stays_on_loopback(self, compose):
        """It already was, and it is the neighbouring line — a regression here
        would be easy to make while editing the one below it."""
        bind = published_ports(compose).get("9223")
        assert bind is not None, "the browser API port is no longer published"
        assert _is_loopback(bind), (
            f"the browser API port publishes on {bind!r}, not loopback"
        )

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
        """Each source spells the default its own way — a Jinja variable whose
        default lives in `defaults/main.yml`, or a compose `${VAR:-default}`.
        What has to hold everywhere is that an operator who sets nothing gets
        loopback."""
        text = COMPOSE_SOURCES[name].read_text()
        bind = published_ports(text).get("6080")
        assert bind is not None, f"{name} no longer publishes the noVNC port"
        if bind.startswith("{{"):
            assert BIND_VAR in bind
            assert _is_loopback(_defaults()[BIND_VAR])
        else:
            match = re.fullmatch(r"\$\{[A-Z_]+:-([^}]+)\}", bind)
            assert match, (
                f"{name} publishes noVNC on {bind!r}, which is neither a "
                "variable with a default nor something an operator can change"
            )
            assert _is_loopback(match.group(1)), (
                f"{name} defaults the noVNC bind to {match.group(1)!r}"
            )


class TestAnExposedConsoleIsRefused:
    """The guard, exercised by evaluating its conditions rather than by reading them.

    Reading a list of Jinja expressions tells you almost nothing about what they
    decide, and this one has to get several awkward cases right: an empty value,
    a key with no value at all, two spellings of IPv6 loopback, and an address
    with a port smuggled into it. So the conditions are pulled out of the
    playbook and run — the same shape as `TestTheseAssertionsCanFail` elsewhere
    in the suite, and the reason the first cut's bugs were findable at all.

    The password default stays empty deliberately: generating one leaves the
    operator unable to log in to a console they can reach, and generating on
    upgrade would change the meaning of an existing setting. Refusing the deploy
    says what is wrong and what to do about it.
    """

    GUARD_NAME = "Refuse an unauthenticated or unreachable noVNC console"

    @pytest.fixture(scope="class")
    def guard(self) -> dict:
        matching = [t for t in _tasks() if t.get("name") == self.GUARD_NAME]
        assert len(matching) == 1, (
            f"expected exactly one task named {self.GUARD_NAME!r}, "
            f"found {len(matching)}"
        )
        return matching[0]

    @staticmethod
    def _evaluate(guard: dict, bind, password, external) -> bool:
        """Run the guard's conditions under Jinja with Ansible's `match` test."""
        from jinja2 import Environment

        env = Environment()
        env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
        env.filters["bool"] = (
            lambda v: str(v).strip().lower() in ("true", "yes", "on", "1")
        )
        context = {
            "istota_browser_vnc_bind_address": bind,
            "istota_browser_vnc_password": password,
            "istota_browser_vnc_external_url": external,
        }
        resolved = {
            key: env.from_string(value).render(**context)
            for key, value in guard["vars"].items()
        }
        scope = {**context, **resolved}
        return all(
            env.from_string("{{ (%s) | bool }}" % condition).render(**scope) == "True"
            for condition in guard["ansible.builtin.assert"]["that"]
        )

    @pytest.mark.parametrize(
        "bind, password, external, expected, why",
        [
            ("127.0.0.1", "", "", True, "the default: loopback needs no password"),
            ("127.0.0.1", "secret", "", True, "loopback with a password is fine"),
            ("10.0.0.5", "secret", "", True,
             "a VPN address with a password — the shape this exists to allow"),
            ("10.0.0.5", "", "", False,
             "a VPN address with no password — the ISSUE-297 defect itself"),
            ("0.0.0.0", "", "", False, "the wildcard with no password"),
            ("0.0.0.0", "secret", "", True,
             "the wildcard with a password is the operator's call to make"),
            ("", "secret", "", False,
             "an empty bind is every interface; a password must not rescue it"),
            (None, "", "", False,
             "a key written with no value must fail legibly, not raise TypeError"),
            ("::1", "", "", True, "IPv6 loopback, bare"),
            ("[::1]", "", "", True, "IPv6 loopback, the form compose wants"),
            ("localhost", "", "", True, "the hostname spelling of loopback"),
            ("127.0.0.1:6080", "", "", False,
             "a port smuggled into the address would render a 4-part port spec"),
            ("127.0.0.1", "", "https://vnc.example.com:6080", False,
             "an external URL that cannot resolve to a loopback-bound console"),
            ("10.0.0.5", "secret", "https://vnc.example.com:6080", True,
             "an external URL with a bind address that can serve it"),
        ],
    )
    def test_the_guard_decides_correctly(
        self, guard, bind, password, external, expected, why
    ):
        assert self._evaluate(guard, bind, password, external) is expected, why

    def test_the_guard_runs_before_the_compose_file_is_written(self):
        """A guard that writes the artifact and then refuses it is not a guard.

        The template task renders docker-compose.browser.yml to the host. If the
        assert ran after it, a refused run would abort with the wildcard-bound
        file on disk, where the watchdog or a manual `docker compose up` would
        use it — handlers do not fire on a failed play, but the file is still
        there.
        """
        names = [t.get("name") for t in _tasks()]
        assert names.index(self.GUARD_NAME) < names.index(
            "Deploy browser container docker-compose"
        ), "the guard runs after the compose file it refuses has been written"

    def test_the_guard_only_runs_when_the_browser_is_enabled(self, guard):
        assert "istota_browser_enabled" in str(guard.get("when", "")), (
            "the guard would fail a deployment that does not run the browser"
        )

    def test_the_guard_uses_no_collection_the_role_does_not_already_use(self, guard):
        """A guard that raises is worse than no guard: it fails the deploy it
        exists to protect, for a reason unrelated to what it checks.

        The first cut used `ansible.utils.ipaddr`, which appears nowhere else in
        the role, is declared as a dependency nowhere, and needs the netaddr
        package. It would have raised on every run.
        """
        allowed = {"ansible.builtin", "ansible.posix", "community.general"}
        text = str(guard["ansible.builtin.assert"]["that"]) + str(guard.get("vars", ""))
        used = set(re.findall(r"\b([a-z_]+\.[a-z_]+)\.[a-z_]+\(", text))
        assert used <= allowed, (
            f"the guard uses {used - allowed}, which the role does not otherwise "
            "depend on and nothing installs"
        )

    def test_the_failure_message_says_what_to_do(self, guard):
        fail_msg = str(guard["ansible.builtin.assert"].get("fail_msg", ""))
        for name in (PASSWORD_VAR, BIND_VAR, "istota_browser_vnc_external_url"):
            assert name in fail_msg, f"the message does not name {name}"


class TestAValueTheGuardAcceptsCanBeRendered:
    """Anything the guard blesses has to produce a port spec compose can parse.

    These two came apart in the first cut: the guard accepted a bare `::1`, and
    the template rendered `"::1:6080:6080"` — four colons, which compose
    rejects. A value that passes validation and then breaks the deploy is worse
    than one that is refused up front.
    """

    @pytest.mark.parametrize(
        "bind, expected",
        [
            ("127.0.0.1", "127.0.0.1:6080:6080"),
            ("10.0.0.5", "10.0.0.5:6080:6080"),
            ("::1", "[::1]:6080:6080"),
            ("[::1]", "[::1]:6080:6080"),
            ("localhost", "localhost:6080:6080"),
        ],
    )
    def test_the_rendered_port_spec_is_well_formed(self, bind, expected):
        from jinja2 import Environment, StrictUndefined

        env = Environment(
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        rendered = env.from_string(COMPOSE_TEMPLATE.read_text()).render(
            **{**_defaults(), BIND_VAR: bind}
        )
        spec = [
            line.strip().strip('-" ')
            for line in rendered.splitlines()
            if line.strip().endswith(':6080"')
        ]
        assert spec == [expected], f"rendered {spec}, wanted {[expected]}"
        # And compose can read it back as three fields.
        assert len(_split_port_spec(expected)) == 3


class TestTheEntrypointStillHonoursThePassword:
    """The guard is worth nothing if the container ignores the value."""

    def test_a_password_is_passed_to_x11vnc(self):
        text = ENTRYPOINT.read_text()
        assert "-passwd" in text, "x11vnc is no longer given a password at all"
        assert re.search(r'if\s+\[\s+-n\s+"\$VNC_PASSWORD"\s+\]', text), (
            "the entrypoint no longer gates -passwd on VNC_PASSWORD being set"
        )

    def test_the_password_reaches_the_container(self):
        """The env file is what carries it; a guard on a variable the container
        never sees would be theatre."""
        env_tasks = [
            task
            for task in _tasks()
            if "copy" in task
            and "VNC_PASSWORD" in str(task["copy"].get("content", ""))
        ]
        assert env_tasks, "nothing writes VNC_PASSWORD into the browser env file"


class TestTheseAssertionsCanFail:
    """Each check above asserts over template text, and reading one tells you
    little about whether it can go red."""

    @pytest.mark.parametrize(
        "source, mutate, why",
        [
            ("ansible",
             # The bind field is a Jinja expression now (it brackets IPv6), so
             # the mutation replaces the whole field rather than a fixed string.
             lambda t: re.sub(
                 r'^(\s*- ")[^"]*(:\{\{ istota_browser_vnc_port \}\}:6080")$',
                 r"\g<1>0.0.0.0\g<2>", t, flags=re.M),
             "the Ansible template back to every interface"),
            ("ansible",
             lambda t: t.replace("127.0.0.1:{{ istota_browser_api_port }}",
                                 "0.0.0.0:{{ istota_browser_api_port }}"),
             "the API port opened up"),
            ("docker-full",
             lambda t: t.replace("${BROWSER_VNC_BIND:-127.0.0.1}", "0.0.0.0"),
             "the Docker stack back to every interface"),
            ("docker-full",
             lambda t: t.replace("${BROWSER_VNC_BIND:-127.0.0.1}",
                                 "${BROWSER_VNC_BIND:-0.0.0.0}"),
             "a variable whose default is still wide open"),
            ("docker-browser",
             lambda t: t.replace("${BROWSER_VNC_BIND:-127.0.0.1}", "0.0.0.0"),
             "the standalone browser stack back to every interface"),
        ],
        ids=[
            "ansible_all_interfaces", "ansible_api_opened",
            "docker_all_interfaces", "docker_default_wide_open",
            "docker_browser_all_interfaces",
        ],
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
            if source == "ansible":
                checks.test_the_vnc_port_binds_a_variable_not_a_literal(broken_text)
                checks.test_the_api_port_stays_on_loopback(broken_text)

    def test_a_loopback_default_check_rejects_a_wildcard(self):
        assert not _is_loopback("0.0.0.0")
        assert not _is_loopback("10.0.0.5")
        assert _is_loopback("127.0.0.1")
