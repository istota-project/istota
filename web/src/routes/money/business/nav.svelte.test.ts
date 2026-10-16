import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Layout from './+layout.svelte';
import ClientsRedirect from './clients/+page.svelte';
import SettingsRedirect from './settings/+page.svelte';

const nav = vi.hoisted(() => ({ goto: vi.fn() }));

vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$app/navigation', () => nav);
vi.mock('$app/state', () => ({
  page: { url: new URL('http://localhost/istota/money/business/work') },
}));

afterEach(() => {
  cleanup();
  nav.goto.mockReset();
});

describe('the Business section', () => {
  it('is Work | Invoices, with clients moved to Settings → Invoicing', () => {
    const children = createRawSnippet(() => ({ render: () => '<div></div>' }));
    render(Layout, { props: { children } });
    const labels = screen.getAllByRole('link').map((a) => a.textContent?.trim());
    expect(labels).toEqual(['Work', 'Invoices']);
  });

  it.each([
    ['clients', ClientsRedirect],
    ['settings', SettingsRedirect],
  ])('redirects the old %s route to Invoicing', (_name, Page) => {
    render(Page);
    expect(nav.goto).toHaveBeenCalledWith('/istota/money/settings/invoicing', {
      replaceState: true,
    });
  });
});
