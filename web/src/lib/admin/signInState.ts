/**
 * How a user signs in to the web UI, in the words the admin Users table and
 * the user editor both show. One place, so the row and the editor it opens
 * cannot name the same state two ways.
 */
export function signInStateLabel(
  identity: { disabled: boolean } | null,
  state: string | undefined,
): string {
  if (!identity) return 'Nextcloud only';
  if (identity.disabled) return 'Disabled';
  return state === 'password_set' ? 'Password set' : 'Email code';
}
