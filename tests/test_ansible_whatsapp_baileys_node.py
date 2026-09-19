"""The Baileys sidecar's unit must not be started without the interpreter it
execs (ISSUE-494).

Two halves of the role disagreed about what ``istota_update_only`` means. The
Node install carries ``not istota_update_only`` and is skipped; the Baileys
tasks carry no such term and run, so an update-only converge deploys, enables
and starts a unit whose ``ExecStart`` is an absolute path to a binary the play
just declined to install.

Mostly that is loud — ``npm ci`` fails first on a host with no npm and the play
stops. The hole is the case where it does not: the dependency install is gated
on the lockfile checksum matching a marker inside ``node_modules``, so a host
that already has a populated tree skips it entirely. Nothing between that skip
and the start checks for an interpreter, and the failure then surfaces as a
systemd start error rather than as a sentence naming the missing dependency.
It is reachable by the pairing procedure we document: ``istota whatsapp pair``
falls back to the checkout's copy of the sidecar, so an operator who pairs by
hand before converging populates ``node_modules`` themselves.

The fix is the issue's option 2 — keep the unit tasks running under update-only
and assert the interpreter up front. Option 1 (gate the unit tasks on
``not istota_update_only`` too) is the tidier rule and has the worse failure
mode: an operator flipping the provider under update-only would get no sidecar
and no error at all.

What this file cannot see: nothing anywhere executes this assert. The ``deploy``
tier converges with WhatsApp off, so the parse below is the whole of its
coverage. It establishes that the guard is present, correctly gated and
correctly ordered — not that it fires.

One gate combination is outside the guard and is deliberately not claimed here.
Both new tasks carry ``not istota_web_only``, while the update-only restart loop
carries only ``istota_update_only and (item.enabled | bool)`` — so setting both
flags at once starts the sidecar with the guard skipped. That loop also restarts
the scheduler under ``istota_web_only``, which contradicts what web-only means,
so the gate is wrong for four units rather than for this one and fixing it is
not ISSUE-494's change. ``test_the_guard_precedes_the_update_only_restart_loop``
asserts ordering alone, which is what holds.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS_FILE = REPO / "deploy" / "ansible" / "tasks" / "main.yml"
UNIT_TEMPLATE = (
    REPO / "deploy" / "ansible" / "templates" / "istota-whatsapp-baileys.service.j2"
)

UNIT_GATE = "istota_whatsapp_baileys_unit_wanted"


@pytest.fixture(scope="module")
def tasks() -> list[dict]:
    return yaml.safe_load(TASKS_FILE.read_text())


@pytest.fixture(scope="module")
def exec_interpreter() -> str:
    """The interpreter token the unit execs, read off the template.

    Derived rather than spelled, so the guard below cannot drift from the thing
    it guards: pointing the unit at another interpreter with the guard left
    naming the old one turns this file red instead of turning the deployment
    into the failure ISSUE-494 is about.

    This is the raw token — today a Jinja expression rather than a literal —
    because what the three sites have to share is the *variable*, not a value
    each resolves separately. Three equal literals satisfy a value comparison
    and drift the moment one is edited.
    """
    match = re.search(
        r"^ExecStart=(\{\{[^}]+\}\}|\S+)", UNIT_TEMPLATE.read_text(), re.MULTILINE
    )
    assert match, f"no ExecStart in {UNIT_TEMPLATE}"
    return match.group(1).strip()


@pytest.fixture(scope="module")
def node_bin_variable(exec_interpreter) -> str:
    """The Ansible variable name behind that token."""
    match = re.fullmatch(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", exec_interpreter)
    assert match, (
        "the unit's ExecStart interpreter is not a single variable, so the "
        f"play and the cron cannot share it: {exec_interpreter!r}"
    )
    return match.group(1)


@pytest.fixture(scope="module")
def node_bin_default(node_bin_variable) -> str:
    """Its default, which is the path a deployment actually gets."""
    defaults = yaml.safe_load(
        (REPO / "deploy" / "ansible" / "defaults" / "main.yml").read_text()
    )
    assert node_bin_variable in defaults, (
        f"{node_bin_variable} is used but has no default, so a play that does "
        "not set it fails on an undefined variable"
    )
    value = str(defaults[node_bin_variable])
    assert value.startswith("/"), (
        "systemd execs ExecStart with no PATH lookup, so a bare name never "
        f"resolves: {value!r}"
    )
    return value


def _when(task: dict) -> str:
    """A task's `when`, flattened — the role writes it both as a string and as
    a list of clauses, and a caller asking "does this gate appear" wants one
    answer for both spellings."""
    when = task.get("when", "")
    if isinstance(when, list):
        return " and ".join(str(clause) for clause in when)
    return str(when)


def _index_of(tasks: list[dict], name: str) -> int:
    for index, task in enumerate(tasks):
        if task.get("name") == name:
            return index
    raise AssertionError(f"no task named {name!r} in {TASKS_FILE}")


def _stat_path(task: dict) -> str | None:
    stat = task.get("stat") or task.get("ansible.builtin.stat")
    if isinstance(stat, dict):
        return stat.get("path")
    return None


def _interpreter_probe(tasks: list[dict], interpreter: str) -> tuple[int, dict]:
    """The task that reads the interpreter off the host, with its index."""
    for index, task in enumerate(tasks):
        if _stat_path(task) == interpreter and UNIT_GATE in _when(task):
            return index, task
    raise AssertionError(
        f"no task stats {interpreter} under the {UNIT_GATE} gate — the role can "
        "still start the sidecar unit without the interpreter it execs"
    )


def _interpreter_assert(tasks: list[dict], probe_index: int) -> tuple[int, dict]:
    """The assert reading that probe's registered result.

    Matching requires the clause to read ``.stat.exists`` rather than merely to
    mention the register. A task with ``register:`` always leaves the name
    defined — ``{"skipped": true}`` when its ``when`` was false — so a clause
    like ``baileys_node_bin is defined`` is true on every host alive and would
    satisfy a name-only match while checking nothing. That degenerate guard is
    the thing this file exists to exclude, so it must not be able to pass.
    """
    register = tasks[probe_index].get("register")
    assert register, "the interpreter probe registers nothing for an assert to read"
    for index, task in enumerate(tasks):
        body = task.get("assert") or task.get("ansible.builtin.assert")
        if not isinstance(body, dict):
            continue
        that = body.get("that")
        clauses = [str(c) for c in (that if isinstance(that, list) else [that])]
        if any(f"{register}.stat.exists" in clause for clause in clauses):
            return index, task
    raise AssertionError(
        f"nothing asserts on {register}.stat.exists, so the probe records the "
        "missing interpreter and the play carries on to start the unit anyway"
    )


class TestTheInterpreterIsGuarded:
    def test_the_role_reads_the_interpreter_off_the_host(self, tasks, exec_interpreter):
        """The discriminating check is the exact path in `ExecStart`, not
        whether some `node` is resolvable: systemd execs an absolute path with
        no shell, so a `command -v node` probe passes on a host where node sits
        anywhere else on PATH while the unit still dies at start."""
        _interpreter_probe(tasks, exec_interpreter)

    def test_a_missing_interpreter_fails_the_play(self, tasks, exec_interpreter):
        probe_index, _ = _interpreter_probe(tasks, exec_interpreter)
        _interpreter_assert(tasks, probe_index)

    def test_the_probe_resolves_symlinks(self, tasks, exec_interpreter):
        """`stat` defaults `follow: false` and then calls `lstat`, which
        *succeeds* on a dangling symlink — so the default reports
        ``exists: true`` for an interpreter whose target is gone, and the guard
        passes on exactly the host it exists to refuse.

        Measured against ansible-core rather than read off the docs: a symlink
        pointing at nothing gives ``exists=True`` by default and
        ``exists=False`` under ``follow: yes``. Debian packages node as a
        symlink, so this is the ordinary shape rather than a contrived one.
        """
        _, probe = _interpreter_probe(tasks, exec_interpreter)
        stat = probe.get("stat") or probe.get("ansible.builtin.stat")
        assert stat.get("follow") is True, (
            "the probe uses lstat semantics, so a dangling "
            f"{exec_interpreter} symlink reports as present"
        )

    def test_the_guard_requires_an_executable(self, tasks, exec_interpreter):
        """Existence is not enough: systemd execs the file, so a present but
        non-executable interpreter dies at start exactly as a missing one
        does, and `stat` already returns the answer."""
        probe_index, probe = _interpreter_probe(tasks, exec_interpreter)
        _, guard = _interpreter_assert(tasks, probe_index)
        body = guard.get("assert") or guard.get("ansible.builtin.assert")
        that = body.get("that")
        clauses = [str(c) for c in (that if isinstance(that, list) else [that])]
        register = probe.get("register")
        assert any(f"{register}.stat.executable" in c for c in clauses), (
            "the guard accepts a present but non-executable interpreter"
        )

    def test_the_failure_message_names_the_remedy(self, tasks, exec_interpreter):
        """A systemd start error turned into a sentence is the whole value of
        the fix, so the message has to say what to do: run the full play, which
        is where the Node install lives."""
        probe_index, _ = _interpreter_probe(tasks, exec_interpreter)
        _, guard = _interpreter_assert(tasks, probe_index)
        body = guard.get("assert") or guard.get("ansible.builtin.assert")
        message = str(body.get("fail_msg", ""))
        assert "istota_update_only" in message, (
            "the message does not name the setting that caused the skip"
        )


class TestTheGate:
    def test_the_guard_runs_under_update_only(self, tasks, exec_interpreter):
        """The point of the whole fix. A `not istota_update_only` term here
        would switch the guard off in exactly the converge it exists for, and
        it would read as correct beside every other task in the file."""
        probe_index, probe = _interpreter_probe(tasks, exec_interpreter)
        _, guard = _interpreter_assert(tasks, probe_index)
        for task in (probe, guard):
            assert "istota_update_only" not in _when(task), (
                f"{task.get('name')!r} is gated on istota_update_only, so it is "
                "skipped on the converge ISSUE-494 is about"
            )

    def test_the_guard_is_scoped_to_a_deployment_that_wants_the_unit(
        self, tasks, exec_interpreter
    ):
        """A host running no sidecar should not be made to install node."""
        probe_index, probe = _interpreter_probe(tasks, exec_interpreter)
        _, guard = _interpreter_assert(tasks, probe_index)
        for task in (probe, guard):
            when = _when(task)
            assert UNIT_GATE in when
            # The negated spelling specifically: a bare `istota_web_only` in
            # the gate would be the opposite instruction and would match a
            # substring test just as happily.
            assert "not istota_web_only" in when, (
                f"{task.get('name')!r} runs on a web-only converge, which "
                "deploys no sidecar"
            )


class TestTheOrdering:
    """The guard has to precede every task that can put the unit on the host or
    start it — a refusal that lands after the unit is already running has
    reported the problem too late to have prevented it."""

    @pytest.mark.parametrize(
        "later_task",
        [
            "Deploy WhatsApp Baileys sidecar service",
            "Enable and start WhatsApp Baileys sidecar",
        ],
    )
    def test_the_guard_comes_first(self, tasks, exec_interpreter, later_task):
        probe_index, _ = _interpreter_probe(tasks, exec_interpreter)
        guard_index, _ = _interpreter_assert(tasks, probe_index)
        assert probe_index < guard_index < _index_of(tasks, later_task), (
            f"the interpreter guard runs after {later_task!r}"
        )

    def test_the_guard_precedes_the_update_only_restart_loop(
        self, tasks, exec_interpreter
    ):
        """The second start point, and the one that is easy to miss: the
        update-only restart loop at the end of the role names the sidecar unit
        too, so it starts it on precisely the converge that installed no node."""
        probe_index, _ = _interpreter_probe(tasks, exec_interpreter)
        guard_index, _ = _interpreter_assert(tasks, probe_index)
        restart_index = next(
            (
                index
                for index, task in enumerate(tasks)
                if "istota_update_only" in _when(task)
                and "whatsapp-baileys" in str(task.get("loop", ""))
            ),
            None,
        )
        # A bare `next()` would raise StopIteration here and surface the drift
        # as an error with no statement of what was being checked.
        assert restart_index is not None, (
            "no update-only restart loop names the sidecar unit; if it was "
            "renamed, this ordering claim needs rechecking"
        )
        assert guard_index < restart_index

    def test_the_guard_precedes_the_dependency_install(self, tasks, exec_interpreter):
        """`npm ci` is the loud failure this fix sits behind, so refusing ahead
        of it means a refused play has mutated nothing — the sidecar is not
        stopped for an install that cannot happen."""
        probe_index, _ = _interpreter_probe(tasks, exec_interpreter)
        guard_index, _ = _interpreter_assert(tasks, probe_index)
        assert guard_index < _index_of(tasks, "Install the WhatsApp sidecar's dependencies")
        assert guard_index < _index_of(
            tasks, "Stop the WhatsApp sidecar for its dependency install"
        )


class TestOneInterpreterPath:
    """Three places have to agree on the interpreter, so they read one variable.

    The unit's `ExecStart`, the play's pre-flight assert and the cron's start
    arm each need the same path, and three equal literals is the arrangement
    where a guard checks one path while systemd execs another — silently, since
    a passing guard and a dying unit look identical from the play's output.
    """

    def test_the_play_stats_the_variable_the_unit_execs(
        self, tasks, exec_interpreter, node_bin_variable
    ):
        _, probe = _interpreter_probe(tasks, exec_interpreter)
        stat = probe.get("stat") or probe.get("ansible.builtin.stat")
        assert node_bin_variable in str(stat.get("path"))

    def test_the_default_is_absolute(self, node_bin_default):
        assert node_bin_default.startswith("/")

    def test_no_site_hardcodes_the_path(self, node_bin_default, node_bin_variable):
        """The literal may appear only where the variable is defined."""
        offenders = []
        for rel in (
            "deploy/ansible/tasks/main.yml",
            "deploy/ansible/templates/istota-whatsapp-baileys.service.j2",
            "deploy/ansible/templates/istota-update.sh.j2",
        ):
            text = (REPO / rel).read_text()
            for number, line in enumerate(text.splitlines(), 1):
                if node_bin_default in line and node_bin_variable not in line:
                    offenders.append(f"{rel}:{number}")
        assert not offenders, (
            f"{node_bin_default} is spelled out rather than taken from "
            f"{node_bin_variable} at: {offenders}"
        )


class TestTheAutoUpdateCron:
    """The play is not the only thing that starts this unit.

    `.claude/rules/whatsapp.md`: "on the reference host the two-minute
    auto-update cron *is* the deploy path". The play's assert cannot reach that
    script, so a host left with the unit enabled and no interpreter — by a
    pre-fix update-only converge, or by node being removed out of band — would
    have the cron start it every two minutes indefinitely.
    """

    @pytest.fixture(scope="module")
    def cron(self) -> str:
        return (
            REPO / "deploy" / "ansible" / "templates" / "istota-update.sh.j2"
        ).read_text()

    def test_the_start_arm_checks_the_interpreter(self, cron, node_bin_variable):
        assert f'"{{{{ {node_bin_variable} }}}}"' in cron, (
            "the cron names its own interpreter path rather than the variable "
            "the unit's ExecStart is rendered from, so the two can drift"
        )
        start = cron.index('systemctl start "${NAMESPACE}-whatsapp-baileys"')
        preceding = cron[:start]
        guard = preceding.rindex("-x ")
        # Inside the same `if`, not merely somewhere earlier in the file.
        assert "\nfi\n" not in preceding[guard:], (
            "the interpreter check does not guard the sidecar start arm"
        )

    def test_it_tests_executability_rather_than_existence(self, cron):
        """`-e` is true for a dangling symlink; `-x` resolves it and also
        catches a present but non-executable file — the same two cases
        `follow: yes` plus the executable clause cover in the play."""
        guard = next(ln for ln in cron.splitlines() if ln.startswith("if [ ! -x"))
        assert "BAILEYS_NODE_BIN" in guard
        assert "-e " not in guard

    def test_it_says_so_rather_than_failing_silently(self, cron):
        """The cron cannot install node, so the only useful thing it can do is
        name the cause; a silent skip is the condition ISSUE-494 is about."""
        line = next(
            ln
            for ln in cron.splitlines()
            if "not starting" in ln and "whatsapp-baileys" in ln
        )
        assert "ERROR" in line and "full play" in line


class TestTheCronRefusesToRestartIntoAStaleUnit:
    """The same shape as the interpreter guard, for the unit's environment.

    **This script delivers the program and only a full play delivers the
    unit**, which is the asymmetry that makes it a hazard rather than a
    nicety. The sidecar requires three environment variables and exits 2
    without any one of them, so a commit adding a fourth reaches a
    cron-updated host as a new `index.js` against the unit file the last full
    play rendered. `Restart=always` then loops it, and the refusal has nowhere
    to go: `StandardOutput=null`, and the program's only log destination is a
    file inside the 0700 session directory. What an operator sees is an empty
    `journalctl` and `doctor` reporting no sidecar connected, which names the
    wrong cause.

    `ISTOTA_BAILEYS_MEDIA_DIR` is the variable that has been added, so it is
    the one the guard names.
    """

    @pytest.fixture(scope="module")
    def cron(self) -> str:
        return (
            REPO / "deploy" / "ansible" / "templates" / "istota-update.sh.j2"
        ).read_text()

    @pytest.fixture(scope="module")
    def unit(self) -> str:
        return (
            REPO / "deploy" / "ansible" / "templates"
            / "istota-whatsapp-baileys.service.j2"
        ).read_text()

    def test_the_variable_it_greps_for_is_one_the_unit_actually_sets(
        self, cron, unit,
    ):
        """A guard naming a variable the unit never sets refuses for ever.

        This is the arm that inverts the failure: the check is a `grep` over a
        file this repository also renders, so the two have to agree or every
        cron run on a correctly-deployed host skips the restart and says the
        unit is stale.
        """
        assert "ISTOTA_BAILEYS_MEDIA_DIR" in cron
        assert "Environment=ISTOTA_BAILEYS_MEDIA_DIR=" in unit

    def test_it_guards_both_arms_that_touch_the_unit(self, cron):
        """`restart` and `start` alike.

        A stale unit refuses the program whichever verb is used, so guarding
        only the restart arm would leave the ordinary no-change tick starting
        it into the same loop every two minutes.
        """
        start = cron.index('systemctl start "${NAMESPACE}-whatsapp-baileys"')
        restart = cron.index('systemctl restart "${NAMESPACE}-whatsapp-baileys"')
        preceding = cron[: min(start, restart)]
        guard = preceding.rindex("BAILEYS_UNIT_STALE")
        assert "\nfi\n" not in preceding[guard:], (
            "the stale-unit check does not guard the arms that start the unit"
        )

    def test_it_reads_the_unit_on_disk_rather_than_systemd_s_loaded_copy(
        self, cron,
    ):
        """`systemctl show -p Environment` reports what systemd last loaded.

        The cron runs before any `daemon-reload` a full play would have done,
        so the loaded copy can be older than the file — and the file is what
        the next restart will read.
        """
        assert "/etc/systemd/system/${NAMESPACE}-whatsapp-baileys.service" in cron
        # Directives rather than mentions, the rule the unit's own `PartOf=`
        # test states: a comment in this script names `systemctl show` in
        # order to say why it is not used, and a substring test cannot tell
        # that apart from a call.
        commands = [
            ln.strip() for ln in cron.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        assert not any("systemctl show" in ln for ln in commands)

    def test_it_says_so_rather_than_skipping_silently(self, cron):
        """The cron cannot render the unit, so naming the cause and the remedy
        is the whole of what it can usefully do."""
        line = next(
            ln
            for ln in cron.splitlines()
            if "predates ISTOTA_BAILEYS_MEDIA_DIR" in ln
        )
        assert "ERROR" in line and "full play" in line
        assert "exit 2" in line, (
            "the message does not say what would happen, which is the thing "
            "an operator reading an empty journal needs"
        )

    def test_a_unit_file_that_is_absent_is_not_treated_as_stale(self, cron):
        """A host with the unit not yet installed at all is a different case.

        `-f` first, so the grep runs against a file that exists; without it
        `grep` on a missing path is non-zero and every such host would be
        reported stale rather than simply having nothing to restart.
        """
        guard = next(
            ln for ln in cron.splitlines()
            if ln.lstrip().startswith("if [") and "BAILEYS_UNIT" in ln
        )
        assert '-f "$BAILEYS_UNIT"' in guard
        # And the two are one condition rather than two statements, so a unit
        # that is absent never reaches the grep at all.
        assert "&&" in guard and "grep -q" in guard


class TestTheNodeInstallTaskName:
    """The issue's own "also worth a look": the task was named for the developer
    skill while its condition had grown to three consumers, so the name was a
    release behind what it does.

    Derived from the gate rather than pinned to a string, so a fourth consumer
    added to the `when` without a matching word in the name turns this red — the
    drift itself is what is guarded, not the one instance of it.
    """

    # The word a reader should find in the name for each gate term.
    CONSUMERS = {
        "istota_web_enabled": "web",
        "istota_whatsapp_baileys_unit_wanted": "whatsapp",
        "istota_nodejs_enabled": "developer",
    }

    @staticmethod
    def _install(tasks: list[dict]) -> dict:
        """Selected on `istota_nodejs_enabled`, which only the install carries.

        Selecting on "a task whose name says Node and whose gate mentions the
        sidecar" also matches the interpreter probe this same fix adds, and the
        probe's `when` names none of the consumers below — so `missing` comes
        back empty and the test passes having checked nothing. Only file order
        kept the right task selected, which is not a property to rest on: the
        rename this class guards is exactly what would reorder the match.
        """
        found = [t for t in tasks if "istota_nodejs_enabled" in _when(t)]
        assert len(found) == 1, (
            "expected exactly one task gated on istota_nodejs_enabled, got "
            f"{[t.get('name') for t in found]}"
        )
        return found[0]

    def test_the_name_covers_every_consumer_its_gate_does(self, tasks):
        install = self._install(tasks)
        when, name = _when(install), str(install.get("name")).lower()
        missing = [
            word
            for term, word in self.CONSUMERS.items()
            if term in when and word not in name
        ]
        assert not missing, (
            f"{install.get('name')!r} is gated on consumers it does not name: "
            f"{missing}"
        )
