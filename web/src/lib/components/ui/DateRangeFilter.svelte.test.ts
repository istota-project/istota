import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { readLayer } from '../../styles/cascade';
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

describe('the in-field placeholder', () => {
  /**
   * iOS renders an empty date input as *nothing* — no `mm/dd/yyyy` hint the
   * way desktop has one — so the range filter read as two blank rounded boxes
   * with no clue they were dates at all.
   */
  const texts = (c: Element) => [...c.querySelectorAll('.date-ph')].map((n) => n.textContent);

  it('names each empty input', () => {
    const { container } = render(DateRangeFilter, { from: '', to: '' });
    expect(texts(container)).toEqual(['Start', 'End']);
  });

  it('drops away once the input carries a value', () => {
    const { container } = render(DateRangeFilter, { from: '2026-01-01', to: '' });
    expect(texts(container)).toEqual(['End']);
  });

  it('takes custom wording', () => {
    const { container } = render(DateRangeFilter, {
      from: '',
      to: '',
      fromPlaceholder: 'Earliest',
      toPlaceholder: 'Latest',
    });
    expect(texts(container)).toEqual(['Earliest', 'Latest']);
  });

  it('is hidden from a screen reader and untappable', () => {
    // The input already carries an aria-label, so the overlay is decoration;
    // and it covers the field, so a stray pointer target would eat the tap
    // that is supposed to open the picker.
    const { container } = render(DateRangeFilter, { from: '', to: '' });
    for (const ph of container.querySelectorAll('.date-ph')) {
      expect(ph).toHaveAttribute('aria-hidden', 'true');
    }
    const source = readFileSync(
      join(process.cwd(), 'src/lib/components/ui/DateRangeFilter.svelte'),
      'utf8',
    );
    expect(source.match(/\.date-ph \{([^}]*)\}/)?.[1]).toMatch(/pointer-events:\s*none/);
  });
});

describe('it claims no class primitives.css already publishes', () => {
  /**
   * Svelte scoping stops this component's rules leaking out; it does nothing
   * about a *global* rule landing on a bare class name used here. The state
   * class started life as `empty`, which primitives.css publishes as the
   * shared empty-state block — so every field silently took its `2rem 1rem`,
   * and the filter grew 70px of vertical padding nothing in this file
   * mentioned. The same trap is waiting on `caption`, `muted`, `banner`.
   */
  const shared = new Set(
    [...readLayer('primitives').matchAll(/^\.([a-z][\w-]*)[\s,{:.]/gm)].map((m) => m[1]),
  );

  it('parses the shared blocks it checks against', () => {
    expect(shared.has('empty')).toBe(true);
    expect(shared.size).toBeGreaterThan(5);
  });

  it('uses none of them, in either state', () => {
    for (const props of [
      { from: '', to: '' },
      { from: '2026-01-01', to: '2026-02-01', labelled: true },
    ]) {
      const { container } = render(DateRangeFilter, props);
      for (const el of container.querySelectorAll<HTMLElement>('*')) {
        for (const cls of el.classList) {
          expect(shared.has(cls), `${cls} is a primitives.css shared block`).toBe(false);
        }
      }
      cleanup();
    }
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

  it('keeps the pair on one line', () => {
    // A range is one control: wrapping mid-filter stacked input / "to" /
    // input into a three-line column beside the type Select on a phone. The
    // row moves as a unit instead — .filter-bar wraps, this does not — and
    // each field is free to shrink to share whatever row it lands on.
    const rule = source.match(/\.date-range \{([^}]*)\}/)?.[1]?.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(rule, 'the row rule should exist').toBeDefined();
    expect(rule).toContain('flex-wrap: nowrap');
  });

  it('caps a field so a desktop row does not stretch it', () => {
    // The fields are the flexible member of the filter bar, so without a cap
    // two of them share the whole width of a wide screen — a date in a box
    // three times its own length.
    const rule = source.match(/\.date-field \{([^}]*)\}/)?.[1]?.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(rule).toMatch(/max-width:/);
  });

  it('leaves the field wrappers shrinkable', () => {
    // The floor in primitives.css is `min(100%, 8em)`, so an input can only
    // stay inside a narrow row if the box it measures that 100% against can
    // itself shrink. Without this the nowrap row overflows the gutter.
    const rule = source.match(/\.date-field \{([^}]*)\}/)?.[1]?.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(rule, 'the field rule should exist').toBeDefined();
    expect(rule).toMatch(/min-width:\s*0/);
  });
});
