import { getContext, setContext, type Snippet } from 'svelte';
import type { AppsList } from './api';

const APPS_LIST = Symbol('apps-list');

/** What a page under the section puts in the app bar: the app view's name and its tools. */
export interface AppsHeader {
  title: string;
  tools?: Snippet;
}

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
  /**
   * The width of the pane the section's pages render in, in CSS px, or null
   * before it has been measured. Display ranges are judged against this, not
   * the window: on a desktop the sidebar takes its share.
   */
  readonly paneWidth: number | null;
  /** Fetch the list again, after an install, a reinstall or a return to the index. */
  reload: () => Promise<void>;
  /** Replace the app bar's title and tools; `null` puts back "Apps". */
  setHeader: (header: AppsHeader | null) => void;
}

export function setAppsList(ctx: AppsListContext): void {
  setContext(APPS_LIST, ctx);
}

export function getAppsList(): AppsListContext {
  const ctx = getContext<AppsListContext | undefined>(APPS_LIST);
  if (!ctx) throw new Error('getAppsList() outside the apps layout');
  return ctx;
}
