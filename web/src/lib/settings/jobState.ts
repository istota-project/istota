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

const ACCESS_BY_DEFAULT = {
  open: { label: 'Full access by default', variant: 'warn' },
  public: { label: 'Public by default', variant: 'info' },
} as const;

export function accessBadge(subject: {
  state: keyof typeof ACCESS;
  by_default: keyof typeof ACCESS_BY_DEFAULT | null;
}) {
  return subject.by_default ? ACCESS_BY_DEFAULT[subject.by_default] : ACCESS[subject.state];
}
