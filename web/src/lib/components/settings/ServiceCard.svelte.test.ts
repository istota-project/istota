/**
 * A service card whose credentials come from the user's KDBX vault.
 *
 * The server refuses the write either way — `PUT` and `DELETE` on a vault-owned
 * service answer 409 — so what this state is for is narrower and worth being
 * exact about: it stops somebody typing a credential into a form that was never
 * going to take it. A write that got through would not be corrected on the next
 * sync, because it touches no byte of the KDBX and the sync is edge-triggered on
 * the file's digest, so the form's promise would be wrong permanently and
 * silently. The 409 is the boundary; this is what keeps a user from walking into
 * it.
 *
 * Every assertion here is paired with the unmanaged render of the same card,
 * because "disabled" and "absent" are easy to confuse in a DOM query and an
 * ordinary card has to keep working.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { ServiceCard as ServiceCardData } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import ServiceCard from './ServiceCard.svelte';

const EXPLANATION = /come from your credential vault/i;

function card(over: Partial<ServiceCardData> = {}): ServiceCardData {
  return {
    service: 'karakeep',
    label: 'Karakeep',
    status: 'configured',
    fields: [
      { key: 'base_url', label: 'Base URL', type: 'url' },
      { key: 'api_key', label: 'API key', type: 'password' },
    ],
    configured_keys: ['base_url', 'api_key'],
    last_updated: null,
    ...over,
  };
}

afterEach(cleanup);

describe('a vault-managed service card', () => {
  it('disables every field and says why', () => {
    render(ServiceCard, { service: card({ vault_managed: true }) });

    expect(screen.getByText(EXPLANATION)).toBeTruthy();
    const inputs = screen.getAllByRole('textbox', { hidden: true });
    expect(inputs.length).toBeGreaterThan(0);
    for (const input of document.querySelectorAll('input')) {
      expect((input as HTMLInputElement).disabled).toBe(true);
    }
  });

  it('offers no way to clear a stored value', () => {
    // Clearing is a DELETE, which the same 409 refuses. Leaving the button
    // would offer the one action on this card that is guaranteed to fail — and
    // it is the destructive one, so its failure is the confusing kind.
    render(ServiceCard, { service: card({ vault_managed: true }) });
    expect(screen.queryByTitle('Clear stored value')).toBeNull();
  });

  it('withdraws from the app bar Save button', () => {
    // The button is shared by the whole page, so a card that can never be
    // saved must not be one of the things claiming it — otherwise Save appears
    // for a page with nothing writable on it. `null` is the store's own way of
    // saying nobody registered, which is what makes `HeaderSave` invisible.
    render(ServiceCard, { service: card({ vault_managed: true }) });
    expect(get(settingsSave)).toBeNull();
  });
});

describe('an ordinary service card', () => {
  it('leaves its fields editable and says nothing about a vault', () => {
    // The control for all three above. Without it each of them would pass
    // against a card component that had stopped rendering fields at all.
    render(ServiceCard, { service: card() });

    expect(screen.queryByText(EXPLANATION)).toBeNull();
    const inputs = [...document.querySelectorAll('input')] as HTMLInputElement[];
    expect(inputs.length).toBe(2);
    expect(inputs.every((i) => !i.disabled)).toBe(true);
  });

  it('keeps its clear buttons', () => {
    render(ServiceCard, { service: card() });
    expect(screen.getAllByTitle('Clear stored value').length).toBe(2);
  });

  it('still claims the app bar Save button', () => {
    // The control for the withdrawal above: without it that assertion passes
    // against a component that had stopped registering at all.
    render(ServiceCard, { service: card() });
    expect(get(settingsSave)).not.toBeNull();
  });

  it('treats a missing flag as unmanaged', () => {
    // The payload carries `vault_managed` on every card, but an older client
    // and every hand-built fixture omit it — and `undefined` must read as
    // "editable" rather than disabling a card nothing owns.
    const { vault_managed: _dropped, ...rest } = card({ vault_managed: true });
    render(ServiceCard, { service: rest as ServiceCardData });
    expect(screen.queryByText(EXPLANATION)).toBeNull();
  });
});
