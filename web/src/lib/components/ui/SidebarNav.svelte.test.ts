import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { Star, Users } from '@lucide/svelte';
import SidebarNav from './SidebarNav.svelte';

afterEach(cleanup);

const items = [
  { href: '/istota/admin', label: 'Status', active: true },
  { href: '/istota/admin/users', label: 'Users', icon: Users },
];

describe('SidebarNav', () => {
  it('is a named nav of links, one per item', () => {
    render(SidebarNav, { items, label: 'Admin sections' });
    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((l) => l.textContent?.trim())).toEqual(['Status', 'Users']);
    expect(links[1]).toHaveAttribute('href', '/istota/admin/users');
  });

  it('marks the active item as the current page and no other', () => {
    render(SidebarNav, { items, label: 'Admin sections' });
    expect(screen.getByRole('link', { name: 'Status' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Users' })).not.toHaveAttribute('aria-current');
  });

  it('draws an item icon inside its link, hidden from a screen reader', () => {
    render(SidebarNav, { items, label: 'Admin sections' });
    const svg = screen.getByRole('link', { name: 'Users' }).querySelector('svg')!;
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
  });

  it('shows a count as a pill titled with its noun, and nothing at zero', () => {
    render(SidebarNav, {
      label: 'Views',
      items: [
        { href: '?view=unread', label: 'Unread', count: 5, countLabel: 'unread' },
        { href: '?view=starred', label: 'Starred', count: 0, countLabel: 'starred' },
      ],
    });
    const pill = screen.getByRole('link', { name: /Unread/ }).querySelector('.count-pill')!;
    expect(pill).toHaveTextContent('5');
    expect(pill).toHaveAttribute('title', '5 unread');
    expect(screen.getByRole('link', { name: /Starred/ }).querySelector('.count-pill')).toBeNull();
  });

  it('takes the muted count tone', () => {
    render(SidebarNav, {
      label: 'Views',
      items: [
        { href: '?view=starred', label: 'Starred', icon: Star, count: 2, countTone: 'muted' },
      ],
    });
    expect(screen.getByRole('link').querySelector('.count-pill')).toHaveClass('muted');
  });

  it('renders an item mark straight after its label', () => {
    const mark = createRawSnippet(() => ({ render: () => '<span data-testid="mark">off</span>' }));
    render(SidebarNav, {
      label: 'Briefings',
      items: [{ href: '?name=daily', label: 'Daily', mark }],
    });
    expect(screen.getByRole('link')).toContainElement(screen.getByTestId('mark'));
  });

  it('hands the item and the click to onnavigate', async () => {
    const onnavigate = vi.fn();
    render(SidebarNav, { items, label: 'Admin sections', onnavigate });
    await fireEvent.click(screen.getByRole('link', { name: 'Users' }));
    expect(onnavigate).toHaveBeenCalledOnce();
    expect(onnavigate.mock.calls[0][0]).toBe(items[1]);
    expect(onnavigate.mock.calls[0][1]).toBeInstanceOf(MouseEvent);
  });

  it('draws the rule under the block only when asked', () => {
    const { container, unmount } = render(SidebarNav, { items, label: 'A' });
    expect(container.querySelector('nav')).not.toHaveClass('divider');
    unmount();
    const second = render(SidebarNav, { items, label: 'B', divider: true });
    expect(second.container.querySelector('nav')).toHaveClass('divider');
  });
});
