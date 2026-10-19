import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import SortHeader from './SortHeader.svelte';

afterEach(cleanup);

describe('SortHeader spoken label', () => {
  // "newest first" was right only for a date column; read on an amount or an
  // invoice number it said something false.
  it('names the direction, not a date order', () => {
    render(SortHeader, { label: 'Amount', active: true, descending: true, onclick: vi.fn() });
    const button = screen.getByRole('button');
    expect(button.getAttribute('aria-label')).toBe(
      'Sorted by amount, descending. Reverse the order',
    );
    expect(button.getAttribute('aria-label')).not.toMatch(/newest|oldest/);
  });

  it('says ascending for an ascending sort and offers a sort when inactive', () => {
    render(SortHeader, {
      label: 'Number',
      name: 'invoice number',
      active: true,
      descending: false,
      onclick: vi.fn(),
    });
    expect(screen.getByRole('button').getAttribute('aria-label')).toBe(
      'Sorted by invoice number, ascending. Reverse the order',
    );
    cleanup();
    render(SortHeader, { label: 'Date', active: false, descending: true, onclick: vi.fn() });
    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Sort by date');
  });
});
