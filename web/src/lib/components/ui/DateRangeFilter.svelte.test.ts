import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import DateRangeFilter from './DateRangeFilter.svelte';

afterEach(cleanup);

/**
 * /health/history and /location/history each had this filter: the same flex
 * row, the same input paint, the same webkit picker-icon `filter`, differing
 * only in whether the captions were drawn and whether the inputs capped at
 * today.
 *
 * Both had got the fiddly part right independently: the calendar picker icon
 * ships dark, so the dark theme inverts it and the light theme has to undo
 * that, and each page carried both halves — one of them two hundred lines away
 * from the rest of its date CSS. Pinning it here is what stops the third page
 * shipping with half of it, which is the failure this consolidation is for.
 */

describe('DateRangeFilter', () => {
  it('renders two date inputs, named for a screen reader', () => {
    render(DateRangeFilter, { from: '2026-01-01', to: '2026-02-01' });
    expect(screen.getByLabelText('From date')).toBeInTheDocument();
    expect(screen.getByLabelText('To date')).toBeInTheDocument();
  });

  it('reports a change on either input', async () => {
    const onChange = vi.fn();
    render(DateRangeFilter, { from: '', to: '', onChange });
    await fireEvent.change(screen.getByLabelText('From date'), {
      target: { value: '2026-03-01' },
    });
    expect(onChange).toHaveBeenCalledOnce();
    await fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2026-03-05' } });
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it('draws a separator instead of captions by default', () => {
    // A date input, the word "to" and a second date input reads as a range on
    // its own, and dropping the captions buys most of a phone's width back.
    const { container } = render(DateRangeFilter, { from: '', to: '' });
    expect(container.querySelector('.date-sep')?.textContent).toBe('to');
    expect(container.querySelector('.date-cap')).toBeNull();
  });

  it('draws captions on request, associated with their inputs', () => {
    const { container } = render(DateRangeFilter, { from: '', to: '', labelled: true });
    const caps = [...container.querySelectorAll('label.date-cap')];
    expect(caps.map((c) => c.textContent)).toEqual(['From', 'To']);
    expect(container.querySelector('.date-sep')).toBeNull();
    // `for` has to resolve, or clicking the caption focuses nothing.
    for (const cap of caps) {
      const id = cap.getAttribute('for')!;
      expect(container.querySelector(`#${CSS.escape(id)}`)).toBeTruthy();
    }
  });

  it('gives each instance its own ids', () => {
    // Two filters on one page would otherwise both point their captions at
    // the first one's inputs.
    const a = render(DateRangeFilter, { from: '', to: '', labelled: true }).container;
    const b = render(DateRangeFilter, { from: '', to: '', labelled: true }).container;
    const idOf = (c: Element) => c.querySelector('input')!.id;
    expect(idOf(a)).not.toBe(idOf(b));
  });

  it('caps both inputs when given a max', () => {
    render(DateRangeFilter, { from: '', to: '', max: '2026-08-01' });
    expect(screen.getByLabelText('From date')).toHaveAttribute('max', '2026-08-01');
    expect(screen.getByLabelText('To date')).toHaveAttribute('max', '2026-08-01');
  });
});

describe('the styles it carries', () => {
  const source = readFileSync(
    join(process.cwd(), 'src/lib/components/ui/DateRangeFilter.svelte'),
    'utf8',
  );

  it('sets no min-width on the inputs', () => {
    // primitives.css floors it — an unset date input otherwise collapses to a
    // blank sliver on iOS — and any rule here would outrank that floor. One
    // of the two filters this replaces shipped exactly that bug.
    // dateInputs.test.ts is the tree-wide net; this is the local one.
    //
    // Asserted against the input rule specifically, not the whole <style>:
    // `.date-range` itself carries `min-width: 0`, which is the opposite
    // thing — it lets the wrapping row shrink inside a flex parent.
    // Comments stripped first: the rule explains in prose why it sets no
    // min-width, and a naive scan finds the words in the sentence saying so.
    const rule = source
      .match(/\.date-range input\[type='date'\] \{([^}]*)\}/)?.[1]
      ?.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(rule, 'the input rule should exist').toBeDefined();
    expect(rule).not.toMatch(/min-width/);
  });

  it('filters the picker glyph through the token, not a theme rule', () => {
    // The glyph is drawn by a UA pseudo-element that can only be filtered, and
    // it ships dark: the dark theme lifts it, the light theme leaves it. That
    // used to be two rules, one of them naming the theme, and BOTH pages had
    // to carry both halves — several hundred lines apart in one of them.
    // --calendar-icon-filter carries the direction, so there is one rule and
    // no half to forget. tokens.test.ts holds the parity.
    expect(source).toContain('filter: var(--calendar-icon-filter)');
    expect(source, 'no theme-conditional rule should remain').not.toContain("data-theme='light'");
  });

  it('wraps rather than squeezing', () => {
    // Two date inputs lifted to the 16px touch floor plus their captions do
    // not fit one phone row.
    expect(source).toContain('flex-wrap: wrap');
  });
});
