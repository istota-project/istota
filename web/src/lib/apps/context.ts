import { getContext, setContext } from 'svelte';
import type { AppsList } from './api';

const APPS_LIST = Symbol('apps-list');

/**
 * The Apps list, fetched by `routes/apps/+layout.svelte` and read by the pages
 * under it. The layout needs it for the sidebar on every page of the section,
 * so "All apps" reads the same answer rather than asking again. The fields are
 * getters so the layout can back them with `$state`.
 */
export interface AppsListContext {
  readonly list: AppsList | null;
  readonly loading: boolean;
  readonly error: string;
  /** Fetch the list again, after an install, a reinstall or a return to the index. */
  reload: () => Promise<void>;
}

export function setAppsList(ctx: AppsListContext): void {
  setContext(APPS_LIST, ctx);
}

export function getAppsList(): AppsListContext {
  const ctx = getContext<AppsListContext | undefined>(APPS_LIST);
  if (!ctx) throw new Error('getAppsList() outside the apps layout');
  return ctx;
}
