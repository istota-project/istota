import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { flushSync } from 'svelte';
import Harness from './Disclosure.harness.svelte';

afterEach(cleanup);
beforeEach(() => localStorage.clear());

/** jsdom does not toggle <details> on a summary click; do what the browser does. */
async function toggle(details: HTMLDetailsElement) {
  details.open = !details.open;
  await fireEvent(details, new Event('toggle'));
  flushSync();
}

describe('Disclosure', () => {
  it('is a native <details> with the summary and one chevron marker', () => {
    const { container } = render(Harness, { summary: 'Advanced' });
    const details = container.querySelector('details')!;
    expect(details).toHaveClass('ui-disclosure', 'variant-inline');
    expect(details.open).toBe(false);
    const summary = details.querySelector('summary')!;
    expect(summary).toHaveTextContent('Advanced');
    const marker = summary.querySelector('.ui-disclosure-marker')!;
    expect(marker.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('Hidden body')).toBeInTheDocument();
  });

  it('opens from its bound value and reports a toggle back', async () => {
    const { container } = render(Harness, { open: true });
    const details = container.querySelector('details')!;
    expect(details.open).toBe(true);
    await toggle(details);
    expect(screen.getByTestId('open')).toHaveTextContent('closed');
  });

  it('takes a snippet summary and a trailing snippet inside the summary', () => {
    const { container } = render(Harness, {
      withSummarySnippet: true,
      withTrailing: true,
      variant: 'section',
    });
    const summary = container.querySelector('summary')!;
    expect(summary.querySelector('[data-testid="rich"]')).not.toBeNull();
    expect(summary.querySelector('.ui-disclosure-trailing [data-testid="total"]')).not.toBeNull();
    expect(container.querySelector('details')).toHaveClass('variant-section');
  });

  it('remembers its state under a storage key', async () => {
    const first = render(Harness, { storageKey: 'test:disclosure' });
    await toggle(first.container.querySelector('details')!);
    expect(localStorage.getItem('test:disclosure')).toBe('true');
    cleanup();
    const second = render(Harness, { storageKey: 'test:disclosure' });
    expect(second.container.querySelector('details')!.open).toBe(true);
  });

  it('draws the card variant', () => {
    const { container } = render(Harness, { variant: 'card' });
    expect(container.querySelector('details')).toHaveClass('variant-card');
  });
});
