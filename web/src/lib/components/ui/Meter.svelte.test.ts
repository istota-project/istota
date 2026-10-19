import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import Meter from './Meter.svelte';

afterEach(cleanup);

describe('Meter', () => {
  it('is a named meter carrying its value and range', () => {
    render(Meter, { value: 30, max: 120, label: 'Talk tasks' });
    const meter = screen.getByRole('meter', { name: 'Talk tasks' });
    expect(meter).toHaveAttribute('aria-valuemin', '0');
    expect(meter).toHaveAttribute('aria-valuemax', '120');
    expect(meter).toHaveAttribute('aria-valuenow', '30');
    const fill = meter.querySelector<HTMLElement>('.ui-meter-fill')!;
    expect(fill.style.width).toBe('25%');
  });

  it('defaults to a 0-100 range and the accent fill', () => {
    render(Meter, { value: 40, label: 'Uploading a.txt' });
    const meter = screen.getByRole('meter');
    expect(meter).toHaveAttribute('aria-valuemax', '100');
    expect(meter.querySelector<HTMLElement>('.ui-meter-fill')!.style.background).toBe(
      'var(--accent)',
    );
  });

  it('clamps the fill to the track', () => {
    render(Meter, { value: 150, max: 100, label: 'Over' });
    expect(
      screen.getByRole('meter').querySelector<HTMLElement>('.ui-meter-fill')!.style.width,
    ).toBe('100%');
  });

  it('stacks segments, sized against their total, and says what each is', () => {
    render(Meter, {
      label: '24h source breakdown',
      segments: [
        { value: 3, color: 'red', label: 'talk 3' },
        { value: 1, color: 'blue', label: 'cron 1' },
      ],
    });
    const meter = screen.getByRole('meter', { name: '24h source breakdown' });
    expect(meter).toHaveAttribute('aria-valuenow', '4');
    expect(meter).toHaveAttribute('aria-valuemax', '4');
    expect(meter).toHaveAttribute('aria-valuetext', 'talk 3, cron 1');
    const segs = meter.querySelectorAll<HTMLElement>('.ui-meter-fill');
    expect(segs).toHaveLength(2);
    expect(segs[0].style.width).toBe('75%');
    expect(segs[0]).toHaveAttribute('title', 'talk 3');
    expect(segs[1].style.width).toBe('25%');
  });

  it('is the sm height unless asked to be xs', () => {
    render(Meter, { value: 1, label: 'x' });
    expect(screen.getByRole('meter')).toHaveClass('size-sm');
    cleanup();
    render(Meter, { value: 1, label: 'x', size: 'xs' });
    expect(screen.getByRole('meter')).toHaveClass('size-xs');
  });
});
