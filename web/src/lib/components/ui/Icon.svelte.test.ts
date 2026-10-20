import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import { Star, X } from '@lucide/svelte';
import Icon from './Icon.svelte';

afterEach(cleanup);

describe('Icon', () => {
  it('is decoration without a label', () => {
    const { container } = render(Icon, { icon: X });
    const box = container.querySelector('.ui-icon');
    expect(box?.getAttribute('aria-hidden')).toBe('true');
    expect(box?.getAttribute('role')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('is a named image with a label', () => {
    render(Icon, { icon: X, label: 'Failed' });
    const img = screen.getByRole('img', { name: 'Failed' });
    expect(img.getAttribute('aria-hidden')).toBeNull();
    expect(img.classList.contains('ui-icon')).toBe(true);
  });

  it('draws the glyph at the full size of a token-sized box', () => {
    const { container } = render(Icon, { icon: X, size: 'lg' });
    const box = container.querySelector('.ui-icon');
    expect(box?.classList.contains('ui-icon-lg')).toBe(true);
    const svg = box?.querySelector('svg');
    expect(svg?.getAttribute('width')).toBe('100%');
    expect(svg?.getAttribute('height')).toBe('100%');
  });

  it('defaults to the sm step', () => {
    const { container } = render(Icon, { icon: X });
    expect(container.querySelector('.ui-icon-sm')).toBeTruthy();
  });

  it('passes the stroke width and fill to the glyph', () => {
    const { container } = render(Icon, { icon: Star, stroke: 1.5, fill: 'currentColor' });
    const svg = container.querySelector('svg');
    expect(svg?.getAttribute('stroke-width')).toBe('1.5');
    expect(svg?.getAttribute('fill')).toBe('currentColor');
  });

  it('keeps lucide stroke width when none is given', () => {
    const { container } = render(Icon, { icon: X });
    expect(container.querySelector('svg')?.getAttribute('stroke-width')).toBe('2');
  });

  it('adds a placement class beside its own', () => {
    const { container } = render(Icon, { icon: X, class: 'caret' });
    expect(container.querySelector('.ui-icon.caret')).toBeTruthy();
  });
});
