import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { ServiceCard } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

function service(name: string, label: string): ServiceCard {
  return {
    service: name,
    label,
    status: 'configured',
    fields: [],
    configured_keys: [],
    last_updated: null,
  };
}

await fillApiDouble(api, {
  getModuleServices: vi.fn(async () => ({
    module: 'location',
    module_enabled: true,
    services: [service('overland', 'Overland'), service('carto', 'Carto')],
  })),
  getLocationSettingsInfo: vi.fn(async () => ({
    webhook_url: 'https://server.example.com/webhooks/location',
    module_enabled: true,
    place_detection: { accuracy_threshold_m: 100, visit_exit_minutes: 10 },
  })),
});

import Page from './+page.svelte';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('puts the This device card above the module service cards', async () => {
  render(Page);
  const overland = await screen.findByRole('heading', { name: 'Overland' });
  const device = screen.getByRole('heading', { name: 'This device' });
  const carto = screen.getByRole('heading', { name: 'Carto' });
  expect(device.compareDocumentPosition(overland) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(device.compareDocumentPosition(carto) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
