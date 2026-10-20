import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import ViewFilters from './ViewFilters.svelte';

afterEach(cleanup);

const href = (view: string) => `/istota/chat/?view=${view}`;

describe('ViewFilters', () => {
  it('links each view to its URL and marks the current one', () => {
    render(ViewFilters, { current: 'unread', href, onselect: () => {}, unread: 3 });
    const unread = screen.getByRole('link', { name: /^Unread/ });
    expect(unread).toHaveAttribute('href', '/istota/chat/?view=unread');
    expect(unread).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'All' })).not.toHaveAttribute('aria-current');
  });

  it('applies a plain click in place instead of navigating', async () => {
    const onselect = vi.fn();
    render(ViewFilters, { current: null, href, onselect });
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
    await fireEvent(screen.getByRole('link', { name: 'Starred' }), event);
    expect(onselect).toHaveBeenCalledWith('starred');
    expect(event.defaultPrevented).toBe(true);
  });

  it('leaves a modified click to the browser, so the view opens in a new tab', async () => {
    const onselect = vi.fn();
    render(ViewFilters, { current: null, href, onselect });
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, metaKey: true });
    await fireEvent(screen.getByRole('link', { name: 'All' }), event);
    expect(onselect).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });
});
