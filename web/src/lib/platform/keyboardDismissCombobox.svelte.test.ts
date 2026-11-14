/**
 * The keyboard dismiss against a real AutocompleteInput. Its window capture
 * listener sees a tap's pointerup before the option does, so a blur there
 * closes the menu and the option's own handler finds nothing open: a tap on
 * a suggestion picked nothing on an iPhone, while a press that moved a few
 * pixels, which the dismiss treats as a scroll, picked it.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { AutocompleteInput } from '@minot/ui';
import { installKeyboardDismiss } from './input';

let stop: (() => void) | undefined;

beforeEach(() => {
  window.matchMedia = ((q: string) => ({
    matches: q.includes('coarse'),
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  stop?.();
  stop = undefined;
  cleanup();
});

it('picks a tapped suggestion with the keyboard dismiss installed', async () => {
  const onSelect = vi.fn();
  render(AutocompleteInput, {
    value: '',
    options: ['Home', 'Office'],
    ariaLabel: 'Wifi zone place',
    onSelect,
  });
  stop = installKeyboardDismiss();
  const input = screen.getByRole('combobox', { name: 'Wifi zone place' });
  input.focus();
  await tick();

  const option = screen.getByRole('option', { name: 'Office' });
  await fireEvent.pointerDown(option, { clientY: 100 });
  await fireEvent.pointerUp(option, { clientY: 100 });

  expect(onSelect).toHaveBeenCalledExactlyOnceWith('Office');
  expect(input).toHaveValue('Office');
  expect(document.activeElement).toBe(input);
});
