/**
 * A service card, and the read-only state it no longer has.
 *
 * A credential vault used to be able to own a *typed* service: the file was the
 * authority for `karakeep` or `ntfy`, the next sync overwrote whatever was typed
 * here, and `PUT` / `DELETE` on those keys answered 409 — so the card rendered
 * disabled with a sentence saying where to edit it instead. A vault writes one
 * flat namespace of shared credentials now and overwrites no typed service, so
 * the refusal went and this card is ordinary again for every service.
 *
 * The first block is the guard on that. It drives the exact payload the old
 * branch keyed on and requires a working, editable card — which is what makes
 * it a test of the removal rather than of a fixture nobody builds any more.
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

/** The sentence the removed branch rendered. Nothing may produce it again. */
const EXPLANATION = /come from your credential vault/i;

function card(over: Partial<ServiceCardData> = {}): ServiceCardData {
  return {
    service: 'karakeep',
    label: 'Karakeep',
    status: 'configured',
    fields: [
      { key: 'base_url', label: 'Base URL', type: 'text' },
      { key: 'api_key', label: 'API key', type: 'password' },
    ],
    configured_keys: ['base_url', 'api_key'],
    last_updated: null,
    ...over,
  };
}

afterEach(cleanup);

describe('the retired vault-managed state', () => {
  // The payload key is gone, so the only way to drive the old branch is to put
  // it back by hand. A card that rendered read-only for this is a card whose
  // branch came back.
  const asManaged = () => ({ ...card(), vault_managed: true }) as unknown as ServiceCardData;

  it('leaves the fields editable', () => {
    render(ServiceCard, { service: asManaged() });

    const inputs = [...document.querySelectorAll('input')] as HTMLInputElement[];
    expect(inputs.length).toBe(2);
    expect(inputs.every((i) => !i.disabled)).toBe(true);
  });

  it('says nothing about a vault owning the service', () => {
    render(ServiceCard, { service: asManaged() });
    expect(screen.queryByText(EXPLANATION)).toBeNull();
  });

  it('keeps its clear buttons', () => {
    render(ServiceCard, { service: asManaged() });
    expect(screen.getAllByTitle('Clear stored value').length).toBe(2);
  });

  it('still claims the app bar Save button', () => {
    render(ServiceCard, { service: asManaged() });
    expect(get(settingsSave)).not.toBeNull();
  });
});

describe('an ordinary service card', () => {
  it('leaves its fields editable and says nothing about a vault', () => {
    // The control for the block above: each of those assertions would pass
    // against a component that had stopped rendering fields at all.
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
    render(ServiceCard, { service: card() });
    expect(get(settingsSave)).not.toBeNull();
  });

  it('claims nothing when it has no writable fields', () => {
    // The surviving reason a card withdraws from the shared Save button: a
    // service with no fields of its own. Without this the assertions above pass
    // against a component that registers unconditionally.
    render(ServiceCard, { service: card({ fields: [], configured_keys: [] }) });
    expect(get(settingsSave)).toBeNull();
  });
});
