import { pushState, replaceState } from '$app/navigation';
import { page } from '$app/state';
import { untrack } from 'svelte';

export type Params = Record<string, string>;
export interface UrlSelectionSpec<T> {
  key: string;
  params: readonly string[];
  /** Store reconciliation ignores event params such as a chat jump target. */
  compareKeys?: readonly string[];
  encode(sel: T): Params;
  decode(params: Params): T | null;
  /** Return null until the page has applied current() after its initial load. */
  read(): T | null;
  apply(sel: T): void | Promise<void>;
}
export interface UrlSelection<T> {
  push(sel: T): void;
  /** Install effects once during component initialization. */
  start(): void;
  current(): T | null;
}
function canonical(params: Params, keys: readonly string[]): string {
  const query = new URLSearchParams();
  for (const key of [...keys].sort()) {
    if (params[key] !== undefined) query.set(key, params[key]);
  }
  return query.toString();
}

export function createUrlSelection<T>(spec: UrlSelectionSpec<T>): UrlSelection<T> {
  let started = false;
  let observed: string | undefined;
  const compareKeys = spec.compareKeys ?? spec.params;

  function warn(error: unknown): void {
    console.warn('urlSelection', spec.key, error);
  }

  function urlParams(): Params {
    const state = (page.state as Record<string, unknown>)[spec.key];
    const fromState = state !== null && typeof state === 'object' && !Array.isArray(state);
    const params: Params = {};
    for (const key of spec.params) {
      const value = fromState
        ? (state as Record<string, unknown>)[key]
        : page.url.searchParams.get(key);
      if (typeof value === 'string' && value) params[key] = value;
    }
    return params;
  }

  function current(): T | null {
    try {
      return spec.decode(urlParams());
    } catch (error) {
      warn(error);
      return null;
    }
  }

  function apply(sel: T): void {
    try {
      Promise.resolve(spec.apply(sel)).catch(warn);
    } catch (error) {
      warn(error);
    }
  }

  function write(params: Params, push: boolean): void {
    try {
      const query = new URLSearchParams(location.search);
      for (const key of spec.params) query.delete(key);
      for (const [key, value] of Object.entries(params)) query.set(key, value);
      const search = query.toString();
      const url = location.pathname + (search ? `?${search}` : '') + location.hash;
      const state = { ...page.state, [spec.key]: params };
      if (push) pushState(url, state);
      else replaceState(url, state);
      // A write from this helper must not replay its own apply as a popstate.
      observed = canonical(params, spec.params);
    } catch (error) {
      warn(error);
    }
  }

  function push(sel: T): void {
    try {
      const params = spec.encode(sel);
      if (started && canonical(params, spec.params) === canonical(urlParams(), spec.params)) return;
      apply(sel);
      if (started) write(params, true);
    } catch (error) {
      warn(error);
    }
  }

  function start(): void {
    if (started || typeof window === 'undefined') return;
    started = true;
    // The page owns initial application, after its list has loaded.
    observed = untrack(() => canonical(urlParams(), spec.params));
    $effect(() => {
      const state = (page.state as Record<string, unknown>)[spec.key];
      // Only the namespace is reactive; store changes must not trigger replay.
      void state;
      untrack(() => {
        const params = urlParams();
        const next = canonical(params, spec.params);
        if (next === observed) return;
        observed = next;
        const sel = current();
        const shown = spec.read();
        if (sel === null) {
          // No apply means no store mutation to wake the reconcile effect.
          if (shown !== null) write(spec.encode(shown), false);
        } else if (
          shown === null ||
          canonical(spec.encode(sel), spec.params) !== canonical(spec.encode(shown), spec.params)
        ) {
          apply(sel);
        }
      });
    });
    $effect(() => {
      const shown = spec.read();
      untrack(() => {
        if (shown === null) return;
        const params = spec.encode(shown);
        if (canonical(params, compareKeys) !== canonical(urlParams(), compareKeys)) {
          write(params, false);
        }
      });
    });
  }

  return { push, start, current };
}
