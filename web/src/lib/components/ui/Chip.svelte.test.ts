import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Chip from './Chip.svelte';

afterEach(cleanup);

const text = (s: string) => createRawSnippet(() => ({ render: () => `<span>${s}</span>` }));

describe('Chip', () => {
  it('is a toggle button that reports its state when asked', async () => {
    const onclick = vi.fn();
    render(Chip, { checked: true, 'aria-pressed': true, onclick, children: text('Images') });
    const chip = screen.getByRole('button', { name: 'Images' });
    expect(chip).toHaveClass('checked');
    expect(chip).toHaveAttribute('aria-pressed', 'true');
    expect(chip).toHaveAttribute('type', 'button');
    await fireEvent.click(chip);
    expect(onclick).toHaveBeenCalledOnce();
  });

  it('passes other attributes through, for a group that drives it', () => {
    render(Chip, { role: 'radio', 'aria-checked': 'true', children: text('30d') });
    expect(screen.getByRole('radio', { name: '30d' })).toHaveAttribute('aria-checked', 'true');
  });

  it('draws the compact size as its own class', () => {
    render(Chip, { size: 'xs', children: text('Room') });
    expect(screen.getByRole('button', { name: 'Room' })).toHaveClass('chip', 'xs');
  });

  it('is a label with a named remove button when removable', async () => {
    const onremove = vi.fn();
    const { container } = render(Chip, {
      removable: true,
      size: 'xs',
      removeLabel: 'Remove news@example.com',
      onremove,
      children: text('news@example.com'),
    });
    const chip = container.querySelector('.chip')!;
    expect(chip.tagName).toBe('SPAN');
    expect(chip).toHaveTextContent('news@example.com');
    const remove = screen.getByRole('button', { name: 'Remove news@example.com' });
    expect(chip).toContainElement(remove);
    // One button only: the label is not a second way to remove.
    expect(screen.getAllByRole('button')).toHaveLength(1);
    await fireEvent.click(remove);
    expect(onremove).toHaveBeenCalledOnce();
  });
});
