import { UserRound, SlidersHorizontal, Send, KeyRound, Plug } from '@lucide/svelte';
import type { LucideIcon } from '@lucide/svelte';

/**
 * The user settings sections, in sidebar order.
 *
 * Read by `routes/settings/+layout.svelte`, which renders the sidebar, and by
 * the tests that walk every section — one list, so the two cannot disagree
 * about which sections exist. Same shape as `MONEY_SETTINGS_SECTIONS`.
 *
 * `href` is the suffix after `/settings`; the index section is `''`.
 */
export interface UserSettingsSection {
  href: string;
  label: string;
  icon: LucideIcon;
}

export const USER_SETTINGS_SECTIONS: UserSettingsSection[] = [
  { href: '', label: 'Account', icon: UserRound },
  { href: '/preferences', label: 'Preferences', icon: SlidersHorizontal },
  { href: '/delivery', label: 'Delivery', icon: Send },
  { href: '/credentials', label: 'Credentials', icon: KeyRound },
  { href: '/connections', label: 'Connections', icon: Plug },
];
