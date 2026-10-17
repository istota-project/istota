import { readFileSync } from 'node:fs';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, screen, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { Immunization } from '$lib/api';
import { formatDate } from '$lib/dateFormat';
import { base } from '$app/paths';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import Page from './+page.svelte';

const dose: Immunization = {
  id: 17,
  name: 'Pneumococcal conjugate vaccine (20-valent)',
  date_given: '2026-09-12',
  product_name: null,
  manufacturer: null,
  dose_label: null,
  lot_number: null,
  route: null,
  site: null,
  administered_by: null,
  facility: null,
  encounter_id: null,
  cvx_code: null,
  notes: null,
  source: 'manual',
};

// jsdom has no layout or media-query evaluation. Apply the real page rules
// for each width here; browser preview checks the resulting geometry.
let pageStyle: HTMLStyleElement;
function useStylesAt(width: number) {
  const css = ['src/routes/health/immunizations/+page.svelte', 'src/routes/health/+layout.svelte']
    .map((path) => readFileSync(path, 'utf8').split('<style>')[1].split('</style>')[0])
    .join('\n')
    .replace(/:global\(([^)]+)\)/g, '$1');
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css);
  let active = '';
  for (const rule of sheet.cssRules) {
    if (rule instanceof CSSMediaRule) {
      const max = rule.conditionText.match(/max-width:\s*(\d+)px/);
      if (!max) throw new Error(`Unhandled media query: ${rule.conditionText}`);
      if (width <= Number(max[1])) {
        active += Array.from(rule.cssRules, (child) => child.cssText).join('\n');
      }
    } else {
      active += rule.cssText;
    }
  }
  pageStyle = document.createElement('style');
  pageStyle.textContent = active;
  document.head.append(pageStyle);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getImmunizationCoverage.mockResolvedValue({ coverage: [], other: [] });
  api.listImmunizations.mockResolvedValue({ immunizations: [dose] });
  api.listImmunizationRefs.mockResolvedValue({ refs: [] });
});

afterEach(() => {
  cleanup();
  document.querySelectorAll('.health-frame').forEach((element) => element.remove());
  pageStyle?.remove();
});

function mountPage() {
  const target = document.createElement('div');
  target.className = 'health-frame';
  document.body.append(target);
  render(Page, { target });
}

async function historyRow() {
  const link = await screen.findByRole('link', { name: dose.name });
  return link.closest('tr')!;
}

describe('immunization history layout', () => {
  it('keeps the vaccine and date in a stacked phone row without empty placeholders', async () => {
    useStylesAt(390);
    mountPage();
    const row = await historyRow();
    expect(within(row).getByText(formatDate(dose.date_given))).toBeTruthy();
    expect(row.textContent).not.toContain('—');
    expect(row.querySelector('td.date')?.textContent?.trim()).toBe(formatDate(dose.date_given));
    expect(getComputedStyle(row).display).toBe('flex');
    expect(getComputedStyle(row).flexWrap).toBe('wrap');
    for (const cell of row.querySelectorAll('td')) {
      expect(getComputedStyle(cell).paddingTop).toBe('0px');
      expect(getComputedStyle(cell).paddingBottom).toBe('0px');
      expect(getComputedStyle(cell).borderBottomWidth).toBe('0px');
    }
    const emptyCells = [...row.querySelectorAll('td')].filter(
      (cell) => !cell.textContent?.trim() && !cell.querySelector('button'),
    );
    expect(emptyCells).toHaveLength(4);
    for (const cell of emptyCells) expect(getComputedStyle(cell).display).toBe('none');
    expect(within(row).getAllByRole('button', { name: 'Immunization actions' })).toHaveLength(1);
  });

  it('preserves desktop table cells, including the actions cell', async () => {
    useStylesAt(1024);
    mountPage();
    const row = await historyRow();
    expect(row.querySelectorAll('td')).toHaveLength(7);
    for (const cell of row.querySelectorAll('td')) {
      expect(getComputedStyle(cell).display).toBe('table-cell');
    }
  });

  it('keeps populated details and reloads the history through the vaccine filter', async () => {
    api.listImmunizations.mockResolvedValue({
      immunizations: [
        {
          ...dose,
          product_name: 'Example vaccine',
          dose_label: 'Booster',
          facility: 'Example clinic',
          notes: 'Imported from a vaccination record.',
        },
      ],
    });
    useStylesAt(390);
    mountPage();
    const row = await historyRow();
    const dateCell = row.querySelector('td.date')!;
    expect(dateCell).toHaveTextContent(
      `${formatDate(dose.date_given)} · Example vaccine · Booster · Example clinic`,
    );
    expect(getComputedStyle(row.querySelector('td.notes')!).display).toBe('none');
    for (const selector of ['td.vaccine', 'td.date']) {
      const style = getComputedStyle(row.querySelector(selector)!);
      expect(style.whiteSpace).toBe('nowrap');
      expect(style.textOverflow).toBe('ellipsis');
      expect(style.overflow).toBe('hidden');
    }
    const filter = screen.getByPlaceholderText('Filter by vaccine name');
    await fireEvent.input(filter, { target: { value: 'Pneumococcal' } });
    await fireEvent.change(filter);
    expect(api.listImmunizations).toHaveBeenLastCalledWith({ name: 'Pneumococcal', limit: 500 });
    expect(await screen.findByRole('link', { name: dose.name })).toHaveAttribute(
      'href',
      `${base}/health/immunizations/vaccine?name=${encodeURIComponent(dose.name)}`,
    );
    expect(getComputedStyle(filter).maxWidth).toBe('none');
    expect(getComputedStyle(filter.closest('.history-head')!).flexDirection).toBe('column');
  });
});
