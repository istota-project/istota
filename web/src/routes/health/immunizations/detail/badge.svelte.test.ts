import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { page } from '../../../../../vitest-stubs/app-state.svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { getImmunization } from '$lib/api';
import Page from './+page.svelte';

beforeEach(() => {
  page.url = new URL('http://localhost/istota/health/immunizations/detail/?id=7');
  vi.mocked(getImmunization).mockResolvedValue({
    immunization: { id: 7, name: 'Tetanus', date_given: '2026-01-02' },
    encounter: { id: 3, encounter_type: 'visit', encounter_date: '2026-01-02' },
    documents: [],
  } as never);
});
afterEach(() => {
  cleanup();
  page.url = new URL('http://localhost/');
});

describe('immunization detail linked encounter', () => {
  // The page wrote `.badge type-other` with no rule for it in the file, so the
  // encounter type rendered as plain text.
  it('draws the encounter type with the Badge primitive', async () => {
    render(Page);
    const type = await screen.findByText('visit');
    expect(type.classList.contains('badge-neutral')).toBe(true);
  });
});
