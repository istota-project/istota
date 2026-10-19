import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import StatusDot from './StatusDot.svelte';

afterEach(cleanup);

describe('StatusDot', () => {
  it('is hidden from a screen reader without a label', () => {
    const { container } = render(StatusDot, { tone: 'success' });
    const dot = container.querySelector('.ui-status-dot')!;
    expect(dot).toHaveAttribute('aria-hidden', 'true');
    expect(dot).not.toHaveAttribute('role');
  });

  it('is a named image with a label', () => {
    render(StatusDot, { tone: 'danger', label: 'Unavailable' });
    expect(screen.getByRole('img', { name: 'Unavailable' })).toHaveClass('tone-danger');
  });

  it.each(['success', 'warn', 'danger', 'info', 'neutral'] as const)(
    'carries the %s tone as a class',
    (tone) => {
      const { container } = render(StatusDot, { tone });
      expect(container.querySelector(`.ui-status-dot.tone-${tone}`)).not.toBeNull();
    },
  );

  it('is the md size unless asked to be small', () => {
    const { container } = render(StatusDot, { tone: 'info' });
    expect(container.querySelector('.ui-status-dot')).toHaveClass('size-md');
    cleanup();
    const small = render(StatusDot, { tone: 'info', size: 'sm' });
    expect(small.container.querySelector('.ui-status-dot')).toHaveClass('size-sm');
  });

  it('paints from the saturated dot tokens, never the -fg tints', () => {
    // A dot carries no text, so it takes the --status-dot-* family; the
    // notification panel's dot used the -fg tints against that rule.
    const source = readFileSync(resolve(__dirname, 'StatusDot.svelte'), 'utf8');
    for (const tone of ['success', 'warn', 'danger', 'info']) {
      expect(source).toContain(`var(--status-dot-${tone})`);
    }
    expect(source).not.toMatch(/--status-\w+-fg/);
  });
});
