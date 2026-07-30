import type { ImmunizationStatus } from '$lib/api';

// The label map and the badge colouring were written out on every page that
// showed an immunization status — identically for the labels, and as five
// hand-written hsla() fills for the colours, each pairing with a --status-*-fg
// token that already carried the matching -bg it was open-coding.

const LABELS: Record<ImmunizationStatus, string> = {
  up_to_date: 'Up to date',
  due_soon: 'Due soon',
  overdue: 'Overdue',
  series_incomplete: 'Series incomplete',
  never_recorded: 'Never recorded',
  expired: 'Expired',
  risk_based: 'Risk-based',
  recorded: 'Recorded',
};

export type BadgeVariant = 'neutral' | 'danger' | 'warn' | 'success' | 'info' | 'partial';

// `series_incomplete` is deliberately off the severity ramp: the schedule is
// part-done, not late, and giving it a severity would rank it against overdue.
const VARIANTS: Record<ImmunizationStatus, BadgeVariant> = {
  up_to_date: 'success',
  due_soon: 'warn',
  overdue: 'danger',
  expired: 'danger',
  series_incomplete: 'partial',
  never_recorded: 'neutral',
  recorded: 'neutral',
  risk_based: 'neutral',
};

export function immunizationStatusLabel(status: ImmunizationStatus): string {
  return LABELS[status] ?? status;
}

export function immunizationStatusVariant(status: ImmunizationStatus): BadgeVariant {
  return VARIANTS[status] ?? 'neutral';
}

/** A diagnosis's clinical status. "Active" is danger-tinted because that is
 *  what the pages showed before this was a shared map, not a claim that an
 *  active condition is an error. */
export function diagnosisStatusVariant(status: string): BadgeVariant {
  if (status === 'active') return 'danger';
  if (status === 'chronic') return 'warn';
  if (status === 'resolved') return 'success';
  return 'neutral';
}

/** A diagnosis's severity, on the ordinary severity ramp. */
export function severityVariant(severity: string): BadgeVariant {
  if (severity === 'severe') return 'danger';
  if (severity === 'moderate') return 'warn';
  if (severity === 'mild') return 'success';
  return 'neutral';
}

/** Extraction confidence, as reported by the OCR/parse review screens. */
export function confidenceVariant(confidence: string): BadgeVariant {
  if (confidence === 'high') return 'success';
  if (confidence === 'medium') return 'warn';
  if (confidence === 'low') return 'danger';
  return 'neutral';
}
