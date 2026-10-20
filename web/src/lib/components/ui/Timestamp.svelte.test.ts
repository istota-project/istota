import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import Timestamp from './Timestamp.svelte';
import { formatDate, formatDateTime, formatRelative, formatTime } from '$lib/dateFormat';

afterEach(cleanup);

const ISO = '2026-09-05T14:22:31Z';

describe('Timestamp', () => {
  it('renders a <time> with a machine-readable datetime and the date as text', () => {
    const { container } = render(Timestamp, { value: ISO });
    const time = container.querySelector('time')!;
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute('datetime', ISO);
    expect(time.textContent).toBe(formatDate(ISO));
  });

  it('puts the absolute date and time in the title', () => {
    const { container } = render(Timestamp, { value: ISO });
    expect(container.querySelector('time')).toHaveAttribute('title', formatDateTime(ISO));
  });

  it('draws no title when asked not to, or when it would repeat the text', () => {
    const off = render(Timestamp, { value: ISO, title: false });
    expect(off.container.querySelector('time')).not.toHaveAttribute('title');
    cleanup();
    const same = render(Timestamp, { value: ISO, format: 'datetime' });
    expect(same.container.querySelector('time')).not.toHaveAttribute('title');
  });

  it('renders each format through dateFormat', () => {
    const rel = render(Timestamp, { value: ISO, format: 'relative' });
    expect(rel.container.querySelector('time')!.textContent).toBe(formatRelative(ISO));
    cleanup();
    const time = render(Timestamp, { value: ISO, format: 'time' });
    expect(time.container.querySelector('time')!.textContent).toBe(formatTime(ISO));
    cleanup();
    const opts = { month: 'long', day: 'numeric' } as const;
    const date = render(Timestamp, { value: ISO, options: opts });
    expect(date.container.querySelector('time')!.textContent).toBe(formatDate(ISO, opts));
  });

  it('keeps a bare calendar date as the datetime and titles it with the date', () => {
    const { container } = render(Timestamp, {
      value: '2026-09-05',
      options: { month: '2-digit', day: '2-digit' },
    });
    const time = container.querySelector('time')!;
    expect(time).toHaveAttribute('datetime', '2026-09-05');
    expect(time).toHaveAttribute('title', formatDate('2026-09-05'));
  });

  it('renders the empty fallback, with no <time>, for a missing value', () => {
    const { container } = render(Timestamp, { value: null, empty: 'Never' });
    expect(container.querySelector('time')).toBeNull();
    expect(container.textContent).toBe('Never');
    cleanup();
    const bare = render(Timestamp, { value: undefined });
    expect(bare.container.textContent).toBe('');
  });

  it('leaves an unparseable value as text with no datetime', () => {
    const { container } = render(Timestamp, { value: 'sometime' });
    const time = container.querySelector('time')!;
    expect(time.textContent).toBe('sometime');
    expect(time).not.toHaveAttribute('datetime');
  });

  it('passes a placement class through', () => {
    const { container } = render(Timestamp, { value: ISO, class: 'caption' });
    expect(container.querySelector('time')).toHaveClass('caption');
  });
});
