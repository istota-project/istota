import { getContext, setContext } from 'svelte';
import type { UserProfile } from '$lib/api';

const USER_SETTINGS = Symbol('user-settings');

/**
 * The profile record the Account, Preferences and Delivery sections all edit,
 * held by `routes/settings/+layout.svelte`.
 *
 * One record and one save across three routes: the layout persists across a
 * section switch, so an edit made on Account and not yet saved is still there
 * — and still behind the app-bar Save — after a visit to Delivery. Each field
 * is a getter so the layout can back it with `$state`; `profile` is the
 * layout's own proxy, so a section binding to one of its fields is editing
 * the record the save reads.
 */
export interface UserSettingsContext {
  readonly profile: UserProfile | null;
  readonly allModules: string[];
  readonly loading: boolean;
  /** Page-level banners, shared so a section's message survives the save's reload. */
  error: string;
  info: string;
  /** Re-fetch the profile and re-resolve the identity. */
  reload: () => Promise<void>;
}

export function setUserSettings(ctx: UserSettingsContext): void {
  setContext(USER_SETTINGS, ctx);
}

export function getUserSettings(): UserSettingsContext {
  const ctx = getContext<UserSettingsContext | undefined>(USER_SETTINGS);
  if (!ctx) throw new Error('getUserSettings() outside the settings layout');
  return ctx;
}
