/**
 * What an assignment's state looks like and which steering it allows, for the list and
 * the detail. The steering rules mirror `assignments/lifecycle.py`, which decides: a
 * button here only saves a refusal.
 */
import type { AssignmentLedger, AssignmentState, AssignmentSummary } from '$lib/api';

export const ASSIGNMENT_STATE_LABEL: Record<AssignmentState, string> = {
  draft: 'Draft',
  sleeping: 'Sleeping',
  active: 'Running',
  waiting: 'Waiting',
  paused: 'Paused',
  completed: 'Completed',
  abandoned: 'Abandoned',
};

export const ASSIGNMENT_STATE_VARIANT = {
  draft: 'neutral',
  sleeping: 'info',
  active: 'success',
  waiting: 'warn',
  paused: 'warn',
  completed: 'success',
  abandoned: 'neutral',
} as const;

const PAUSE_REASON: Record<string, string> = {
  user: 'You paused it.',
  budget: 'Its budget is spent, or too little is left for another wake.',
  stalled: 'Its last wakes made no progress that could be measured.',
  failures: 'Its last wakes failed.',
  thrash: 'It kept failing on the same step.',
  deadline: 'Its deadline has passed.',
};

const WAKE_DUE: Record<string, string> = {
  time: 'Its next wake is due now.',
  inbox: 'Something is waiting for it, so it wakes on the next check.',
  deadline: 'Its deadline has passed, so it wakes on the next check.',
};

const FIRED_BY: Record<string, string> = {
  time: 'On schedule',
  inbox: 'Your input',
  deadline: 'Deadline',
  manual: 'Requested',
};

const OUTCOME: Record<string, string> = {
  next: 'Set its next step',
  no_next_step: 'No next step set',
  failed: 'Failed',
  cancelled: 'Cancelled',
  not_active: 'Not run',
  complete_proposed: 'Proposed completion',
  complete_refused: 'Completion refused',
  completed: 'Completed',
};

export function isFinished(state: AssignmentState): boolean {
  return state === 'completed' || state === 'abandoned';
}

export function proposesCompletion(assignment: AssignmentSummary): boolean {
  return assignment.state === 'waiting' && assignment.waiting_on.includes('completion');
}

/** A reason in words. An abandoned assignment's is what the user typed, and is text. */
export function reasonText(assignment: AssignmentSummary): string | null {
  const reason = assignment.state_reason;
  if (assignment.state === 'waiting') {
    if (proposesCompletion(assignment)) return 'It says it is done, and waits for your answer.';
    return 'Waiting on you.';
  }
  if (!reason) return null;
  if (assignment.state === 'paused') return PAUSE_REASON[reason] ?? reason;
  if (assignment.state === 'abandoned') return reason;
  return null;
}

export function wakeDueText(due: string | null): string | null {
  return due ? (WAKE_DUE[due] ?? 'It wakes on the next check.') : null;
}

export function firedByText(firedBy: string): string {
  return FIRED_BY[firedBy] ?? firedBy;
}

export function outcomeText(outcome: string | null): string {
  if (!outcome) return 'Still running';
  return OUTCOME[outcome] ?? outcome;
}

export function steering(assignment: AssignmentSummary) {
  const { state, state_reason } = assignment;
  const live = state !== 'draft' && !isFinished(state);
  return {
    pause: state === 'sleeping' || state === 'active' || state === 'waiting',
    // A budget pause ends with a top-up: resume refuses while the budget is spent.
    resume: state === 'paused' && state_reason !== 'budget',
    wake: state === 'sleeping' || (state === 'waiting' && !proposesCompletion(assignment)),
    abandon: live,
    live,
  };
}

/** Dollars in the charter's unit. Cents, unless the figure would round away to nothing. */
export function usd(amount: number): string {
  return `$${amount > 0 && amount < 0.01 ? amount.toFixed(4) : amount.toFixed(2)}`;
}

function share(used: number, cap: number | undefined): number {
  return cap && cap > 0 ? (used / cap) * 100 : 0;
}

/** The nearer of the two caps as a percentage, since either one stops the assignment. */
export function budgetUsedPercent(assignment: AssignmentSummary, ledger: AssignmentLedger): number {
  const used = Math.max(
    share(ledger.cost_usd, assignment.budget.max_cost_usd),
    share(ledger.wakes, assignment.budget.max_wakes),
  );
  return Math.min(100, Math.round(used));
}

export function budgetLine(assignment: AssignmentSummary, ledger: AssignmentLedger): string {
  const cost = `${usd(ledger.cost_usd)} of ${usd(assignment.budget.max_cost_usd ?? 0)} list price`;
  return `${cost}, ${ledger.wakes} of ${assignment.budget.max_wakes ?? 0} wakes`;
}
