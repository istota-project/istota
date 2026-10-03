import type { UserProfile } from '$lib/api';

/**
 * Fields the deployment writes on every converge (`istota user ensure
 * --managed`). The profile PUT refuses an edit to one with a 409, so the page
 * renders it disabled and says why, rather than letting a save fail.
 */
export const MANAGED_BADGE = 'Set by your administrator';

export function isManaged(profile: Pick<UserProfile, 'managed'> | null, field: string): boolean {
  return !!profile?.managed?.includes(field);
}

/** The badge text for a field, or `undefined` when the user owns it. */
export function managedBadge(
  profile: Pick<UserProfile, 'managed'> | null,
  field: string,
): string | undefined {
  return isManaged(profile, field) ? MANAGED_BADGE : undefined;
}
