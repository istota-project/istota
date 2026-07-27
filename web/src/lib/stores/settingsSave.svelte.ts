import { onDestroy } from 'svelte';
import { derived, writable, type Readable } from 'svelte/store';

/**
 * One contributor to the open settings page's save — the page's own form, or a
 * `ServiceCard`'s pending credential edits. A page may have several.
 */
export interface SettingsSaveState {
  /** Whether this contributor holds unsaved edits. */
  dirty: boolean;
  /** Whether this contributor's save is in flight. */
  saving: boolean;
  /** Persist. Must handle its own errors — the aggregate save awaits it. */
  save: () => void | Promise<void>;
  /**
   * Button text at rest, when the default "Save changes" is wrong. At most one
   * contributor per page should set it; whichever does wins regardless of
   * registration order.
   */
  label?: string;
}

/** The whole app bar's view of the page: what to show, and what to run. */
export interface AggregateSave {
  dirty: boolean;
  saving: boolean;
  save: () => Promise<void>;
  label?: string;
}

const registry = writable<Map<symbol, SettingsSaveState>>(new Map());

/**
 * The open page's save, or null when nothing registered one — which is what
 * lets `HeaderSave` live unconditionally in a module layout's `tools` slot and
 * stay invisible on every page of that section except its settings page.
 *
 * Contributors are aggregated rather than replaced: `/settings` edits the
 * profile *and* holds a `ServiceCard` per connected service, and each of those
 * writes through a different endpoint. One button covers them all, and only
 * the ones actually holding edits are asked to save.
 */
export const settingsSave: Readable<AggregateSave | null> = derived(registry, (map) => {
  const parts = [...map.values()];
  if (parts.length === 0) return null;
  return {
    dirty: parts.some((p) => p.dirty),
    saving: parts.some((p) => p.saving),
    label: parts.find((p) => p.label)?.label,
    save: async () => {
      // Sequential, so a failure surfaces against its own card rather than
      // racing three error banners onto the page at once. Each contributor
      // catches its own errors, so one failure does not strand the rest.
      for (const part of parts) {
        if (part.dirty) await part.save();
      }
    },
  };
});

/**
 * Publish this component's save into the app bar for as long as it is mounted.
 * Call it once during component init; `state` is re-read whenever anything it
 * touches changes, so `dirty`/`saving` stay live. Return null to withdraw the
 * contribution — a page whose module is switched off has nothing to save, and
 * an OAuth `ServiceCard` has no fields to write.
 */
export function useSettingsSave(state: () => SettingsSaveState | null): void {
  // Identity is per call site, so a component registers and withdraws only its
  // own entry. Teardown order between an outgoing page and an incoming one is
  // therefore irrelevant.
  const token = Symbol('settings-save');

  function withdraw() {
    registry.update((map) => {
      if (!map.has(token)) return map;
      const next = new Map(map);
      next.delete(token);
      return next;
    });
  }

  $effect(() => {
    const part = state();
    if (!part) {
      withdraw();
      return;
    }
    registry.update((map) => new Map(map).set(token, part));
  });

  onDestroy(withdraw);
}
