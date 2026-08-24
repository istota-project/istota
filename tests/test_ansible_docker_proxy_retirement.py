"""The docker-proxy retirement block, and the glyph that made it a no-op.

The Docker socket proxy is retired: nothing binds a Docker socket into a
sandbox at any path. What survives is the block in ``tasks/main.yml`` that takes
the old units off a host that already ran them, and it is the kind of code
nobody looks at again — it runs on every deploy, reports ``ok``, and is only
ever wrong about hosts that already exist.

It was wrong. ``systemctl list-units`` prefixes each line with a status glyph:
a space for a healthy unit, ``●`` or ``×`` for a failed or not-found one.
Without ``--plain`` the block's ``awk '{print $1}'`` therefore yields the unit
name in the ordinary case and the bare glyph in exactly the case the block
exists for, so the stop ran against a unit named ``●``, failed, and was
swallowed by its own ``failed_when: false``. The unit file was then deleted out
from under a unit still enabled and running, and the host kept a
``not-found / failed (226/NAMESPACE)`` entry with nothing left that could ever
clear it. Verified on a live deployment, where the pre-fix command returned
``●`` and nothing else.

Both halves are asserted here, and both assertions are shown able to fail:
``TestTheseAssertionsCanFail`` feeds the predicates the pre-fix text and
requires them to reject it. A test over a config file that has never been shown
to go red says nothing, and this file's whole subject is a task that reported
success while doing nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO / "deploy" / "ansible" / "tasks" / "main.yml"

BLOCK_NAME = "Retire the docker-proxy units"


@pytest.fixture(scope="module")
def tasks() -> list[dict]:
    return yaml.safe_load(TASKS_FILE.read_text())


@pytest.fixture(scope="module")
def retirement(tasks) -> list[dict]:
    """The inner task list of the retirement block."""
    for task in tasks:
        if task.get("name") == BLOCK_NAME:
            return task["block"]
    raise AssertionError(f"no {BLOCK_NAME!r} task in {TASKS_FILE}")


# ---------------------------------------------------------------------------
# The assertions, as functions so the negative controls can reuse them
# ---------------------------------------------------------------------------

def listing_is_plain(shell: str) -> bool:
    """Does the unit listing strip systemd's status glyph before awk sees it?

    ``--plain`` is the only flag that does this. Asking for it by name rather
    than trying to detect a glyph-tolerant awk program: the role's parser is one
    field wide, and anything cleverer there is a second thing to keep right.
    """
    return "--plain" in shell


def resets_failed_state(block: list[dict]) -> bool:
    """Is there a task clearing the retired units' failed state?"""
    return any(_reset_argv(task) is not None for task in block)


def _reset_argv(task: dict) -> list | None:
    argv = (task.get("ansible.builtin.command") or task.get("command") or {})
    if not isinstance(argv, dict):
        return None
    argv = argv.get("argv")
    if isinstance(argv, list) and "reset-failed" in argv:
        return argv
    return None


# ---------------------------------------------------------------------------


class TestTheListing:
    def test_it_strips_the_status_glyph(self, retirement):
        """Without `--plain`, awk yields `●` for exactly the failed units."""
        listing = next(
            t for t in retirement
            if t.get("name") == "List existing docker-proxy instance units"
        )
        shell = listing["ansible.builtin.shell"]
        assert listing_is_plain(shell)

    def test_the_stop_loops_over_that_listing(self, retirement):
        """The glyph bug only mattered because the stop consumes this list.

        If the stop is ever rewritten to glob directly, `--plain` stops being
        load-bearing and this file is asserting about nothing.
        """
        stop = next(
            t for t in retirement
            if t.get("name") == "Stop and disable every docker-proxy unit"
        )
        assert "retired_docker_proxy_units.stdout_lines" in stop["loop"]


class TestTheFailedState:
    def test_the_block_clears_it(self, retirement):
        assert resets_failed_state(retirement)

    def test_it_resets_by_name_not_by_glob(self, retirement):
        """A glob would reach units this block never stopped.

        `reset-failed 'istota-docker-proxy@*'` looks equivalent and is not: it
        is evaluated by systemd against everything currently failed, where the
        registered list is only what the listing found.
        """
        argv = next(a for t in retirement if (a := _reset_argv(t)) is not None)
        assert "{{ item }}" in argv
        assert not any("*" in str(part) for part in argv)

    def test_it_runs_after_the_unit_files_are_removed(self, retirement):
        """Ordering, because a host still carrying the glyph bug enters the
        failed state *during* the removal — the unit was never stopped, so
        deleting its file is what fails it."""
        names = [t.get("name") for t in retirement]
        removal = names.index(
            "Remove the docker-proxy unit, tmpfiles snippet and socket directory"
        )
        reset = next(
            i for i, t in enumerate(retirement) if _reset_argv(t) is not None
        )
        assert reset > removal

    def test_it_cannot_fail_the_play(self, retirement):
        """`reset-failed` on a host with nothing failed is not an error, but a
        cleanup task taking the deploy down would be a worse bug than the
        residue it clears."""
        task = next(t for t in retirement if _reset_argv(t) is not None)
        assert task.get("failed_when") is False


class TestTheseAssertionsCanFail:
    """Both predicates, fed the text they were written against."""

    def test_the_listing_check_rejects_the_pre_fix_command(self):
        pre_fix = (
            "systemctl list-units --type=service --all --no-legend \\\n"
            "  '{{ istota_namespace }}-docker-proxy@*.service' \\\n"
            "  | awk '{print $1}' \\\n"
            "  || true\n"
        )
        assert not listing_is_plain(pre_fix)

    def test_the_reset_check_rejects_a_block_without_one(self):
        pre_fix_block = [
            {"name": "List existing docker-proxy instance units"},
            {"name": "Stop and disable every docker-proxy unit"},
            {"name": "Remove the docker-proxy unit, tmpfiles snippet and socket directory"},
        ]
        assert not resets_failed_state(pre_fix_block)
