"""One retry decision for the two places that used to make it (F4).

`process_one_task` classified a finished attempt twice: once in the branch that
writes the `tasks` row, and again ~130 lines later in the block that emits the
terminal task event a watching client waits for. The second copy derived five of
the six non-retryable classes and omitted the sixth, SIGPIPE — so a command task
killed by a `| head` was marked `failed` by the branch while the event block
computed `will_retry = True`, emitted "Attempt failed — retrying in N min…", and
skipped the terminal frame entirely.

**That divergence was latent, not live, and the tests below say so rather than
claiming a fix that had no live path.** `is_sigpipe` is gated on `task.command`,
and `process_one_task` builds an `EventWriter` only on the brain path — the
`elif task.command:` arm leaves it `None`, so the block holding the wrong answer
never ran for the only task shape that could produce that answer. What this
stage removes is the second derivation, so the two cannot disagree again if
either of those two facts changes.

The table below is the deliverable. It enumerates all 64 combinations of the six
flags against an oracle written from the *row-writing branch's* pre-change
source, which is the copy that was right, and re-runs the same 64 with
`success=True`.
"""

import itertools
import pathlib

import pytest

from istota.db import Task
from istota.scheduler import RetryDecision, decide_retry, retry_flags
from istota.shell_exec import SIGPIPE_NOTE

FLAG_NAMES = (
    "is_cancelled", "is_policy", "is_oom",
    "is_requeued", "is_permanent", "is_sigpipe",
)


def _task(*, attempt_count: int = 0, max_attempts: int = 3, command=None) -> Task:
    return Task(
        id=1, status="running", source_type="scheduled", user_id="testuser",
        prompt="", command=command,
        attempt_count=attempt_count, max_attempts=max_attempts,
    )


def _oracle_will_retry(task, flags: dict) -> bool:
    """What the row-writing branch decided, transcribed from before the change.

    Deliberately re-derived rather than imported: a table checked against the
    implementation it is testing passes by construction. The nesting is the
    branch's own — cancellation, a policy refusal and a shutdown requeue each
    took a distinct arm ahead of the retry test, so any of the three suppressed
    a retry regardless of the attempt budget.
    """
    if flags["is_cancelled"]:
        return False
    if flags["is_policy"]:
        return False
    if flags["is_requeued"]:
        return False
    return (
        task.attempt_count < task.max_attempts - 1
        and not flags["is_oom"]
        and not flags["is_permanent"]
        and not flags["is_sigpipe"]
    )


def _old_event_block_will_retry(task, flags: dict, *, success: bool) -> bool:
    """What the terminal-event block decided, transcribed from before the change.

    Identical to the branch's rule with one term missing — `is_sigpipe`. This
    exists to keep the removed disagreement stated somewhere a reader can see
    it, and to make the "they now agree" assertions below able to fail.
    """
    return (
        (not success)
        and not flags["is_cancelled"]
        and not flags["is_policy"]
        and not flags["is_oom"]
        and not flags["is_requeued"]
        and not flags["is_permanent"]
        and task.attempt_count < task.max_attempts - 1
    )


def _all_flag_combinations():
    for bits in itertools.product((False, True), repeat=len(FLAG_NAMES)):
        yield dict(zip(FLAG_NAMES, bits))


class TestDecideRetryAgainstTheBranchItReplaced:
    @pytest.mark.parametrize("flags", list(_all_flag_combinations()))
    def test_all_64_combinations_match_the_row_writing_branch(self, flags):
        # attempt_count 0 of 3, so the budget is open and the flags decide.
        task = _task(attempt_count=0, max_attempts=3)
        decision = decide_retry(task, "boom", **flags)
        assert decision.will_retry is _oracle_will_retry(task, flags), flags

    @pytest.mark.parametrize("flags", list(_all_flag_combinations()))
    def test_all_64_combinations_refuse_a_retry_on_the_last_attempt(self, flags):
        # attempt_count 2 of 3 — the budget is spent, so nothing retries however
        # the flags fall. Without this row the table above could pass with the
        # attempt-count term deleted.
        task = _task(attempt_count=2, max_attempts=3)
        decision = decide_retry(task, "boom", **flags)
        assert decision.will_retry is False, flags
        assert _oracle_will_retry(task, flags) is False, flags

    @pytest.mark.parametrize("flags", list(_all_flag_combinations()))
    def test_a_successful_attempt_never_retries(self, flags):
        task = _task(attempt_count=0, max_attempts=3)
        decision = decide_retry(task, "the answer", success=True, **flags)
        assert decision == RetryDecision(False, 0, "success"), flags

    @pytest.mark.parametrize("attempt_count,expected", [(0, 1), (1, 4), (2, 16)])
    def test_the_backoff_ladder_is_1_4_16(self, attempt_count, expected):
        task = _task(attempt_count=attempt_count, max_attempts=8)
        decision = decide_retry(
            task, "boom", **dict.fromkeys(FLAG_NAMES, False),
        )
        assert decision.will_retry is True
        assert decision.delay_minutes == expected

    def test_a_decision_that_does_not_retry_carries_no_delay(self):
        task = _task(attempt_count=0, max_attempts=3)
        for name in FLAG_NAMES:
            flags = dict.fromkeys(FLAG_NAMES, False) | {name: True}
            decision = decide_retry(task, "boom", **flags)
            assert decision.will_retry is False, name
            assert decision.delay_minutes == 0, name

    def test_the_reason_names_the_arm_the_call_sites_branch_on(self):
        # The row-writing branch selects its arm on `reason`, so these four
        # strings are load-bearing rather than diagnostic.
        task = _task(attempt_count=0, max_attempts=3)
        base = dict.fromkeys(FLAG_NAMES, False)
        assert decide_retry(task, "x", **(base | {"is_cancelled": True})).reason == "cancelled"
        assert decide_retry(task, "x", **(base | {"is_policy": True})).reason == "policy"
        assert decide_retry(task, "x", **(base | {"is_requeued": True})).reason == "requeued"
        assert decide_retry(task, "x", **base).reason == "retry"
        assert decide_retry(
            task, "x", **(base | {"is_cancelled": True, "is_policy": True}),
        ).reason == "cancelled", "cancellation must win, as the branch order has it"

    def test_an_exhausted_budget_is_distinguishable_from_a_refusal(self):
        base = dict.fromkeys(FLAG_NAMES, False)
        spent = decide_retry(_task(attempt_count=2, max_attempts=3), "x", **base)
        assert spent.reason == "attempts_exhausted"


class TestTheTwoSitesNoLongerDisagree:
    """The removed defect, kept stated so the fix can go red."""

    @pytest.mark.parametrize("flags", list(_all_flag_combinations()))
    def test_the_old_event_block_disagreed_on_exactly_the_sigpipe_rows(self, flags):
        task = _task(attempt_count=0, max_attempts=3)
        branch = _oracle_will_retry(task, flags)
        event_block = _old_event_block_will_retry(task, flags, success=False)
        disagreed = branch != event_block
        # The two copies differed by one term, so they disagree exactly where
        # that term is what decides — a SIGPIPE failure with nothing else set.
        expected = flags["is_sigpipe"] and not (
            flags["is_cancelled"] or flags["is_policy"]
            or flags["is_oom"] or flags["is_requeued"] or flags["is_permanent"]
        )
        assert disagreed is expected, flags

    @pytest.mark.parametrize("flags", list(_all_flag_combinations()))
    def test_the_shared_decision_now_answers_the_branch_everywhere(self, flags):
        task = _task(attempt_count=0, max_attempts=3)
        decision = decide_retry(task, "boom", **flags)
        assert decision.will_retry is _oracle_will_retry(task, flags), flags

    def test_a_sigpipe_command_failure_opens_the_terminal_frame_gate(self):
        # The event block emits the terminal `error` + `done` frames under
        # `not decision.will_retry and not flags["is_requeued"]`. This is the
        # input that used to fail that gate while the row was already `failed`.
        task = _task(attempt_count=0, max_attempts=3, command="yes | head -1")
        result = f"Command failed with exit 141. {SIGPIPE_NOTE}"
        flags = retry_flags(task, result, success=False)
        decision = decide_retry(task, result, **flags)

        assert flags["is_sigpipe"] is True
        assert decision.reason == "sigpipe"
        assert decision.will_retry is False
        assert flags["is_requeued"] is False
        # …and the copy this replaces did not.
        assert _old_event_block_will_retry(task, flags, success=False) is True


class TestRetryFlags:
    def test_a_sigpipe_note_without_a_command_is_not_a_sigpipe_failure(self):
        # `_execute_command_task` is the only thing that composes this text; an
        # LLM answer quoting it must not suppress a retry the task earned.
        task = _task(command=None)
        flags = retry_flags(task, f"the model said: {SIGPIPE_NOTE}", success=False)
        assert flags["is_sigpipe"] is False

    def test_a_cancellation_is_recognised_by_its_exact_result_text(self):
        task = _task()
        assert retry_flags(task, "Cancelled by user", success=False)["is_cancelled"]
        assert not retry_flags(task, "cancelled by user", success=False)["is_cancelled"]

    def test_an_oom_kill_is_recognised(self):
        task = _task()
        flags = retry_flags(task, "Process killed (likely out of memory)", success=False)
        assert flags["is_oom"] is True

    def test_a_successful_attempt_carries_no_failure_class(self):
        # `result` on the success path is the model's answer. Classifying it
        # would let an answer reading like a cancellation change what the
        # watching client is sent.
        task = _task(command="yes | head -1")
        flags = retry_flags(task, "Cancelled by user", success=True)
        assert flags == dict.fromkeys(FLAG_NAMES, False)

    def test_the_keys_are_exactly_decide_retrys_keyword_names(self):
        # The two call sites splat this straight in; a key that drifts is a
        # TypeError at runtime, and this is where it should be caught instead.
        flags = retry_flags(_task(), "boom", success=False)
        assert set(flags) == set(FLAG_NAMES)
        decide_retry(_task(), "boom", **flags)  # must not raise


class TestNoSecondCopyComesBack:
    """The pin. Ten prose "this mirrors X" comments did not stop this drifting.

    Grep-shaped rather than behavioural, because the failure being guarded is a
    *third* implementation appearing — which no behavioural test can see, since
    each copy passes its own tests right up to the day they disagree.
    """

    def _scheduler_source(self) -> str:
        import istota.scheduler

        return pathlib.Path(istota.scheduler.__file__).read_text(encoding="utf-8")

    def test_the_backoff_shift_is_written_once(self):
        source = self._scheduler_source()
        hits = [
            line.strip() for line in source.splitlines()
            if "1 << (task.attempt_count" in line
        ]
        assert len(hits) == 1, (
            "the 1/4/16 backoff is stated more than once in scheduler.py; it "
            f"lives in decide_retry and nowhere else. Found: {hits}"
        )

    def test_the_attempt_budget_test_is_written_once(self):
        source = self._scheduler_source()
        hits = [
            line.strip() for line in source.splitlines()
            if "max_attempts - 1" in line
        ]
        assert len(hits) == 1, (
            "'attempt_count < max_attempts - 1' is stated more than once in "
            f"scheduler.py; decide_retry owns it. Found: {hits}"
        )

    def test_the_terminal_event_block_derives_no_flag_of_its_own(self):
        # What the block held before this stage. Any of these reappearing means
        # the second classifier is back, whether or not it agrees today.
        #
        # `is_cancelled = ` is deliberately not on the list: `run_task_inline`
        # (the REPL path, which by contract has no retry ladder at all) derives
        # that one flag on its own to choose between `cancelled` and `error`,
        # and folding it into the six-way classifier would give a function that
        # never retries a retry decision.
        source = self._scheduler_source()
        for banned in (
            "is_policy = ", "is_oom = ", "is_requeued = ",
            "is_permanent_api = ", "will_retry = ",
        ):
            assert banned not in source, (
                f"scheduler.py assigns {banned.strip()!r} again — the retry "
                "classification belongs to retry_flags/decide_retry"
            )
