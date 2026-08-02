import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import StatTile from './StatTile.svelte';

afterEach(cleanup);

const text = (s: string) => createRawSnippet(() => ({ render: () => `<span>${s}</span>` }));

/**
 * One tile, four implementations before this existed: `/admin`'s `.kpi`,
 * money/taxes' `.summary-card`, and money's portfolio-overview and cash-flow
 * tiles, the last two byte-identical to each other. Two tells that they were
 * one thing rather than four: `.card-label` was `.micro-label` retyped by hand
 * in both money copies, and the four disagreed on ORDER — admin rendered
 * label→value→sub while money rendered value→label. A reader met two different
 * tiles for one idea depending on which page they were on.
 *
 * So the component settles the parts that were drift (order, label typography,
 * the numeral treatment) and keeps props only for the parts that were a real
 * choice: whether the tile carries its own card surface, and whether its text
 * is centred.
 */

describe('StatTile', () => {
  it('renders its label and value', () => {
    render(StatTile, { label: 'Total value', children: text('$1,234') });
    expect(screen.getByText('Total value')).toBeInTheDocument();
    expect(screen.getByText('$1,234')).toBeInTheDocument();
  });

  it('labels with the shared .micro-label rather than its own type', () => {
    // Both money copies had written --text-xs + uppercase + a letter-spacing
    // out by hand, which is .micro-label's whole declaration. Reading the
    // global is what stops a fifth spelling appearing.
    const { container } = render(StatTile, { label: 'Holdings', children: text('12') });
    expect(container.querySelector('.micro-label')).toBeTruthy();
  });

  it('puts the label before the value', () => {
    // The inconsistency this component exists to settle. Label-first matches
    // .micro-label's documented contract — the small heading *over* a section,
    // a list or a definition term — and it is what makes the tile one
    // component rather than one with a "which way round" prop.
    const { container } = render(StatTile, { label: 'Accounts', children: text('4') });
    const tile = container.querySelector('.stat-tile')!;
    const kids = [...tile.children];
    expect(kids[0].classList.contains('micro-label')).toBe(true);
    expect(kids[1].classList.contains('stat-value')).toBe(true);
  });

  it('renders the sub line only when given one', () => {
    const { container: without } = render(StatTile, { label: 'A', children: text('1') });
    expect(without.querySelector('.stat-sub')).toBeNull();
    cleanup();
    const { container: with_ } = render(StatTile, {
      label: 'A',
      sub: '3/day (30d)',
      children: text('1'),
    });
    expect(with_.querySelector('.stat-sub')).toBeTruthy();
    expect(screen.getByText('3/day (30d)')).toBeInTheDocument();
  });

  it('is bare by default and takes a card surface on request', () => {
    // admin's tiles sit inside a bigger card and must not draw a second one;
    // money's sit directly on the pane and supply their own.
    const { container: bare } = render(StatTile, { label: 'A', children: text('1') });
    expect(bare.querySelector('.stat-surface')).toBeNull();
    cleanup();
    const { container: carded } = render(StatTile, {
      label: 'A',
      surface: true,
      children: text('1'),
    });
    expect(carded.querySelector('.stat-surface')).toBeTruthy();
  });

  it('centres on request and starts aligned otherwise', () => {
    const { container: start } = render(StatTile, { label: 'A', children: text('1') });
    expect(start.querySelector('.stat-center')).toBeNull();
    cleanup();
    const { container: centered } = render(StatTile, {
      label: 'A',
      align: 'center',
      children: text('1'),
    });
    expect(centered.querySelector('.stat-center')).toBeTruthy();
  });

  it('passes a class through for placement', () => {
    // Same contract as Field's: without it a page has to fork the component to
    // place it — a grid span, a column start — which is how the copies started.
    const { container } = render(StatTile, {
      label: 'A',
      class: 'col-total',
      children: text('1'),
    });
    expect(container.querySelector('.stat-tile.col-total')).toBeTruthy();
  });
});

describe('the value colour hook', () => {
  const source = readFileSync(join(process.cwd(), 'src/lib/components/ui/StatTile.svelte'), 'utf8');

  it('reads --stat-value-fg, so a caller can tint without reaching inside', () => {
    // Every adopter tints its value and only its value: money by direction
    // (--money-income / --money-expense), admin by severity (a failed-task
    // count going --status-warn-fg). A page's scoped CSS cannot reach into a
    // child component, so the hook is a custom property — the same answer
    // Badge gives with --badge-bg / --badge-fg.
    expect(source).toContain('--stat-value-fg');
  });

  it('lets a dense row step the numeral size down', () => {
    // The four copies sat at 1.2rem, 1.1rem, 1rem and --text-base. One default
    // with a hook, rather than four literals: taxes' row is tight and wants
    // the small step, and nothing else should have to care.
    expect(source).toContain('--stat-value-size');
  });

  it('keeps figures tabular so a column of numbers lines up', () => {
    expect(source).toContain('font-variant-numeric: tabular-nums');
  });
});
