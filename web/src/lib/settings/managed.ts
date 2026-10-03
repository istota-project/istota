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

/** The same lock as the admin user editor shows it: the admin is the one who
 *  can change the inventory, so the badge names where the value comes from. */
export const ADMIN_MANAGED_BADGE = 'Set by deployment';

/** Where an admin changes a managed field. The `istota_users` keys match the
 *  profile field names one to one, `whatsapp_number` included. */
export function adminManagedHint(userId: string, field: string): string {
  return `Change istota_users.${userId}.${field} in inventory, or set istota_user_profile_mode: seed.`;
}

/** The badge text for a field, or `undefined` when the user owns it. */
export function managedBadge(
  profile: Pick<UserProfile, 'managed'> | null,
  field: string,
): string | undefined {
  return isManaged(profile, field) ? MANAGED_BADGE : undefined;
}
