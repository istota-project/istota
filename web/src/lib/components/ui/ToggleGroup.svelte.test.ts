import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import Harness from './ToggleGroup.harness.svelte';

afterEach(cleanup);

describe('ToggleGroup', () => {
  it('is a named group of radios in single mode, with the value checked', () => {
    render(Harness, { mode: 'single' });
    const group = screen.getByRole('group', { name: 'Range' });
    const radios = screen.getAllByRole('radio');
    expect(radios.map((r) => r.textContent?.trim())).toEqual(['30d', '90d', '1y', 'All']);
    expect(group).toContainElement(radios[0]);
    expect(screen.getByRole('radio', { name: '90d' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('radio', { name: '30d' })).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByRole('radio', { name: '1y' })).toBeDisabled();
  });

  it('selects on click and reports the change', async () => {
    const onValueChange = vi.fn();
    render(Harness, { mode: 'single', onValueChange });
    await fireEvent.click(screen.getByRole('radio', { name: '30d' }));
    expect(screen.getByTestId('value')).toHaveTextContent('30d');
    expect(onValueChange).toHaveBeenCalledWith('30d');
    expect(screen.getByRole('radio', { name: '30d' })).toHaveClass('checked');
  });

  it('keeps a selection when the selected option is pressed again', async () => {
    const onValueChange = vi.fn();
    render(Harness, { mode: 'single', onValueChange });
    await fireEvent.click(screen.getByRole('radio', { name: '90d' }));
    expect(screen.getByTestId('value')).toHaveTextContent('90d');
    expect(screen.getByRole('radio', { name: '90d' })).toHaveAttribute('aria-checked', 'true');
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it('moves focus with the arrow keys and skips a disabled option', async () => {
    render(Harness, { mode: 'single' });
    const ninety = screen.getByRole('radio', { name: '90d' });
    ninety.focus();
    await fireEvent.keyDown(ninety, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(screen.getByRole('radio', { name: 'All' }));
    await fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    expect(document.activeElement).toBe(ninety);
  });

  it('toggles each option on its own in multiple mode, with aria-pressed', async () => {
    const onValueChange = vi.fn();
    render(Harness, { mode: 'multiple', onValueChange });
    const feeds = screen.getByRole('button', { name: 'feeds' });
    const money = screen.getByRole('button', { name: 'money' });
    expect(feeds).toHaveAttribute('aria-pressed', 'true');
    expect(money).toHaveAttribute('aria-pressed', 'false');
    await fireEvent.click(money);
    expect(screen.getByTestId('value')).toHaveTextContent('feeds,money');
    await fireEvent.click(feeds);
    expect(screen.getByTestId('value')).toHaveTextContent('money');
    expect(onValueChange).toHaveBeenLastCalledWith(['money']);
  });

  it('names icon-only options by their label, as aria-label and title', () => {
    render(Harness, { mode: 'icons' });
    const grid = screen.getByRole('radio', { name: 'Grid view' });
    expect(grid).toHaveAttribute('title', 'Grid view');
    expect(grid.textContent?.trim()).toBe('');
    expect(grid.querySelector('svg')).not.toBeNull();
  });

  it('takes its name from the Field caption inside a Field', () => {
    render(Harness, { mode: 'field' });
    const group = screen.getByRole('group', { name: 'Disabled modules' });
    const caption = screen.getByText('Disabled modules');
    expect(group).toHaveAttribute('aria-labelledby', caption.id);
    // The caption is not a <label>: pressing it must not press a chip.
    expect(caption.tagName).not.toBe('LABEL');
  });
});
