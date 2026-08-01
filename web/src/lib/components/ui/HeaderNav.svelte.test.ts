import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import HeaderNav from './HeaderNav.svelte';

const goto = vi.fn();
vi.mock('$app/navigation', () => ({ goto: (href: string) => goto(href) }));

afterEach(() => {
  cleanup();
  goto.mockClear();
});

const items = [
  { href: '/istota/health/stats', label: 'Stats' },
  { href: '/istota/health/bloodwork', label: 'Bloodwork', active: true },
];

describe('HeaderNav', () => {
  it('renders an inline link per item for the desktop row', () => {
    render(HeaderNav, { items });
    for (const item of items) {
      expect(screen.getByRole('link', { name: item.label })).toHaveAttribute('href', item.href);
    }
  });

  it('renders the mobile dropdown as a button, never a native <select>', () => {
    // ISSUE-224: the touch floor in app.css redefines the --text-* tokens on
    // `input`/`select`/`textarea`, so a native <select> here is pushed to 16px
    // — which made the single most-seen control in the app the heaviest thing
    // in a compact bar. A bits-ui trigger is a <button>: WebKit never zoomed
    // for it, the floor never reaches it, and it sizes as designed. Asserted on
    // the element rather than on a computed size because the floor lives in a
    // global stylesheet jsdom does not apply.
    const { container } = render(HeaderNav, { items, ariaLabel: 'Health section' });
    expect(container.querySelector('select')).toBeNull();
    expect(screen.getByRole('button', { name: 'Health section' }).tagName).toBe('BUTTON');
  });

  it('shows the active item as the dropdown selection', () => {
    render(HeaderNav, { items, ariaLabel: 'Health section' });
    expect(screen.getByRole('button', { name: 'Health section' })).toHaveTextContent('Bloodwork');
  });

  it('falls back to the first item when none is active', () => {
    // A settings sub-page reached via the cog is in no nav item's section, and
    // the trigger must still show something.
    render(HeaderNav, { items: items.map((i) => ({ ...i, active: false })), ariaLabel: 'Section' });
    expect(screen.getByRole('button', { name: 'Section' })).toHaveTextContent('Stats');
  });

  it('navigates when a dropdown option is chosen', async () => {
    render(HeaderNav, { items, ariaLabel: 'Section' });
    const trigger = screen.getByRole('button', { name: 'Section' });
    await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(trigger);
    const option = await screen.findByRole('option', { name: 'Stats' });
    await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(option);
    expect(goto).toHaveBeenCalledWith('/istota/health/stats');
  });
});
