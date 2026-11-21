/**
 * What a scheduled job's state and access look like, for the admin pane, the user's
 * own Scheduled jobs page and the network access card: one mapping, so the three
 * cannot disagree on a word or a colour.
 */
export type JobState = 'running' | 'disabled' | 'suspended';

export const STATE_LABEL = {
  running: 'Running',
  disabled: 'Disabled',
  suspended: 'Suspended',
} as const;

export const STATE_VARIANT = {
  running: 'success',
  disabled: 'neutral',
  suspended: 'warn',
} as const;

const ACCESS = {
  open: { label: 'Full access', variant: 'warn' },
  public: { label: 'Public', variant: 'info' },
  limited: { label: 'Limited', variant: 'neutral' },
} as const;

const OPERATOR_DEFAULT = "Your operator's default, with no approval for this job";

/** The badge names what the job runs with; where that is the operator's default
 *  rather than an approval, only the tooltip says so, and the card's caption. */
export function accessBadge(subject: {
  state: keyof typeof ACCESS;
  by_default: 'open' | 'public' | null;
}): { label: string; variant: (typeof ACCESS)[keyof typeof ACCESS]['variant']; title?: string } {
  if (subject.by_default) return { ...ACCESS[subject.by_default], title: OPERATOR_DEFAULT };
  return ACCESS[subject.state];
}
