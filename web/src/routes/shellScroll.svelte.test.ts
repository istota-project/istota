import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { createRawSnippet, tick, type Component } from 'svelte';
import { page } from '../../vitest-stubs/app-state.svelte';
import AdminLayout from './admin/+layout.svelte';
import HealthLayout from './health/+layout.svelte';
import PortfolioLayout from './money/portfolio/+layout.svelte';
import ReportsLayout from './money/reports/+layout.svelte';

const children = createRawSnippet(() => ({ render: () => '<div></div>' }));
const go = async (url: string) => {
  page.url = new URL(url, 'http://localhost');
  await tick();
};
const open = async (layout: unknown, url: string) => {
  await go(url);
  render(layout as Component<{ children: typeof children }>, { children });
  await tick();
};

afterEach(cleanup);

describe.each([
  ['admin', AdminLayout, '/istota/admin/users', '/istota/admin/groups'],
  ['health', HealthLayout, '/istota/health/labs', '/istota/health/documents'],
])('the %s shell', (_name, layout, from, to) => {
  it('starts the next sub-route at the top of the pane', async () => {
    await open(layout, from);
    const pane = document.querySelector('.shell-main') as HTMLElement;
    pane.scrollTop = 400;
    await go(to);
    expect(pane.scrollTop).toBe(0);
  });

  it('stays where it is when only the query changes', async () => {
    await open(layout, from);
    const pane = document.querySelector('.shell-main') as HTMLElement;
    pane.scrollTop = 400;
    await go(`${from}?range=90`);
    expect(pane.scrollTop).toBe(400);
  });
});

describe.each([
  [
    'portfolio',
    PortfolioLayout,
    '/istota/money/portfolio/overview',
    '/istota/money/portfolio/history',
  ],
  [
    'reports',
    ReportsLayout,
    '/istota/money/reports/income-statement',
    '/istota/money/reports/balance-sheet',
  ],
])('the money %s frame', (_name, layout, from, to) => {
  it('starts the next sub-route at the top of its scroller', async () => {
    await open(layout, from);
    const body = document.querySelector('.money-section-body') as HTMLElement;
    body.scrollTop = 400;
    await go(to);
    expect((document.querySelector('.money-section-body') as HTMLElement).scrollTop).toBe(0);
  });
});
