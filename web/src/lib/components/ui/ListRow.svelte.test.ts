import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import Harness from './ListRow.harness.svelte';

afterEach(cleanup);

describe('ListRow', () => {
  it('renders a link when given an href, so middle-click and the URL preview survive', () => {
    render(Harness, { href: '/istota/admin/users', label: 'Users' });
    const link = screen.getByRole('link', { name: 'Users' });
    expect(link).toHaveAttribute('href', '/istota/admin/users');
    expect(link).not.toHaveAttribute('aria-current');
  });

  it('marks the active link as the current page', () => {
    const { container } = render(Harness, { href: '/istota/admin', active: true });
    expect(screen.getByRole('link')).toHaveAttribute('aria-current', 'page');
    expect(container.querySelector('.ui-list-row')).toHaveClass('active');
  });

  it('lets the caller name a different current (a view inside a page)', () => {
    render(Harness, { href: '/istota/files/?view=rooms', active: true, ariaCurrent: 'location' });
    expect(screen.getByRole('link')).toHaveAttribute('aria-current', 'location');
  });

  it('renders a button for onclick, with no aria-current however active', async () => {
    const onclick = vi.fn();
    const { container } = render(Harness, { onclick, active: true, label: 'Errands' });
    const button = screen.getByRole('button', { name: 'Errands' });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).not.toHaveAttribute('aria-current');
    expect(container.querySelector('.ui-list-row')).toHaveClass('active');
    await fireEvent.click(button);
    expect(onclick).toHaveBeenCalledOnce();
  });

  it('is an inert row with neither href nor onclick', () => {
    const { container } = render(Harness, { label: 'Unavailable' });
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    expect(container.querySelector('.ui-list-row-hit')).toHaveTextContent('Unavailable');
  });

  it('puts the icon, mark and badge inside the hit target and the trailing action beside it', () => {
    const { container } = render(Harness, {
      href: '/istota/chat/?room=a',
      withIcon: true,
      withMark: true,
      withBadge: true,
      withTrailing: true,
    });
    const link = screen.getByRole('link');
    expect(link).toContainElement(screen.getByTestId('icon'));
    expect(link).toContainElement(screen.getByTestId('mark'));
    expect(link).toContainElement(screen.getByTestId('badge'));
    const trailing = screen.getByTestId('trailing');
    expect(link).not.toContainElement(trailing);
    expect(container.querySelector('.ui-list-row')).toContainElement(trailing);
  });

  it('ellipsises the label and keeps the mark straight after it', () => {
    const { container } = render(Harness, { href: '/x', withMark: true, label: 'A long name' });
    const label = container.querySelector('.ui-list-row-label')!;
    expect(label).toHaveTextContent('A long name');
    expect(label.nextElementSibling).toHaveClass('ui-list-row-mark');
  });

  it('draws the meta lines under the label', () => {
    const { container } = render(Harness, { onclick: () => {}, withMeta: true });
    const meta = container.querySelector('.ui-list-row-meta')!;
    expect(meta).toContainElement(screen.getByTestId('meta'));
    expect(container.querySelector('.ui-list-row-line')!.nextElementSibling).toBe(meta);
  });

  it('takes a density and a tint', () => {
    const { container } = render(Harness, { href: '/x', density: 'sm', tint: 'var(--x)' });
    const row = container.querySelector<HTMLElement>('.ui-list-row')!;
    expect(row).toHaveClass('density-sm', 'tinted');
    expect(row.style.getPropertyValue('--row-tint')).toBe('var(--x)');
  });

  it('is base density and untinted by default', () => {
    const { container } = render(Harness, { href: '/x' });
    const row = container.querySelector<HTMLElement>('.ui-list-row')!;
    expect(row).toHaveClass('density-base');
    expect(row).not.toHaveClass('tinted');
  });
});
