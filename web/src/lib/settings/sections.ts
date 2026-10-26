import {
  UserRound,
  SlidersHorizontal,
  Send,
  KeyRound,
  Plug,
  Wallet,
  Bell,
  Smartphone,
  LayoutGrid,
} from '@lucide/svelte';
import type { LucideIcon } from '@lucide/svelte';

/**
 * The user settings sections, in sidebar order.
 *
 * Read by `routes/settings/+layout.svelte`, which renders the sidebar, and by
 * the tests that walk every section — one list, so the two cannot disagree
 * about which sections exist. Same shape as `MONEY_SETTINGS_SECTIONS`.
 *
 * `href` is the suffix after `/settings`; the index section is `''`.
 * `feature` names the `/me` feature a section belongs to, for one the user may
 * not have (Apps is an experimental module); the sidebar leaves it out then.
 */
export interface UserSettingsSection {
  href: string;
  label: string;
  icon: LucideIcon;
  feature?: 'apps';
}

export const USER_SETTINGS_SECTIONS: UserSettingsSection[] = [
  { href: '', label: 'Account', icon: UserRound },
  { href: '/preferences', label: 'Preferences', icon: SlidersHorizontal },
  { href: '/notifications', label: 'Notifications', icon: Bell },
  { href: '/delivery', label: 'Delivery', icon: Send },
  { href: '/credentials', label: 'Credentials', icon: KeyRound },
  { href: '/wallet', label: 'Wallet', icon: Wallet },
  { href: '/connections', label: 'Connections', icon: Plug },
  { href: '/devices', label: 'Devices', icon: Smartphone },
  { href: '/apps', label: 'Apps', icon: LayoutGrid, feature: 'apps' },
];
