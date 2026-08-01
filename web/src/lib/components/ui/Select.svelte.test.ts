import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import Select from './Select.svelte';

afterEach(cleanup);

const yearOptions = [
  { value: '', label: 'All' },
  { value: '2026', label: '2026' },
];

describe('Select', () => {
  it('shows the selected option label on the trigger', () => {
    render(Select, { value: '2026', options: yearOptions, ariaLabel: 'Year' });
    expect(screen.getByRole('button', { name: 'Year' })).toHaveTextContent('2026');
  });

  it('falls back to the placeholder when nothing matches', () => {
    render(Select, { value: 'nope', options: yearOptions, ariaLabel: 'Year' });
    expect(screen.getByRole('button', { name: 'Year' })).toHaveTextContent('Select…');
  });

  describe('minChars', () => {
    it('reserves label width so the trigger does not resize with the selection', () => {
      // The money year filter is the case: "All" is narrower than "2026", so the
      // control shrank and grew as you changed years, shifting whatever sat
      // beside it. `ch` rather than a px literal so the reservation tracks the
      // trigger's own font size, which moves with the text-scale preference.
      const { container } = render(Select, {
        value: '',
        options: yearOptions,
        ariaLabel: 'Year',
        minChars: 4,
      });
      const label = container.querySelector('.ui-select-label') as HTMLElement;
      expect(label.style.minWidth).toBe('4ch');
    });

    it('reserves nothing when unset, so an ordinary Select shrinks to fit', () => {
      const { container } = render(Select, { value: '2026', options: yearOptions });
      const label = container.querySelector('.ui-select-label') as HTMLElement;
      expect(label.style.minWidth).toBe('');
    });
  });
});
