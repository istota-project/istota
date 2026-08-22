"""The devbox network isolation, asserted rather than assumed.

``skill.md`` tells the model, in the prompt, that the devbox network blocks
RFC1918 and cloud metadata. Until ISSUE-283 nothing checked that claim. Four
``DROP`` rules in the ``DOCKER-USER`` chain and one sysctl are what back it,
and they lived in 39 lines of generated shell with no witness of any kind — a
mistyped CIDR, a lost ``-j DROP``, a rule appended to a chain nothing jumps
to, or a Jinja variable rendering empty would all have shipped silently, and
the failure mode is a container with more reach than the prompt says it has.

The rules exist in *three* places, which the entry did not mention and which
is its own drift risk: ``tasks/main.yml`` applies them immediately through the
``ansible.builtin.iptables`` module, ``istota-devbox-iptables.sh.j2``
re-applies them at boot behind ``After=docker.service`` (Docker flushes its own
chains on restart and preserves ``DOCKER-USER``), and a third pair of tasks
removes them when the devbox is disabled. Any one drifting from the others
leaves a host isolated until it reboots, or after it reboots but not before, or
still filtering a subnet the role has stopped owning. All three are read here
and required to agree — **including the rule comments**, because a comment
match is part of a rule's identity as far as ``iptables -C`` is concerned, and
a teardown that omits it silently deletes nothing.

**What this cannot see, and it is a lot.** This renders templates; it does not
run iptables. It proves the scripts ask for the right rules, not that a
container is unable to reach anything:

  * whether the rules are ever *reached*. Both writers append to
    ``DOCKER-USER``, and a stock dockerd before v28 ships that chain
    containing ``-j RETURN`` — everything appended after it is never
    evaluated. That is ISSUE-295, and it is why the append/insert flag is
    deliberately **not** pinned below: this file must not go red when that is
    fixed.
  * traffic terminating on the host. ``DOCKER-USER`` is reached from
    ``FORWARD``, so the bridge gateway address is outside these rules
    entirely (ISSUE-296), as is any published port, which ``docker-proxy``
    re-originates from the host (ISSUE-297).
  * address space the four destinations do not name, and IPv6, which no
    ``ip6tables`` rule anywhere covers (ISSUE-298).
  * a source address the container chose for itself; ``NET_RAW`` permits that
    and every rule here is ``-s``-scoped (ISSUE-299).

For a witness that a container really cannot reach a destination you want the
``linux``-marked test described in ISSUE-283, which applies the script inside a
network namespace and probes. That is deliberately not in this file.

The assertions are functions over rendered text rather than test bodies, so
``TestTheseAssertionsCanFail`` can feed them deliberately broken renderings.
A test asserting against generated output tells you almost nothing unless it
has been shown able to go red — and the first cut of this file passed against
a script appending its four perfect rules to a chain nothing references.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
SCRIPT_TEMPLATE = ANSIBLE / "templates" / "istota-devbox-iptables.sh.j2"
UNIT_TEMPLATE = ANSIBLE / "templates" / "istota-devbox-iptables.service.j2"
COMPOSE_TEMPLATE = ANSIBLE / "templates" / "docker-compose.devbox.yml.j2"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"

CHAIN = "DOCKER-USER"

# The destinations the prompt's promise rests on, each with the comment that
# identifies it in the chain. Spelled out here rather than derived from the
# templates, so a rule silently disappearing from one of them fails. The
# comments are load-bearing: `iptables -C` matches them, so the apply and the
# teardown must agree on the exact string or the teardown deletes nothing.
EXPECTED_RULES = {
    "169.254.169.254/32": "istota-devbox: block cloud metadata",
    "10.0.0.0/8": "istota-devbox: block 10.0.0.0/8",
    "172.16.0.0/12": "istota-devbox: block 172.16.0.0/12",
    "192.168.0.0/16": "istota-devbox: block 192.168.0.0/16",
}
EXPECTED_DESTINATIONS = set(EXPECTED_RULES)


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS_FILE.read_text())


def _render(template: Path, **overrides) -> str:
    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    return env.from_string(template.read_text()).render(**{**_defaults(), **overrides})


# ---------------------------------------------------------------------------
# The assertions, as functions over rendered text.

_ENSURE_DROP_CALL = re.compile(r'^ensure_drop\s+"([^"]*)"\s+"([^"]*)"\s*$', re.M)

# `-A` today, `-I <chain> 1` once ISSUE-295 lands. Both are "add a rule", and
# this file asserts everything about the rule except which of the two it is.
_ADD_RULE = re.compile(r"iptables -(?:A|I)\b[^\n]*")


def dropped_rules(script: str) -> dict[str, str]:
    return dict(_ENSURE_DROP_CALL.findall(script))


def dropped_destinations(script: str) -> set[str]:
    return set(dropped_rules(script))


def check_no_jinja_survives(text: str) -> None:
    for token in ("{{", "}}", "{%", "%}"):
        assert token not in text, f"unrendered Jinja {token!r} survived into the output"


def check_no_empty_expansion(script: str) -> None:
    """A Jinja variable that renders empty leaves `SUBNET=""` behind, and every
    rule is then scoped to nothing. iptables rejects it, so the boot unit fails
    and the host runs with no rules at all until someone reads the journal.

    (An earlier cut of this masked the opening quote of `SUBNET="` to avoid a
    false positive that does not exist — the healthy render contains no `""` at
    all — which made the empty case the one thing it could not see.)
    """
    assert '""' not in script, (
        "a value renders empty — a Jinja variable expanded to nothing"
    )
    for dest, comment in _ENSURE_DROP_CALL.findall(script):
        assert dest.strip(), "ensure_drop called with an empty destination"
        assert comment.strip(), "ensure_drop called with an empty comment"


def check_subnet_is_a_real_cidr(script: str) -> None:
    match = re.search(r'^SUBNET="([^"]*)"\s*$', script, re.M)
    assert match, "the script no longer sets SUBNET"
    subnet = match.group(1)
    assert subnet.strip(), "SUBNET rendered empty"
    # Raises ValueError on anything that is not a network. A subnet with host
    # bits set (172.30.0.5/24) is a typo, and strict=True is what catches it.
    ipaddress.ip_network(subnet, strict=True)


def check_every_rule_is_a_guarded_drop(script: str) -> None:
    """`ensure_drop` has to test with `-C` before adding, and both forms have to
    name the right chain, scope to the subnet, and jump to DROP.

    The guard is not tidiness: the boot unit re-runs on every boot, so an
    unguarded add grows a duplicate rule each time.
    """
    body = re.search(r"ensure_drop\(\)\s*\{(.*?)\n\}", script, re.S)
    assert body, "the script no longer defines an ensure_drop() function"
    # Fold shell line continuations first, so a rule split across lines is read
    # as the one command it is.
    text = re.sub(r"\\\n\s*", " ", body.group(1))

    checks = re.findall(r"iptables -C\b[^\n]*", text)
    adds = _ADD_RULE.findall(text)
    assert len(checks) == 1, f"expected one `iptables -C` guard, found {len(checks)}"
    assert len(adds) == 1, f"expected one rule-adding call, found {len(adds)}"

    guard_at, add_at = text.index(checks[0]), text.index(adds[0])
    assert guard_at < add_at, (
        "the add is not guarded by the check — the rule would be added again "
        "on every boot"
    )
    conditional = re.search(r"if\s*!", text)
    assert conditional and conditional.start() < guard_at, (
        "the `-C` is not inside a negated conditional preceding it, so it does "
        "not gate the add"
    )

    for rule in checks + adds:
        flat = " ".join(rule.split())
        # The chain is the assertion this file was missing: four perfect DROP
        # rules appended to a chain nothing jumps to are inert, and every other
        # check here passes on them.
        assert re.search(rf"iptables -[ACI] {CHAIN}\b", flat), (
            f"rule does not target the {CHAIN} chain: {flat!r}"
        )
        # Not `endswith`: the `-C` form carries `2>/dev/null; then` after the
        # target. What matters is that DROP is the jump, and the only one.
        assert " -j DROP" in flat, f"rule does not jump to DROP: {flat!r}"
        assert flat.count(" -j ") == 1, f"rule has more than one target: {flat!r}"
        assert '-s "$SUBNET"' in flat, f"rule is not scoped to the subnet: {flat!r}"
        assert '-d "$dest"' in flat, f"rule names no destination: {flat!r}"
        assert '--comment "$comment"' in flat, (
            f"rule carries no comment, so the teardown's `-C` cannot match it: {flat!r}"
        )


def check_it_fails_loudly(script: str) -> None:
    """`set -e` is what turns a rejected rule into a failed unit rather than a
    host that boots looking healthy with three of its four rules."""
    assert re.search(r"^set -euo pipefail\s*$", script, re.M), (
        "the script does not `set -euo pipefail`, so a failed rule is silent"
    )


ALL_CHECKS = (
    check_no_jinja_survives,
    check_no_empty_expansion,
    check_subnet_is_a_real_cidr,
    check_every_rule_is_a_guarded_drop,
    check_it_fails_loudly,
)


@pytest.fixture(scope="module")
def script() -> str:
    return _render(SCRIPT_TEMPLATE)


@pytest.fixture(scope="module")
def tasks() -> list[dict]:
    return yaml.safe_load(TASKS_FILE.read_text())


class TestTheBootScriptAsksForTheRightRules:
    def test_all_four_destinations_are_dropped(self, script):
        assert dropped_rules(script) == EXPECTED_RULES

    @pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda c: c.__name__)
    def test_structural_check(self, check, script):
        check(script)


class TestTheFlagsActuallyGate:
    """Both blocks are wrapped in `{% if %}`, and this asserts the flags gate
    the *rendered script* — nothing more. Note what that does not cover: on an
    already-deployed host, setting `istota_devbox_block_rfc1918: false` skips
    the apply tasks and does not reach the teardown (which is gated on
    `istota_devbox_enabled`), so the live rules stay until the next reboot
    regenerates the script without them."""

    def test_metadata_flag_removes_only_the_metadata_rule(self):
        script = _render(SCRIPT_TEMPLATE, istota_devbox_block_metadata=False)
        assert dropped_destinations(script) == EXPECTED_DESTINATIONS - {
            "169.254.169.254/32"
        }

    def test_rfc1918_flag_removes_only_the_rfc1918_rules(self):
        script = _render(SCRIPT_TEMPLATE, istota_devbox_block_rfc1918=False)
        assert dropped_destinations(script) == {"169.254.169.254/32"}

    def test_both_default_to_on(self):
        defaults = _defaults()
        assert defaults["istota_devbox_block_metadata"] is True
        assert defaults["istota_devbox_block_rfc1918"] is True


class TestTheThreeSourcesAgree:
    """`tasks/main.yml` applies the rules now, the boot script re-applies them
    after a reboot, and a third pair of tasks removes them when the devbox is
    disabled. One drifting from the others gives a host isolated before a
    reboot but not after, or the reverse, or one still filtering a subnet the
    role has stopped owning — and each of those reads as working when you check
    it the day you deploy."""

    @pytest.fixture
    def iptables_tasks(self, tasks) -> list[dict]:
        return [t for t in tasks if "ansible.builtin.iptables" in t]

    def test_the_tasks_target_docker_user_with_drop(self, iptables_tasks):
        assert iptables_tasks, "no ansible.builtin.iptables tasks found — file moved?"
        for task in iptables_tasks:
            spec = task["ansible.builtin.iptables"]
            assert spec["chain"] == CHAIN
            assert spec["jump"] == "DROP"
            assert spec["state"] in {"present", "absent"}
            assert spec["source"] == "{{ istota_devbox_network_subnet }}"

    @staticmethod
    def _rules(tasks: list[dict], state: str) -> dict[str, str]:
        """The (destination, comment) pairs a set of tasks applies or removes.

        Comment as well as destination, because that is the field the drift
        actually happened in: `iptables -C` matches the comment, so a teardown
        naming the right destination and no comment deletes nothing at all.
        """
        found = {}
        for task in tasks:
            spec = task["ansible.builtin.iptables"]
            if spec["state"] != state:
                continue
            comment = spec.get("comment", "")
            dest = spec["destination"]
            if dest == "{{ item }}":
                for item in task["loop"]:
                    found[item] = comment.replace("{{ item }}", item)
            else:
                found[dest] = comment
        return found

    def test_the_tasks_apply_the_same_rules_as_the_boot_script(
        self, iptables_tasks, script
    ):
        applied = self._rules(iptables_tasks, "present")
        assert applied == EXPECTED_RULES
        assert applied == dropped_rules(script), (
            "the immediate tasks and the boot script disagree about which "
            "destinations to block, or about the comments identifying them"
        )

    def test_disabling_the_devbox_removes_every_rule_it_added(self, iptables_tasks):
        """The bug this assertion was written to catch and originally missed:
        the teardown named all four destinations and no comments, so its `-C`
        probe never matched, the module reported ok/changed=false, and all four
        rules stayed on the host after `istota_devbox_enabled: false`."""
        assert self._rules(iptables_tasks, "absent") == EXPECTED_RULES, (
            "the teardown does not remove exactly the rules the role applies, "
            "comments included — a delete spec whose comment differs matches "
            "nothing and silently deletes nothing"
        )

    def test_both_paths_add_rules_the_same_way(self, iptables_tasks, script):
        """Append versus insert has to be the same on both paths, or a reboot
        changes the rules' position in the chain. Which of the two it is, this
        file does not pin — see ISSUE-295."""
        module_actions = {
            task["ansible.builtin.iptables"].get("action", "append")
            for task in iptables_tasks
            if task["ansible.builtin.iptables"]["state"] == "present"
        }
        assert len(module_actions) == 1, (
            f"the apply tasks disagree among themselves: {module_actions}"
        )
        script_flag = _ADD_RULE.search(script or "") or _ADD_RULE.search(
            re.sub(r"\\\n\s*", " ", SCRIPT_TEMPLATE.read_text())
        )
        assert script_flag, "no rule-adding call found in the boot script"
        script_action = "append" if " -A " in script_flag.group(0) else "insert"
        assert script_action in module_actions, (
            f"the boot script {script_action}s while the tasks {module_actions} — "
            "a reboot would move the rules within the chain"
        )

    def test_the_bridge_sysctl_is_set(self, tasks):
        """Without `net.bridge.bridge-nf-call-iptables`, traffic between two
        containers on the same bridge never traverses the chain, so the DROP
        rules are present and inert. This is the only thing stopping one user's
        devbox reaching another's."""
        matching = [
            t for t in tasks
            if "ansible.posix.sysctl" in t
            and t["ansible.posix.sysctl"]["name"] == "net.bridge.bridge-nf-call-iptables"
        ]
        assert len(matching) == 1, "the bridge-nf sysctl task is missing"
        assert str(matching[0]["ansible.posix.sysctl"]["value"]) == "1"

    def test_the_module_that_sysctl_needs_is_loaded_at_boot(self, tasks):
        """The sysctl task is `failed_when: false`, so on a host where
        br_netfilter is not loaded it passes having done nothing. A
        modules-load.d drop-in is what makes it stick across a reboot — and the
        destination is checked, or a README mentioning the module would do."""
        drop_ins = [
            t["copy"] for t in tasks
            if "copy" in t
            and "/etc/modules-load.d/" in str(t["copy"].get("dest", ""))
        ]
        assert any("br_netfilter" in d.get("content", "") for d in drop_ins), (
            "nothing under /etc/modules-load.d arranges for br_netfilter to "
            "load at boot"
        )

    def test_the_rules_scope_the_subnet_the_network_actually_uses(self):
        """The one drift that would open the boundary completely while leaving
        every other assertion here green: the rules are `-s
        {{ istota_devbox_network_subnet }}`, and if the network stopped using
        that subnet they would all be correctly formed and scoped to a range no
        container has.

        Asserted against the template text rather than a rendered document:
        the compose template interpolates `istota_home`, which is itself a
        template, so rendering it needs the recursive fixed-point resolution
        that lives in `test_ansible_config_template.py`. The property here is
        that both sides name the *same variable*, which the text carries
        directly.
        """
        var = "istota_devbox_network_subnet"
        network = re.search(r"^\s*- subnet:\s*(\S.*?)\s*$",
                            COMPOSE_TEMPLATE.read_text(), re.M)
        assert network, "the devbox compose template no longer pins a subnet"
        assert network.group(1) == "{{ %s }}" % var, (
            f"the network is on {network.group(1)}, the rules scope "
            f"{{{{ {var} }}}} — a rule scoped to a subnet no container uses "
            f"is correctly formed and completely inert"
        )
        ipaddress.ip_network(_defaults()[var], strict=True)


class TestTheUnitRunsAfterDocker:
    """Docker programs its own chains at daemon start. A oneshot that ran
    before it would append to a DOCKER-USER chain that does not exist yet."""

    @pytest.fixture(scope="class")
    def unit(self) -> str:
        return _render(UNIT_TEMPLATE)

    def test_ordering_and_dependency(self, unit):
        assert re.search(r"^After=docker\.service\s*$", unit, re.M)
        assert re.search(r"^Requires=docker\.service\s*$", unit, re.M)

    def test_nothing_unrendered_survives(self, unit):
        check_no_jinja_survives(unit)

    def test_it_execs_the_script_the_role_installs(self, unit, tasks):
        exec_start = re.search(r"^ExecStart=(\S+)\s*$", unit, re.M)
        assert exec_start, "the unit has no ExecStart"
        installed = [
            t["template"]["dest"] for t in tasks
            if "template" in t
            and t["template"]["src"] == "istota-devbox-iptables.sh.j2"
        ]
        assert installed == [exec_start.group(1)], (
            f"the unit execs {exec_start.group(1)}, the role installs {installed}"
        )

    def test_the_unit_file_and_the_enable_task_name_the_same_service(self, tasks):
        dests = [
            Path(t["template"]["dest"]).name for t in tasks
            if "template" in t
            and t["template"]["src"] == "istota-devbox-iptables.service.j2"
        ]
        enabled = [
            t["systemd"]["name"] for t in tasks
            if "systemd" in t and "devbox-iptables" in str(t["systemd"].get("name", ""))
        ]
        assert dests, "the unit template is not deployed by any task"
        assert enabled, "no systemd task references the devbox-iptables unit"
        assert set(enabled) == set(dests), (
            f"the role writes {dests} and enables {enabled}"
        )

    def test_it_is_a_oneshot_that_stays_active(self, unit):
        assert re.search(r"^Type=oneshot\s*$", unit, re.M)
        assert re.search(r"^RemainAfterExit=yes\s*$", unit, re.M)


class TestTheseAssertionsCanFail:
    """The negative control. Every check above asserts against generated text,
    and reading such a check tells you almost nothing about whether it can go
    red — the repo has been bitten once already by a tier that passed against
    an artifact missing the thing it asserted. Each mutation below is a defect
    that would really ship: feed it in, require the matching check to reject it.

    Three of these exist because the first cut of this file did *not* reject
    them: a rule sent to a chain nothing jumps to, a rule sent to FORWARD, and
    a rule carrying no comment.
    """

    @pytest.mark.parametrize(
        "mutate, check",
        [
            # A lost `-j DROP` — the rule then matches and falls through.
            (lambda s: s.replace("-j DROP", "-j RETURN"),
             check_every_rule_is_a_guarded_drop),
            # Rules appended to a chain nothing jumps to. Perfectly formed and
            # completely inert; every other check here passes on it.
            (lambda s: s.replace(CHAIN, "ISTOTA-DEVBOX"),
             check_every_rule_is_a_guarded_drop),
            # Rules put straight into FORWARD, bypassing Docker's own chain.
            (lambda s: s.replace(CHAIN, "FORWARD"),
             check_every_rule_is_a_guarded_drop),
            # The comment dropped — the teardown's `-C` can then never match.
            (lambda s: s.replace(' -m comment --comment "$comment"', ""),
             check_every_rule_is_a_guarded_drop),
            # Source and destination swapped.
            (lambda s: s.replace('-s "$SUBNET" -d "$dest"', '-s "$dest" -d "$SUBNET"'),
             check_every_rule_is_a_guarded_drop),
            # A bare add — the chain grows a duplicate rule every boot.
            (lambda s: re.sub(r"if ! iptables -C.*?fi",
                              'iptables -A DOCKER-USER -s "$SUBNET" -d "$dest" '
                              '-m comment --comment "$comment" -j DROP',
                              s, flags=re.S),
             check_every_rule_is_a_guarded_drop),
            # The subnet variable rendered empty.
            (lambda s: re.sub(r'^SUBNET="[^"]*"$', 'SUBNET=""', s, flags=re.M),
             check_subnet_is_a_real_cidr),
            # Same, caught by the empty-expansion check rather than the CIDR one.
            (lambda s: re.sub(r'^SUBNET="[^"]*"$', 'SUBNET=""', s, flags=re.M),
             check_no_empty_expansion),
            # A subnet with host bits set — a typo iptables would accept.
            (lambda s: re.sub(r'^SUBNET="[^"]*"$', 'SUBNET="172.30.0.5/24"', s,
                              flags=re.M),
             check_subnet_is_a_real_cidr),
            # An unrendered Jinja expression left in the output.
            (lambda s: s.replace("SUBNET=", "SUBNET={{ oops }}", 1),
             check_no_jinja_survives),
            # `set -e` dropped, so a rejected rule leaves the unit green.
            (lambda s: s.replace("set -euo pipefail", "set -uo pipefail"),
             check_it_fails_loudly),
        ],
        ids=[
            "drop_becomes_return", "unreferenced_chain", "straight_to_forward",
            "comment_dropped", "source_dest_swapped", "unguarded_add",
            "empty_subnet_cidr", "empty_subnet_expansion", "subnet_with_host_bits",
            "unrendered_jinja", "no_set_e",
        ],
    )
    def test_a_broken_script_is_rejected(self, mutate, check, script):
        broken = mutate(script)
        assert broken != script, "the mutation changed nothing — anchor stale"
        # ValueError as well as AssertionError: `ip_network` rejects a malformed
        # subnet by raising, and a check that refuses is a check that worked.
        with pytest.raises((AssertionError, ValueError)):
            check(broken)

    def test_a_missing_rule_is_noticed(self, script):
        broken = script.replace('ensure_drop "10.0.0.0/8"', "")
        assert dropped_destinations(broken) != EXPECTED_DESTINATIONS

    def test_the_sources_are_compared_by_value_not_by_count(self, script):
        """Both sides having four entries is not the property; being the same
        four is."""
        broken = script.replace("192.168.0.0/16", "192.168.0.0/24")
        assert dropped_destinations(broken) != EXPECTED_DESTINATIONS

    @pytest.mark.parametrize(
        "mutate",
        [
            # The teardown drops its comments — the bug that was really there.
            lambda t: {**t, "comment": None},
            # A destination quietly changed on one side only.
            lambda t: {**t, "destination": "10.0.0.0/24"},
        ],
        ids=["comment_dropped", "destination_changed"],
    )
    def test_the_task_side_parser_can_go_red(self, tasks, mutate):
        """The script-side mutations above leave the YAML parser untested, and
        an unfalsified comparator is exactly what let the teardown bug through.
        """
        iptables_tasks = [t for t in tasks if "ansible.builtin.iptables" in t]
        mutated = []
        for task in iptables_tasks:
            spec = dict(task["ansible.builtin.iptables"])
            if spec["state"] == "absent":
                spec = {k: v for k, v in mutate(spec).items() if v is not None}
            mutated.append({**task, "ansible.builtin.iptables": spec})
        assert TestTheThreeSourcesAgree._rules(mutated, "absent") != EXPECTED_RULES
