"""`security.devbox_netfilter` — does the live DOCKER-USER chain still reach our rules?

ISSUE-295 in one sentence: four correct DROP rules appended to a chain whose
first entry is `-j RETURN` are never evaluated, and `iptables -S` renders them
identically to four rules that work. Every other witness the repo has over this
boundary reads *templates* — `test_ansible_devbox_iptables.py` proves the role
asks for the right rules in the right position, which is necessary and says
nothing about the chain on a running host. An operator, a host-firewall
integration (`ufw-docker` inserts at the front), or a Docker Desktop daemon can
put a terminal rule ahead of ours at any time, and nothing would report it.

So this check exists to answer the one question the template tests structurally
cannot: on *this* host, right now, is there anything in front of our rules that
would stop them being reached.

The check is driven here against fabricated `iptables -S` output rather than
the host's real chain — a test that ran the real binary would be asserting
about the developer's laptop, and would SKIP on every machine the suite runs
on. What that costs is coverage of the parse-vs-reality seam, and the sample
outputs below are copied from real `iptables -S DOCKER-USER` runs on the
versions in the ISSUE-295 table rather than invented.
"""

from __future__ import annotations

import subprocess

import pytest

from istota import doctor
from istota.config import DevboxConfig
from istota.doctor import CHECKS, CHECK_SCOPES, DEPLOYMENT, FAIL, OK, SKIP, WARN

CHECK_NAME = "security.devbox_netfilter"

# The role's default devbox subnet (deploy/ansible/defaults/main.yml).
DEVBOX_SUBNET = "172.30.0.0/24"

# A rule as dockerd and the role really render it, comment included — the
# comment is how the check identifies our rules, so it is load-bearing here.
OURS = (
    '-A DOCKER-USER -s 172.30.0.0/24 -d {dest} -m comment '
    '--comment "istota-devbox: block {label}" -j DROP'
)


def _chain(*lines: str) -> str:
    """`iptables -S DOCKER-USER` output: the chain declaration, then rules."""
    return "\n".join(("-N DOCKER-USER", *lines)) + "\n"


def _our_rules() -> list[str]:
    return [
        OURS.format(dest="169.254.169.254/32", label="cloud metadata"),
        OURS.format(dest="10.0.0.0/8", label="10.0.0.0/8"),
        OURS.format(dest="172.16.0.0/12", label="172.16.0.0/12"),
        OURS.format(dest="192.168.0.0/16", label="192.168.0.0/16"),
    ]


def _our_destinations() -> list[str]:
    return [
        "169.254.169.254/32",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]


@pytest.fixture
def devbox_config(make_config):
    return make_config(devbox=DevboxConfig(enabled=True))


@pytest.fixture
def fake_iptables(monkeypatch):
    """Replace the subprocess with canned `iptables -S` output."""

    def _install(stdout="", *, returncode=0, stderr="", missing=False):
        calls = []

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            if missing:
                return None
            return subprocess.CompletedProcess(
                argv, returncode, stdout=stdout, stderr=stderr
            )

        monkeypatch.setattr(doctor, "_run", _fake_run)
        return calls

    return _install


@pytest.fixture(autouse=True)
def _no_boot_script(tmp_path, monkeypatch):
    """Point `DEVBOX_BOOT_SCRIPT` at a path that does not exist.

    Without this, every test in the file silently depends on the *absence* of
    `/usr/local/sbin/istota-devbox-iptables` on whatever machine runs the suite
    — passing on a laptop for the reason "this is not a production host", and
    going red on a deployed one. It is also shared mutable state under `-n auto`
    (AGENTS.md, "Verification"). Tests that want the oracle ask for
    `boot_script` and override this.
    """
    monkeypatch.setattr(doctor, "DEVBOX_BOOT_SCRIPT", tmp_path / "absent-script")


@pytest.fixture
def boot_script(tmp_path, monkeypatch):
    """Install a fake `/usr/local/sbin/istota-devbox-iptables` holding `dests`.

    Rendered in the real script's shape — `ensure_drop "<dest>" "<comment>"` —
    so the parser is exercised against the format the role actually writes.
    `TestTheOracleIsTheRealScript` pins that the two stay the same shape.
    """

    def _install(dests, subnet=DEVBOX_SUBNET):
        path = tmp_path / "istota-devbox-iptables"
        body = "\n".join(
            f'ensure_drop "{dest}" "istota-devbox: block {dest}"' for dest in dests
        )
        path.write_text(
            f'#!/bin/bash\nset -euo pipefail\nSUBNET="{subnet}"\n{body}\n'
        )
        monkeypatch.setattr(doctor, "DEVBOX_BOOT_SCRIPT", path)
        return path

    return _install


def _run_check(config, probe=True):
    return doctor.check_devbox_netfilter(config, probe)


# ---------------------------------------------------------------------------
# The healthy answer


class TestTheChainIsHealthy:
    def test_our_rules_at_the_front_is_ok(self, devbox_config, fake_iptables):
        fake_iptables(_chain(*_our_rules()))
        result = _run_check(devbox_config)
        assert result.status == OK
        assert result.name == CHECK_NAME

    def test_a_docker_28_chain_holding_only_our_rules_is_ok(
        self, devbox_config, fake_iptables
    ):
        """Docker 28+ leaves DOCKER-USER empty, so the chain is exactly ours."""
        fake_iptables(_chain(*_our_rules()))
        assert _run_check(devbox_config).status == OK

    def test_a_return_after_our_rules_is_fine(self, devbox_config, fake_iptables):
        """The pre-v28 `-j RETURN` is harmless once it sits *behind* our rules —
        that is the whole shape the fix produces, and it must not read as a
        defect or the check cries wolf on every correctly-fixed host."""
        fake_iptables(_chain(*_our_rules(), "-A DOCKER-USER -j RETURN"))
        assert _run_check(devbox_config).status == OK


# ---------------------------------------------------------------------------
# The defect this check exists for


class TestSomethingShadowsOurRules:
    def test_an_unconditional_return_in_front_fails(self, devbox_config, fake_iptables):
        """The exact pre-v28 state ISSUE-295 was filed about: rules present,
        never evaluated."""
        fake_iptables(_chain("-A DOCKER-USER -j RETURN", *_our_rules()))
        result = _run_check(devbox_config)
        assert result.status == FAIL
        assert result.remedy, "a FAIL an operator cannot act on is a log line"

    def test_an_unconditional_accept_in_front_fails(self, devbox_config, fake_iptables):
        fake_iptables(_chain("-A DOCKER-USER -j ACCEPT", *_our_rules()))
        assert _run_check(devbox_config).status == FAIL

    def test_the_detail_names_the_shadowing_rule(self, devbox_config, fake_iptables):
        """An operator has to know *what* to remove. A check saying only "the
        rules are unreachable" sends them to read the chain themselves."""
        fake_iptables(_chain("-A DOCKER-USER -j RETURN", *_our_rules()))
        assert "RETURN" in _run_check(devbox_config).detail

    def test_a_conditional_accept_in_front_warns_rather_than_fails(
        self, devbox_config, fake_iptables
    ):
        """Docker Desktop seeds `-i eth0 -j ACCEPT`, and ufw-docker inserts
        matched rules. Those terminate *some* traffic, not all of it, so the
        honest answer is a WARN naming it — calling it a FAIL would be claiming
        more than the chain says."""
        fake_iptables(
            _chain("-A DOCKER-USER -i eth0 -j ACCEPT", *_our_rules())
        )
        result = _run_check(devbox_config)
        assert result.status == WARN
        assert result.remedy

    def test_an_unconditional_return_outranks_a_conditional_one(
        self, devbox_config, fake_iptables
    ):
        """Both present: the unconditional one decides, because it settles the
        question the conditional one only raises."""
        fake_iptables(
            _chain(
                "-A DOCKER-USER -i eth0 -j ACCEPT",
                "-A DOCKER-USER -j RETURN",
                *_our_rules(),
            )
        )
        assert _run_check(devbox_config).status == FAIL

    def test_a_terminal_rule_between_our_rules_is_caught(
        self, devbox_config, fake_iptables
    ):
        """Scanning only up to the *first* of our rules reports this chain as
        healthy while rules two to four are unreachable. `iptables -I
        DOCKER-USER 2` by an operator reaches exactly this shape."""
        ours = _our_rules()
        fake_iptables(_chain(ours[0], "-A DOCKER-USER -j RETURN", *ours[1:]))
        assert _run_check(devbox_config).status == FAIL

    def test_a_preceding_drop_is_not_a_finding(self, devbox_config, fake_iptables):
        """Only RETURN and ACCEPT end evaluation in a way that skips our rules.
        A DROP in front is someone blocking more, not less."""
        fake_iptables(
            _chain("-A DOCKER-USER -s 10.1.0.0/16 -j DROP", *_our_rules())
        )
        assert _run_check(devbox_config).status == OK


class TestTheParserReadsRealChainOutput:
    """The shapes a regex over the raw line gets wrong.

    Every one of these was measured against the first cut of this check and
    produced the wrong verdict — a real bypass reported as a healthy chain, or
    a total bypass downgraded to a WARN that nothing alerts on. They are the
    specification for the tokenising parser that replaced it, and they are here
    rather than in a scratch file because a parser regression restores a
    security check that lies.
    """

    @pytest.mark.parametrize(
        "rule, why",
        [
            (
                '-A DOCKER-USER -m comment --comment "docker default" -j RETURN',
                "a comment is an annotation, not a match condition, so this "
                "ends the chain for every packet",
            ),
            (
                '-A DOCKER-USER -m comment --comment "see -j DROP note" -j RETURN',
                "the -j inside the comment must not be read as the target",
            ),
            (
                "-A DOCKER-USER -g SOMECHAIN",
                "a goto returns to FORWARD, not to DOCKER-USER, so our rules "
                "are never reached",
            ),
            (
                "-A DOCKER-USER -j ufw-user-forward",
                "a jump into a user-defined chain this check does not follow",
            ),
            (
                "-A DOCKER-USER -s 172.16.0.0/12 -j RETURN",
                "what `ufw-docker install` writes; the devbox subnet "
                "172.30.0.0/24 is inside it, so every devbox packet returns",
            ),
        ],
        ids=[
            "commented_return", "comment_contains_dash_j", "goto",
            "jump_to_user_chain", "ufw_docker_covering_return",
        ],
    )
    def test_a_shadowing_rule_in_front_fails(
        self, devbox_config, fake_iptables, boot_script, rule, why
    ):
        boot_script(_our_destinations())
        fake_iptables(_chain(rule, *_our_rules()))
        result = _run_check(devbox_config)
        assert result.status == FAIL, f"{why}: got {result.status} — {result.detail}"

    def test_a_terminal_rule_that_provably_cannot_match_the_devbox_is_ok(
        self, devbox_config, fake_iptables, boot_script
    ):
        """The counterpart to the ufw-docker case, and the reason the overlap
        test is an overlap test rather than "is it scoped at all".

        `-s 10.9.0.0/16` does not overlap `172.30.0.0/24`, so this rule ends the
        chain for somebody else's traffic and provably not for ours. Reporting
        it would be crying wolf, and a check that cries wolf is one nobody
        reads — the same reason the non-root answer is a SKIP.
        """
        boot_script(_our_destinations())
        fake_iptables(
            _chain("-A DOCKER-USER -s 10.9.0.0/16 -j RETURN", *_our_rules())
        )
        result = _run_check(devbox_config)
        assert result.status == OK
        assert "10.9.0.0/16" in result.detail, (
            "the rule was dismissed without saying which rule was dismissed"
        )

    def test_an_undecidable_terminal_rule_warns(
        self, devbox_config, fake_iptables, boot_script
    ):
        """Docker Desktop seeds `-i eth0 -j ACCEPT`. Whether that catches devbox
        traffic depends on the bridge's interface name, which is a generated
        hash this check cannot learn — so the honest answer is a WARN naming the
        rule, not a guess in either direction."""
        boot_script(_our_destinations())
        fake_iptables(
            _chain("-A DOCKER-USER -i eth0 -j ACCEPT", *_our_rules())
        )
        result = _run_check(devbox_config)
        assert result.status == WARN
        assert "eth0" in result.detail


class TestOurRulesAreMissingEntirely:
    def test_an_empty_chain_fails(self, devbox_config, fake_iptables, boot_script):
        """Devbox enabled, the boot script says four destinations, and not one
        of our rules is on the host. The role never ran, or a reboot lost them
        and the oneshot did not fire."""
        boot_script(_our_destinations())
        fake_iptables(_chain())
        result = _run_check(devbox_config)
        assert result.status == FAIL
        assert result.remedy

    def test_a_chain_of_other_peoples_rules_fails(
        self, devbox_config, fake_iptables, boot_script
    ):
        boot_script(_our_destinations())
        fake_iptables(_chain("-A DOCKER-USER -j RETURN"))
        assert _run_check(devbox_config).status == FAIL

    def test_blocking_nothing_on_purpose_is_not_a_failure(
        self, devbox_config, fake_iptables, boot_script
    ):
        """`istota_devbox_block_metadata: false` and
        `istota_devbox_block_rfc1918: false` is a supported operator choice: both
        apply tasks are gated on those flags and both `{% if %}` blocks in the
        boot script render nothing, so an empty chain is the correct state.

        Reporting it as FAIL would page admins hourly on a healthy host
        (`scheduler.py` alerts on entry into failure), and an alert that fires
        when nothing is wrong is how a real one gets ignored.
        """
        boot_script([])
        fake_iptables(_chain())
        assert _run_check(devbox_config).status == OK

    def test_no_oracle_means_no_verdict(self, devbox_config, fake_iptables):
        """The docker-compose deployment installs no boot script and adds no
        rules by design (docs/deployment/docker.md). With nothing to say what
        the host *should* block, an empty chain cannot be called a failure."""
        fake_iptables(_chain())
        assert _run_check(devbox_config).status == SKIP

    def test_a_rule_wearing_our_comment_but_not_dropping_is_a_finding(
        self, devbox_config, fake_iptables, boot_script
    ):
        """Identity by comment substring alone would count this as one of ours,
        exclude it from the shadowing scan, and report a chain where nothing is
        reached as healthy."""
        boot_script(_our_destinations())
        fake_iptables(
            _chain(
                '-A DOCKER-USER -m comment --comment "istota-devbox: override" '
                "-j RETURN",
                *_our_rules(),
            )
        )
        result = _run_check(devbox_config)
        assert result.status == FAIL
        assert result.remedy

    def test_a_partial_ruleset_is_reported(
        self, devbox_config, fake_iptables, boot_script
    ):
        """Three of four is the shape a failed `set -e` boot leaves behind: the
        unit died partway and the host came up filtering less than it says.

        The oracle is the boot script the role installed on *this* host, not a
        count hardcoded here. A constant would have to be updated every time the
        blocklist changes (ISSUE-298 widens it), and the first time someone
        forgot, the check would report a healthy host as broken or a partial one
        as fine.
        """
        boot_script(_our_destinations())
        fake_iptables(_chain(*_our_rules()[:3]))
        result = _run_check(devbox_config)
        assert result.status == WARN
        assert "192.168.0.0/16" in result.detail, (
            "the detail does not name the rule that is missing"
        )
        assert result.remedy

    def test_a_complete_ruleset_against_the_script_is_ok(
        self, devbox_config, fake_iptables, boot_script
    ):
        boot_script(_our_destinations())
        fake_iptables(_chain(*_our_rules()))
        assert _run_check(devbox_config).status == OK

    def test_no_boot_script_means_no_completeness_claim(
        self, devbox_config, fake_iptables
    ):
        """A docker-compose deployment installs no boot script. The check still
        answers the shadowing question — it just cannot say whether the rule set
        is complete, and must not invent an expected count in order to."""
        fake_iptables(_chain(*_our_rules()[:2]))
        assert _run_check(devbox_config).status == OK


# ---------------------------------------------------------------------------
# When the check must decline to answer


class TestTheGateIsADisjunction:
    """There are two switches now, and this is the only witness over the devbox
    network boundary.

    `[devbox] enabled` gates the *skill's* capability. `[developer.container]
    backend` gates the transport that routes every build into the container. So
    `backend = devbox` with `devbox.enabled = false` is a deployment where every
    build in the estate runs in a container whose egress filtering nothing
    checks — a silent gap, of exactly the class this check exists to close.
    """

    def _container_config(self, make_config, tmp_path):
        from istota.config import ContainerConfig, DeveloperConfig

        repos = tmp_path / "repos"
        repos.mkdir(exist_ok=True)
        return make_config(
            developer=DeveloperConfig(
                enabled=True,
                repos_dir=str(repos),
                container=ContainerConfig(backend="devbox"),
            ),
        )

    def test_the_backend_alone_makes_the_check_run(
        self, make_config, tmp_path, fake_iptables, boot_script
    ):
        """The control for the exact gap the disjunction exists to close: the
        skill's capability is off and every build still runs in the container."""
        boot_script(_our_destinations())
        calls = fake_iptables(_chain(*_our_rules()))

        result = _run_check(self._container_config(make_config, tmp_path))

        assert calls, "the chain was not read for a deployment routing builds into it"
        assert result.status == OK

    def test_the_backend_alone_still_finds_a_shadowed_chain(
        self, make_config, tmp_path, fake_iptables, boot_script
    ):
        """Running is not the property; *finding* is. A gate correction that let
        the check run and answer OK on a broken chain would be no better."""
        boot_script(_our_destinations())
        fake_iptables(_chain("-A DOCKER-USER -j RETURN", *_our_rules()))

        result = _run_check(self._container_config(make_config, tmp_path))

        assert result.status == FAIL

    def test_neither_switch_still_skips(self, make_config, fake_iptables):
        calls = fake_iptables(_chain(*_our_rules()))

        result = _run_check(make_config())

        assert result.status == SKIP
        assert not calls, "the chain was read for a deployment that has no devbox"


class TestItSkipsRatherThanGuessing:
    def test_devbox_disabled_skips(self, make_config, fake_iptables):
        calls = fake_iptables(_chain(*_our_rules()))
        result = _run_check(make_config())
        assert result.status == SKIP
        assert not calls, "the chain was read for a deployment that has no devbox"

    def test_probe_disabled_skips_and_spawns_nothing(self, devbox_config, fake_iptables):
        """`load_config` runs doctor with probe=False on the start-up path."""
        calls = fake_iptables(_chain(*_our_rules()))
        result = _run_check(devbox_config, probe=False)
        assert result.status == SKIP
        assert not calls

    def test_no_iptables_binary_skips(self, devbox_config, fake_iptables):
        """Every non-Linux host the suite runs on, and the check must not call
        that a broken boundary."""
        fake_iptables(missing=True)
        assert _run_check(devbox_config).status == SKIP

    def test_permission_denied_skips(self, devbox_config, fake_iptables):
        """The daemon does not run as root, so this is the *common* answer in
        production. Reporting it as FAIL would train operators to ignore the
        check — the one outcome worse than not having it."""
        fake_iptables(
            "",
            returncode=4,
            stderr="iptables v1.8.9 (nf_tables): Could not fetch rule set "
            "generation id: Permission denied (you must be root)",
        )
        result = _run_check(devbox_config)
        assert result.status == SKIP
        assert "root" in result.detail.lower() or "permission" in result.detail.lower()

    def test_a_missing_chain_is_not_a_skip(self, devbox_config, fake_iptables):
        """`No chain/target/match by that name` means dockerd never created
        DOCKER-USER — a real finding, not an inability to look."""
        fake_iptables(
            "",
            returncode=1,
            stderr="iptables: No chain/target/match by that name.",
        )
        result = _run_check(devbox_config)
        assert result.status == FAIL
        assert result.remedy


# ---------------------------------------------------------------------------
# Registry wiring


class TestTheOracleIsTheRealScript:
    """The completeness comparison parses the boot script, so the parser has to
    read the script the role really renders — not the fake above.

    This is the one seam a fabricated fixture cannot cover: `boot_script` writes
    what *this file* thinks the format is, and would keep passing after the
    template changed shape. Rendering the real template and feeding it to the
    real parser is what makes the fixture honest.
    """

    @pytest.fixture(scope="class")
    def rendered(self):
        from tests.test_ansible_devbox_iptables import SCRIPT_TEMPLATE, _render

        return _render(SCRIPT_TEMPLATE)

    def test_the_parser_finds_every_destination_the_template_blocks(self, rendered):
        from tests.test_ansible_devbox_iptables import dropped_destinations

        assert doctor.parse_devbox_boot_script(rendered) == dropped_destinations(
            rendered
        )

    def test_it_finds_something_at_all(self, rendered):
        """Guards the vacuous pass: two empty sets compare equal, so the
        assertion above holds just as well against a parser that never matches."""
        assert len(doctor.parse_devbox_boot_script(rendered)) >= 4

    def test_a_script_it_cannot_read_yields_nothing_rather_than_raising(self):
        """Doctor runs on the start-up path; a parser that raised on an
        unexpected file would take the daemon down over a diagnostic."""
        assert doctor.parse_devbox_boot_script("#!/bin/bash\nexit 0\n") == set()


class TestItIsWiredIn:
    def test_it_is_registered(self):
        assert CHECK_NAME in dict(CHECKS)

    def test_it_is_deployment_scoped(self):
        """It reads a host's live firewall — nothing an image can answer."""
        assert CHECK_SCOPES[CHECK_NAME] == DEPLOYMENT

    def test_it_is_not_a_deep_check(self):
        """Deep means "spawns a namespace". This runs one short read-only
        command, so it belongs in the default run — a check that only fires
        under `--deep` would not be looked at on the host that needs it."""
        assert CHECK_NAME not in doctor.DEEP_CHECKS
