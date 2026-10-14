import { cleanup, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import PageLoader from './PageLoader.svelte';
import PageLoading from './PageLoading.svelte';

// The hold count is module state, so a component left mounted would hold the bar
// up for the next test.
afterEach(() => cleanup());

describe('PageLoader', () => {
  it('is up while any pane is loading and down once the last one is done', async () => {
    render(PageLoader);
    expect(screen.queryByTestId('page-loader')).toBeNull();

    // A cold load: the root layout and a page both loading at once.
    const outer = render(PageLoading);
    const inner = render(PageLoading, { label: 'Loading groups…' });
    await tick();
    expect(screen.getByTestId('page-loader')).toBeTruthy();

    outer.unmount();
    await tick();
    expect(screen.getByTestId('page-loader')).toBeTruthy();

    inner.unmount();
    await tick();
    expect(screen.queryByTestId('page-loader')).toBeNull();
  });

  it('keeps the pane slot, with its label as hidden text only', () => {
    const { container } = render(PageLoading, { label: 'Loading groups…' });
    const slot = container.querySelector('.page-loading');
    expect(slot?.classList.contains('center-msg')).toBe(true);
    expect(slot?.textContent?.trim()).toBe('Loading groups…');
    expect(slot?.querySelector('.visually-hidden')?.textContent).toBe('Loading groups…');
  });
});
