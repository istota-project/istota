import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Badge from './Badge.svelte';
import IconButton from './IconButton.svelte';

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

  it('keeps `partial` off the severity ramp as its own variant', () => {
    // "Series incomplete" is part-done, not late. Giving it a severity would
    // rank it against overdue, which is the distinction the purple carries.
    const { container } = render(Badge, { variant: 'partial', children: text('x') });
    expect(container.querySelector('.badge-danger')).toBeNull();
    expect(container.querySelector('.badge-warn')).toBeNull();
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
});
