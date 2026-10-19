import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import SortHeader, { sortAria } from './SortHeader.svelte';

afterEach(cleanup);

describe('SortHeader spoken label', () => {
  // "newest first" was right only for a date column; read on an amount or an
  // invoice number it said something false.
  it('names the direction, not a date order', () => {
    render(SortHeader, { label: 'Amount', direction: 'descending', onclick: vi.fn() });
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
      direction: 'ascending',
      onclick: vi.fn(),
    });
    expect(screen.getByRole('button').getAttribute('aria-label')).toBe(
      'Sorted by invoice number, ascending. Reverse the order',
    );
    cleanup();
    render(SortHeader, { label: 'Date', direction: null, onclick: vi.fn() });
    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Sort by date');
  });

  it('calls onclick and carries the page column class for placement', async () => {
    const onclick = vi.fn();
    render(SortHeader, { label: 'Date', direction: null, onclick, class: 'col-date' });
    const button = screen.getByRole('button');
    expect(button.classList.contains('col-date')).toBe(true);
    await fireEvent.click(button);
    expect(onclick).toHaveBeenCalledOnce();
  });
});

describe('sortAria', () => {
  it('passes a direction through and reads no sort as none', () => {
    expect(sortAria('ascending')).toBe('ascending');
    expect(sortAria('descending')).toBe('descending');
    expect(sortAria(null)).toBe('none');
  });
});
