import { writable } from 'svelte/store';

export type PortfolioGroupBy = 'total' | 'group' | 'account_type' | 'asset_class';

// Overview's group filter ('' = all groups). The Select lives in the portfolio
// section header (rendered by the layout, like the reports year filter), so
// the value crosses a layout/page boundary and lives here. The page pins the
// option list from its unfiltered load and only extends it, never shrinks it —
// a filtered response narrows by_group to one slice.
export const portfolioGroup = writable('');
export const portfolioKnownGroups = writable<string[]>([]);

// History's stacking dimension, same header arrangement.
export const portfolioGroupBy = writable<PortfolioGroupBy>('total');
