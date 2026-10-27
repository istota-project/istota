import type { AdminConfigView, ConfigSecretFile, ConfigSecretSource } from '$lib/api';

export interface CredentialStatus {
  label: string;
  variant: 'success' | 'warn' | 'info' | 'neutral';
}

/**
 * The badge a credential field shows: where its value is held, from the
 * view's `secrets` entry. A credential with no entry (one only config.toml
 * holds, by design) says only whether it is set.
 */
export function credentialStatus(set: boolean, source?: ConfigSecretSource): CredentialStatus {
  if (source === 'file') return { label: 'Set', variant: 'success' };
  if (source === 'inline') return { label: 'In config.toml', variant: 'warn' };
  if (source === 'environment') return { label: 'Set outside the stack', variant: 'info' };
  if (source === 'unknown') return { label: 'Unknown here', variant: 'neutral' };
  return set ? { label: 'Set', variant: 'success' } : { label: 'Not set', variant: 'neutral' };
}

/** The view's secrets entries by the config key that reads them. */
export function secretsByConfigKey(view: AdminConfigView | null): Map<string, ConfigSecretFile> {
  const found = new Map<string, ConfigSecretFile>();
  for (const secret of view?.secrets ?? []) {
    if (secret.config_key) found.set(secret.config_key, secret);
  }
  return found;
}
