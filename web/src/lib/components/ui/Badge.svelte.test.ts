import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Badge from './Badge.svelte';
import IconButton from './IconButton.svelte';
import { X } from '@lucide/svelte';

afterEach(cleanup);

const text = (s: string) => createRawSnippet(() => ({ render: () => `<span>${s}</span>` }));

describe('Badge', () => {
  it('renders its content', () => {
    render(Badge, { children: text('Overdue') });
    expect(screen.getByText('Overdue')).toBeInTheDocument();
  });

  it('defaults to the neutral variant', () => {
    const { container } = render(Badge, { children: text('Recorded') });
    expect(container.querySelector('.badge-neutral')).toBeTruthy();
  });

  it.each(['danger', 'warn', 'success', 'info', 'partial'] as const)(
    'carries the %s variant as a class',
    (variant) => {
      const { container } = render(Badge, { variant, children: text('x') });
      expect(container.querySelector(`.badge-${variant}`)).toBeTruthy();
    },
  );

  it('is the default size unless asked to be small', () => {
    const { container } = render(Badge, { children: text('x') });
    expect(container.querySelector('.badge-sm')).toBeNull();
    cleanup();
    const small = render(Badge, { size: 'sm', children: text('x') });
    expect(small.container.querySelector('.badge.badge-sm')).toBeTruthy();
  });

  it('keeps `partial` off the severity ramp as its own variant', () => {
    // "Series incomplete" is part-done, not late. Giving it a severity would
    // rank it against overdue, which is the distinction the purple carries.
    const { container } = render(Badge, { variant: 'partial', children: text('x') });
    expect(container.querySelector('.badge-danger')).toBeNull();
    expect(container.querySelector('.badge-warn')).toBeNull();
  });

  it('is solid and uppercase unless asked otherwise', () => {
    const { container } = render(Badge, { children: text('x') });
    const badge = container.querySelector('.badge')!;
    expect(badge).not.toHaveClass('badge-subtle');
    expect(badge).not.toHaveClass('badge-lower');
  });

  it('draws the lowercase neutral pill as tone="subtle" uppercase={false}', () => {
    const { container } = render(Badge, {
      tone: 'subtle',
      uppercase: false,
      variant: 'success',
      children: text('Configured'),
    });
    const badge = container.querySelector('.badge')!;
    expect(badge).toHaveClass('badge-subtle', 'badge-lower', 'badge-success');
    expect(badge).toHaveTextContent('Configured');
  });

  it('puts an icon before the text and passes a placement class', () => {
    const icon = createRawSnippet(() => ({ render: () => '<svg data-testid="glyph"></svg>' }));
    const { container } = render(Badge, {
      icon,
      class: 'reach',
      children: text('Tasks can read'),
    });
    const badge = container.querySelector('.badge.reach')!;
    expect(badge.firstElementChild).toBe(screen.getByTestId('glyph'));
    expect(badge).toHaveTextContent('Tasks can read');
  });
});

describe('IconButton', () => {
  it('names itself for a screen reader', () => {
    // The label is required precisely because an icon-only button has no text
    // to fall back on, and the hand-rolled ones kept shipping without one.
    render(IconButton, { label: 'Move up', children: text('↑') });
    expect(screen.getByRole('button', { name: 'Move up' })).toBeInTheDocument();
  });

  it('fires onclick', async () => {
    const onclick = vi.fn();
    render(IconButton, { label: 'Close', onclick, children: text('×') });
    await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onclick).toHaveBeenCalledOnce();
  });

  it('honours disabled', () => {
    render(IconButton, { label: 'Move up', disabled: true, children: text('↑') });
    expect(screen.getByRole('button', { name: 'Move up' })).toBeDisabled();
  });

  it('defaults to type=button so it cannot submit a surrounding form', () => {
    render(IconButton, { label: 'Remove', children: text('×') });
    expect(screen.getByRole('button', { name: 'Remove' })).toHaveAttribute('type', 'button');
  });

  it.each(['sm', 'md', 'round'] as const)('carries the %s size as a class', (size) => {
    const { container } = render(IconButton, { label: 'x', size, children: text('x') });
    expect(container.querySelector(`.icon-btn-${size}`)).toBeTruthy();
  });

  it('marks a destructive action without changing its shape', () => {
    const { container } = render(IconButton, {
      label: 'Delete',
      danger: true,
      children: text('×'),
    });
    expect(container.querySelector('.icon-btn.danger')).toBeTruthy();
  });

  it('draws an icon prop through Icon at its own step', () => {
    const { container } = render(IconButton, { label: 'Close', size: 'sm', icon: X });
    const glyph = container.querySelector('.icon-btn .ui-icon');
    expect(glyph?.classList.contains('ui-icon-sm')).toBe(true);
    expect(glyph?.getAttribute('aria-hidden')).toBe('true');
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
  });

  it('defaults an md button to the md glyph and takes iconSize over it', () => {
    const md = render(IconButton, { label: 'a', icon: X });
    expect(md.container.querySelector('.ui-icon-md')).toBeTruthy();
    cleanup();
    const lg = render(IconButton, { label: 'b', icon: X, iconSize: 'lg' });
    expect(lg.container.querySelector('.ui-icon-lg')).toBeTruthy();
  });
});
