import { Plug, Receipt, PieChart } from 'lucide-svelte';
import type { ComponentType } from 'svelte';

/**
 * The money settings sections, in sidebar order.
 *
 * Lives here rather than in the settings layout because the sidebar is rendered
 * by `routes/money/+layout.svelte` — that layout owns the module's `AppShell`,
 * and a `Sidebar` has to be a sibling of `.shell-main` to be a column beside it
 * rather than a block scrolling inside it. Keeping the list in one importable
 * module is what stops the layout and the routes disagreeing about it.
 *
 * `href` is the suffix after `/money/settings`; the index section is `''`.
 */
export interface MoneySettingsSection {
  href: string;
  label: string;
  icon: ComponentType;
}

export const MONEY_SETTINGS_SECTIONS: MoneySettingsSection[] = [
  { href: '', label: 'Connections', icon: Plug },
  { href: '/invoicing', label: 'Invoicing', icon: Receipt },
  { href: '/portfolio', label: 'Portfolio', icon: PieChart },
];
