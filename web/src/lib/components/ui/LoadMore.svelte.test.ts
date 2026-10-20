import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import LoadMore from './LoadMore.svelte';
import Pagination from './Pagination.svelte';

afterEach(cleanup);

describe('LoadMore', () => {
  it('draws a secondary button that loads the next page', async () => {
    const onload = vi.fn();
    render(LoadMore, { onload, hasMore: true, label: 'Load older' });
    const button = screen.getByRole('button', { name: 'Load older' });
    expect(button).toHaveClass('btn-secondary', 'btn-sm');
    await fireEvent.click(button);
    expect(onload).toHaveBeenCalledOnce();
  });

  it('disables itself and says so while a page is loading', () => {
    render(LoadMore, { onload: vi.fn(), hasMore: true, loading: true });
    const button = screen.getByRole('button', { name: 'Loading…' });
    expect(button).toBeDisabled();
  });

  it('draws nothing to press once there is no more, only the note', () => {
    render(LoadMore, { onload: vi.fn(), hasMore: false, note: 'Stopped at the scan limit.' });
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('Stopped at the scan limit.')).toHaveClass('caption');
  });

  it('draws nothing at all when there is no more and nothing to say', () => {
    const { container } = render(LoadMore, { onload: vi.fn(), hasMore: false });
    expect(container.querySelector('.ui-load-more')).toBeNull();
  });

  it('shows a failure beside the button, which stays to retry', () => {
    render(LoadMore, { onload: vi.fn(), hasMore: true, error: 'Could not load.' });
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load.');
    expect(screen.getByRole('button', { name: 'Load more' })).toBeEnabled();
  });
});

describe('LoadMore auto', () => {
  let callbacks: ((entries: { isIntersecting: boolean }[]) => void)[];
  let observed: Element[];

  beforeEach(() => {
    callbacks = [];
    observed = [];
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(cb: (entries: { isIntersecting: boolean }[]) => void) {
          callbacks.push(cb);
        }
        observe(el: Element) {
          observed.push(el);
        }
        unobserve() {}
        disconnect() {}
      },
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it('loads when its sentinel comes into view, with no button', async () => {
    const onload = vi.fn(async () => {});
    render(LoadMore, { onload, hasMore: true, auto: true });
    expect(screen.queryByRole('button')).toBeNull();
    expect(observed).toHaveLength(1);
    callbacks[0]([{ isIntersecting: true }]);
    await tick();
    expect(onload).toHaveBeenCalledOnce();
  });

  it('does not load while a page is already loading', async () => {
    const onload = vi.fn();
    render(LoadMore, { onload, hasMore: true, auto: true, loading: true });
    callbacks[0]([{ isIntersecting: true }]);
    await tick();
    expect(onload).not.toHaveBeenCalled();
  });

  it('looks again after a load, unless the load said not to', async () => {
    const onload = vi.fn(async () => {});
    render(LoadMore, { onload, hasMore: true, auto: true });
    callbacks[0]([{ isIntersecting: true }]);
    await tick();
    await tick();
    expect(observed.length).toBe(2);

    const failing = vi.fn(async () => false as const);
    cleanup();
    observed = [];
    callbacks = [];
    render(LoadMore, { onload: failing, hasMore: true, auto: true });
    callbacks[0]([{ isIntersecting: true }]);
    await tick();
    await tick();
    expect(observed.length).toBe(1);
  });
});

describe('Pagination', () => {
  it('pages back and forward, disabled at the ends', async () => {
    const onchange = vi.fn();
    render(Pagination, { page: 1, pages: 3, onchange });
    expect(screen.getByRole('navigation', { name: 'Pages' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Previous/ })).toBeDisabled();
    expect(screen.getByText('1 / 3')).toHaveClass('caption');
    await fireEvent.click(screen.getByRole('button', { name: /Next/ }));
    expect(onchange).toHaveBeenCalledWith(2);
  });

  it('disables Next on the last page', () => {
    render(Pagination, { page: 3, pages: 3, onchange: vi.fn() });
    expect(screen.getByRole('button', { name: /Next/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Previous/ })).toBeEnabled();
  });

  it('draws nothing for a single page', () => {
    const { container } = render(Pagination, { page: 1, pages: 1, onchange: vi.fn() });
    expect(container.querySelector('nav')).toBeNull();
  });
});
